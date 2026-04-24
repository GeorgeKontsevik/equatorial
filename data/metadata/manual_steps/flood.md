# Manual Steps For NASA MODIS NRT Flood

Automatic download did not produce any flood tile for the requested window.

Requested setup:
- product: MCDWD_L3_F3_NRT
- collection: 61
- dates: 2024-07-01 .. 2024-09-30
- bbox: [8.699028, -3.990695, 14.502346039000088, 2.3156445020000547]
- tiles: h18v08, h18v09, h19v08, h19v09

What to do:
1. Verify that files are available for the requested date range in the LANCE archive.
2. If Earthdata authentication is required, provide the appropriate token/cookies and re-run.
3. Download required tiles manually into:
   `data/raw/flood/nasa_modis/MCDWD_L3_F3_NRT/<year>/<doy>/`
