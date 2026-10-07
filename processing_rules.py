"""Esquema y reglas internas compartidas por el preprocesamiento."""

from __future__ import annotations

from dataclasses import dataclass


TIMEZONE_ASSUMPTION = "America/Lima"
METEO_VARS = ["TEMP", "HR", "PP", "DIR_VIENTO", "VEL_VIENTO"]
PM25_VARS = ["PM2_5"]
KEY_COLUMNS = ["ESTACION", "FECHA", "HORA"]
METADATA_COLUMNS = [
    "LONGITUD",
    "LATITUD",
    "ALTITUD",
    "RED",
    "PROVINCIA",
    "DISTRITO",
]
MEASUREMENT_COLUMNS = [*METEO_VARS, *PM25_VARS]
MAX_WIND_SPEED_MS = 75.0


@dataclass(frozen=True)
class VariableQualityRule:
    """Reglas duras y operativas mantenidas solo por el productor."""

    hard_min: float | None
    hard_max: float | None
    plausible_min: float
    plausible_max: float


QUALITY_RULES = {
    "TEMP": VariableQualityRule(-273.15, None, -10.0, 60.0),
    "HR": VariableQualityRule(0.0, 100.0, 0.0, 100.0),
    "PP": VariableQualityRule(0.0, None, 0.0, 60.0),
    "DIR_VIENTO": VariableQualityRule(0.0, 360.0, 0.0, 360.0),
    "VEL_VIENTO": VariableQualityRule(0.0, None, 0.0, MAX_WIND_SPEED_MS),
    "PM2_5": VariableQualityRule(0.0, None, 0.0, 1000.0),
}

OPERATIONAL_RANGES = {
    variable: (rule.plausible_min, rule.plausible_max)
    for variable, rule in QUALITY_RULES.items()
}

BASE_SOURCE_STATES = (
    "senamhi_observed",
    "senamhi_operational_outlier",
    "missing_hard_invalid",
    "missing_explicit",
    "missing_implicit",
    "missing_structural",
    "not_applicable",
)
SCALAR_IMPUTATION_METHODS = (
    "short_linear",
    "weekly_seasonal",
    "cross_year_analog",
    "interstation_regression",
)
PP_IMPUTATION_METHODS = (*SCALAR_IMPUTATION_METHODS, "short_zero")
WIND_IMPUTATION_METHODS = (
    "short_vector_linear",
    "weekly_vector_seasonal",
    "cross_year_vector_analog",
    "interstation_vector_regression",
)


def imputation_methods_for(variable: str) -> tuple[str, ...]:
    if variable == "PP":
        return PP_IMPUTATION_METHODS
    if variable in {"DIR_VIENTO", "VEL_VIENTO"}:
        return WIND_IMPUTATION_METHODS
    if variable in {*METEO_VARS, *PM25_VARS}:
        return SCALAR_IMPUTATION_METHODS
    raise ValueError(f"Variable sin reglas: {variable!r}")


def allowed_sources_for(variable: str) -> tuple[str, ...]:
    return (
        *BASE_SOURCE_STATES,
        *(f"imputed_{method}" for method in imputation_methods_for(variable)),
    )
