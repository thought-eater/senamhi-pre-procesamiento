"""Pruebas de software de las decisiones metodológicas del pipeline.

Estas pruebas usan series sintéticas pequeñas. No pretenden demostrar que un
método sea científicamente óptimo; protegen contratos concretos para que una
refactorización no cambie silenciosamente qué horas se imputan o qué evidencia
se considera aceptable.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

import senamhi_preprocesamiento as pipeline
from processing_rules import OPERATIONAL_RANGES, QUALITY_RULES


def gap_record(start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Crea el mismo registro mínimo que produce ``find_gaps``."""
    n_steps = int((end - start) / pd.Timedelta(hours=1)) + 1
    return pd.Series({"start": start, "end": end, "n_steps": n_steps, "duration_h": n_steps})


def station_frame(
    index: pd.DatetimeIndex,
    values: np.ndarray | pd.Series,
    latitude: float,
    longitude: float,
) -> pd.DataFrame:
    """Construye una estación sintética con metadatos geográficos constantes."""
    return pd.DataFrame(
        {
            "TEMP": np.asarray(values, dtype=float),
            "LATITUD": latitude,
            "LONGITUD": longitude,
        },
        index=index,
    )


def wind_station_frame(
    index: pd.DatetimeIndex,
    direction: np.ndarray | pd.Series,
    speed: np.ndarray | pd.Series,
    latitude: float,
    longitude: float,
) -> pd.DataFrame:
    """Construye una estación sintética con las dos mediciones de viento."""
    return pd.DataFrame(
        {
            "DIR_VIENTO": np.asarray(direction, dtype=float),
            "VEL_VIENTO": np.asarray(speed, dtype=float),
            "LATITUD": latitude,
            "LONGITUD": longitude,
        },
        index=index,
    )


class ShortGapTests(unittest.TestCase):
    def test_linear_interpolation_changes_only_requested_gap(self) -> None:
        index = pd.date_range("2025-01-01", periods=12, freq="1h")
        series = pd.Series(np.arange(12, dtype=float), index=index)
        series.loc[index[2:4]] = np.nan
        series.loc[index[7:9]] = np.nan

        result = pipeline.impute_short_gap(
            series,
            gap_record(index[2], index[3]),
            method="linear",
            variable="TEMP",
        )

        self.assertTrue(result.loc[index[2:4]].notna().all())
        self.assertTrue(result.loc[index[7:9]].isna().all())

    def test_linear_interpolation_does_not_cross_excluded_values(self) -> None:
        index = pd.date_range("2025-01-01", periods=5, freq="1h")
        series = pd.Series([10.0, np.nan, np.nan, np.nan, 20.0], index=index)

        result = pipeline.impute_short_gap(
            series,
            gap_record(index[1], index[1]),
            method="linear",
            variable="TEMP",
        )

        self.assertTrue(pd.isna(result.loc[index[1]]))

    def test_precipitation_does_not_assume_zero_without_two_anchors(self) -> None:
        index = pd.date_range("2025-01-01", periods=4, freq="1h")
        series = pd.Series([np.nan, np.nan, 0.0, 0.0], index=index)

        result = pipeline.impute_short_gap(
            series,
            gap_record(index[0], index[1]),
            method="zero",
            variable="PP",
        )

        self.assertTrue(result.loc[index[0:2]].isna().all())

    def test_logical_method_reports_linear_when_zero_policy_has_no_anchors(self) -> None:
        index = pd.date_range("2025-01-01", periods=5, freq="1h")
        series = pd.Series([np.nan, np.nan, 1.0, 2.0, 3.0], index=index)

        method = pipeline.resolve_short_method(
            series,
            gap_record(index[0], index[1]),
            requested_method="zero",
            variable="PP",
        )

        self.assertEqual(method, "linear")


