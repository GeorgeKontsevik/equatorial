# equatorial

`equatorial` now has two complementary pieces:

1. A reproducible raw-data acquisition pipeline for transport-climate-agriculture risk inputs.
2. The existing Bolivia sample workflow in [bolivia_sample_task_i.py](/Users/gk/Code/super-duper-disser/equatorial/bolivia_sample_task_i.py:1) and [Bolivia_Sample_Task_I.ipynb](/Users/gk/Code/super-duper-disser/equatorial/Bolivia_Sample_Task_I.ipynb:1).

The new data pipeline is conservative by design:
- it only automates official, reasonably stable sources;
- it validates downloaded files;
- it writes a machine-readable catalog;
- and when access is manual or credential-gated, it writes explicit instructions instead of failing the whole run.

## Layout

Key project paths:

- `src/data/fetch.py`: fetch CLI
- `src/data/inspect.py`: spatial metadata inspection CLI
- `src/data/fetchers/`: dataset-specific fetchers
- `config/datasets.yaml`: example configuration
- `data/raw/`: local data lake
- `data/metadata/catalog.csv`: machine-readable inventory
- `reports/data_inventory.md`: human-readable inventory report

Expected data-lake structure:

```text
data/
  raw/
    gadm/
    osm/
    chirps/
    era5/
    flood/
    coastaldem/
    soilgrids/
    ibtracs/
    cams/
    gem/
    landslide_susceptibility/
    flopros/
    worldcover/
  metadata/
    catalog.csv
    catalog.json
    manual_steps/
  logs/
```

Storage convention:

- global datasets: `data/raw/<dataset>/global/`
- country datasets: `data/raw/<dataset>/<ISO3>/`
- bbox / tiled datasets keep their source-specific subfolders when they are not naturally country-wise

## Setup

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
uv venv .venv
source .venv/bin/activate
uv pip install -e .
```

This project uses a dedicated submodule-local virtual environment:

- env path: `.venv/` inside `equatorial`
- package manager: `uv`
- no shared top-level environment is required

## Fetch Raw Data

Run the acquisition pipeline:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.fetch --config config/datasets.yaml
```

For a local one-off country cache warm-up without editing the main config:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.fetch --config config/datasets.yaml --country-code GAB --datasets gadm,road_surface
```

That mode is intended for country-wise local caching:

- `gadm` is downloaded once per country and then reused from `data/raw/gadm/<ISO3>/`
- `road_surface` is downloaded once per country and then reused from `data/raw/road_surface/<ISO3>/`

Then inspect downloaded assets and enrich the catalog with CRS, bounds, raster shape, and layer metadata:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.inspect --config config/datasets.yaml
```

Render quick-look previews for the current study area:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
MPLCONFIGDIR=/tmp/mpl-equatorial .venv/bin/python -m src.data.render_country_previews --config config/datasets.yaml
```

For a different country without editing the main config:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
MPLCONFIGDIR=/tmp/mpl-equatorial .venv/bin/python -m src.data.render_country_previews --config config/datasets.yaml --country-code GAB
```

The preview PNGs are written to:

- `outputs/country_preview/<ISO3>/`
- plus a small `manifest.json` in the same folder

## Change Study Area

The config now supports a single top-level `study_area` block. In the common case, changing territory means editing only this section in [datasets.yaml](/Users/gk/Code/super-duper-disser/equatorial/config/datasets.yaml:1):

```yaml
study_area:
  country_code: BOL
  country_name: Bolivia
  slug: bolivia
  geofabrik_id: south-america/bolivia
  bbox: [-69.8, -22.9, -57.4, -9.6]
```

Those values are then reused automatically for:

- `gadm.country_codes`
- `osm.geofabrik_ids` when that dataset is enabled
- `chirps`, `flood`, `coastaldem`, `soilgrids`, and `road_surface` bbox defaults
- `era5.request.area`
- templated names such as `era5.target_filename`

So if you want a different country, update `study_area` and rerun the fetch/inspect commands.

This first pass currently aims to cover:

