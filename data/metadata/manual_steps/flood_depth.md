# Manual Steps For Flood-Depth Rasters

This road-hazard row needs actual water depth in meters. Flood extent,
likelihood, or positive water classification is not enough.

Acceptable inputs:
- hydraulic model output in meters
- event flood-depth raster in meters
- scenario/return-period depth raster in meters, if the run is explicitly a scenario run

Expected local layout:

```text
data/raw/flood_depth/GAB/
  flood_depth_YYYY_MM_DD.tif
```

or, for a static/scenario depth layer reused for every week:

```text
data/raw/flood_depth/GAB/flood_depth_static_m.tif
```

Each raster should be a single-band GeoTIFF. Values must be meters. Nodata
should be set in the raster metadata where possible.
