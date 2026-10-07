"""Pruebas de software offline para la adquisición de precipitación ECMWF IFS."""

from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pandas as pd

import integrar_precipitacion_ifs as ifs


def open_meteo_payload(
    unit: str = "mm", timezone: str = "America/Lima"
) -> dict:
    return {
        "latitude": -12.05,
        "longitude": -77.01,
        "timezone": timezone,
        "hourly_units": {"precipitation": unit},
        "hourly": {
            "time": [
                "2025-01-01T00:00",
                "2025-01-01T01:00",
                "2025-01-01T02:00",
            ],
            "precipitation": [0.0, 0.2, None],
        },
    }


class IfsAcquisitionTests(unittest.TestCase):
    def test_open_meteo_request_is_explicit(self) -> None:
        url = ifs.open_meteo_url(-12.0, -77.0, "2025-01-01", "2025-01-02")
        query = parse_qs(urlparse(url).query)

        self.assertEqual(query["models"], ["ecmwf_ifs"])
        self.assertEqual(query["timezone"], ["America/Lima"])
        self.assertEqual(query["hourly"], ["precipitation"])
        self.assertEqual(query["elevation"], ["nan"])

    def test_normalizes_to_independent_pp_ifs_column(self) -> None:
        frame = ifs.normalize_open_meteo_payload("A", open_meteo_payload())

        self.assertEqual(list(frame.columns), ["ESTACION", "TIMESTAMP_LOCAL", "PP_IFS"])
        self.assertEqual(frame["PP_IFS"].iloc[:2].tolist(), [0.0, 0.2])
        self.assertTrue(pd.isna(frame["PP_IFS"].iloc[2]))

    def test_rejects_wrong_timezone_or_unit(self) -> None:
        with self.assertRaisesRegex(ValueError, "Zona horaria inesperada"):
            ifs.normalize_open_meteo_payload(
                "A", open_meteo_payload(timezone="UTC")
            )
        with self.assertRaisesRegex(ValueError, "milímetros"):
            ifs.normalize_open_meteo_payload(
                "A", open_meteo_payload(unit="inch")
            )

    def test_masks_hard_invalid_and_operational_outlier_candidates(self) -> None:
        payload = open_meteo_payload()
        payload["hourly"]["precipitation"] = [-0.1, 61.0, 0.5]

        frame = ifs.normalize_open_meteo_payload("A", payload)

        self.assertTrue(frame["PP_IFS"].iloc[:2].isna().all())
        self.assertEqual(frame["PP_IFS"].iloc[2], 0.5)

    def test_acquisition_returns_every_station_hour_without_touching_pp(self) -> None:
        stations = pd.DataFrame(
            {
                "ESTACION": ["A", "B"],
                "LATITUD": [-12.0, -12.1],
                "LONGITUD": [-77.0, -77.1],
            }
        )
        grid = pd.date_range("2025-01-01", periods=3, freq="1h")
        with patch.object(ifs, "fetch_open_meteo", return_value=open_meteo_payload()):
            result = ifs.acquire_pp_ifs(stations, grid)

        self.assertEqual(len(result), 6)
        self.assertEqual(list(result.columns), ["ESTACION", "FECHA", "HORA", "PP_IFS"])
        self.assertEqual(result.groupby("ESTACION").size().to_dict(), {"A": 3, "B": 3})

    def test_incomplete_remote_grid_aborts_acquisition(self) -> None:
        stations = pd.DataFrame(
            {"ESTACION": ["A"], "LATITUD": [-12.0], "LONGITUD": [-77.0]}
        )
        grid = pd.date_range("2025-01-01", periods=4, freq="1h")
        with patch.object(ifs, "fetch_open_meteo", return_value=open_meteo_payload()):
            with self.assertRaisesRegex(ValueError, "omitió"):
                ifs.acquire_pp_ifs(stations, grid)

    def test_module_has_no_cli_or_persistence_api(self) -> None:
        for name in ["main", "build_parser", "run", "prepare_run_directory", "write_json"]:
            self.assertFalse(hasattr(ifs, name))


if __name__ == "__main__":
    unittest.main()