- `gadm`: country GeoPackages from GADM 4.1
- `road_surface`: country-level HeiGIT road-surface downloads (`paved / unpaved`) with manual fallback
- `chirps`: monthly global CHIRPS GeoTIFFs
- `era5`: ERA5 / ERA5-Land via CDS API, with manual fallback when credentials or licence acceptance are missing
- `era5_spi`: ready-made global monthly SPI GeoTIFFs derived from ERA5 via Drought.gov / NOAA NIDIS
- `landslide_susceptibility`: NASA global landslide susceptibility map clip for the active study area
- `flood`: JRC / Copernicus global flood hazard tiles by bbox
- `coastaldem`: manual request workflow with catalog placeholder
- `soilgrids`: WCS subset attempt with manual fallback
- `ibtracs`: NOAA IBTrACS CSV / NetCDF
- `cams`: CAMS global reanalysis via the ADS API, with manual fallback when credentials are missing
- `gem`: global seismic hazard raster from the GEM open version on Zenodo
- `liquefaction`: global liquefaction susceptibility raster from Zenodo
- `flopros`: official FLOPROS supplement archive (protection-standard metadata, not a raster hazard layer)
- `worldcover`: ESA WorldCover 10 m land-cover tiles intersecting the active study area

## Critical Fixes

The current transport-climate threshold table includes several indicators defined at daily or hourly scale
for example intense rainfall in `mm/day`, strong winds in `m/s`, and heat stress thresholds tied to hot surface conditions.

For first-pass collection, the pipeline currently keeps some climate products in monthly form:

- `CHIRPS` currently configured as monthly
- `ERA5-Land` currently configured as monthly means
- `CAMS` currently configured as monthly means

This is a temporary fallback for data collection continuity, not a methodologically final setup.

Critical fix still required:

- switch rainfall thresholds to daily/event-scale precipitation inputs
- switch heat and wind thresholds to daily/hourly values, ideally including maxima where appropriate
- treat monthly layers only as broad contextual indicators until the finer temporal products are wired in

Operational resolution summary for current sources:

| Source | Spatial resolution | Temporal resolution in current pipeline | Can replace monthly with daily/hourly? | Recommended target cadence |
| --- | --- | --- | --- | --- |
| `SPAM harvested area` | `0.083333°` (`5 arc-min`), about `~9 x 9 km` | static (`2010`) | `No` | keep static |
| `SPAM production` | `0.083333°` (`5 arc-min`), about `~9 x 9 km` | static (`2010`) | `No` | keep static |
| `CHIRPS` | `0.05°`, about `~5.5 x 5.5 km` | monthly | `Yes` | daily |
| `ERA5-Land` | `0.1°`, about `~11 x 11 km` | monthly | `Yes` | daily or hourly |
| `ERA5 SPI` | about `~0.25°`, about `~28-31 km` | monthly | `No` for a simple daily swap; this is already a derived monthly drought index | keep monthly SPI, or replace with daily precipitation / soil-moisture workflows |
| `CAMS` | about `~0.75°`, about `~80 km` | monthly | `Partly` | daily or sub-daily when product access supports it |
| `Flood hazard` | `3 arc-second`, about `~90 m` | static return-period layer | `No` | keep static |
| `Landslide susceptibility` | about `30 arc-second`, about `~1 km` | static | `No` | keep static |
| `WorldCover` | `10 m` | static (`2021`) | `No` | keep static |
| `SoilGrids` | about `250 m` | static | `No` | keep static |
| `GEM` | about `0.05°`, about `~5-6 km` | static | `No` | keep static |
| `Liquefaction` | about `0.0108° x 0.0105°`, about `~1.1 km` | static | `No` | keep static |
| `IBTrACS` | track points / lines, not a raster grid | event time series | `Not applicable` | keep event/time-series form |
| `GADM` | vector polygons | static versioned boundaries | `No` | keep static |
| `road_surface` | vector lines | static snapshot | `No` | keep static |
| `GeoNames cities` | point layer | static snapshot | `No` | keep static |

## Agreed Next Scenario Step

The next agreed SPAM-based routing experiment is intentionally deferred until the routing setup is ready.

Planned rule for the first pass:

- use `SPAM production` cells as agricultural origins
- keep them separated by crop, not only as one aggregated surface
- prioritize high-production cells using crop-specific upper quantiles such as `p95`
- route each selected origin to the nearest city node as a simple first-pass destination proxy

This note is a future-work decision only. It does not change the current pipeline behavior yet.

## Article Data vs Current Collection

Comparison between data described in the Nature Communications article (`10.1038/s41467-019-10442-3`) and the current `equatorial` first-pass collection.

Status legend:

- `🟢` downloaded / already present in current collection
- `🟡` configured as manual or placeholder, but not downloaded yet
- `🔴` not currently represented in `equatorial`
- `⚪` article-only reference or parameter note; no direct local layer expected

### Transport And Boundaries

| Status | In Article (Methods) | Article Source(s) | Spatial Detail In Article | Availability | What Exists In Current `equatorial` Collection | Current Source(s) | Spatial Detail In Current Collection | Where This Comes From In Current Project |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `🟢` | Road network with `paved / unpaved` attribution for exposure | `OpenStreetMap` planet file (article data availability notes July 17, 2018 snapshot) + OSM surface tagging (`surface=*`, `tracktype=*`) | Vector line assets; paved/unpaved classification comes from OSM road-surface tags; analysis split across 46,566 GADM level 1/2 regions | Open | `road_surface` is the intended current source for road-surface attribution in `equatorial`, and the fetcher now resolves direct country downloads from the active `study_area` | `HeiGIT` Road Surface Data on HDX (`https://data.humdata.org/organization/heidelberg-institute-for-geoinformation-technology?dataseries_name=Heidelberg%20Institute%20for%20Geoinformation%20Technology%20-%20Road%20Surface%20Data&q=&ext_page_size=25`), direct country files from `downloads.ohsome.org`, reference paper DOI `https://doi.org/10.1016/j.isprsjprs.2025.02.020` | Country-wise vector road segments with binary `paved / unpaved` attribution; stored per ISO3 under `data/raw/road_surface/<ISO3>/` | Country-level download, file read, and PNG preview rendering were validated on the `GAB` example (`data/raw/road_surface/GAB/` and `outputs/road_surface_preview/GAB/`) |
| `🟢` | `GADM` administrative boundaries (level 1/2 split) | `GADM` administrative datasets (article methods refer to level 1/2 split) | Vector administrative polygons at GADM level 1 and 2 | Open (terms apply) | `gadm` is configured from the active `study_area`, and the current STP artifact has already been downloaded | `https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg` | Country-level vector GeoPackage per ISO3; current study-area file is `data/raw/gadm/STP/gadm41_STP.gpkg` | Auto-resolved from `study_area.country_code`; fetch/inspect were rerun successfully for `STP` |

### Seismic Hazards

| Status | In Article (Methods) | Article Source(s) | Spatial Detail In Article | Availability | What Exists In Current `equatorial` Collection | Current Source(s) | Spatial Detail In Current Collection | Where This Comes From In Current Project |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `🟢` | Earthquake hazard maps from `UNISDR GAR 2015`; intensity bands mapped via `USGS ShakeMap` to PGA bins | `UNISDR GAR 2015` portal (`risk.preventionweb.net`) + `USGS ShakeMap` mapping reference | Global PGA hazard maps; five return periods (1/250 to 1/2475); article notes coarse global resolution | Mixed: published research products; not a simple direct API download path | We still do not reproduce the article's original `UNISDR GAR 2015` source directly, but the current project now uses `GEM` as an operational global substitute with automatic download and local reuse | `GEM` open version via Zenodo (`https://zenodo.org/records/8409647`) and product page `https://www.globalquakemodel.org/product/global-seismic-hazard-map/` | Global seismic hazard raster stored under `data/raw/gem/global/`; the open version is interpolated from hazard values calculated at about `~6 km` point spacing | This is green as a working project source, not as an exact article-source replica: `equatorial` now auto-downloads the GEM global raster and clips it by country downstream |
| `🟢` | Global liquefaction susceptibility map (Zhu et al. model family + geospatial predictors) | `Zenodo` liquefaction dataset: `https://doi.org/10.5281/zenodo.2583746` | Global raster in `EPSG:4326`; coastal model applied within `20 km` of coastline and inland model elsewhere; cell values are susceptibility classes `0-5` | Open research dataset | `liquefaction` is now configured as a global source with automatic download and local reuse from `data/raw/liquefaction/global/` | `Zenodo` direct file download (`liquefaction_v1_deg.tif`) | Single-band `EPSG:4326` raster, about `0.0108 x 0.0105` degree cells, shape `13313 x 33212`, `uint8` susceptibility classes | Article-methods mapping corrected from the Zenodo dataset and Zhu model description; current fetcher prefers the local global copy and downloads it automatically if missing |

