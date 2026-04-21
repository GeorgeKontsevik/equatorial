# Data Inventory

This report is generated from `data/metadata/catalog.csv`.

## Downloaded Successfully
- `chirps` -> `data/raw/chirps/global/monthly/chirps-v3.0.2024.01.tif` | raw resolution: ~0.05 degree global precipitation grid | detected resolution: 0.05000000074505806 x 0.05000000074505806 | CRS: EPSG:4326
- `flood` -> `data/raw/flood/jrc_glofas/README.txt` | raw resolution: 3 arc-seconds (~90 m) | detected resolution: n/a | CRS: n/a
- `flood` -> `data/raw/flood/jrc_glofas/RP100/ID118_N10_W0_RP100_depth.tif` | raw resolution: 3 arc-seconds (~90 m) flood depth map | detected resolution: 0.0008333333356347339 x 0.0008333333333325754 | CRS: EPSG:4326
- `flood` -> `data/raw/flood/jrc_glofas/RP100/ID119_N0_W0_RP100_depth.tif` | raw resolution: 3 arc-seconds (~90 m) flood depth map | detected resolution: 0.0008333333356347339 x 0.0008333333333325754 | CRS: EPSG:4326
- `flood` -> `data/raw/flood/jrc_glofas/copyright.txt` | raw resolution: 3 arc-seconds (~90 m) | detected resolution: n/a | CRS: n/a
- `flood` -> `data/raw/flood/jrc_glofas/tile_extents.geojson` | raw resolution: 3 arc-seconds (~90 m) | detected resolution: n/a | CRS: EPSG:4326
- `flopros` -> `data/raw/flopros/global/flopros_parameter_scale_note.csv` | raw resolution: Parameter note file | detected resolution: n/a | CRS: n/a
- `flopros` -> `data/raw/flopros/global/nhess-16-1049-2016-supplement.zip` | raw resolution: Official FLOPROS supplement archive | detected resolution: n/a | CRS: n/a
- `flopros` -> `data/raw/flopros/global/original/Scussolini_etal_Suppl_info/FLOPROS_Database_Design_&_Policy_layers_V1.xlsx` | raw resolution: Design and policy layers workbook from official FLOPROS supplement | detected resolution: n/a | CRS: n/a
- `flopros` -> `data/raw/flopros/global/original/Scussolini_etal_Suppl_info/FLOPROS_shp_V1/FLOPROS_shp_V1.shp` | raw resolution: Protection-standard polygons and attributes from official FLOPROS supplement shapefile | detected resolution: n/a | CRS: EPSG:4326
- `gadm` -> `data/raw/gadm/GAB/gadm41_GAB.gpkg` | raw resolution: vector administrative boundaries | detected resolution: n/a | CRS: EPSG:4326
- `gem` -> `data/raw/gem/global/v2023_1_pga_475_rock_3min.tif` | raw resolution: global seismic hazard raster interpolated from hazard values calculated at about ~6 km point spacing | detected resolution: 0.05 x 0.049996666666667 | CRS: EPSG:4326
- `ibtracs` -> `data/raw/ibtracs/global/v04r01/netcdf/IBTrACS.ALL.v04r01.nc` | raw resolution: Track points / lines | detected resolution: n/a | CRS: n/a
- `ibtracs` -> `data/raw/ibtracs/global/v04r01/netcdf/IBTrACS.since1980.v04r01.nc` | raw resolution: Track points / lines | detected resolution: n/a | CRS: n/a
- `ibtracs` -> `data/raw/ibtracs/global/v04r01/netcdf/IBTrACS_SerialNumber_NameMapping_v04r01_20260419.txt` | raw resolution: attribute lookup | detected resolution: n/a | CRS: n/a
- `liquefaction` -> `data/raw/liquefaction/global/liquefaction_v1_deg.tif` | raw resolution: global raster in EPSG:4326; susceptibility classes 0-5 from the Zhu model family | detected resolution: 0.010839414458117548 x 0.010517205624277022 | CRS: EPSG:4326
- `road_surface` -> `data/raw/road_surface/GAB/heigit_gab_roadsurface_lines.gpkg` | raw resolution: vector road segments with binary paved/unpaved attribution (country releases) | detected resolution: n/a | CRS: EPSG:4326
- `soilgrids` -> `data/raw/soilgrids/bdod_0-5cm_Q0.5.tif` | raw resolution: 250 m | detected resolution: 250.00572225548092 x 250.00831265628815 | CRS: n/a
- `soilgrids` -> `data/raw/soilgrids/clay_0-5cm_Q0.5.tif` | raw resolution: 250 m | detected resolution: 250.00572225548092 x 250.00831265628815 | CRS: n/a
- `soilgrids` -> `data/raw/soilgrids/sand_0-5cm_Q0.5.tif` | raw resolution: 250 m | detected resolution: 250.00572225548092 x 250.00831265628815 | CRS: n/a
- `soilgrids` -> `data/raw/soilgrids/silt_0-5cm_Q0.5.tif` | raw resolution: 250 m | detected resolution: 250.00572225548092 x 250.00831265628815 | CRS: n/a
- `soilgrids` -> `data/raw/soilgrids/soc_0-5cm_Q0.5.tif` | raw resolution: 250 m | detected resolution: 250.00572225548092 x 250.00831265628815 | CRS: n/a

## Manual Steps Required
- `coastaldem` -> `data/metadata/manual_steps/coastaldem.md` | note: CoastalDEM requires manual acquisition in this first-pass pipeline.
- `era5` -> `data/metadata/manual_steps/era5.md` | note: CDS API credentials were not detected.

## Failed
- None.

## Skipped
- `osm` -> Dataset disabled in config.

## Recommended Replacements
- `gadm`: Recommended replacement for legacy admin boundary layers: current GADM 4.1 country GeoPackages.
- `osm`: Legacy placeholder only; not the active road-surface source in the current equatorial setup.
- `road_surface`: Recommended paved/unpaved road-surface source: HeiGIT global road-surface dataset on HDX (Mapillary + OSM matching).
- `chirps`: Recommended replacement for historical precipitation forcing: CHIRPS v3 precipitation rasters.
- `era5`: Recommended replacement for older climate reanalysis inputs: ERA5-Land or ERA5 via the Copernicus CDS API.
- `flood`: Recommended replacement for coarse flood proxies: JRC/Copernicus river flood hazard maps; GloFAS only as fallback.
- `coastaldem`: Recommended coastal screening elevation source when access is granted: CoastalDEM.
- `soilgrids`: Recommended replacement for coarse soil covariates: SoilGrids 250 m layers.
- `ibtracs`: Recommended tropical cyclone track archive: NOAA IBTrACS v04r01.
- `gem`: Recommended operational global seismic hazard source in this project: GEM open seismic hazard raster from Zenodo.
- `liquefaction`: Recommended global liquefaction susceptibility source: Zhu model family raster from Zenodo.
- `flopros`: FLOPROS is a protection-standard parameter dataset, not a raster hazard layer.