class LongGapTests(unittest.TestCase):
    def test_weekly_mean_requires_donors_for_each_hour(self) -> None:
        index = pd.date_range("2025-01-01", "2025-02-15 23:00", freq="1h")
        observed = pd.Series(np.nan, index=index)
        gap_start = pd.Timestamp("2025-01-22 10:00")
        gap_end = pd.Timestamp("2025-01-22 14:00")
        gap_idx = pd.date_range(gap_start, gap_end, freq="1h")

        # Cuatro semanas aportan el mismo patrón. La media por hora debe
        # recuperar [10, 11, 12, 13, 14], no una media global del intervalo.
        for weeks in (-2, -1, 1, 2):
            observed.loc[gap_idx + pd.Timedelta(weeks=weeks)] = np.arange(10, 15)

        result, n_donors = pipeline.impute_long_weekly(
            observed,
            gap_record(gap_start, gap_end),
            n_weeks=2,
            min_donors=3,
        )

        self.assertIsNotNone(result)
        np.testing.assert_allclose(result.loc[gap_idx], np.arange(10, 15))
        self.assertEqual(n_donors, 4)

    def test_cascade_uses_cross_year_only_for_weekly_remainder(self) -> None:
        index = pd.date_range("2025-01-01", "2026-02-01", freq="1h")
        observed = pd.Series(np.nan, index=index)
        gap_start = pd.Timestamp("2025-01-22 10:00")
        gap_end = pd.Timestamp("2025-01-22 14:00")
        gap_idx = pd.date_range(gap_start, gap_end, freq="1h")

        # La evidencia semanal existe solo para las primeras tres horas.
        for weeks in (-2, -1, 1):
            observed.loc[gap_idx[:3] + pd.Timedelta(weeks=weeks)] = [1.0, 2.0, 3.0]
        # El análogo anual completa exclusivamente las dos horas restantes.
        observed.loc[gap_idx + pd.DateOffset(years=1)] = [10, 20, 30, 40, 50]

        config = pipeline.ImputeConfig(min_weekly_donors=3)
        result, steps, remaining = pipeline.impute_long_gap(
            observed.copy(),
            observed,
            gap_record(gap_start, gap_end),
            config,
            {"OBJETIVO": station_frame(index, observed, -12.0, -77.0)},
            "OBJETIVO",
            "TEMP",
        )

        np.testing.assert_allclose(result.loc[gap_idx], [1, 2, 3, 40, 50])
        self.assertEqual(
            [step["method_used"] for step in steps],
            [
                "weekly_seasonal",
                "cross_year_analog",
            ],
        )
        self.assertEqual([step["n_values_imputed"] for step in steps], [3, 2])
        self.assertEqual(remaining, 0)

    def test_operational_outlier_is_not_a_gap_or_temporal_donor(self) -> None:
        index = pd.date_range("2025-01-01", "2026-02-01", freq="1h")
        values = pd.Series(20.0, index=index)
        target = pd.Timestamp("2025-01-22 10:00")
        values.loc[target] = 200.0
        values.loc[target + pd.Timedelta(weeks=1)] = 200.0
        values.loc[target + pd.DateOffset(years=1)] = 200.0
        frame = station_frame(index, values, -12.0, -77.0)
        frame["FECHA"] = index.year * 10000 + index.month * 100 + index.day
        frame["HORA"] = index.hour * 10000

        pipeline.prepare_analytic_snapshot({"A": frame}, ["TEMP"])

        self.assertFalse(bool(frame.at[target, pipeline.eligibility_column("TEMP")]))
        self.assertTrue(
            pipeline.find_gaps(
                frame["TEMP"], frame[pipeline.eligibility_column("TEMP")]
            ).empty
        )
        donors = pipeline.build_weekly_donors(
            frame["TEMP"], target, target, n_weeks=1
        )
        self.assertTrue(pd.isna(donors.loc[target, 1]))
        self.assertIsNone(
            pipeline.impute_long_crossyear(
                frame["TEMP"], gap_record(target, target)
            )
        )


