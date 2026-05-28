# Manual Steps For ERA5 / ERA5-Land

The CDS API call did not complete successfully.

Dataset page:
- https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land

Configured dataset id:
- `reanalysis-era5-land`

Configured request:
```python
{'product_type': 'reanalysis', 'variable': ['2m_temperature', 'skin_temperature', 'total_precipitation', 'volumetric_soil_water_layer_1', '10m_u_component_of_wind', '10m_v_component_of_wind'], 'year': ['2024'], 'day': ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12', '13', '14', '15', '16', '17', '18', '19', '20', '21', '22', '23', '24', '25', '26', '27', '28', '29', '30', '31'], 'time': ['00:00', '01:00', '02:00', '03:00', '04:00', '05:00', '06:00', '07:00', '08:00', '09:00', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '16:00', '17:00', '18:00', '19:00', '20:00', '21:00', '22:00', '23:00'], 'data_format': 'netcdf', 'download_format': 'unarchived', 'area': [1.63, -81.22, -5.21, -74.98], 'month': ['01']}
```

What to check:
1. Your CDS credentials are valid.
2. You accepted the licence for the dataset.
3. The request fields match the selected dataset.
4. After fixing the issue, re-run the fetch command.
