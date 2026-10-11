"""
US County Dashboard
-------------------
Maps every US county and ranks them by population growth, with a slider to
switch the map to violent crime rate, education (bachelor's degree or higher),
or median household income.

Data sources (downloaded automatically on first run, then cached in ./data):
  * Population growth  - Census Population Estimates Program, Vintage 2025
                         (CO-EST2025-alldata: July 1 estimates 2020-2025)
  * Median income      - ACS 5-year 2020-2024, table B19013
  * Education          - ACS 5-year 2020-2024, table B15003 (age 25+)
  * Violent crime rate - County Health Rankings analytic data (v043, from FBI
                         UCR, violent crimes per 100,000 residents)
  * County shapes      - Census cartographic boundary file, 2024, 1:20m

Run:
    pip install -r requirements.txt
    streamlit run county_dashboard.py

Optional: set a free Census API key (https://api.census.gov/data/key_signup.html)
in the environment as CENSUS_API_KEY to avoid rate limits.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PEP_URLS = [
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/counties/totals/co-est2025-alldata.csv",
    # Fallback to the prior vintage if the 2025 file moves
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/counties/totals/co-est2024-alldata.csv",
]
ACS_YEAR = 2024  # ACS 5-year 2020-2024
ACS_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
CHR_URLS = [
    "https://www.countyhealthrankings.org/sites/default/files/media/document/analytic_data2026.csv",
    "https://www.countyhealthrankings.org/sites/default/files/media/document/analytic_data2025.csv",
    "https://www.countyhealthrankings.org/sites/default/files/media/document/analytic_data2024.csv",
]
SHAPE_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_county_20m.zip"

HEADERS = {"User-Agent": "county-dashboard/1.0 (research use)"}


# --------------------------------------------------------------------------
# Download helpers
# --------------------------------------------------------------------------
def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=120, **kw)
    r.raise_for_status()
    return r


def _first_working(urls: list[str]) -> tuple[str, bytes]:
    errors = []
    for u in urls:
        try:
            return u, _get(u).content
        except Exception as e:  # noqa: BLE001
            errors.append(f"{u}: {e}")
    raise RuntimeError("All sources failed:\n" + "\n".join(errors))


# --------------------------------------------------------------------------
# 1. Population growth (Census PEP)
# --------------------------------------------------------------------------
def parse_pep(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), encoding="latin-1", dtype={"STATE": str, "COUNTY": str})
    df = df[df["SUMLEV"].astype(int) == 50].copy()
    df["fips"] = df["STATE"].str.zfill(2) + df["COUNTY"].str.zfill(3)

    pop_cols = sorted(c for c in df.columns if c.startswith("POPESTIMATE") and c[-4:].isdigit())
    first, last = pop_cols[0], pop_cols[-1]           # POPESTIMATE2020, POPESTIMATE2025
    prev = pop_cols[-2]
    out = pd.DataFrame({
        "fips": df["fips"],
        "county": df["CTYNAME"],
        "state": df["STNAME"],
        "pop_start": df[first],
        "pop_prev": df[prev],
        "population": df[last],
    })
    out["growth_total_pct"] = (out["population"] / out["pop_start"] - 1) * 100
    out["growth_1yr_pct"] = (out["population"] / out["pop_prev"] - 1) * 100
    out.attrs["period"] = (first[-4:], last[-4:])
    return out


def load_population() -> pd.DataFrame:
    cache = DATA_DIR / "pep.csv"
    if not cache.exists():
        _, raw = _first_working(PEP_URLS)
        cache.write_bytes(raw)
    return parse_pep(cache.read_bytes())


# --------------------------------------------------------------------------
# 2. ACS: median income + education
# --------------------------------------------------------------------------
ACS_VARS = {
    "B19013_001E": "median_income",
    "B15003_001E": "edu_total",
    "B15003_022E": "edu_ba",
    "B15003_023E": "edu_ma",
    "B15003_024E": "edu_prof",
    "B15003_025E": "edu_phd",
}


def parse_acs(rows: list[list[str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df["fips"] = df["state"].str.zfill(2) + df["county"].str.zfill(3)
    for code in ACS_VARS:
        df[code] = pd.to_numeric(df[code], errors="coerce")
        df.loc[df[code] < 0, code] = np.nan          # Census sentinel values (-666666666 etc.)
    df = df.rename(columns=ACS_VARS)
    df["bachelors_pct"] = (df[["edu_ba", "edu_ma", "edu_prof", "edu_phd"]].sum(axis=1, min_count=4)
                           / df["edu_total"]) * 100
    return df[["fips", "median_income", "bachelors_pct"]]


def load_acs() -> pd.DataFrame:
    cache = DATA_DIR / "acs.json"
    if not cache.exists():
        params = {"get": ",".join(ACS_VARS), "for": "county:*", "in": "state:*"}
        key = os.environ.get("CENSUS_API_KEY")
        if key:
            params["key"] = key
        cache.write_text(json.dumps(_get(ACS_URL, params=params).json()))
    return parse_acs(json.loads(cache.read_text()))


# --------------------------------------------------------------------------
# 3. Violent crime (County Health Rankings)
# --------------------------------------------------------------------------
def parse_chr(raw: bytes) -> pd.DataFrame:
    # Row 0 = long labels, row 1 = variable codes -> use row 1 as header
    df = pd.read_csv(io.BytesIO(raw), header=1, dtype={"fipscode": str}, low_memory=False)
    df["fips"] = df["fipscode"].str.zfill(5)
    df = df[~df["fips"].str.endswith("000")]          # drop state & national rows
    df["violent_crime_rate"] = pd.to_numeric(df["v043_rawvalue"], errors="coerce")
    return df[["fips", "violent_crime_rate"]]


def load_crime() -> pd.DataFrame:
    cache = DATA_DIR / "chr.csv"
    if not cache.exists():
        _, raw = _first_working(CHR_URLS)
        cache.write_bytes(raw)
    return parse_chr(cache.read_bytes())


# --------------------------------------------------------------------------
# 4. County shapes -> GeoJSON
# --------------------------------------------------------------------------
def _round_coords(coords, nd=4):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    return [_round_coords(c, nd) for c in coords]


def load_geojson() -> dict:
    cache = DATA_DIR / "counties.geojson"
    if not cache.exists():
        import shapefile  # pyshp

        zpath = DATA_DIR / "counties_shp.zip"
        zpath.write_bytes(_get(SHAPE_URL).content)
        with zipfile.ZipFile(zpath) as z:
            base = next(n[:-4] for n in z.namelist() if n.endswith(".shp"))
            reader = shapefile.Reader(
                shp=io.BytesIO(z.read(base + ".shp")),
                shx=io.BytesIO(z.read(base + ".shx")),
                dbf=io.BytesIO(z.read(base + ".dbf")),
            )
            features = []
            for sr in reader.shapeRecords():
                geom = sr.shape.__geo_interface__
                features.append({
                    "type": "Feature",
                    "id": sr.record["GEOID"],
                    "properties": {"GEOID": sr.record["GEOID"]},
                    "geometry": {"type": geom["type"],
                                 "coordinates": _round_coords(geom["coordinates"])},
                })
        cache.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        zpath.unlink(missing_ok=True)
    return json.loads(cache.read_text())


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------
def build_table(pop: pd.DataFrame, acs: pd.DataFrame, crime: pd.DataFrame) -> pd.DataFrame:
    df = pop.merge(acs, on="fips", how="left").merge(crime, on="fips", how="left")
    df["growth_rank"] = df["growth_total_pct"].rank(ascending=False, method="min").astype("Int64")
    df.attrs = pop.attrs
    return df


# --------------------------------------------------------------------------
# Dashboard (Streamlit)
# --------------------------------------------------------------------------
METRICS = {
    "Population growth": dict(col=None, fmt="{:+.1f}%", scale="RdBu", diverging=True,
                              note="Percent change in resident population"),
    "Violent crime rate": dict(col="violent_crime_rate", fmt="{:,.0f}", scale="Reds",
                               note="Violent crimes per 100,000 residents (County Health Rankings, FBI UCR)"),
    "Education": dict(col="bachelors_pct", fmt="{:.1f}%", scale="Blues",
                      note="Share of adults 25+ with a bachelor's degree or higher (ACS 2020-2024)"),
    "Median income": dict(col="median_income", fmt="${:,.0f}", scale="Greens",
                          note="Median household income (ACS 2020-2024)"),
}


def run_app() -> None:
    import plotly.express as px
    import streamlit as st

    st.set_page_config(page_title="US County Dashboard", layout="wide")

    @st.cache_data(show_spinner="Downloading Census and crime data (first run only)...")
    def get_data():
        pop = load_population()
        return build_table(pop, load_acs(), load_crime()), pop.attrs["period"]

    @st.cache_data(show_spinner="Loading county boundaries...")
    def get_geo():
        return load_geojson()

    df, (y0, y1) = get_data()
    geo = get_geo()

    st.title("US Counties: Population Growth Dashboard")

    # ---- Controls -------------------------------------------------------
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        metric = st.select_slider("Map layer", options=list(METRICS), value="Population growth")
    with c2:
        period = st.radio("Growth period", [f"{y0}-{y1} (cumulative)", f"{int(y1)-1}-{y1} (1 year)"],
                          horizontal=False)
    with c3:
        min_pop = st.select_slider("Minimum population",
                                   options=[0, 1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000],
                                   value=10_000,
                                   format_func=lambda v: f"{v:,}")
    states = st.multiselect("States (leave empty for all)", sorted(df["state"].unique()))

    growth_col = "growth_total_pct" if "cumulative" in period else "growth_1yr_pct"
    view = df[df["population"] >= min_pop]
    if states:
        view = view[view["state"].isin(states)]
    view = view.copy()
    view["growth_rank"] = view[growth_col].rank(ascending=False, method="min").astype("Int64")

    spec = METRICS[metric]
    col = spec["col"] or growth_col

    # ---- KPIs -----------------------------------------------------------
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Counties shown", f"{len(view):,}")
    k2.metric("Median growth", f"{view[growth_col].median():+.2f}%")
    top = view.nlargest(1, growth_col)
    if len(top):
        k3.metric("Fastest growing", f"{top['county'].iat[0]}, {top['state'].iat[0]}",
                  f"{top[growth_col].iat[0]:+.1f}%")
    k4.metric("Growing counties", f"{(view[growth_col] > 0).mean():.0%}")

    # ---- Map ------------------------------------------------------------
    vals = view[col].dropna()
    lo, hi = (vals.quantile(0.02), vals.quantile(0.98)) if len(vals) else (0, 1)
    if spec.get("diverging"):
        m = max(abs(lo), abs(hi))
        lo, hi = -m, m

    fig = px.choropleth(
        view, geojson=geo, locations="fips", featureidkey="properties.GEOID",
        color=col, color_continuous_scale=spec["scale"], range_color=(lo, hi),
        scope="usa", hover_name=view["county"] + ", " + view["state"],
        hover_data={
            "fips": False, col: False,
            "population": ":,",
            growth_col: ":+.2f",
            "violent_crime_rate": ":,.0f",
            "bachelors_pct": ":.1f",
            "median_income": "$,.0f",
            "growth_rank": True,
        },
        labels={
            "population": f"Population {y1}", growth_col: "Growth %",
            "violent_crime_rate": "Violent crime /100k", "bachelors_pct": "Bachelor's+ %",
            "median_income": "Median income", "growth_rank": "Growth rank",
        },
    )
    fig.update_traces(marker_line_width=0.1, marker_line_color="rgba(255,255,255,0.4)")
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=560,
                      coloraxis_colorbar=dict(title=metric, thickness=12))
    st.caption(spec["note"] + (" - colors clipped at the 2nd/98th percentile" if len(vals) else ""))
    st.plotly_chart(fig, use_container_width=True)

    # ---- Ranking table --------------------------------------------------
    st.subheader("Counties ranked by population growth")
    table = (view.sort_values(growth_col, ascending=False)
                 [["growth_rank", "county", "state", "population", growth_col,
                   "violent_crime_rate", "bachelors_pct", "median_income"]]
                 .rename(columns={
                     "growth_rank": "Rank", "county": "County", "state": "State",
                     "population": f"Pop. {y1}", growth_col: "Growth %",
                     "violent_crime_rate": "Violent crime /100k",
                     "bachelors_pct": "Bachelor's+ %", "median_income": "Median income"}))
    st.dataframe(
        table, hide_index=True, use_container_width=True, height=520,
        column_config={
            f"Pop. {y1}": st.column_config.NumberColumn(format="%d"),
            "Growth %": st.column_config.NumberColumn(format="%+.2f%%"),
            "Violent crime /100k": st.column_config.NumberColumn(format="%.0f"),
            "Bachelor's+ %": st.column_config.NumberColumn(format="%.1f%%"),
            "Median income": st.column_config.NumberColumn(format="$%d"),
        },
    )
    st.download_button("Download this table as CSV", table.to_csv(index=False),
                       "county_rankings.csv", "text/csv")

    with st.expander("Data notes"):
        st.markdown(
            f"- **Population:** Census Population Estimates, July 1 {y0} to July 1 {y1}.\n"
            "- **Income & education:** ACS 5-year estimates 2020-2024 (small counties have wide margins of error).\n"
            "- **Crime:** County Health Rankings violent crime rate (FBI UCR, multi-year average). "
            "Some counties are blank where agencies did not report.\n"
            "- **Connecticut** uses its 9 planning regions instead of the old 8 counties in Census data since 2022."
        )


if __name__ == "__main__":
    import sys
    if "streamlit" in sys.modules or os.environ.get("STREAMLIT_SERVER_PORT"):
        run_app()
    else:
        # Allow `python county_dashboard.py --export` to just build the merged CSV
        if "--export" in sys.argv:
            out = build_table(load_population(), load_acs(), load_crime())
            out.to_csv(DATA_DIR / "county_data.csv", index=False)
            print(f"Wrote {len(out):,} counties to {DATA_DIR / 'county_data.csv'}")
        else:
            run_app()
