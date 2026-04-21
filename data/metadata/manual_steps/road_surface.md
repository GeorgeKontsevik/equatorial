# Manual Steps For Road Surface (`paved / unpaved`)

Direct automated download is not hardcoded in this first pass because HDX road-surface releases are country-wise and may change over time.

Primary dataset page:
- https://data.humdata.org/organization/heidelberg-institute-for-geoinformation-technology?dataseries_name=Heidelberg%20Institute%20for%20Geoinformation%20Technology%20-%20Road%20Surface%20Data&q=&ext_page_size=25

Reference paper:
- https://doi.org/10.1016/j.isprsjprs.2025.02.020

What to do:
1. Open the HDX page and download the country files you need (for example Bolivia and Ecuador).
2. Keep the original metadata and citation files from the download package.
3. Place the downloaded files under `data/raw/road_surface/<ISO3>/`.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml` and `python -m src.data.inspect --config config/datasets.yaml`.
