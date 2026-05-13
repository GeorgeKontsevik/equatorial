from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import Point

from src.data import run_multisource_road_overlay as overlay


class _FakeRaster:
    def __init__(self, data: np.ndarray) -> None:
        self._data = data
        self.transform = from_origin(0.0, 4.0, 1.0, 1.0)
        self.crs = None
        self.nodata = -9999.0
        self.width = data.shape[1]
        self.height = data.shape[0]
        self.block_shapes = [(2, 2)]

    def __enter__(self) -> "_FakeRaster":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def sample(self, coords):
        raise AssertionError("cell-based overlay should not call rasterio.sample")

    def read(self, indexes: int, window: Window | None = None):
        if indexes != 1:
            raise AssertionError("test raster expects band 1 reads only")
        if window is None:
            return self._data.copy()
        row_off = int(window.row_off)
        col_off = int(window.col_off)
        height = int(window.height)
        width = int(window.width)
        return self._data[row_off : row_off + height, col_off : col_off + width].copy()


class SampleRasterPathsTest(unittest.TestCase):
    def test_samples_unique_cells_without_pointwise_sampling(self) -> None:
        raster = _FakeRaster(
            np.asarray(
                [
                    [1.0, 2.0, 3.0, 4.0],
                    [5.0, -9999.0, 7.0, 8.0],
                    [9.0, 10.0, 11.0, 12.0],
                    [13.0, 14.0, 15.0, 16.0],
                ],
                dtype="float64",
            )
        )
        probe_points = gpd.GeoSeries(
            [
                Point(0.1, 3.9),
                Point(0.2, 3.8),
                Point(1.1, 2.8),
                Point(3.4, 1.2),
                Point(9.0, 9.0),
            ],
            crs="EPSG:4326",
        )

        with patch.object(overlay.rasterio, "open", return_value=raster):
            values = overlay._sample_raster_paths([Path("fake.tif")], probe_points)

        self.assertEqual(values.shape[0], 5)
        self.assertEqual(values[0], 1.0)
        self.assertEqual(values[1], 1.0)
        self.assertTrue(np.isnan(values[2]))
        self.assertEqual(values[3], 12.0)
        self.assertTrue(np.isnan(values[4]))


class Era5PathResolutionTest(unittest.TestCase):
    def test_falls_back_to_monthly_hourly_files_when_merged_target_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            era5_root = Path(tmpdir) / "era5"
            era5_root.mkdir(parents=True, exist_ok=True)
            for month in range(1, 13):
                (era5_root / f"era5-land-hourly-brazil-2024-{month:02d}.nc").write_text("", encoding="utf-8")

            paths = overlay._era5_paths_from_config(
                Path(tmpdir),
                {"target_filename": "era5-land-brazil.nc"},
                analysis_start=date(2024, 1, 1),
                analysis_end=date(2024, 12, 31),
            )

        self.assertEqual(len(paths), 12)
        self.assertTrue(all(path.name.startswith("era5-land-hourly-brazil-2024-") for path in paths))


class FloodPathResolutionTest(unittest.TestCase):
    def test_falls_back_to_filename_dates_when_catalog_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            flood_dir = project_root / "data" / "raw" / "flood" / "copernicus_gfm" / "GFM" / "2024" / "001"
            flood_dir.mkdir(parents=True, exist_ok=True)
            path = flood_dir / "ENSEMBLE_FLOOD_20240101T092104_VV_SA020M_E081N075T3.tif"
            path.write_text("", encoding="utf-8")

            by_week = overlay._flood_paths_by_week_start(
                project_root / "data",
                project_root / "data" / "raw",
                [date(2024, 1, 1), date(2024, 1, 8)],
            )

        self.assertEqual(by_week[date(2024, 1, 1)], [path])
        self.assertEqual(by_week[date(2024, 1, 8)], [])

    def test_falls_back_to_monthly_hourly_files_for_hourly_q3_target_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            era5_root = Path(tmpdir) / "era5"
            era5_root.mkdir(parents=True, exist_ok=True)
            for month in range(1, 13):
                (era5_root / f"era5-land-hourly-brazil-2024-{month:02d}.nc").write_text("", encoding="utf-8")

            paths = overlay._era5_paths_from_config(
                Path(tmpdir),
                {"target_filename": "era5-land-hourly-brazil-2024-q3.nc"},
                analysis_start=date(2024, 1, 1),
                analysis_end=date(2024, 12, 31),
            )

        self.assertEqual(len(paths), 12)
        self.assertTrue(all(path.name.startswith("era5-land-hourly-brazil-2024-") for path in paths))


if __name__ == "__main__":
    unittest.main()
