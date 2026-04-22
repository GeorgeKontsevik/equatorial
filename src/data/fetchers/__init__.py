"""Dataset fetcher registry."""

from __future__ import annotations

from src.data.fetchers import (
    cams,
    chirps,
    coastaldem,
    era5,
    era5_spi,
    flood,
    flopros,
    gadm,
    gem,
    ibtracs,
    landslide_susceptibility,
    liquefaction,
    osm,
    road_surface,
    soilgrids,
    worldcover,
)

FETCHER_REGISTRY = {
    "gadm": gadm.fetch,
    "osm": osm.fetch,
    "road_surface": road_surface.fetch,
    "chirps": chirps.fetch,
    "era5": era5.fetch,
    "era5_spi": era5_spi.fetch,
    "flood": flood.fetch,
    "coastaldem": coastaldem.fetch,
    "soilgrids": soilgrids.fetch,
    "ibtracs": ibtracs.fetch,
    "gem": gem.fetch,
    "liquefaction": liquefaction.fetch,
    "flopros": flopros.fetch,
    "landslide_susceptibility": landslide_susceptibility.fetch,
    "cams": cams.fetch,
    "worldcover": worldcover.fetch,
}
