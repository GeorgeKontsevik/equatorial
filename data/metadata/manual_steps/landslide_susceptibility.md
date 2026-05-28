# Manual Steps For NASA Global Landslide Susceptibility

Automatic export from the NASA map service did not complete successfully.

Service roots attempted:
- https://gis.earthdata.nasa.gov/gis05/rest/services/Landslides/Global_Landslide_Susceptibility/ImageServer
- https://maps.nccs.nasa.gov/server/rest/services/global_landslide_catalog/landslide_susceptibility/MapServer
- https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer

Suggested export URLs:
- https://gis.earthdata.nasa.gov/gis05/rest/services/Landslides/Global_Landslide_Susceptibility/ImageServer/exportImage?bbox=-74.99%2C-34.75%2C-27.84%2C6.27&bboxSR=4326&imageSR=4326&size=5658%2C4923&format=tiff&pixelType=S8&f=image
- https://maps.nccs.nasa.gov/server/rest/services/global_landslide_catalog/landslide_susceptibility/MapServer/export?bbox=-74.99%2C-34.75%2C-27.84%2C6.27&bboxSR=4326&imageSR=4326&size=5658%2C4923&format=tiff&pixelType=S8&f=image&transparent=false
- https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer/export?bbox=-74.99%2C-34.75%2C-27.84%2C6.27&bboxSR=4326&imageSR=4326&size=5658%2C4923&format=tiff&pixelType=S8&f=image&transparent=false

What to do:
1. Open the service in a browser or GIS client.
2. Export or save a TIFF clip for the study-area bbox.
3. Place it under `data/raw/landslide_susceptibility/global/`.
4. Re-run the fetch and inspect commands.
