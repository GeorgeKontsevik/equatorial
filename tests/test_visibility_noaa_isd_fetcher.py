from __future__ import annotations

import unittest
from datetime import datetime

import pandas as pd

from src.data.fetchers import visibility_noaa_isd


class VisibilityNoaaIsdFetcherTest(unittest.TestCase):
    def test_select_stations_prefers_target_country_within_bbox(self) -> None:
        history = pd.DataFrame(
            [
                {
                    "USAF": "802220",
                    "WBAN": "99999",
                    "CTRY": "CO",
                    "STATION NAME": "Bogota",
                    "LAT": "4.702",
                    "LON": "-74.147",
                    "BEGIN": "19410301",
                    "END": "20250824",
                    "lat_num": 4.702,
                    "lon_num": -74.147,
                    "begin_num": 19410301,
                    "end_num": 20250824,
                },
                {
                    "USAF": "817000",
                    "WBAN": "99999",
                    "CTRY": "BR",
                    "STATION NAME": "Manaus",
                    "LAT": "-2.433",
                    "LON": "-59.567",
                    "BEGIN": "20160704",
                    "END": "20250123",
                    "lat_num": -2.433,
                    "lon_num": -59.567,
                    "begin_num": 20160704,
                    "end_num": 20250123,
                },
            ]
        )

        selected = visibility_noaa_isd._select_stations(
            history,
            [-74.24, -34.0, -28.59, 5.52],
            datetime(2024, 1, 1),
            datetime(2024, 12, 31),
            1,
            country_code="BRA",
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(str(selected.iloc[0]["CTRY"]).upper(), "BR")


if __name__ == "__main__":
    unittest.main()
