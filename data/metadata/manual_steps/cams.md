# Manual Steps For CAMS / ADS

The ADS API call did not complete successfully.

Dataset page:
- https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4-monthly

Configured dataset id:
- `cams-global-reanalysis-eac4-monthly`

Configured request:
```python
{'date': ['2024-01-01/2024-03-31'], 'variable': ['particulate_matter_2.5um', 'particulate_matter_10um', 'dust_aerosol_optical_depth_550nm'], 'format': 'netcdf'}
```

What to check:
1. Your ADS credentials are valid.
2. You accepted the licence for the dataset.
3. The request fields match the selected CAMS dataset.
4. After fixing the issue, re-run the fetch command.
