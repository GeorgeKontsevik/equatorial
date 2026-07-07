# equatorial

---

[![OSA-improved](https://img.shields.io/badge/improved%20by-OSA-yellow)](https://github.com/aimclub/OSA)

Built with:

![duckdb](https://img.shields.io/badge/DuckDB-FFF000.svg?style={0}&logo=DuckDB&logoColor=black)
![jupyter](https://img.shields.io/badge/Jupyter-F37626.svg?style={0}&logo=Jupyter&logoColor=white)
![numpy](https://img.shields.io/badge/NumPy-013243.svg?style={0}&logo=NumPy&logoColor=white)
![pandas](https://img.shields.io/badge/pandas-150458.svg?style={0}&logo=pandas&logoColor=white)
![psycopg](https://img.shields.io/badge/PostgreSQL-4169E1.svg?style={0}&logo=PostgreSQL&logoColor=white)
![scipy](https://img.shields.io/badge/SciPy-8CAAE6.svg?style={0}&logo=SciPy&logoColor=white)
![sqlalchemy](https://img.shields.io/badge/SQLAlchemy-D71F00.svg?style={0}&logo=SQLAlchemy&logoColor=white)

---

## Table of Contents

- [Overview](#overview)
- [Core Features](#core-features)
- [Installation](#installation)
- [Getting Started](#getting-started)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Citation](#citation)

---

## Overview

equatorial is a Python-based geospatial data tooling project for assembling and analyzing transport, climate, and agriculture datasets, with a focus on equatorial-belt studies and accessibility-oriented analysis. It is aimed at developers and data scientists working with data-fetching, processing, and database-backed spatial workflows, including notebook-assisted exploration and reproducible figure generation. The repository provides script-driven entry points for data acquisition, inspection, rendering, and analysis rather than a single monolithic application. New contributors should start with the Getting Started guidance for the runnable path and expected outputs.

---

## Core Features

- Config-driven data fetching orchestrates country- and date-scoped acquisition of raw geospatial and climate inputs, giving developers a repeatable way to build study-area datasets from the same pipeline.
- Database-backed accessibility processing runs road-network analysis against PostgreSQL/PostGIS, enabling scalable origin-to-network computation for transport and accessibility workflows.
- Geospatial data handling spans vector, raster, tabular, and NetCDF formats, so the project can combine heterogeneous environmental and transport inputs in one workflow.
- Country preview rendering and inspection commands support quick validation of fetched data and derived outputs, helping developers check results without digging through raw files manually.
- Notebook-assisted analysis is supported alongside scripts, making exploratory work and reproducible research easier to share within the same repository.

---

## Installation

**Prerequisites:** requires Python >=3.11

Install equatorial using one of the following methods:

**Build from source:**

1. Clone the equatorial repository:
```sh
git clone https://github.com/GeorgeKontsevik/equatorial
```

2. Navigate to the project directory:
```sh
cd equatorial
```

3. Install the project dependencies:

```sh
pip install -r requirements.txt
```

---

## Getting Started

## Prerequisites

- Python 3.11
- The project dependencies listed in `pyproject.toml`
- Bash for the repository scripts
- Access to the data sources and database used by the pipeline when you run fetch or processing jobs

## Quick start

1. Clone the repository and enter it.
2. Create and activate a Python environment, then install the project dependencies from `pyproject.toml`.
3. If you want to generate the full-year equator sample raw-data fetch configs, run:

```bash
bash scripts/fetch_equator_700km_full_year_data.sh
```

4. To run the full-year fetch for the default configuration, set `DRY_RUN=0`:

```bash
DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
```

5. To limit the fetch to specific country codes, use `ONLY`:

```bash
ONLY="AGO,BDI,BRN" DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
```

6. For accessibility processing, the repository also provides the weekly A* runner at `scripts/run_weekly_astar_accessibility.py`.

---

## Documentation

A detailed equatorial description is available [here](https://github.com/GeorgeKontsevik/equatorial/tree/main/docs).

---

## Contributing

- **[Report Issues](https://github.com/GeorgeKontsevik/equatorial/issues)**: Submit bugs found or log feature requests for the project.

- **[Submit Pull Requests](https://github.com/GeorgeKontsevik/equatorial/tree/main/CONTRIBUTING.md)**: To learn more about making a contribution to equatorial.

---

## Citation

If you use this software, please cite it as below.

### APA format:

    GeorgeKontsevik (2026). equatorial repository [Computer software]. https://github.com/GeorgeKontsevik/equatorial

### BibTeX format:

    @misc{equatorial,

        author = {GeorgeKontsevik},

        title = {equatorial repository},

        year = {2026},

        publisher = {github.com},

        journal = {github.com repository},

        howpublished = {\url{https://github.com/GeorgeKontsevik/equatorial}},

        url = {https://github.com/GeorgeKontsevik/equatorial}

    }

---