class InterstationRegressionTests(unittest.TestCase):
    def test_rejects_near_low_r2_and_uses_next_station(self) -> None:
        rng = np.random.default_rng(42)
        index = pd.date_range("2025-01-01", periods=240, freq="1h")
        far = np.linspace(0, 20, len(index)) + np.sin(np.arange(len(index)) / 8)
        target = 2.0 * far + 3.0
        near = rng.normal(size=len(index))
        gap_idx = index[180:186]
        target_with_gap = target.copy()
        target_with_gap[180:186] = np.nan

        stations = {
            "OBJETIVO": station_frame(index, target_with_gap, -12.00, -77.00),
            "CERCA_RUIDOSA": station_frame(index, near, -12.01, -77.00),
            "LEJOS_UTIL": station_frame(index, far, -12.20, -77.00),
        }
        config = pipeline.ImputeConfig(
            interstation_min_r2=0.8,
            predictor_min_gap_coverage=1.0,
        )

        result, steps = pipeline.impute_long_interstation(
            stations["OBJETIVO"]["TEMP"],
            gap_record(gap_idx[0], gap_idx[-1]),
            stations,
            "OBJETIVO",
            "TEMP",
            config,
        )

        rejected = [step for step in steps if not step["accepted"]]
        accepted = [step for step in steps if step["accepted"]]
        self.assertTrue(any(step["source_station"] == "CERCA_RUIDOSA" for step in rejected))
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["source_station"], "LEJOS_UTIL")
        self.assertGreaterEqual(accepted[0]["interstation_r2"], 0.8)
        np.testing.assert_allclose(result.loc[gap_idx], target[180:186], atol=1e-8)

    def test_rejects_humidity_predictions_above_one_hundred(self) -> None:
        index = pd.date_range("2025-01-01", periods=240, freq="1h")
        base = np.linspace(0.0, 1.0, len(index))
        predictor = base.copy()
        predictor[-6:] = 2.0
        target = 40.0 + 50.0 * base
        target[-6:] = np.nan
        gap_idx = index[-6:]
        stations = {
            "OBJETIVO": station_frame(index, target, -12.0, -77.0),
            "DONANTE": station_frame(index, predictor, -12.1, -77.0),
        }

        result, steps = pipeline.impute_long_interstation(
            stations["OBJETIVO"]["TEMP"].rename("HR"),
            gap_record(gap_idx[0], gap_idx[-1]),
            {
                name: frame.rename(columns={"TEMP": "HR"})
                for name, frame in stations.items()
            },
            "OBJETIVO",
            "HR",
            pipeline.ImputeConfig(interstation_min_r2=0.8),
        )

        self.assertTrue(result.loc[gap_idx].isna().all())
        self.assertTrue(any(step["rejection_reason"] == "predictions_out_of_range" for step in steps))

    def test_operational_outliers_are_excluded_from_ols_pairs(self) -> None:
        index = pd.date_range("2025-01-01", periods=100, freq="1h")
        target = station_frame(index, np.linspace(10.0, 20.0, len(index)), -12.0, -77.0)
        predictor = station_frame(index, np.linspace(11.0, 21.0, len(index)), -12.1, -77.0)
        for frame in (target, predictor):
            frame["FECHA"] = index.year * 10000 + index.month * 100 + index.day
            frame["HORA"] = index.hour * 10000
        target.loc[index[10], "TEMP"] = 200.0
        predictor.loc[index[20], "TEMP"] = 200.0
        stations = {"A": target, "B": predictor}
        pipeline.prepare_analytic_snapshot(stations, ["TEMP"])

        _, _, training_pairs = pipeline.build_interstation_model(
            stations, "A", "B", "TEMP", index[30], index[30], 5
        )

        self.assertEqual(training_pairs, 97)


