"""Dataset fetcher registry."""

from __future__ import annotations

from src.data.fetchers import (
    chirps,
    coastaldem,
    era5,
    flood,
    flopros,
    gadm,
    gem,
    ibtracs,
    liquefaction,
    osm,
    road_surface,
    soilgrids,
)

FETCHER_REGISTRY = {
    "gadm": gadm.fetch,
    "osm": osm.fetch,
    "road_surface": road_surface.fetch,
    "chirps": chirps.fetch,
    "era5": era5.fetch,
    "flood": flood.fetch,
    "coastaldem": coastaldem.fetch,
    "soilgrids": soilgrids.fetch,
    "ibtracs": ibtracs.fetch,
    "gem": gem.fetch,
    "liquefaction": liquefaction.fetch,
    "flopros": flopros.fetch,
}
