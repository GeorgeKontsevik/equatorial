from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

from src.data import run_weekly_factor_boxplots_streaming as streaming


class WeeklyFactorBoxplotsStreamingTest(unittest.TestCase):
    def test_weighted_stats_returns_expected_quartiles(self) -> None:
        frame = pd.DataFrame({"value": [1.0, 2.0, 5.0], "count": [1, 2, 1]})
        stats = streaming._weighted_stats(frame)
        self.assertEqual(stats["n_values"], 4)
        self.assertEqual(stats["min"], 1.0)
        self.assertEqual(stats["q25"], 1.0)
        self.assertEqual(stats["median"], 2.0)
        self.assertEqual(stats["q75"], 2.0)
        self.assertEqual(stats["max"], 5.0)

    def test_counts_to_percentiles_maps_scoped_values_from_global_cdf(self) -> None:
        raw = pd.DataFrame({"value": [10.0, 20.0, 30.0], "count": [1, 2, 1]})
        scoped = pd.DataFrame({"value": [20.0, 30.0], "count": [3, 1]})
        out = streaming._counts_to_percentiles(raw, scoped)
        expected = pd.DataFrame({"value": [75.0, 100.0], "count": [3, 1]})
        pd.testing.assert_frame_equal(out.reset_index(drop=True), expected)

    def test_surface_mask_respects_unknown_surface_scenarios(self) -> None:
        surface = pd.Series(["paved", "unknown", "unpaved", "unknown"])
        actual = streaming._surface_mask(surface, "actual_unpaved", "unpaved")
        as_paved = streaming._surface_mask(surface, "unknown_as_paved", "unpaved")
        as_unpaved = streaming._surface_mask(surface, "unknown_as_unpaved", "unpaved")
        np.testing.assert_array_equal(actual, np.array([False, False, True, False]))
        np.testing.assert_array_equal(as_paved, np.array([False, False, True, False]))
        np.testing.assert_array_equal(as_unpaved, np.array([False, True, True, True]))

    def test_valid_probe_point_mask_rejects_null_and_empty_geometries(self) -> None:
        geoms = gpd.GeoSeries([Point(1.0, 2.0), None, Point()], crs="EPSG:4326")
        mask = streaming._valid_probe_point_mask(geoms)
        np.testing.assert_array_equal(mask, np.array([True, False, False]))

    def test_has_visibility_station_files_ignores_metadata_only_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_root = Path(tmpdir) / "raw"
            target = raw_root / "visibility_noaa_isd" / "BRA"
            target.mkdir(parents=True, exist_ok=True)
            (target / "stations.csv").write_text("station_id\n123\n", encoding="utf-8")

            has_files = streaming._has_visibility_station_files(
                raw_root,
                "BRA",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
            )

        self.assertFalse(has_files)

    def test_has_usable_visibility_values_detects_missing_vis_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_root = Path(tmpdir) / "raw"
            target = raw_root / "visibility_noaa_isd" / "BRA" / "2024"
            target.mkdir(parents=True, exist_ok=True)
            (target / "81609099999.csv").write_text("DATE,VIS\n2024-01-01T00:00:00Z,999999\n", encoding="utf-8")

            has_usable = streaming._has_usable_visibility_values(
                raw_root,
                "BRA",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
            )

        self.assertFalse(has_usable)


if __name__ == "__main__":
    unittest.main()