class ValidationTests(unittest.TestCase):
    def test_operational_ranges_are_derived_from_quality_rules(self) -> None:
        self.assertEqual(
            OPERATIONAL_RANGES,
            {
                variable: (rule.plausible_min, rule.plausible_max)
                for variable, rule in QUALITY_RULES.items()
            },
        )

    def test_save_outputs_publishes_standard_artifacts(self) -> None:
        output = pd.DataFrame(
            {
                "ESTACION": ["A"],
                "FECHA": [20250101],
                "HORA": [0],
                "PM2_5": [10.0],
                "PM2_5_SOURCE": ["senamhi_observed"],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            pipeline.save_outputs(
                output,
                pd.DataFrame(),
                "summary\n",
                Path("input.csv"),
                output_dir,
            )
            self.assertTrue((output_dir / "input_imputed.csv").exists())
            self.assertTrue((output_dir / "input_imputation_log.csv").exists())
            self.assertTrue((output_dir / "input_summary.txt").exists())

    def test_snapshot_masks_outliers_only_in_working_copy(self) -> None:
        index = pd.date_range("2025-01-01", periods=3, freq="1h")
        frame = pd.DataFrame({"TEMP": [20.0, 70.0, 21.0]}, index=index)
        stations = {"A": frame.copy()}

        pipeline.prepare_analytic_snapshot(stations, ["TEMP"])

        self.assertTrue(pd.isna(stations["A"].loc[index[1], "TEMP"]))
        self.assertFalse(
            bool(stations["A"].loc[index[1], pipeline.eligibility_column("TEMP")])
        )
        self.assertEqual(frame.loc[index[1], "TEMP"], 70.0)

    def test_quality_rules_cover_hard_boundaries_for_every_variable(self) -> None:
        cases = {
            "TEMP": (20.0, [-274.0, np.inf]),
            "HR": (100.0, [-0.1, 100.1, np.inf]),
            "PP": (0.0, [-0.1, np.inf]),
            "DIR_VIENTO": (180.0, [-0.1, 360.1, np.inf]),
            "VEL_VIENTO": (2.0, [-0.1, np.inf]),
            "PM2_5": (12.0, [-0.1, np.inf]),
        }
        for variable, (valid, invalid_values) in cases.items():
            with self.subTest(variable=variable):
                values = [valid, *invalid_values, valid]
                index = pd.date_range("2025-01-01", periods=len(values), freq="1h")
                frame = pd.DataFrame(
                    {"FECHA": 20250101, "HORA": index.hour * 10000, variable: values},
                    index=index,
                )

                pipeline.prepare_analytic_snapshot({"A": frame}, [variable])

                self.assertEqual(frame[variable].notna().sum(), 2)
                self.assertTrue(
                    frame.loc[index[1:-1], pipeline.eligibility_column(variable)].all()
                )

    def test_operational_outlier_is_excluded_from_analytic_view(self) -> None:
        index = pd.date_range("2025-01-01", periods=2, freq="1h")
        frame = pd.DataFrame(
            {"FECHA": 20250101, "HORA": [0, 10000], "TEMP": [20.0, 200.0]},
            index=index,
        )

        pipeline.prepare_analytic_snapshot({"A": frame}, ["TEMP"])

        self.assertTrue(pd.isna(frame.loc[index[1], "TEMP"]))
        self.assertFalse(
            bool(frame.loc[index[1], pipeline.eligibility_column("TEMP")])
        )

    def test_structural_edges_are_not_imputed(self) -> None:
        index = pd.date_range("2025-01-01", periods=5, freq="1h")
        frame = station_frame(index, [np.nan, 10.0, np.nan, 12.0, np.nan], -12.0, -77.0)
        stations = {"A": frame}
        pipeline.prepare_analytic_snapshot(stations, ["TEMP"])

        imputed, log_df = pipeline.impute_file(stations, ["TEMP"], pipeline.ImputeConfig())

        self.assertTrue(pd.isna(imputed["A"].loc[index[0], "TEMP"]))
        self.assertEqual(imputed["A"].loc[index[2], "TEMP"], 11.0)
        self.assertTrue(pd.isna(imputed["A"].loc[index[4], "TEMP"]))
        self.assertEqual(log_df["n_values_imputed"].sum(), 1)

    def test_output_embeds_source(self) -> None:
        index = pd.date_range("2025-01-01", periods=3, freq="1h")
        frame = station_frame(index, [10.0, np.nan, 12.0], -12.0, -77.0)
        frame["FECHA"] = 20250101
        frame["HORA"] = index.hour * 10000
        stations = {"A": frame}
        preserved = {"A": frame.copy()}
        pipeline.prepare_analytic_snapshot(stations, ["TEMP"])
        analytic = {"A": stations["A"].copy()}
        imputed, log_df = pipeline.impute_file(stations, ["TEMP"], pipeline.ImputeConfig())
        output_views, states = pipeline.build_output_views(
            preserved, analytic, imputed, log_df, ["TEMP"]
        )
        output = pipeline.reconstruct_output_df(output_views)

        pipeline.validate_processed_output(
            preserved, analytic, imputed, output, log_df, states, ["TEMP"]
        )
        self.assertEqual(output["TEMP_SOURCE"].tolist(), [
            "senamhi_observed", "imputed_short_linear", "senamhi_observed"
        ])
        output.loc[0, "TEMP_SOURCE"] = "imputed_unknown"
        with self.assertRaisesRegex(ValueError, "SOURCE desconocido"):
            pipeline.validate_processed_output(
                preserved,
                analytic,
                imputed,
                output,
                log_df,
                states,
                ["TEMP"],
            )
        summary = pipeline.build_execution_summary(states, "meteo")
        self.assertIn("imputed_short_linear: 1", summary)

    def test_summary_reports_external_ifs_coverage(self) -> None:
        states = pd.DataFrame(
            {
                "station": ["A"],
                "variable": ["PP"],
                "origin_state": ["senamhi_observed"],
                "source": ["senamhi_observed"],
                "output_present": [True],
                "is_applicable": [True],
            }
        )
        ifs_values = pd.DataFrame(
            {
                "ESTACION": ["A", "A"],
                "FECHA": [20250101, 20250101],
                "HORA": [0, 10000],
                "PP_IFS": [0.1, np.nan],
            }
        )

        summary = pipeline.build_execution_summary(states, "meteo", ifs_values)

        self.assertIn("PRECIPITACION EXTERNA ECMWF IFS (PP_IFS)", summary)
        self.assertIn("TOTAL: 1/2 (50.00%)", summary)
        self.assertIn("no es una observacion SENAMHI", summary)

    def test_operational_outlier_is_preserved_and_hard_invalid_is_imputable(self) -> None:
        index = pd.date_range("2025-01-01", periods=5, freq="1h")
        frame = station_frame(index, [20.0, -274.0, 24.0, 200.0, 25.0], -12.0, -77.0)
        frame["FECHA"] = 20250101
        frame["HORA"] = index.hour * 10000
        stations = {"A": frame}
        preserved = {"A": frame.copy()}
        pipeline.prepare_analytic_snapshot(stations, ["TEMP"])
        analytic = {"A": stations["A"].copy()}
        imputed, log_df = pipeline.impute_file(stations, ["TEMP"], pipeline.ImputeConfig())
        output_views, states = pipeline.build_output_views(
            preserved, analytic, imputed, log_df, ["TEMP"]
        )
        output = output_views["A"]
        serialized = pipeline.reconstruct_output_df(output_views)
        pipeline.validate_processed_output(
            preserved,
            analytic,
            imputed,
            serialized,
            log_df,
            states,
            ["TEMP"],
        )
        self.assertEqual(output.loc[index[3], "TEMP"], 200.0)
        self.assertEqual(
            output.loc[index[3], "TEMP_SOURCE"], "senamhi_operational_outlier"
        )
        self.assertEqual(
            states.loc[states["timestamp"].eq(index[3]), "source"].iloc[0],
            "senamhi_operational_outlier",
        )
        self.assertAlmostEqual(output.loc[index[1], "TEMP"], 22.0)
        self.assertEqual(output.loc[index[1], "TEMP_SOURCE"], "imputed_short_linear")
        hard_invalid_state = states.loc[states["timestamp"].eq(index[1])].iloc[0]
        self.assertEqual(hard_invalid_state["origin_state"], "hard_invalid")
        self.assertEqual(hard_invalid_state["source"], "imputed_short_linear")

    def test_cli_is_frozen_to_input_selection(self) -> None:
        destinations = {
            action.dest for action in pipeline.build_parser()._actions if action.dest != "help"
        }
        self.assertEqual(destinations, {"meteo", "pm25", "only"})

    def test_schema_rejects_unknown_measurement_instead_of_filling_it(self) -> None:
        raw = pd.DataFrame(
            {
                "ESTACION": ["A"],
                "FECHA": [20250101],
                "HORA": [0],
                "TEMP": [20.0],
                "NUEVA_MEDICION": [1.0],
            }
        )

        with self.assertRaisesRegex(ValueError, "Columnas no clasificadas"):
            pipeline.validate_input_schema(raw)

    def test_schema_requires_both_wind_fields(self) -> None:
        raw = pd.DataFrame(
            {
                "ESTACION": ["A"],
                "FECHA": [20250101],
                "HORA": [0],
                "DIR_VIENTO": [90.0],
            }
        )

        with self.assertRaisesRegex(ValueError, "deben aparecer juntas"):
            pipeline.validate_input_schema(raw)

    def test_schema_rejects_duplicate_station_hour(self) -> None:
        raw = pd.DataFrame(
            {
                "ESTACION": ["A", "A"],
                "FECHA": [20250101, 20250101],
                "HORA": [0, 0],
                "LONGITUD": [-77.0, -77.0],
                "LATITUD": [-12.0, -12.0],
                "TEMP": [20.0, 21.0],
            }
        )

        with self.assertRaisesRegex(ValueError, "Claves horarias duplicadas"):
            pipeline.validate_input_schema(raw)

    def test_schema_enforces_dataset_specific_measurements(self) -> None:
        raw = pd.DataFrame(
            {
                "ESTACION": ["A"],
                "FECHA": [20250101],
                "HORA": [0],
                "LONGITUD": [-77.0],
                "LATITUD": [-12.0],
                "PM2_5": [10.0],
            }
        )

        with self.assertRaisesRegex(ValueError, "Contrato de mediciones incompatible"):
            pipeline.validate_input_schema(raw, pipeline.METEO_VARS)


class WindVectorTests(unittest.TestCase):
    def test_circular_mean_crosses_north(self) -> None:
        result = pipeline.circular_mean_degrees(pd.Series([350.0, 10.0]))

        self.assertAlmostEqual(result, 0.0, places=10)

    def test_cardinal_directions_follow_meteorological_convention(self) -> None:
        index = pd.date_range("2025-01-01", periods=4, freq="1h")
        direction = pd.Series([0.0, 90.0, 180.0, 270.0], index=index)
        speed = pd.Series(10.0, index=index)

        components = pipeline.wind_to_components(direction, speed)
        rebuilt_direction, rebuilt_speed = pipeline.components_to_wind(components)

        np.testing.assert_allclose(rebuilt_direction, direction, atol=1e-10)
        np.testing.assert_allclose(rebuilt_speed, speed, atol=1e-10)
        np.testing.assert_allclose(
            components.to_numpy(),
            [[0, -10], [-10, 0], [0, 10], [10, 0]],
            atol=1e-10,
        )

    def test_short_gap_interpolates_across_north_without_angular_jump(self) -> None:
        index = pd.date_range("2025-01-01", periods=3, freq="1h")
        frame = wind_station_frame(
            index,
            [350.0, np.nan, 10.0],
            [10.0, np.nan, 10.0],
            -12.0,
            -77.0,
        )
        stations = {"A": frame}
        components = {
            "A": pipeline.wind_to_components(frame["DIR_VIENTO"], frame["VEL_VIENTO"])
        }

        direction, speed, log_df = pipeline.impute_station_wind(
            frame[["DIR_VIENTO", "VEL_VIENTO"]],
            "A",
            pipeline.ImputeConfig(),
            components,
            stations,
        )

        self.assertAlmostEqual(direction.loc[index[1]], 0.0, places=10)
        self.assertAlmostEqual(speed.loc[index[1]], 10 * np.cos(np.radians(10)), places=10)
        np.testing.assert_allclose(direction.loc[index[[0, 2]]], [350.0, 10.0])
        self.assertEqual(set(log_df["variable"]), {"DIR_VIENTO", "VEL_VIENTO"})

    def test_calm_wind_keeps_direction_undefined(self) -> None:
        index = pd.date_range("2025-01-01", periods=1, freq="1h")
        frame = wind_station_frame(index, [np.nan], [0.0], -12.0, -77.0)
        components = pipeline.wind_to_components(frame["DIR_VIENTO"], frame["VEL_VIENTO"])

        direction, speed = pipeline.components_to_wind(components)

        self.assertTrue(pd.isna(direction.iloc[0]))
        self.assertEqual(speed.iloc[0], 0.0)

    def test_calm_wind_output_has_not_applicable_direction(self) -> None:
        index = pd.date_range("2025-01-01", periods=3, freq="1h")
        frame = wind_station_frame(
            index, [360.0, 90.0, 180.0], [5.0, 0.0, 5.0], -12.0, -77.0
        )
        frame["FECHA"] = 20250101
        frame["HORA"] = index.hour * 10000
        stations = {"A": frame}
        preserved = {"A": frame.copy()}
        variables = ["DIR_VIENTO", "VEL_VIENTO"]

        pipeline.prepare_analytic_snapshot(stations, variables)
        analytic = {"A": stations["A"].copy()}
        imputed, log_df = pipeline.impute_file(
            stations, variables, pipeline.ImputeConfig()
        )
        output_views, states = pipeline.build_output_views(
            preserved, analytic, imputed, log_df, variables
        )
        output = pipeline.reconstruct_output_df(output_views)

        pipeline.validate_processed_output(
            preserved, analytic, imputed, output, log_df, states, variables
        )
        self.assertEqual(analytic["A"].loc[index[0], "DIR_VIENTO"], 0.0)
        self.assertTrue(pd.isna(output.loc[1, "DIR_VIENTO"]))
        self.assertEqual(output.loc[1, "DIR_VIENTO_SOURCE"], "not_applicable")

    def test_numerical_calm_does_not_define_meteorological_threshold(self) -> None:
        index = pd.date_range("2025-01-01", periods=2, freq="1h")
        components = pd.DataFrame(
            {"WIND_U": [np.finfo(float).eps, 1e-10], "WIND_V": [0.0, 0.0]},
            index=index,
        )

        direction, speed = pipeline.components_to_wind(components)

        self.assertEqual(speed.iloc[0], 0.0)
        self.assertTrue(pd.isna(direction.iloc[0]))
        self.assertGreater(speed.iloc[1], 0.0)
        self.assertTrue(pd.notna(direction.iloc[1]))

    def test_calm_candidate_cannot_override_observed_direction(self) -> None:
        index = pd.date_range("2025-01-01", periods=3, freq="1h")
        frame = wind_station_frame(
            index,
            [0.0, 90.0, 180.0],
            [10.0, np.nan, 10.0],
            -12.0,
            -77.0,
        )
        stations = {"A": frame}
        components = {
            "A": pipeline.wind_to_components(frame["DIR_VIENTO"], frame["VEL_VIENTO"])
        }

        direction, speed, log_df = pipeline.impute_station_wind(
            frame[["DIR_VIENTO", "VEL_VIENTO"]],
            "A",
            pipeline.ImputeConfig(),
            components,
            stations,
        )

        self.assertEqual(direction.loc[index[1]], 90.0)
        self.assertTrue(pd.isna(speed.loc[index[1]]))
        conflicts = log_df[
            log_df["rejection_reason"]
            == "calm_candidate_conflicts_with_observed_component"
        ]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts.iloc[0]["rejected_timestamps"], index[1].isoformat())

    def test_partial_gap_preserves_observed_direction(self) -> None:
        index = pd.date_range("2025-01-01", periods=3, freq="1h")
        frame = wind_station_frame(index, [90.0, 91.0, 90.0], [10.0, np.nan, 10.0], -12.0, -77.0)
        stations = {"A": frame}
        components = {
            "A": pipeline.wind_to_components(frame["DIR_VIENTO"], frame["VEL_VIENTO"])
        }

        direction, speed, _ = pipeline.impute_station_wind(
            frame[["DIR_VIENTO", "VEL_VIENTO"]],
            "A",
            pipeline.ImputeConfig(),
            components,
            stations,
        )

        self.assertEqual(direction.loc[index[1]], 91.0)
        self.assertAlmostEqual(speed.loc[index[1]], 10.0)

    def test_negative_speed_is_masked_without_touching_source_frame(self) -> None:
        index = pd.date_range("2025-01-01", periods=2, freq="1h")
        frame = wind_station_frame(index, [90.0, 90.0], [5.0, -1.0], -12.0, -77.0)
        stations = {"A": frame.copy()}

        pipeline.prepare_analytic_snapshot(
            stations, ["DIR_VIENTO", "VEL_VIENTO"]
        )

        self.assertTrue(pd.isna(stations["A"].loc[index[1], "VEL_VIENTO"]))
        self.assertEqual(frame.loc[index[1], "VEL_VIENTO"], -1.0)

    def test_vector_interstation_regression_fills_both_fields(self) -> None:
        index = pd.date_range("2025-01-01", periods=240, freq="1h")
        predictor_direction = (np.arange(len(index)) * 7.0) % 360.0
        predictor_speed = 5.0 + np.sin(np.arange(len(index)) / 12.0)
        target_direction = predictor_direction.copy()
        target_speed = predictor_speed.copy()
        gap_index = index[180:186]
        target_direction[180:186] = np.nan
        target_speed[180:186] = np.nan
        stations = {
            "OBJETIVO": wind_station_frame(
                index, target_direction, target_speed, -12.0, -77.0
            ),
            "DONANTE": wind_station_frame(
                index, predictor_direction, predictor_speed, -12.1, -77.0
            ),
        }
        components = {
            station: pipeline.wind_to_components(
                frame["DIR_VIENTO"], frame["VEL_VIENTO"]
            )
            for station, frame in stations.items()
        }

        direction, speed, log_df = pipeline.impute_station_wind(
            stations["OBJETIVO"][["DIR_VIENTO", "VEL_VIENTO"]],
            "OBJETIVO",
            pipeline.ImputeConfig(interstation_min_r2=0.95),
            components,
            stations,
        )

        np.testing.assert_allclose(direction.loc[gap_index], predictor_direction[180:186])
        np.testing.assert_allclose(speed.loc[gap_index], predictor_speed[180:186])
        accepted = log_df[
            (log_df["method_used"] == "interstation_vector_regression")
            & log_df["accepted"]
        ]
        self.assertEqual(set(accepted["variable"]), {"DIR_VIENTO", "VEL_VIENTO"})

    def test_internal_components_are_not_added_to_output(self) -> None:
        index = pd.date_range("2025-01-01", periods=3, freq="1h")
        frame = wind_station_frame(index, [0.0, np.nan, 0.0], [5.0, np.nan, 5.0], -12.0, -77.0)

        imputed, _ = pipeline.impute_file(
            {"A": frame}, pipeline.METEO_VARS, pipeline.ImputeConfig()
        )

        self.assertNotIn("WIND_U", imputed["A"].columns)
        self.assertNotIn("WIND_V", imputed["A"].columns)


if __name__ == "__main__":
    unittest.main()
