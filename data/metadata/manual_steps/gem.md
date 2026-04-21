# Manual Steps For GEM Global Seismic Hazard

Automatic download of the GEM open version did not succeed in the current environment.

Official product page:
- https://www.globalquakemodel.org/product/global-seismic-hazard-map/

Open dataset reference:
- https://zenodo.org/records/8409647/files/GEM-GSHM_PGA-475y-rock_v2023.zip?download=1

What to do:
1. Download the GEM open-version ZIP archive.
2. Place the ZIP under `data/raw/gem/global/`.
3. Extract it so that `v2023_1_pga_475_rock_3min.tif` is present under `data/raw/gem/global/`.
4. Re-run the fetch and inspect commands.
