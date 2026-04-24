# Manual Steps For ESA WorldCover

Automatic download of the required WorldCover tiles did not complete successfully.

Dataset page:
- https://esa-worldcover.org/en/data-access

Requested configuration:
- year: 2021
- version: v200
- layer: Map
- bbox: [6.433333, -0.016667, 7.5, 1.75]

Expected tile:
- ESA_WorldCover_10m_2021_v200_S03E006_Map.tif

Suggested direct URL:
- https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_S03E006_Map.tif

What to do:
1. Download the required tile manually from the ESA WorldCover public bucket, Zenodo package, or WorldCover download portal.
2. Place it under `data/raw/worldcover/2021/v200/map/`.
3. Re-run the fetch and inspect commands.