### Cyclone Hazards

| Status | In Article (Methods) | Article Source(s) | Spatial Detail In Article | Availability | What Exists In Current `equatorial` Collection | Current Source(s) | Spatial Detail In Current Collection | Where This Comes From In Current Project |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `🟢` | Tropical cyclone hazard maps from `UNISDR GAR 2015` | `UNISDR GAR 2015` cyclone hazard maps | Global cyclone wind hazard maps (3-second gust speed) with five return periods (1/50 to 1/1000) across major cyclone basins | Legacy/research hazard products | No explicit `UNISDR GAR 2015` cyclone hazard layer; current pipeline uses `ibtracs` tracks | `IBTrACS` NOAA NCEI (`v04r01` netcdf endpoints in catalog) | `ibtracs` stored as track point/line time-series under `data/raw/ibtracs/global/`, not as gridded return-period hazard map | Article-methods mapping added in this comparison; `ibtracs` already exists in project docs/tables |

### Flood Hazards

| Status | In Article (Methods) | Article Source(s) | Spatial Detail In Article | Availability | What Exists In Current `equatorial` Collection | Current Source(s) | Spatial Detail In Current Collection | Where This Comes From In Current Project |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `🟢` | River and surface flood hazard from `Fathom` global pluvial/fluvial maps | `Fathom Global` pluvial/fluvial flood hazard dataset (May 2017 in article) | 3-arcsecond (about 90 m) gridded water-depth hazard; ten return periods (1/5 to 1/1000); global coverage 56S to 60N | Generally licensed research/commercial ecosystem | Current pipeline uses `JRC/Copernicus GLOFAS` flood hazard tiles (`flood` dataset), and the active `STP` bbox tiles were already downloaded during the latest fetch run | `https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-GLOFAS/flood_hazard/` | 3-arcsecond (about 90 m) raster tiles selected by bbox intersection; current STP run produced tiles such as `ID118_N10_W0_RP100_depth.tif` and `ID119_N0_W0_RP100_depth.tif` under `data/raw/flood/jrc_glofas/` | `flood` already exists in project docs/tables; article-vs-source mismatch identified in this comparison |
| `🟡` | Coastal flooding stack (`LISFLOOD-FP`, `MERIT-DEM`, `WAVEWATCH-III`, `DFLOW-FM`) | JRC coastal flood map workflow (article methods) | MERIT-DEM at 3 arcsecond input; inundation simulation at 90 m; coastal segments around 75 km with land up to 100 km inland | Multi-source modeling stack; not a single quick download product | No full coastal hazard-model stack currently automated; only `coastaldem` manual placeholder | `CoastalDEM` product page: `https://www.climatecentral.org/coastaldem-v2.1` | No coastal inundation simulation outputs currently cataloged; only manual acquisition placeholder for CoastalDEM | Article-methods mapping added in this comparison; `coastaldem` already exists in project docs/tables |

### Parameters And Assumptions

| Status | In Article (Methods) | Article Source(s) | Spatial Detail In Article | Availability | What Exists In Current `equatorial` Collection | Current Source(s) | Spatial Detail In Current Collection | Where This Comes From In Current Project |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `⚪` | `FLOPROS` flood protection standards used for design-standard assumptions | `FLOPROS` database/publication (used in article as protection standard assumptions) | Parameter/standard layer used as flood protection design assumption (non-raster in article workflow context) | Published dataset/reference material (parameter/metadata style) | `flopros` is now downloaded from the official NHESS supplement and stored locally as workbook + shapefile + note file | Official supplement archive: `https://nhess.copernicus.org/articles/16/1049/2016/nhess-16-1049-2016-supplement.zip` | Protection-standard shapefile and spreadsheet; still not a hazard raster | Current project now keeps the official FLOPROS supplement locally as an adjustment/protection input rather than only as a placeholder |

