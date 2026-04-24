# Manual Steps For Road Surface `BRN RWA BDI GNQ TGO LKA LBR BEN SUR GUY GHA UGA ECU GAB PHL CIV MYS COG CMR PNG SOM THA KEN CAF SDS FRA NGA VEN TZA ETH COL AGO PER IDN COD BRA` (`paved / unpaved`)

Automatic download from the direct country file URL did not complete successfully.

Direct file URL:
- https://downloads.ohsome.org/hdx/mapillary_road_surface/heigit_brn rwa bdi gnq tgo lka lbr ben sur guy gha uga ecu gab phl civ mys cog cmr png som tha ken caf sds fra nga ven tza eth col ago per idn cod bra_roadsurface_lines.gpkg

Dataset page:
- https://data.humdata.org/organization/heidelberg-institute-for-geoinformation-technology?dataseries_name=Heidelberg%20Institute%20for%20Geoinformation%20Technology%20-%20Road%20Surface%20Data&q=&ext_page_size=25

Reference paper:
- https://doi.org/10.1016/j.isprsjprs.2025.02.020

What to do:
1. Download the file manually from the direct URL above.
2. Place it under `data/raw/road_surface/BRN RWA BDI GNQ TGO LKA LBR BEN SUR GUY GHA UGA ECU GAB PHL CIV MYS COG CMR PNG SOM THA KEN CAF SDS FRA NGA VEN TZA ETH COL AGO PER IDN COD BRA/heigit_brn rwa bdi gnq tgo lka lbr ben sur guy gha uga ecu gab phl civ mys cog cmr png som tha ken caf sds fra nga ven tza eth col ago per idn cod bra_roadsurface_lines.gpkg`.
3. Re-run `python -m src.data.fetch --config config/datasets.yaml` and `python -m src.data.inspect --config config/datasets.yaml`.
