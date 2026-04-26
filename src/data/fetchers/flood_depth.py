"""Manual contract for flood-depth rasters used by road-hazard thresholds."""

from __future__ import annotations

from src.data.catalog import CatalogRecord
from src.data.utils import manual_record


FLOOD_DEPTH_NOTE = (
    "Flood-depth rasters must represent standing water depth in meters on/around roads. "
    "Copernicus GFM flood extent is not a valid substitute for this dataset."
)


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Write explicit manual instructions for flood-depth inputs."""

    bbox = dataset_cfg.get("bbox")
    country_code = str(dataset_cfg.get("country_code", "")).upper() or "<ISO3>"
    source_url = str(dataset_cfg.get("source_url", "manual flood-depth raster/model output"))
    instructions = f"""# Manual Steps For Flood-Depth Rasters

This road-hazard row needs actual water depth in meters. Flood extent,
likelihood, or positive water classification is not enough.

Acceptable inputs:
- hydraulic model output in meters
- event flood-depth raster in meters
- scenario/return-period depth raster in meters, if the run is explicitly a scenario run

Expected local layout:

```text
data/raw/flood_depth/{country_code}/
  flood_depth_YYYY_MM_DD.tif
```

or, for a static/scenario depth layer reused for every week:

```text
data/raw/flood_depth/{country_code}/flood_depth_static_m.tif
```

Each raster should be a single-band GeoTIFF. Values must be meters. Nodata
should be set in the raster metadata where possible.
"""
    return [
        manual_record(
            dataset_name="flood_depth",
            source_url=source_url,
            context=context,
            instruction_text=instructions,
            license_or_access_note=str(dataset_cfg.get("license_or_access_note", FLOOD_DEPTH_NOTE)),
            spatial_resolution_raw=str(dataset_cfg.get("spatial_resolution_raw", "source/model dependent")),
            temporal_resolution=str(dataset_cfg.get("temporal_resolution", "event, weekly, or scenario static")),
            bbox=bbox,
            notes=FLOOD_DEPTH_NOTE,
        )
    ]