### Current Extra Layers Not In That Article

| Status | In Article (Methods) | Article Source(s) | Spatial Detail In Article | Availability | What Exists In Current `equatorial` Collection | Current Source(s) | Spatial Detail In Current Collection | Where This Comes From In Current Project |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `🟡` | Extra first-pass layers in current pipeline (`CHIRPS`, `ERA5`, `SoilGrids`, `GEM`) | Not part of the article's core hazard-input stack | Not part of the article's core hazard-input stack | Mostly open or credential-gated depending on source | Mixed state: several layers already exist locally for the current setup (`CHIRPS`, `IBTrACS`, liquefaction, `GEM`), while `ERA5`, `SoilGrids` refresh, and coastal products still need additional work | `CHIRPS` (`data.chc.ucsb.edu`), `ERA5` (`cds.climate.copernicus.eu`), `SoilGrids` (`maps.isric.org`), `IBTrACS` (`ncei.noaa.gov`), `GEM` (`zenodo.org`), global liquefaction raster (`zenodo.org`) | `CHIRPS` about 0.05 degree, `ERA5` about 0.1 degree, `SoilGrids` about 250 m when valid, `IBTrACS` track points/lines, `GEM` about `0.05°`, liquefaction about `0.0108 x 0.0105` degree | These are current-pipeline extensions; several are already materialized locally, while others remain intentionally manual or need re-fetching for the active territory |

## Additional Reference Article: Change Of Precipitation

Notes from the Nature Communications paper `Global transportation infrastructure exposure to the change of precipitation in a warmer world` (`https://doi.org/10.1038/s41467-023-38203-3`).

This paper is methodologically different from the 2019 multi-hazard article above. It focuses on future shifts in extreme precipitation return periods and transport drainage-design exposure, not on the broader multi-hazard asset-risk stack.

### Climate And Exposure Inputs

| Status | In Additional Article | Article Source(s) | Spatial Detail In Article | Availability | What Exists In Current `equatorial` Collection | Current Source(s) | Spatial Detail In Current Collection | Where This Comes From In Current Project |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `🔴` | Future extreme precipitation projections | `NASA Earth Exchange Global Daily Downscaled Projections (NEX-GDDP)` derived from `CMIP5` | Gridded climate projections at `0.25°` (about 25 km) global resolution | Open research dataset | No `NEX-GDDP` dataset is currently configured in `equatorial` | Current climate layers are `CHIRPS` for observed precipitation and `ERA5-Land` for reanalysis variables | `CHIRPS` about `0.05°`; `ERA5-Land` about `0.1°`; both differ from the article's future climate projection dataset | Added from user-provided `change of precipitation.pdf`; this mapping is not yet represented as a dedicated dataset key in `config/datasets.yaml` |

## Credentials And Manual Sources

`ERA5` / `ERA5-Land` requires CDS API access. The fetcher checks:

- `CDSAPI_URL`
- `CDSAPI_KEY`
- or `~/.cdsapirc`

If credentials are missing, or a source needs manual approval, the pipeline writes:

- a catalog row with `status=manual`
- a Markdown instruction file under `data/metadata/manual_steps/`

That keeps the pipeline runnable end-to-end even when some datasets are not fully automatable.

Current manual or semi-manual items in this repo:

- `era5`: manual unless CDS credentials and licence acceptance are already set up; this is why the fetcher can fall back to instructions instead of downloading automatically.
- `coastaldem`: manual because this project does not currently rely on a stable public direct-download/API path for the exact study-area asset we want.

## Bolivia Sample Workflow

The original sample workflow still runs separately:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python bolivia_sample_task_i.py --country-code BOL
```

Notebook wrapper:

```bash
Bolivia_Sample_Task_I.ipynb
```
