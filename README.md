# US County Dashboard

Maps all ~3,140 US counties, ranks them by population growth, and lets you slide the map between population growth, violent crime rate, education, and median income.

## Setup

```bash
pip install -r requirements.txt
streamlit run county_dashboard.py
```

The first run downloads about 15 MB from the Census Bureau and County Health Rankings and caches it in `./data`. Later runs load from that cache. Delete `./data` to force a fresh download (for example when the ACS 2021-2025 release comes out in December 2026; bump `ACS_YEAR` to 2025 at that point).

Optional: get a free Census API key at https://api.census.gov/data/key_signup.html and set it as `CENSUS_API_KEY` to avoid rate limits.

## Export only the merged data

```bash
python county_dashboard.py --export
```

Writes `data/county_data.csv` with one row per county (FIPS, population 2020-2025, growth %, median income, bachelor's %, violent crime rate, growth rank). Useful for joining into other models.

## Controls

- **Map layer slider:** Population growth, Violent crime rate, Education, Median income
- **Growth period:** cumulative 2020-2025 or the latest single year
- **Minimum population:** hides tiny counties, whose growth rates swing wildly on small numbers
- **States:** filter to one or more states; ranks are recalculated within the filter

## Sources

| Metric | Source |
|---|---|
| Population growth | Census Population Estimates, Vintage 2025 (CO-EST2025-alldata) |
| Median household income | ACS 5-year 2020-2024, B19013 |
| Bachelor's degree or higher | ACS 5-year 2020-2024, B15003 (age 25+) |
| Violent crime rate | County Health Rankings, measure v043 (FBI UCR, per 100k) |
| County boundaries | Census cartographic boundary file 2024, 1:20m |
