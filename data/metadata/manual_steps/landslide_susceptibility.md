# Manual Steps For NASA Global Landslide Susceptibility

Automatic export from the NASA map service did not complete successfully.

Service roots attempted:
- https://maps.nccs.nasa.gov/server/rest/services/global_landslide_catalog/landslide_susceptibility/MapServer
- https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer

Suggested export URLs:
- https://maps.nccs.nasa.gov/server/rest/services/global_landslide_catalog/landslide_susceptibility/MapServer/export?bbox=6.433333%2C-0.016667%2C7.5%2C1.75&bboxSR=4326&imageSR=4326&size=129%2C213&format=tiff&transparent=false&f=image
- https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer/export?bbox=6.433333%2C-0.016667%2C7.5%2C1.75&bboxSR=4326&imageSR=4326&size=129%2C213&format=tiff&transparent=false&f=image

What to do:
1. Open the service in a browser or GIS client.
2. Export or save a TIFF clip for the study-area bbox.
3. Place it under `data/raw/landslide_susceptibility/global/`.
4. Re-run the fetch and inspect commands.
