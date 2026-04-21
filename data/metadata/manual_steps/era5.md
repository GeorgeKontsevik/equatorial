# Manual Steps For ERA5 / ERA5-Land

The CDS API credentials were not found.

Expected credential locations:
- environment variables: `CDSAPI_URL` and `CDSAPI_KEY`
- accepted aliases in `.env`: `CDS_API_URL` and `CDS_API_KEY`
- or `~/.cdsapirc`

Dataset page:
- https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-monthly-means

What to do:
1. Create or sign in to a Copernicus Climate Data Store account.
2. Accept the dataset licence on the dataset page if prompted.
3. Configure `~/.cdsapirc` or the relevant environment variables.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml`.
