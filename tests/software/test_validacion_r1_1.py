"""Casos adversos del verificador; los resultados reales se evalúan aparte."""

from collections import Counter
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

from validacion.r1_1 import verificar_datasets as verify


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.raw, self.final, self.log = (root / name for name in ("raw.csv", "final.csv", "log.csv"))
        self.raw_rows = [
            {"ESTACION": "A", "FECHA": "20250101", "HORA": str(hour * 10000),
             "LONGITUD": "-77", "LATITUD": "-12", "ALTITUD": "100",
             "DISTRITO": "LIMA", "PROVINCIA": "LIMA", "PM2_5": value}
            for hour, value in enumerate(("10", "", "12", ""))
        ]
        self.final_rows = [
            {**row, "PM2_5": value, "PM2_5_SOURCE": source}
            for row, value, source in zip(self.raw_rows, ("10", "11", "12", ""),
                                         ("senamhi_observed", "imputed_short_linear",
                                          "senamhi_observed", "missing_structural"))
        ]
        self.log_rows = [{"station": "A", "variable": "PM2_5", "method_used": "short_linear",
                          "accepted": "True", "n_values_imputed": "1",
                          "imputed_timestamps": "2025-01-01T01:00:00"}]

    def run_verification(self):
        for path, rows in ((self.raw, self.raw_rows), (self.final, self.final_rows),
                           (self.log, self.log_rows)):
            verify.write_csv(path, rows, list(rows[0]))
        return verify.verify_product("pm25", self.final, self.raw, self.log,
                                     ["PM2_5"], date(2026, 10, 7))

    @staticmethod
    def status(result, criterion):
        return next(row["estado"] for row in result["checks"] if row["criterio"] == criterion)

    def test_sparse_months_do_not_prove_consecutive_observations(self):
        self.assertEqual(verify.longest_run(["2025-01", "2025-03", "2025-05",
                                             "2025-07", "2025-09", "2025-11"]), 1)
        self.assertEqual(verify.longest_run(["2025-12", "2026-01", "2026-01"]), 2)
        result = self.run_verification()
        self.assertEqual(self.status(result, "meses_con_observaciones_PM2_5"), "NO_CUMPLE")
        self.assertEqual(self.status(result, "periodo_y_frecuencia"), "NO_CUMPLE")

    def test_changed_original_cannot_be_claimed_as_observed(self):
        self.final_rows[0]["PM2_5"] = "99"
        result = self.run_verification()
        self.assertEqual(self.status(result, "observaciones_preservadas"), "NO_CUMPLE")

    def test_duplicate_log_contribution_is_rejected(self):
        self.log_rows.append(self.log_rows[0].copy())
        self.assertEqual(self.status(self.run_verification(), "log_imputacion"), "NO_CUMPLE")

    def test_wrong_log_timestamp_is_rejected_even_if_counts_match(self):
        self.log_rows[0]["imputed_timestamps"] = "2025-01-01T03:00:00"
        self.assertEqual(self.status(self.run_verification(), "log_imputacion"), "NO_CUMPLE")

    def test_missing_value_cannot_be_tagged_as_imputed(self):
        self.final_rows[1]["PM2_5"] = ""
        self.assertEqual(self.status(self.run_verification(), "procedencia"), "NO_CUMPLE")

    def test_duplicate_station_hour_is_rejected(self):
        self.final_rows.append(self.final_rows[0].copy())
        with self.assertRaisesRegex(ValueError, "Clave duplicada"):
            self.run_verification()

    def test_coverage_distinguishes_imputed_and_missing(self):
        result = self.run_verification()
        row = result["table"][0]
        self.assertEqual((row["observaciones"], row["imputados"], row["ausencias_antes"],
                          row["ausencias_finales"], row["cobertura_final_pct"]), (2, 1, 2, 1, 75))
        self.assertEqual(self.status(result, "log_imputacion"), "CUMPLE")
        no_applicable = verify.table_row("DIR_VIENTO", Counter())
        self.assertEqual(no_applicable["cobertura_final_pct"], "")

    def test_non_hourly_time_and_nonfinite_values_are_rejected(self):
        with self.assertRaises(ValueError):
            verify.timestamp({"FECHA": "20250101", "HORA": "130100"})
        with self.assertRaises(ValueError):
            verify.number("nan")
        self.assertEqual(verify.timestamp({"FECHA": "20250101", "HORA": "0"}), datetime(2025, 1, 1))

    def test_north_is_equivalent_but_other_angles_and_scalars_are_not(self):
        self.assertTrue(verify.same_observation("DIR_VIENTO", 0.0, 360.0))
        self.assertTrue(verify.same_observation("DIR_VIENTO", 360.0, 0.0))
        self.assertFalse(verify.same_observation("DIR_VIENTO", 0.0, 720.0))
        self.assertFalse(verify.same_observation("DIR_VIENTO", 0.0, 180.0))
        self.assertFalse(verify.same_observation("TEMP", 0.0, 360.0))

    def test_undefined_coverage_is_a_reference_mismatch(self):
        self.assertFalse(verify.matches_reference(
            {"variable": "DIR_VIENTO", "cobertura_final_pct": ""},
            {"variable": "DIR_VIENTO", "cobertura_final_pct": "98.24"},
        ))
