"""Preprocesa e imputa las series horarias obtenidas del portal SENAMHI.

Este módulo recibe por separado el CSV de PM2.5 y el CSV meteorológico. No hace
un ``join`` entre ambos: aplica el mismo procedimiento a cada conjunto y genera
salidas independientes. La unidad de análisis es una serie temporal formada por
una estación, una variable y una observación por hora.

La imputación respeta el orden temporal de los datos. Un gap corto (hasta cuatro
horas por defecto) se interpola entre sus anclas temporales. En un gap largo se
intentan, por cada hora todavía ausente, donantes del mismo día de semana y hora,
un análogo de otro año y una regresión lineal con estaciones cercanas. Los
donantes siempre proceden del snapshot observado: un valor sintético nunca se
reutiliza para fabricar otro valor sintético. Dirección y velocidad del viento
se transforman a componentes cartesianas y se imputan conjuntamente.

La última estrategia se denomina "regresión lineal entre estaciones". La
distancia geográfica ordena los candidatos, pero el modelo no contiene una
matriz de pesos ni un término de autocorrelación espacial; por ello no es una
regresión espacial formal.

Consulta ``../docs/metodologia-imputacion.md`` para los supuestos, limitaciones y
fórmulas, y ``../docs/guia-bibliotecas.md`` para la sintaxis de pandas, NumPy y
scikit-learn utilizada aquí.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import warnings
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import TimeSeriesSplit, cross_val_score

from processing_rules import (
    KEY_COLUMNS,
    MAX_WIND_SPEED_MS,
    MEASUREMENT_COLUMNS,
    METADATA_COLUMNS,
    METEO_VARS,
    OPERATIONAL_RANGES,
    PM25_VARS,
    QUALITY_RULES,
    allowed_sources_for,
)
from integrar_precipitacion_ifs import acquire_pp_ifs

warnings.filterwarnings("ignore", category=FutureWarning)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuración
# ──────────────────────────────────────────────────────────────────────────────

METEO_PATH = (
    Path(__file__).parent.parent
    / "senamhi-var-metereologicas-lima"
    / "var_meteorologicas_lima_2025_2026.csv"
)
PM25_PATH = Path(__file__).parent.parent / "scraper-pm2.5-lima" / "pm25_lima_2025_2026.csv"

WIND_DIRECTION = "DIR_VIENTO"
WIND_SPEED = "VEL_VIENTO"
WIND_COMPONENT_COLUMNS = ["WIND_U", "WIND_V"]
INTERNAL_COLUMN_PREFIX = "__"
PLOT_DPI = 300
RESULTS_ROOT = Path(__file__).parent / "resultados"

IMPUTATION_LOG_COLUMNS = [
    "station",
    "variable",
    "gap_start",
    "gap_end",
    "n_steps",
    "duration_h",
    "method_used",
    "accepted",
    "rejection_reason",
    "n_values_imputed",
    "imputed_timestamps",
    "n_values_remaining",
    "interstation_r2",
    "source_station",
    "distance_km",
    "predictor_coverage",
    "training_pairs",
    "model_intercept",
    "model_coefficient",
    "acceptance_threshold",
    "validation_scheme",
    "n_donors",
    "rejected_timestamps",
]


def eligibility_column(variable: str) -> str:
    """Nombre interno de la máscara de vida observada de una variable."""
    return f"{INTERNAL_COLUMN_PREFIX}ELIGIBLE_{variable}"

# Cobertura mínima (dentro de la ventana del gap) para que una estación
# cercana califique como predictora. Un umbral alto
# (no tolerancia cero) es necesario: estaciones con datos casi perfectos
# igual tienen huecos sueltos de 1h; lo que se busca excluir es una
# estación con una porción sustancial del gap sin datos (p.ej. porque aún
# no reportaba), que de otro modo anularía las predicciones para todas las
# demás estaciones que dependen de ella como predictor.
PREDICTOR_MIN_GAP_COVERAGE = 0.9


@dataclass
class ImputeConfig:
    """Parámetros que cambian decisiones metodológicas del pipeline.

    Mantenerlos juntos permite registrar exactamente con qué política se creó
    un dataset. Los valores por defecto son decisiones operativas del proyecto,
    no constantes universales aplicables a cualquier serie ambiental.
    """

    short_gap_hours: float = 4.0
    short_method: str = "linear"
    pp_short_method: str = "zero"  # "zero"   | "linear"
    weekly_n_weeks: int = 2
    min_weekly_donors: int = 3
    interstation_min_r2: float = 0.5
    predictor_min_gap_coverage: float = PREDICTOR_MIN_GAP_COVERAGE
    interstation_cv_splits: int = 5


# ──────────────────────────────────────────────────────────────────────────────
# Parseo de fechas
# ──────────────────────────────────────────────────────────────────────────────


def parse_datetime_index(df: pd.DataFrame) -> pd.Series:
    """
    Combina FECHA (int YYYYMMDD) + HORA (int H*10000) en un DatetimeIndex.
    Ejemplo: FECHA=20250115, HORA=130000 → 2025-01-15 13:00
    """
    # Estas dos columnas enteras reproducen el formato histórico de SENAMHI.
    # pandas necesita un DatetimeIndex para que reindex, resample e interpolate
    # conozcan la distancia temporal real entre dos observaciones.
    fecha_str = df["FECHA"].astype(str)  # "20250115"
    hora_str = (df["HORA"] // 10000).astype(str).str.zfill(2)  # "13"
    dt_str = fecha_str + hora_str  # "2025011513"
    return pd.to_datetime(dt_str, format="%Y%m%d%H")


def validate_input_schema(
    raw_df: pd.DataFrame, expected_variables: list[str] | None = None
) -> pd.DataFrame:
    """Valida y normaliza el contrato tabular antes de reindexar.

    Solo las columnas declaradas explícitamente como metadatos pueden
    propagarse. Rechazar columnas desconocidas evita volver a tratar una nueva
    medición como si fuera un atributo constante de la estación.
    """
    missing_keys = [column for column in KEY_COLUMNS if column not in raw_df.columns]
    if missing_keys:
        raise ValueError(f"Faltan columnas estructurales: {missing_keys}")

    known_columns = set(KEY_COLUMNS + METADATA_COLUMNS + MEASUREMENT_COLUMNS)
    unknown_columns = sorted(set(raw_df.columns) - known_columns)
    if unknown_columns:
        raise ValueError(f"Columnas no clasificadas en el esquema: {unknown_columns}")

    present_measurements = [
        column for column in MEASUREMENT_COLUMNS if column in raw_df.columns
    ]
    if not present_measurements:
        raise ValueError("El CSV no contiene ninguna variable de medición reconocida")
    if expected_variables is not None:
        missing_measurements = sorted(set(expected_variables) - set(present_measurements))
        unexpected_measurements = sorted(set(present_measurements) - set(expected_variables))
        if missing_measurements or unexpected_measurements:
            raise ValueError(
                "Contrato de mediciones incompatible: "
                f"faltan={missing_measurements}, inesperadas={unexpected_measurements}"
            )
    present_wind = {WIND_DIRECTION, WIND_SPEED}.intersection(raw_df.columns)
    if present_wind and present_wind != {WIND_DIRECTION, WIND_SPEED}:
        raise ValueError("DIR_VIENTO y VEL_VIENTO deben aparecer juntas")
    if raw_df["ESTACION"].isna().any():
        raise ValueError("ESTACION no puede contener valores ausentes")

    numeric_columns = [
        column
        for column in [
            "FECHA",
            "HORA",
            "LONGITUD",
            "LATITUD",
            "ALTITUD",
            *present_measurements,
        ]
        if column in raw_df.columns
    ]
    normalized = raw_df.copy()
    for column in numeric_columns:
        normalized[column] = pd.to_numeric(normalized[column], errors="raise")

    for column, lower, upper in [
        ("LATITUD", -90.0, 90.0),
        ("LONGITUD", -180.0, 180.0),
    ]:
        if column not in normalized.columns:
            raise ValueError(f"Falta el metadato geográfico obligatorio: {column}")
        values = normalized[column]
        valid = values.notna() & np.isfinite(values) & values.between(lower, upper)
        if not valid.all():
            raise ValueError(f"{column} contiene coordenadas ausentes o inválidas")

    for column in ["FECHA", "HORA"]:
        values = normalized[column]
        if values.isna().any() or not np.equal(values, np.floor(values)).all():
            raise ValueError(f"{column} debe contener enteros sin valores ausentes")
        normalized[column] = values.astype(np.int64)

    valid_hours = normalized["HORA"].between(0, 230000) & (
        normalized["HORA"] % 10000 == 0
    )
    if not valid_hours.all():
        invalid = sorted(normalized.loc[~valid_hours, "HORA"].unique().tolist())
        raise ValueError(f"HORA contiene códigos no horarios: {invalid[:10]}")

    duplicate_keys = normalized.duplicated(KEY_COLUMNS, keep=False)
    if duplicate_keys.any():
        examples = normalized.loc[duplicate_keys, KEY_COLUMNS].head(5).to_dict("records")
        raise ValueError(f"Claves horarias duplicadas: {examples}")

    station_names = normalized["ESTACION"].astype(str)
    if station_names.str.strip().eq("").any() or station_names.ne(
        station_names.str.strip()
    ).any():
        raise ValueError("ESTACION debe contener identificadores no vacíos y sin espacios laterales")
    normalized["ESTACION"] = station_names

    for station, group in normalized.groupby("ESTACION", dropna=False):
        for column in METADATA_COLUMNS:
            if column in group.columns and group[column].dropna().nunique() > 1:
                raise ValueError(
                    f"El metadato {column} no es constante para la estación {station}"
                )

    # También valida fechas imposibles antes de mutar cualquier serie.
    timestamps = parse_datetime_index(normalized)
    if len(timestamps) and timestamps.max() - timestamps.min() > pd.Timedelta(days=731):
        raise ValueError("El periodo de entrada excede dos años; revise FECHA antes de reindexar")
    return normalized


# ──────────────────────────────────────────────────────────────────────────────
# Carga y reindexado
# ──────────────────────────────────────────────────────────────────────────────


def load_and_reindex(
    filepath: Path, expected_variables: list[str] | None = None
) -> dict[str, pd.DataFrame]:
    """
    Lee el CSV, parsea el datetime, y reindexea cada estación a una grilla
    horaria completa (start → end, freq=1h). Las filas implícitamente faltantes
    se convierten en filas explícitas con NaN en las columnas de medición.
    Las columnas de metadatos (LONGITUD, LATITUD, ALTITUD, DISTRITO, etc.)
    se rellenan con forward/backward fill ya que son constantes por estación.

    Retorna un diccionario de estación a DataFrame con DatetimeIndex horario.
    """
    raw_df = validate_input_schema(pd.read_csv(filepath), expected_variables)
    log.info(
        "Cargado %s  (%d filas, estaciones: %s)",
        filepath.name,
        len(raw_df),
        sorted(raw_df["ESTACION"].unique()),
    )

    raw_df.index = parse_datetime_index(raw_df)

    meta_cols = [column for column in METADATA_COLUMNS if column in raw_df.columns]

    # Grilla horaria global (min/max sobre TODAS las estaciones), no por
    # estación: así, si una estación empieza a reportar más tarde que las
    # demás, ese período se materializa como gap NaN normal (elegible para
    # la cascada de imputación) en vez de quedar fuera del índice.
    full_idx = pd.date_range(raw_df.index.min(), raw_df.index.max(), freq="1h")

    station_dfs: dict[str, pd.DataFrame] = {}
    for station, grp in raw_df.groupby("ESTACION"):
        grp = grp.sort_index()
        grp = grp.reindex(full_idx)

        # Rellenar metadatos constantes
        for col in meta_cols:
            grp[col] = grp[col].ffill().bfill()
        grp["ESTACION"] = station

        implicit_added = full_idx.difference(raw_df[raw_df["ESTACION"] == station].index)
        if len(implicit_added) > 0:
            log.info(
                "  %s: %d filas implícitas añadidas al reindexar",
                station,
                len(implicit_added),
            )

        station_dfs[station] = grp

    return station_dfs


# ──────────────────────────────────────────────────────────────────────────────
# Preparación analítica
# ──────────────────────────────────────────────────────────────────────────────


def _hard_invalid_mask(values: pd.Series, variable: str) -> pd.Series:
    """Marca valores no finitos o incompatibles con la definición de la variable."""
    rule = QUALITY_RULES[variable]
    numeric = pd.to_numeric(values, errors="coerce")
    invalid = values.notna() & ~np.isfinite(numeric)
    if rule.hard_min is not None:
        invalid |= numeric.notna() & numeric.lt(rule.hard_min)
    if rule.hard_max is not None:
        invalid |= numeric.notna() & numeric.gt(rule.hard_max)
    return invalid


def _operational_outlier_mask(values: pd.Series, variable: str) -> pd.Series:
    """Marca observaciones fuera del dominio operacional curado."""
    numeric = pd.to_numeric(values, errors="coerce")
    lo, hi = OPERATIONAL_RANGES[variable]
    return numeric.notna() & np.isfinite(numeric) & ~numeric.between(lo, hi)


def prepare_analytic_snapshot(
    station_dfs: dict[str, pd.DataFrame], variables: list[str]
) -> None:
    """Prepara in-place los valores aptos para análisis e imputación.

    Los valores fuera del dominio duro u operacional se excluyen del snapshot.
    Las ausencias y valores hard-invalid internos son elegibles para imputación;
    los outliers operacionales y los bordes sin observaciones válidas no lo son.
    """
    for df in station_dfs.values():
        raw_by_variable = {
            variable: df[variable].copy()
            for variable in variables
            if variable in df.columns
        }
        operational_by_variable: dict[str, pd.Series] = {}

        for variable, raw in raw_by_variable.items():
            hard_invalid = _hard_invalid_mask(raw, variable)
            operational = ~hard_invalid & _operational_outlier_mask(raw, variable)
            operational_by_variable[variable] = operational
            df.loc[hard_invalid | operational, variable] = np.nan

        has_wind = {WIND_DIRECTION, WIND_SPEED}.issubset(raw_by_variable)
        if has_wind:
            df.loc[df[WIND_DIRECTION].eq(360.0), WIND_DIRECTION] = 0.0
            df.loc[df[WIND_SPEED].eq(0.0), WIND_DIRECTION] = np.nan
            wind_operational = (
                operational_by_variable[WIND_DIRECTION]
                | operational_by_variable[WIND_SPEED]
            )
            wind_observed = df[WIND_SPEED].eq(0.0) | (
                df[WIND_DIRECTION].notna() & df[WIND_SPEED].notna()
            )
            wind_span = pd.Series(False, index=df.index)
            if wind_observed.any():
                first = wind_observed.index[wind_observed][0]
                last = wind_observed.index[wind_observed][-1]
                wind_span.loc[first:last] = True

        for variable in raw_by_variable:
            observed_for_span = (
                wind_observed
                if has_wind and variable in {WIND_DIRECTION, WIND_SPEED}
                else df[variable].notna()
            )
            eligible = (
                wind_span.copy()
                if has_wind and variable in {WIND_DIRECTION, WIND_SPEED}
                else pd.Series(False, index=df.index)
            )
            if (
                not (has_wind and variable in {WIND_DIRECTION, WIND_SPEED})
                and observed_for_span.any()
            ):
                first = observed_for_span.index[observed_for_span][0]
                last = observed_for_span.index[observed_for_span][-1]
                eligible.loc[first:last] = True
            excluded = (
                wind_operational
                if has_wind and variable in {WIND_DIRECTION, WIND_SPEED}
                else operational_by_variable[variable]
            )
            eligible &= ~excluded
            df[eligibility_column(variable)] = eligible


# ──────────────────────────────────────────────────────────────────────────────
# Detección de gaps
# ──────────────────────────────────────────────────────────────────────────────


def find_gaps(series: pd.Series, eligible: pd.Series | None = None) -> pd.DataFrame:
    """
    Identifica todos los runs de NaN en una Series con DatetimeIndex horario.
    Algoritmo O(n) sin loop Python: codificación run-length con cumsum.

    Retorna DataFrame con columnas:
        gap_id, start, end, n_steps, duration_h
    """
    mask = series.isna()
    if eligible is not None:
        mask &= eligible.reindex(series.index, fill_value=False).astype(bool)
    if not mask.any():
        return pd.DataFrame(columns=["gap_id", "start", "end", "n_steps", "duration_h"])

    # Cada cambio True↔False incrementa el identificador. Agrupar únicamente
    # las posiciones True equivale a una codificación run-length vectorizada.
    runs = mask.ne(mask.shift()).cumsum()
    # Solo los runs donde mask es True (son los gaps)
    gap_series = series[mask]
    grouped = gap_series.groupby(runs[mask])

    records = []
    for gid, grp in grouped:
        records.append(
            {
                "gap_id": gid,
                "start": grp.index[0],
                "end": grp.index[-1],
                "n_steps": len(grp),
                "duration_h": len(grp),  # freq=1h → n_steps == horas
            }
        )
    return pd.DataFrame(records)


# ──────────────────────────────────────────────────────────────────────────────
# Imputación de gaps cortos
# ──────────────────────────────────────────────────────────────────────────────


def _short_gap_window(
    series: pd.Series | pd.DataFrame, gap: pd.Series
) -> pd.Series | pd.DataFrame | None:
    """Devuelve el gap con sus dos anclas horarias inmediatas, si existen."""
    left = gap["start"] - pd.Timedelta(hours=1)
    right = gap["end"] + pd.Timedelta(hours=1)
    if left not in series.index or right not in series.index:
        return None
    anchors = series.loc[[left, right]]
    if isinstance(anchors, pd.DataFrame):
        valid = anchors.notna().all(axis=1)
    else:
        valid = anchors.notna()
    if not valid.all():
        return None
    return series.loc[left:right]


def resolve_short_method(
    series: pd.Series, gap: pd.Series, requested_method: str, variable: str
) -> str:
    """Determina el método corto que realmente puede ejecutarse.

    Esta resolución se separa de la interpolación para que el log no confunda la
    política solicitada con el algoritmo efectivo. ``short_zero`` solo es real
    con dos anclas horarias inmediatas iguales a cero.
    """
    if requested_method == "zero" and variable == "PP":
        window = _short_gap_window(series, gap)
        if window is not None and window.iloc[0] == 0.0 and window.iloc[-1] == 0.0:
            return "zero"
        return "linear"
    return requested_method


def impute_short_gap(series: pd.Series, gap: pd.Series, method: str, variable: str) -> pd.Series:
    """
    Rellena exclusivamente las horas de un gap corto.

    Para PP con method='zero': zero-fill solo si ambos extremos son 0;
    si algún extremo > 0 (evento de lluvia real), cae a lineal.
    Solo se interpola la ventana delimitada por las dos anclas horarias
    inmediatas, por lo que un outlier excluido actúa como barrera.
    """
    s = series.copy()
    g_start, g_end = gap["start"], gap["end"]
    window = _short_gap_window(s, gap)
    if window is None:
        return s

    if method == "zero" and variable == "PP":
        # No se infiere ausencia de lluvia en un borde. Cero solo es defendible
        # cuando hay dos observaciones originales que encierran el gap.
        if window.iloc[0] == 0.0 and window.iloc[-1] == 0.0:
            s.loc[g_start:g_end] = 0.0
            return s
        # Cae a lineal si es un evento de lluvia real
        method = "linear"

    if method == "linear":
        candidate = window.interpolate(method="time", limit_area="inside")
        values = candidate.loc[g_start:g_end]
        # Una interpolación lineal queda dentro del rango de sus dos anclas. No
        # se hace clipping: una salida inválida debe seguir ausente y auditable.
        lo, hi = OPERATIONAL_RANGES.get(variable, (-np.inf, np.inf))
        s.loc[g_start:g_end] = values.where(values.between(lo, hi))
        return s

    return s


# ──────────────────────────────────────────────────────────────────────────────
# Imputación de gaps largos — estrategia 1: media semanal
# ──────────────────────────────────────────────────────────────────────────────


def build_weekly_donors(
    series: pd.Series, g_start: pd.Timestamp, g_end: pd.Timestamp, n_weeks: int
) -> pd.DataFrame | None:
    """
    Construye donantes de la misma hora y día de semana en semanas adyacentes.

    Para ``n_weeks=2`` se consultan ``t-14``, ``t-7``, ``t+7`` y ``t+14`` días.
    El índice de salida es el del gap, mientras que cada columna contiene los
    valores observados en uno de esos desplazamientos. ``reindex`` es importante:
    alinea cada valor donante con la hora que pretende reconstruir y deja NaN
    cuando el timestamp desplazado está fuera del periodo observado.
    """
    gap_idx = pd.date_range(g_start, g_end, freq="1h")
    donors: dict[int, np.ndarray] = {}
    for k in list(range(-n_weeks, 0)) + list(range(1, n_weeks + 1)):
        offset = pd.Timedelta(weeks=k)
        donor_idx = gap_idx + offset
        donors[k] = series.reindex(donor_idx).to_numpy()
    df_donors = pd.DataFrame(donors, index=gap_idx)
    if not df_donors.notna().any().any():
        return None
    return df_donors


def impute_long_weekly(
    series: pd.Series, gap: pd.Series, n_weeks: int, min_donors: int
) -> tuple[pd.Series | None, int]:
    """
    Calcula una media semanal solo donde existe el mínimo de donantes.

    El mínimo se evalúa por hora, no por columna completa. Así, tres semanas
    pueden respaldar una hora aunque una cuarta tenga un dato aislado ausente,
    sin afirmar que todo el gap quedó resuelto. Retorna también el menor número
    de donantes que respaldó una de las horas imputadas para dejarlo en el log.
    """
    donors = build_weekly_donors(series, gap["start"], gap["end"], n_weeks)
    if donors is None:
        return None, 0
    donor_counts = donors.notna().sum(axis=1)
    valid = donor_counts >= min_donors
    if not valid.any():
        return None, 0
    mean_vals = donors.mean(axis=1).where(valid)
    s = series.copy()
    s.loc[gap["start"] : gap["end"]] = mean_vals.values
    return s, int(donor_counts[valid].min())


# ──────────────────────────────────────────────────────────────────────────────
# Imputación de gaps largos — estrategia 2: análogo inter-anual
# ──────────────────────────────────────────────────────────────────────────────


def impute_long_crossyear(series: pd.Series, gap: pd.Series) -> pd.Series | None:
    """
    Construye un candidato con la misma fecha y hora del año adyacente.

    Se prefiere año+1 y se usa año-1 únicamente donde el primer análogo no está
    disponible. La función no exige que todo el gap tenga donante: el orquestador
    copiará las horas disponibles y enviará el resto a la siguiente estrategia.
    La comparabilidad entre años es un supuesto fuerte, no una garantía de que
    clima, tráfico o instrumentación sean idénticos.
    """
    g_start, g_end = gap["start"], gap["end"]
    gap_idx = pd.date_range(g_start, g_end, freq="1h")
    candidate = pd.Series(np.nan, index=gap_idx, dtype=float)
    for delta_years in [1, -1]:
        donor_idx = gap_idx + pd.DateOffset(years=delta_years)
        donor = pd.Series(series.reindex(donor_idx).to_numpy(), index=gap_idx)
        candidate = candidate.fillna(donor)
    if not candidate.notna().any():
        return None
    s = series.copy()
    s.loc[g_start:g_end] = candidate
    return s


# ──────────────────────────────────────────────────────────────────────────────
# Imputación de gaps largos — estrategia 3: regresión entre estaciones
# ──────────────────────────────────────────────────────────────────────────────


def station_distance_km(source: pd.DataFrame, target: pd.DataFrame) -> float | None:
    """Calcula la distancia Haversine entre dos estaciones, en kilómetros.

    Haversine aproxima la Tierra como una esfera y usa latitud/longitud. Aquí la
    distancia solo ordena estaciones candidatas; no entra como coeficiente del
    modelo. Si faltan coordenadas se retorna ``None`` porque no sería correcto
    presentar una estación como "la más cercana" sin poder medirlo.
    """

    def coordinate(df: pd.DataFrame, column: str) -> float | None:
        if column not in df.columns:
            return None
        values = df[column].dropna()
        return None if values.empty else float(values.iloc[0])

    source_lat = coordinate(source, "LATITUD")
    source_lon = coordinate(source, "LONGITUD")
    target_lat = coordinate(target, "LATITUD")
    target_lon = coordinate(target, "LONGITUD")
    if None in (source_lat, source_lon, target_lat, target_lon):
        return None

    lat1, lon1, lat2, lon2 = np.radians([source_lat, source_lon, target_lat, target_lon])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return float(6371.0088 * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a)))


def build_interstation_model(
    all_station_dfs: dict[str, pd.DataFrame],
    target_station: str,
    predictor_station: str,
    variable: str,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
    cv_splits: int,
) -> tuple[LinearRegression | None, float | None, int]:
    """
    Entrena una OLS univariante entre una estación objetivo y una predictora.

    ``LinearRegression`` estima ``objetivo = intercepto + beta * predictora``
    minimizando la suma de residuos cuadrados. Solo se usan timestamps donde
    ambas estaciones fueron observadas y se excluye el gap que se reconstruirá.

    ``TimeSeriesSplit`` conserva el orden cronológico y evalúa cada modelo en un
    bloque posterior a su entrenamiento. Un KFold ordinario mezclaría pasado y
    futuro entre folds, produciendo una evaluación demasiado optimista cuando
    hay autocorrelación temporal. El modelo final sí usa todo el periodo fuera
    del gap porque esta es una imputación retrospectiva, no un pronóstico online.
    """
    target_series = all_station_dfs[target_station][variable]
    predictor_series = all_station_dfs[predictor_station][variable]
    paired = pd.DataFrame({"predictor": predictor_series, "target": target_series})

    # Período de entrenamiento: excluir el gap
    train_mask = ~((paired.index >= g_start) & (paired.index <= g_end))
    paired = paired.loc[train_mask].dropna().sort_index()
    if len(paired) < 48:  # mínimo operativo: dos días de pares horarios
        return None, None, len(paired)

    splits = min(cv_splits, len(paired) - 1)
    if splits < 2:
        return None, None, len(paired)

    model = LinearRegression()
    cv = TimeSeriesSplit(n_splits=splits)
    cv_scores = cross_val_score(model, paired[["predictor"]], paired["target"], cv=cv, scoring="r2")
    finite_scores = cv_scores[np.isfinite(cv_scores)]
    if len(finite_scores) == 0:
        return None, None, len(paired)
    r2_cv = float(np.mean(finite_scores))
    model.fit(paired[["predictor"]], paired["target"])
    return model, r2_cv, len(paired)


def impute_long_interstation(
    series: pd.Series,
    gap: pd.Series,
    all_station_dfs: dict[str, pd.DataFrame],
    target_station: str,
    variable: str,
    config: ImputeConfig,
) -> tuple[pd.Series, list[dict]]:
    """
    Prueba estaciones por cercanía y conserva solo modelos con R² aceptable.

    Una estación puede cubrir parte de las horas pendientes. Si su modelo pasa
    el umbral, esas horas se conservan y se prueba la siguiente estación para
    las restantes. Esta cascada por hora evita dos errores opuestos: descartar
    donantes útiles porque falta una sola hora y afirmar que un método resolvió
    un gap cuando en realidad dejó NaN internos.
    """
    g_start, g_end = gap["start"], gap["end"]
    s = series.copy()
    steps: list[dict] = []
    target_df = all_station_dfs[target_station]
    candidates: list[tuple[float, str]] = []
    for station, df in all_station_dfs.items():
        if station == target_station or variable not in df.columns:
            continue
        distance = station_distance_km(target_df, df)
        if distance is not None:
            candidates.append((distance, station))

    for distance, predictor_station in sorted(candidates):
        remaining_idx = s.loc[g_start:g_end][s.loc[g_start:g_end].isna()].index
        if remaining_idx.empty:
            break
        predictor_gap = all_station_dfs[predictor_station][variable].reindex(remaining_idx)
        coverage = float(predictor_gap.notna().mean())
        if coverage < config.predictor_min_gap_coverage:
            steps.append(
                {
                    "method_used": "interstation_regression",
                    "accepted": False,
                    "rejection_reason": "insufficient_gap_coverage",
                    "n_values_imputed": 0,
                    "imputed_timestamps": None,
                    "interstation_r2": None,
                    "source_station": predictor_station,
                    "distance_km": distance,
                    "predictor_coverage": coverage,
                    "training_pairs": None,
                    "model_intercept": None,
                    "model_coefficient": None,
                    "acceptance_threshold": config.interstation_min_r2,
                    "validation_scheme": f"TimeSeriesSplit({config.interstation_cv_splits})",
                    "n_donors": None,
                }
            )
            continue

        model, r2_cv, training_pairs = build_interstation_model(
            all_station_dfs,
            target_station,
            predictor_station,
            variable,
            g_start,
            g_end,
            config.interstation_cv_splits,
        )
        if model is None:
            steps.append(
                {
                    "method_used": "interstation_regression",
                    "accepted": False,
                    "rejection_reason": "insufficient_training_pairs_or_cv",
                    "n_values_imputed": 0,
                    "imputed_timestamps": None,
                    "interstation_r2": r2_cv,
                    "source_station": predictor_station,
                    "distance_km": distance,
                    "predictor_coverage": coverage,
                    "training_pairs": training_pairs,
                    "model_intercept": None,
                    "model_coefficient": None,
                    "acceptance_threshold": config.interstation_min_r2,
                    "validation_scheme": f"TimeSeriesSplit({config.interstation_cv_splits})",
                    "n_donors": None,
                }
            )
            continue
        if r2_cv is None or r2_cv < config.interstation_min_r2:
            log.warning(
                "  %s.%s: se descarta %s (%.1f km), R² temporal %.3f < %.3f",
                target_station,
                variable,
                predictor_station,
                distance,
                r2_cv,
                config.interstation_min_r2,
            )
            steps.append(
                {
                    "method_used": "interstation_regression",
                    "accepted": False,
                    "rejection_reason": "r2_below_threshold",
                    "n_values_imputed": 0,
                    "imputed_timestamps": None,
                    "interstation_r2": r2_cv,
                    "source_station": predictor_station,
                    "distance_km": distance,
                    "predictor_coverage": coverage,
                    "training_pairs": training_pairs,
                    "model_intercept": float(model.intercept_),
                    "model_coefficient": float(model.coef_[0]),
                    "acceptance_threshold": config.interstation_min_r2,
                    "validation_scheme": f"TimeSeriesSplit({config.interstation_cv_splits})",
                    "n_donors": None,
                }
            )
            continue

        valid = predictor_gap.notna()
        if not valid.any():
            continue
        prediction_index = predictor_gap[valid].index
        predictor_frame = predictor_gap.loc[prediction_index].to_frame("predictor")
        predictions = model.predict(predictor_frame)
        lo, hi = OPERATIONAL_RANGES.get(variable, (-np.inf, np.inf))
        plausible = (predictions >= lo) & (predictions <= hi)
        prediction_index = prediction_index[plausible]
        predictions = predictions[plausible]
        if prediction_index.empty:
            steps.append(
                {
                    "method_used": "interstation_regression",
                    "accepted": False,
                    "rejection_reason": "predictions_out_of_range",
                    "n_values_imputed": 0,
                    "imputed_timestamps": None,
                    "interstation_r2": r2_cv,
                    "source_station": predictor_station,
                    "distance_km": distance,
                    "predictor_coverage": coverage,
                    "training_pairs": training_pairs,
                    "model_intercept": float(model.intercept_),
                    "model_coefficient": float(model.coef_[0]),
                    "acceptance_threshold": config.interstation_min_r2,
                    "validation_scheme": f"TimeSeriesSplit({config.interstation_cv_splits})",
                    "n_donors": None,
                }
            )
            continue
        s.loc[prediction_index] = predictions
        steps.append(
            {
                "method_used": "interstation_regression",
                "accepted": True,
                "rejection_reason": None,
                "n_values_imputed": int(len(prediction_index)),
                "imputed_timestamps": "|".join(ts.isoformat() for ts in prediction_index),
                "interstation_r2": r2_cv,
                "source_station": predictor_station,
                "distance_km": distance,
                "predictor_coverage": coverage,
                "training_pairs": training_pairs,
                "model_intercept": float(model.intercept_),
                "model_coefficient": float(model.coef_[0]),
                "acceptance_threshold": config.interstation_min_r2,
                "validation_scheme": f"TimeSeriesSplit({config.interstation_cv_splits})",
                "n_donors": None,
            }
        )
    return s, steps


# ──────────────────────────────────────────────────────────────────────────────
# Orquestador de gap largo
# ──────────────────────────────────────────────────────────────────────────────


def merge_candidate_values(
    series: pd.Series, candidate: pd.Series, gap: pd.Series
) -> tuple[pd.Series, pd.DatetimeIndex]:
    """Copia las horas todavía ausentes y retorna exactamente cuáles cambió."""
    s = series.copy()
    idx = s.loc[gap["start"] : gap["end"]].index
    candidate_values = candidate.reindex(idx)
    can_fill = s.loc[idx].isna() & candidate_values.notna()
    fill_idx = idx[can_fill]
    s.loc[fill_idx] = candidate_values.loc[fill_idx]
    return s, fill_idx


def impute_long_gap(
    series: pd.Series,
    observed_series: pd.Series,
    gap: pd.Series,
    config: ImputeConfig,
    all_station_dfs: dict[str, pd.DataFrame],
    target_station: str,
    variable: str,
) -> tuple[pd.Series, list[dict], int]:
    """
    Ejecuta la cascada por hora para un gap largo.

    ``series`` contiene lo ya rellenado en la salida; ``observed_series`` es el
    snapshot inmutable usado para buscar donantes. Separarlos evita que el orden
    de iteración convierta una imputación anterior en evidencia para otra.

    Retorna la serie, las entradas de auditoría de métodos aceptados o rechazados
    y el número de horas que permanecen ausentes.
    """
    s = series.copy()
    steps: list[dict] = []

    # 1. Media semanal estacional
    result, n_donors = impute_long_weekly(
        observed_series, gap, config.weekly_n_weeks, config.min_weekly_donors
    )
    if result is not None:
        s, imputed_idx = merge_candidate_values(s, result, gap)
        if len(imputed_idx):
            steps.append(
                {
                    "method_used": "weekly_seasonal",
                    "accepted": True,
                    "rejection_reason": None,
                    "n_values_imputed": int(len(imputed_idx)),
                    "imputed_timestamps": "|".join(ts.isoformat() for ts in imputed_idx),
                    "interstation_r2": None,
                    "source_station": None,
                    "distance_km": None,
                    "predictor_coverage": None,
                    "training_pairs": None,
                    "model_intercept": None,
                    "model_coefficient": None,
                    "acceptance_threshold": None,
                    "validation_scheme": None,
                    "n_donors": n_donors,
                }
            )

    # 2. Análogo inter-anual
    result = impute_long_crossyear(observed_series, gap)
    if result is not None:
        s, imputed_idx = merge_candidate_values(s, result, gap)
        if len(imputed_idx):
            steps.append(
                {
                    "method_used": "cross_year_analog",
                    "accepted": True,
                    "rejection_reason": None,
                    "n_values_imputed": int(len(imputed_idx)),
                    "imputed_timestamps": "|".join(ts.isoformat() for ts in imputed_idx),
                    "interstation_r2": None,
                    "source_station": None,
                    "distance_km": None,
                    "predictor_coverage": None,
                    "training_pairs": None,
                    "model_intercept": None,
                    "model_coefficient": None,
                    "acceptance_threshold": None,
                    "validation_scheme": None,
                    "n_donors": 1,
                }
            )

    # 3. Regresión con estaciones, ya ordenadas por distancia Haversine
    s, regression_steps = impute_long_interstation(
        s, gap, all_station_dfs, target_station, variable, config
    )
    steps.extend(regression_steps)

    remaining = int(s.loc[gap["start"] : gap["end"]].isna().sum())
    return s, steps, remaining


# ──────────────────────────────────────────────────────────────────────────────
# Imputación por (estación, variable)
# ──────────────────────────────────────────────────────────────────────────────


def impute_station_variable(
    series: pd.Series,
    variable: str,
    station: str,
    config: ImputeConfig,
    all_station_dfs: dict[str, pd.DataFrame],
    eligible: pd.Series | None = None,
) -> tuple[pd.Series, pd.DataFrame]:
    """
    Imputa una serie y crea un registro auditable de cada contribución.

    Los gaps se detectan una sola vez sobre la serie observada. Para gaps largos
    puede haber varias filas de log, porque distintos métodos pueden completar
    horas distintas del mismo intervalo. ``n_values_imputed`` siempre se calcula
    comparando NaN antes y después; nunca se presupone que todo el gap fue
    rellenado por el solo hecho de que una función devolvió una serie.
    """
    gaps = find_gaps(series, eligible)
    if gaps.empty:
        return series, pd.DataFrame()

    s = series.copy()
    log_rows = []

    for _, gap in gaps.iterrows():
        duration_h = gap["duration_h"]
        needs_long_cascade = duration_h > config.short_gap_hours

        if not needs_long_cascade:
            # Determinar método para gap corto
            if variable == "PP":
                requested_method = config.pp_short_method
            else:
                requested_method = config.short_method
            method_key = resolve_short_method(series, gap, requested_method, variable)

            before_nan = int(s.loc[gap["start"] : gap["end"]].isna().sum())
            before_slice = s.loc[gap["start"] : gap["end"]].copy()
            s_new = impute_short_gap(series, gap, method_key, variable)
            after_slice = s_new.loc[gap["start"] : gap["end"]]
            after_nan = int(after_slice.isna().sum())
            n_imputed = before_nan - after_nan
            imputed_idx = after_slice.index[before_slice.isna() & after_slice.notna()]
            s.loc[gap["start"] : gap["end"]] = after_slice
            log_rows.append(
                dict(
                    station=station,
                    variable=variable,
                    gap_start=gap["start"],
                    gap_end=gap["end"],
                    n_steps=gap["n_steps"],
                    duration_h=duration_h,
                    method_used=f"short_{method_key}",
                    accepted=n_imputed > 0,
                    rejection_reason=None if n_imputed > 0 else "missing_valid_anchors",
                    n_values_imputed=int(n_imputed),
                    imputed_timestamps="|".join(ts.isoformat() for ts in imputed_idx),
                    n_values_remaining=after_nan,
                    interstation_r2=None,
                    source_station=None,
                    distance_km=None,
                    predictor_coverage=None,
                    training_pairs=None,
                    model_intercept=None,
                    model_coefficient=None,
                    acceptance_threshold=None,
                    validation_scheme=None,
                    n_donors=None,
                )
            )

            # Un gap corto en el borde no tiene dos anclas y permanece NaN. En
            # vez de abandonarlo por su clasificación inicial, se le permite
            # buscar evidencia semanal, anual o de otra estación.
            needs_long_cascade = after_nan > 0

        if needs_long_cascade:
            s_new, steps, remaining = impute_long_gap(
                s, series, gap, config, all_station_dfs, station, variable
            )
            s = s_new
            for step in steps:
                log.info(
                    "  %s.%s  gap %s → %s  método: %s (%d valores)",
                    station,
                    variable,
                    gap["start"].strftime("%Y-%m-%d %H:%M"),
                    gap["end"].strftime("%Y-%m-%d %H:%M"),
                    step["method_used"],
                    step["n_values_imputed"],
                )
                log_rows.append(
                    dict(
                        station=station,
                        variable=variable,
                        gap_start=gap["start"],
                        gap_end=gap["end"],
                        n_steps=gap["n_steps"],
                        duration_h=duration_h,
                        n_values_remaining=remaining,
                        **step,
                    )
                )

            if remaining:
                log_rows.append(
                    dict(
                        station=station,
                        variable=variable,
                        gap_start=gap["start"],
                        gap_end=gap["end"],
                        n_steps=gap["n_steps"],
                        duration_h=duration_h,
                        method_used="unimputable",
                        accepted=False,
                        rejection_reason="no_method_available",
                        n_values_imputed=0,
                        imputed_timestamps=None,
                        n_values_remaining=remaining,
                        interstation_r2=None,
                        source_station=None,
                        distance_km=None,
                        predictor_coverage=None,
                        training_pairs=None,
                        model_intercept=None,
                        model_coefficient=None,
                        acceptance_threshold=None,
                        validation_scheme=None,
                        n_donors=None,
                    )
                )

    return s, pd.DataFrame(log_rows)


def wind_to_components(direction: pd.Series, speed: pd.Series) -> pd.DataFrame:
    """Convierte dirección meteorológica y velocidad a componentes cartesianas.

    SENAMHI expresa la dirección desde donde sopla el viento. Por ello se usan
    ``u=-s*sin(theta)`` y ``v=-s*cos(theta)``. Un viento calmo produce el vector
    cero incluso si su dirección es ausente, porque físicamente no está definida.
    """
    direction = pd.to_numeric(direction, errors="coerce")
    speed = pd.to_numeric(speed, errors="coerce")
    components = pd.DataFrame(np.nan, index=direction.index, columns=WIND_COMPONENT_COLUMNS)
    valid_pair = direction.notna() & speed.notna()
    theta = np.radians(direction.loc[valid_pair] % 360.0)
    components.loc[valid_pair, "WIND_U"] = -speed.loc[valid_pair] * np.sin(theta)
    components.loc[valid_pair, "WIND_V"] = -speed.loc[valid_pair] * np.cos(theta)
    calm = speed.notna() & speed.eq(0.0)
    components.loc[calm, WIND_COMPONENT_COLUMNS] = 0.0
    return components.astype(float)


def _numerical_calm(speed: pd.Series) -> pd.Series:
    """Detecta únicamente cero de coma flotante, no una calma meteorológica."""
    numeric = pd.to_numeric(speed, errors="coerce")
    tolerance = 16.0 * np.finfo(float).eps * np.maximum(1.0, numeric.abs())
    return numeric.notna() & numeric.abs().le(tolerance)


def components_to_wind(components: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Reconstruye dirección ``[0, 360)`` y velocidad desde componentes válidas."""
    u = components["WIND_U"]
    v = components["WIND_V"]
    valid = u.notna() & v.notna()
    speed = pd.Series(np.nan, index=components.index, dtype=float)
    speed.loc[valid] = np.hypot(u.loc[valid], v.loc[valid])
    numerical_calm = valid & _numerical_calm(speed)
    speed.loc[numerical_calm] = 0.0
    direction = pd.Series(np.nan, index=components.index, dtype=float)
    moving = valid & ~numerical_calm
    direction.loc[moving] = (
        np.degrees(np.arctan2(-u.loc[moving], -v.loc[moving])) + 360.0
    ) % 360.0
    return direction, speed


def _valid_wind_candidate(components: pd.DataFrame) -> pd.Series:
    """Indica qué vectores son finitos y respetan el límite de velocidad."""
    finite = np.isfinite(components[WIND_COMPONENT_COLUMNS]).all(axis=1)
    _, speed = components_to_wind(components)
    return finite & speed.between(0.0, MAX_WIND_SPEED_MS)


def _wind_gap_series(components: pd.DataFrame) -> pd.Series:
    """Representa disponibilidad vectorial para reutilizar ``find_gaps``."""
    available = components[WIND_COMPONENT_COLUMNS].notna().all(axis=1)
    return pd.Series(np.where(available, 1.0, np.nan), index=components.index)


def _merge_wind_candidate(
    current: pd.DataFrame,
    candidate: pd.DataFrame,
    gap: pd.Series,
    original: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DatetimeIndex, pd.DatetimeIndex]:
    """Copia vectores plausibles compatibles con los componentes observados."""
    result = current.copy()
    gap_index = result.loc[gap["start"] : gap["end"]].index
    aligned = candidate.reindex(gap_index)
    missing = result.loc[gap_index, WIND_COMPONENT_COLUMNS].isna().any(axis=1)
    candidate_direction, candidate_speed = components_to_wind(aligned)
    observed_direction_only = (
        original[WIND_DIRECTION].reindex(gap_index).notna()
        & original[WIND_SPEED].reindex(gap_index).isna()
    )
    observed_speed_only = (
        original[WIND_SPEED].reindex(gap_index).gt(0.0)
        & original[WIND_DIRECTION].reindex(gap_index).isna()
    )
    conflicts = missing & _valid_wind_candidate(aligned) & _numerical_calm(
        candidate_speed
    ) & (observed_direction_only | observed_speed_only)
    can_fill = missing & _valid_wind_candidate(aligned) & ~conflicts
    fill_index = gap_index[can_fill]
    result.loc[fill_index, WIND_COMPONENT_COLUMNS] = aligned.loc[
        fill_index, WIND_COMPONENT_COLUMNS
    ].to_numpy()
    return result, fill_index, gap_index[conflicts]


def _wind_short_candidate(
    observed: pd.DataFrame, gap: pd.Series
) -> pd.DataFrame:
    """Interpola linealmente cada componente usando solo anclas observadas."""
    window = _short_gap_window(observed, gap)
    if window is None:
        return pd.DataFrame(
            np.nan,
            index=pd.date_range(gap["start"], gap["end"], freq="1h"),
            columns=observed.columns,
        )
    candidate = window.interpolate(method="time", limit_area="inside")
    return candidate.loc[gap["start"] : gap["end"]]


def _wind_weekly_candidate(
    observed: pd.DataFrame, gap: pd.Series, n_weeks: int, min_donors: int
) -> tuple[pd.DataFrame | None, int]:
    """Promedia vectores semanales completos con mínimo de donantes por hora."""
    gap_index = pd.date_range(gap["start"], gap["end"], freq="1h")
    sums = pd.DataFrame(0.0, index=gap_index, columns=WIND_COMPONENT_COLUMNS)
    counts = pd.Series(0, index=gap_index, dtype=int)
    for week in [*range(-n_weeks, 0), *range(1, n_weeks + 1)]:
        donor = observed.reindex(gap_index + pd.Timedelta(weeks=week)).copy()
        donor.index = gap_index
        valid = donor[WIND_COMPONENT_COLUMNS].notna().all(axis=1)
        sums.loc[valid] += donor.loc[valid, WIND_COMPONENT_COLUMNS]
        counts.loc[valid] += 1
    valid_hours = counts >= min_donors
    if not valid_hours.any():
        return None, 0
    candidate = sums.div(counts.replace(0, np.nan), axis=0)
    candidate.loc[~valid_hours, WIND_COMPONENT_COLUMNS] = np.nan
    return candidate, int(counts.loc[valid_hours].min())


def _wind_crossyear_candidate(
    observed: pd.DataFrame, gap: pd.Series
) -> pd.DataFrame | None:
    """Usa el vector del año siguiente y, donde falta, el del anterior."""
    gap_index = pd.date_range(gap["start"], gap["end"], freq="1h")
    candidate = pd.DataFrame(np.nan, index=gap_index, columns=WIND_COMPONENT_COLUMNS)
    for years in [1, -1]:
        donor = observed.reindex(gap_index + pd.DateOffset(years=years)).copy()
        donor.index = gap_index
        donor_valid = donor[WIND_COMPONENT_COLUMNS].notna().all(axis=1)
        missing = candidate[WIND_COMPONENT_COLUMNS].isna().any(axis=1)
        fill = missing & donor_valid
        candidate.loc[fill, WIND_COMPONENT_COLUMNS] = donor.loc[
            fill, WIND_COMPONENT_COLUMNS
        ].to_numpy()
    if not candidate[WIND_COMPONENT_COLUMNS].notna().all(axis=1).any():
        return None
    return candidate


def _wind_audit_step(
    method: str,
    accepted: bool,
    rejection_reason: str | None = None,
    **details,
) -> dict:
    """Construye una contribución vectorial con el esquema de auditoría común."""
    return {
        "method_used": method,
        "accepted": accepted,
        "rejection_reason": rejection_reason,
        "n_values_imputed": 0,
        "imputed_timestamps": None,
        "interstation_r2": details.get("interstation_r2"),
        "source_station": details.get("source_station"),
        "distance_km": details.get("distance_km"),
        "predictor_coverage": details.get("predictor_coverage"),
        "training_pairs": details.get("training_pairs"),
        "model_intercept": details.get("model_intercept"),
        "model_coefficient": details.get("model_coefficient"),
        "acceptance_threshold": details.get("acceptance_threshold"),
        "validation_scheme": details.get("validation_scheme"),
        "n_donors": details.get("n_donors"),
        "rejected_timestamps": details.get("rejected_timestamps"),
    }


def _build_wind_interstation_model(
    component_dfs: dict[str, pd.DataFrame],
    target_station: str,
    predictor_station: str,
    gap: pd.Series,
    cv_splits: int,
) -> tuple[LinearRegression | None, float | None, int]:
    """Ajusta una OLS multisalida ``(u, v) -> (u, v)`` con validación temporal."""
    target = component_dfs[target_station].add_prefix("target_")
    predictor = component_dfs[predictor_station].add_prefix("predictor_")
    paired = target.join(predictor).dropna().sort_index()
    outside_gap = ~paired.index.to_series().between(gap["start"], gap["end"])
    paired = paired.loc[outside_gap]
    if len(paired) < 48:
        return None, None, len(paired)
    splits = min(cv_splits, len(paired) - 1)
    if splits < 2:
        return None, None, len(paired)
    predictor_columns = ["predictor_WIND_U", "predictor_WIND_V"]
    target_columns = ["target_WIND_U", "target_WIND_V"]
    model = LinearRegression()
    scores = cross_val_score(
        model,
        paired[predictor_columns],
        paired[target_columns],
        cv=TimeSeriesSplit(n_splits=splits),
        scoring="r2",
    )
    finite_scores = scores[np.isfinite(scores)]
    if len(finite_scores) == 0:
        return None, None, len(paired)
    r2_cv = float(np.mean(finite_scores))
    model.fit(paired[predictor_columns], paired[target_columns])
    return model, r2_cv, len(paired)


def _wind_interstation_candidates(
    current: pd.DataFrame,
    gap: pd.Series,
    component_dfs: dict[str, pd.DataFrame],
    station_dfs: dict[str, pd.DataFrame],
    target_station: str,
    config: ImputeConfig,
    original: pd.DataFrame,
) -> tuple[pd.DataFrame, list[tuple[dict, pd.DatetimeIndex]]]:
    """Completa vectores pendientes con modelos multisalida de estaciones cercanas."""
    result = current.copy()
    steps: list[tuple[dict, pd.DatetimeIndex]] = []
    candidates = []
    for station, frame in station_dfs.items():
        if station == target_station or station not in component_dfs:
            continue
        distance = station_distance_km(station_dfs[target_station], frame)
        if distance is not None:
            candidates.append((distance, station))

    for distance, predictor_station in sorted(candidates):
        gap_slice = result.loc[gap["start"] : gap["end"], WIND_COMPONENT_COLUMNS]
        remaining_index = gap_slice.index[gap_slice.isna().any(axis=1)]
        if remaining_index.empty:
            break
        predictor_gap = component_dfs[predictor_station].reindex(remaining_index)
        predictor_valid = predictor_gap[WIND_COMPONENT_COLUMNS].notna().all(axis=1)
        coverage = float(predictor_valid.mean())
        common = {
            "source_station": predictor_station,
            "distance_km": distance,
            "predictor_coverage": coverage,
            "acceptance_threshold": config.interstation_min_r2,
            "validation_scheme": f"TimeSeriesSplit({config.interstation_cv_splits}) multisalida",
        }
        if coverage < config.predictor_min_gap_coverage:
            steps.append(
                (
                    _wind_audit_step(
                        "interstation_vector_regression",
                        False,
                        "insufficient_gap_coverage",
                        **common,
                    ),
                    pd.DatetimeIndex([]),
                )
            )
            continue

        model, r2_cv, training_pairs = _build_wind_interstation_model(
            component_dfs,
            target_station,
            predictor_station,
            gap,
            config.interstation_cv_splits,
        )
        model_details = {
            **common,
            "interstation_r2": r2_cv,
            "training_pairs": training_pairs,
        }
        if model is None:
            steps.append(
                (
                    _wind_audit_step(
                        "interstation_vector_regression",
                        False,
                        "insufficient_training_pairs_or_cv",
                        **model_details,
                    ),
                    pd.DatetimeIndex([]),
                )
            )
            continue
        model_details.update(
            model_intercept=json.dumps(model.intercept_.tolist()),
            model_coefficient=json.dumps(model.coef_.tolist()),
        )
        if r2_cv is None or r2_cv < config.interstation_min_r2:
            steps.append(
                (
                    _wind_audit_step(
                        "interstation_vector_regression",
                        False,
                        "r2_below_threshold",
                        **model_details,
                    ),
                    pd.DatetimeIndex([]),
                )
            )
            continue

        prediction_index = remaining_index[predictor_valid]
        predictor_values = predictor_gap.loc[
            prediction_index, WIND_COMPONENT_COLUMNS
        ].rename(columns=lambda column: f"predictor_{column}")
        predictions = model.predict(predictor_values)
        candidate = pd.DataFrame(
            predictions,
            index=prediction_index,
            columns=WIND_COMPONENT_COLUMNS,
        )
        result, imputed_index, conflict_index = _merge_wind_candidate(
            result, candidate, gap, original
        )
        if not conflict_index.empty:
            steps.append(
                (
                    _wind_audit_step(
                        "interstation_vector_regression",
                        False,
                        "calm_candidate_conflicts_with_observed_component",
                        rejected_timestamps="|".join(
                            timestamp.isoformat() for timestamp in conflict_index
                        ),
                        **model_details,
                    ),
                    conflict_index,
                )
            )
        if imputed_index.empty:
            if not conflict_index.empty:
                continue
            steps.append(
                (
                    _wind_audit_step(
                        "interstation_vector_regression",
                        False,
                        "predictions_out_of_range",
                        **model_details,
                    ),
                    imputed_index,
                )
            )
            continue
        steps.append(
            (
                _wind_audit_step(
                    "interstation_vector_regression", True, **model_details
                ),
                imputed_index,
            )
        )
    return result, steps


def _wind_output_fields(
    original: pd.DataFrame, components: pd.DataFrame
) -> tuple[pd.Series, pd.Series]:
    """Combina candidatos vectoriales con campos observados sin reemplazarlos."""
    candidate_direction, candidate_speed = components_to_wind(components)
    direction = original[WIND_DIRECTION].copy()
    speed = original[WIND_SPEED].copy()
    candidate_calm_conflict = (
        direction.notna() & speed.isna() & _numerical_calm(candidate_speed)
    )
    speed = speed.fillna(candidate_speed.mask(candidate_calm_conflict))
    fill_direction = (
        direction.isna() & candidate_direction.notna() & ~_numerical_calm(candidate_speed)
    )
    direction.loc[fill_direction] = candidate_direction.loc[fill_direction]
    return direction, speed


def _record_wind_step(
    rows: list[dict],
    step: dict,
    fill_index: pd.DatetimeIndex,
    gap: pd.Series,
    station: str,
    original: pd.DataFrame,
    current: pd.DataFrame,
) -> None:
    """Registra por campo qué valores produjo una contribución vectorial."""
    direction, speed = _wind_output_fields(original, current)
    values = {WIND_DIRECTION: direction, WIND_SPEED: speed}
    if step["accepted"]:
        wrote_any = False
        for variable, series in values.items():
            actual = fill_index[
                original[variable].reindex(fill_index).isna()
                & series.reindex(fill_index).notna()
            ]
            if actual.empty:
                continue
            wrote_any = True
            row = step.copy()
            row.update(
                station=station,
                variable=variable,
                gap_start=gap["start"],
                gap_end=gap["end"],
                n_steps=gap["n_steps"],
                duration_h=gap["duration_h"],
                n_values_imputed=int(len(actual)),
                imputed_timestamps="|".join(ts.isoformat() for ts in actual),
                n_values_remaining=int(
                    series.loc[gap["start"] : gap["end"]].isna().sum()
                ),
            )
            rows.append(row)
        if wrote_any:
            return

    remaining = int(
        current.loc[gap["start"] : gap["end"], WIND_COMPONENT_COLUMNS]
        .isna()
        .any(axis=1)
        .sum()
    )
    row = step.copy()
    if row.get("accepted"):
        row["accepted"] = False
        row["rejection_reason"] = "no_output_field_written"
        row["n_values_imputed"] = 0
        row["imputed_timestamps"] = None
    row.update(
        station=station,
        variable="VIENTO_VECTOR",
        gap_start=gap["start"],
        gap_end=gap["end"],
        n_steps=gap["n_steps"],
        duration_h=gap["duration_h"],
        n_values_remaining=remaining,
    )
    rows.append(row)


def impute_station_wind(
    original: pd.DataFrame,
    station: str,
    config: ImputeConfig,
    component_dfs: dict[str, pd.DataFrame],
    station_dfs: dict[str, pd.DataFrame],
    eligible: pd.Series | None = None,
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    """Imputa viento como vector y retorna dirección, velocidad y auditoría."""
    observed = component_dfs[station]
    current = observed.copy()
    gaps = find_gaps(_wind_gap_series(observed), eligible)
    rows: list[dict] = []

    for _, gap in gaps.iterrows():
        needs_long_cascade = gap["duration_h"] > config.short_gap_hours
        if not needs_long_cascade:
            candidate = _wind_short_candidate(observed, gap)
            current, fill_index, conflict_index = _merge_wind_candidate(
                current, candidate, gap, original
            )
            step = _wind_audit_step(
                "short_vector_linear",
                not fill_index.empty,
                None
                if not fill_index.empty
                else (
                    "calm_candidate_conflicts_with_observed_component"
                    if not conflict_index.empty
                    else "missing_valid_anchors"
                ),
                rejected_timestamps="|".join(
                    timestamp.isoformat() for timestamp in conflict_index
                )
                or None,
            )
            _record_wind_step(rows, step, fill_index, gap, station, original, current)
            if not fill_index.empty and not conflict_index.empty:
                conflict_step = _wind_audit_step(
                    "short_vector_linear",
                    False,
                    "calm_candidate_conflicts_with_observed_component",
                    rejected_timestamps="|".join(
                        timestamp.isoformat() for timestamp in conflict_index
                    ),
                )
                _record_wind_step(
                    rows,
                    conflict_step,
                    conflict_index,
                    gap,
                    station,
                    original,
                    current,
                )
            needs_long_cascade = (
                current.loc[gap["start"] : gap["end"], WIND_COMPONENT_COLUMNS]
                .isna()
                .any(axis=1)
                .any()
            )

        if needs_long_cascade:
            candidate, n_donors = _wind_weekly_candidate(
                observed, gap, config.weekly_n_weeks, config.min_weekly_donors
            )
            if candidate is not None:
                current, fill_index, conflict_index = _merge_wind_candidate(
                    current, candidate, gap, original
                )
                if not fill_index.empty:
                    step = _wind_audit_step(
                        "weekly_vector_seasonal", True, n_donors=n_donors
                    )
                    _record_wind_step(
                        rows, step, fill_index, gap, station, original, current
                    )
                if not conflict_index.empty:
                    step = _wind_audit_step(
                        "weekly_vector_seasonal",
                        False,
                        "calm_candidate_conflicts_with_observed_component",
                        n_donors=n_donors,
                        rejected_timestamps="|".join(
                            timestamp.isoformat() for timestamp in conflict_index
                        ),
                    )
                    _record_wind_step(
                        rows, step, conflict_index, gap, station, original, current
                    )

            candidate = _wind_crossyear_candidate(observed, gap)
            if candidate is not None:
                current, fill_index, conflict_index = _merge_wind_candidate(
                    current, candidate, gap, original
                )
                if not fill_index.empty:
                    step = _wind_audit_step(
                        "cross_year_vector_analog", True, n_donors=1
                    )
                    _record_wind_step(
                        rows, step, fill_index, gap, station, original, current
                    )
                if not conflict_index.empty:
                    step = _wind_audit_step(
                        "cross_year_vector_analog",
                        False,
                        "calm_candidate_conflicts_with_observed_component",
                        n_donors=1,
                        rejected_timestamps="|".join(
                            timestamp.isoformat() for timestamp in conflict_index
                        ),
                    )
                    _record_wind_step(
                        rows, step, conflict_index, gap, station, original, current
                    )

            current, regression_steps = _wind_interstation_candidates(
                current,
                gap,
                component_dfs,
                station_dfs,
                station,
                config,
                original,
            )
            for step, fill_index in regression_steps:
                _record_wind_step(
                    rows, step, fill_index, gap, station, original, current
                )

        remaining = int(
            current.loc[gap["start"] : gap["end"], WIND_COMPONENT_COLUMNS]
            .isna()
            .any(axis=1)
            .sum()
        )
        if remaining:
            _record_wind_step(
                rows,
                _wind_audit_step("unimputable", False, "no_vector_method_available"),
                pd.DatetimeIndex([]),
                gap,
                station,
                original,
                current,
            )

    direction, speed = _wind_output_fields(original, current)
    return direction, speed, pd.DataFrame(rows)


def impute_file(
    station_dfs: dict[str, pd.DataFrame],
    variables: list[str],
    config: ImputeConfig,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Aplica la misma política a cada par ``(estación, variable)``.

    ``df_imp`` es una copia de salida. ``station_dfs`` permanece como snapshot
    observado y se comparte con los modelos entre estaciones; así ninguna
    estación aprende a partir de valores ya imputados en otra.
    """
    imputed: dict[str, pd.DataFrame] = {}
    all_logs: list[pd.DataFrame] = []
    wind_enabled = {WIND_DIRECTION, WIND_SPEED}.issubset(variables)
    component_dfs = {
        station: wind_to_components(df[WIND_DIRECTION], df[WIND_SPEED])
        for station, df in station_dfs.items()
        if wind_enabled and {WIND_DIRECTION, WIND_SPEED}.issubset(df.columns)
    }

    for station, df in station_dfs.items():
        df_imp = df.copy()
        for var in variables:
            if var in {WIND_DIRECTION, WIND_SPEED}:
                continue
            if var not in df.columns:
                continue
            log.info("Imputando  %s  /  %s …", station, var)
            s_imp, log_df = impute_station_variable(
                df[var],
                var,
                station,
                config,
                station_dfs,
                df.get(eligibility_column(var)),
            )
            df_imp[var] = s_imp
            if not log_df.empty:
                all_logs.append(log_df)
        if station in component_dfs:
            log.info("Imputando  %s  /  VIENTO_VECTOR …", station)
            direction_eligible = df.get(
                eligibility_column(WIND_DIRECTION), pd.Series(True, index=df.index)
            )
            speed_eligible = df.get(
                eligibility_column(WIND_SPEED), pd.Series(True, index=df.index)
            )
            direction, speed, wind_log = impute_station_wind(
                df[[WIND_DIRECTION, WIND_SPEED]],
                station,
                config,
                component_dfs,
                station_dfs,
                direction_eligible & speed_eligible,
            )
            df_imp[WIND_DIRECTION] = direction
            df_imp[WIND_SPEED] = speed
            if not wind_log.empty:
                all_logs.append(wind_log)
        imputed[station] = df_imp

    global_log = (
        pd.concat(all_logs, ignore_index=True)
        if all_logs
        else pd.DataFrame(columns=IMPUTATION_LOG_COLUMNS)
    )
    global_log = global_log.reindex(columns=IMPUTATION_LOG_COLUMNS)
    return imputed, global_log


# ──────────────────────────────────────────────────────────────────────────────
# Reconstrucción del DataFrame de salida
# ──────────────────────────────────────────────────────────────────────────────


def reconstruct_output_df(station_dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Convierte el dict de DataFrames con DatetimeIndex de vuelta al formato
    original: FECHA (int YYYYMMDD), HORA (int H*10000), ESTACION, …
    """
    frames = []
    for station, df in station_dfs.items():
        df_out = df.copy()
        internal_columns = [
            column for column in df_out.columns if column.startswith(INTERNAL_COLUMN_PREFIX)
        ]
        df_out = df_out.drop(columns=internal_columns)
        df_out["FECHA"] = df_out.index.year * 10000 + df_out.index.month * 100 + df_out.index.day
        df_out["HORA"] = df_out.index.hour * 10000
        df_out["ESTACION"] = station
        frames.append(df_out)

    combined = pd.concat(frames)
    # Ordenar igual que el original
    combined = combined.sort_values(["ESTACION", "FECHA", "HORA"]).reset_index(drop=True)
    return combined


# ──────────────────────────────────────────────────────────────────────────────
# Gráficos
# ──────────────────────────────────────────────────────────────────────────────


def _ensure_plots_dir(output_dir: Path) -> Path:
    plots_dir = output_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    return plots_dir


def plot_coverage_heatmap(
    before: dict[str, pd.DataFrame],
    after: dict[str, pd.DataFrame],
    variable: str,
    plots_dir: Path,
    dpi: int,
) -> None:
    """
    Heatmap de cobertura diaria: eje Y = estaciones, eje X = días.
    Verde = dato presente, rojo = faltante. Panel antes/después.
    """
    stations = sorted(before.keys())
    all_dates = pd.date_range(
        min(df.index.min() for df in before.values()),
        max(df.index.max() for df in before.values()),
        freq="D",
    )

    def coverage_matrix(station_dfs):
        mat = np.zeros((len(stations), len(all_dates)))
        for i, st in enumerate(stations):
            if variable not in station_dfs[st].columns:
                mat[i, :] = np.nan
                continue
            daily = station_dfs[st][variable].resample("D").apply(lambda x: x.notna().mean())
            for j, d in enumerate(all_dates):
                mat[i, j] = daily.get(d, np.nan)
        return mat

    mat_before = coverage_matrix(before)
    mat_after = coverage_matrix(after)

    fig, axes = plt.subplots(2, 1, figsize=(16, 6), sharex=True)
    cmap = plt.cm.RdYlGn

    for ax, mat, title in zip(
        axes, [mat_before, mat_after], ["Antes de imputación", "Después de imputación"]
    ):
        im = ax.imshow(mat, aspect="auto", cmap=cmap, vmin=0, vmax=1, interpolation="none")
        ax.set_yticks(range(len(stations)))
        ax.set_yticklabels(stations, fontsize=8)
        ax.set_title(f"{variable} — {title}", fontsize=10)

        # Eje X: etiquetas de mes
        tick_pos = np.linspace(0, len(all_dates) - 1, 12, dtype=int)
        ax.set_xticks(tick_pos)
        ax.set_xticklabels(
            [all_dates[p].strftime("%b %Y") for p in tick_pos],
            fontsize=7,
            rotation=45,
            ha="right",
        )

    fig.suptitle(f"Cobertura horaria diaria — {variable}", fontsize=11)
    # Reservar espacio fijo a la derecha para la colorbar antes de dibujarla
    fig.subplots_adjust(right=0.87, top=0.92, hspace=0.35)
    cbar_ax = fig.add_axes((0.89, 0.15, 0.015, 0.70))  # (left, bottom, w, h)
    fig.colorbar(im, cax=cbar_ax, label="Cobertura diaria (fracción)")
    out = plots_dir / f"heatmap_cobertura_{variable}.png"
    plt.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close()
    log.info("Gráfico guardado: %s", out)


def plot_timeseries_with_imputed(
    before: dict[str, pd.DataFrame],
    after: dict[str, pd.DataFrame],
    log_df: pd.DataFrame,
    variables: list[str],
    plots_dir: Path,
    dpi: int,
) -> None:
    """
    Por cada (estación, variable): serie temporal con puntos imputados
    coloreados por método.
    """
    if log_df.empty:
        return

    method_colors = {
        "short_linear": "#FF8C00",
        "short_zero": "#FF8C00",
        "weekly_seasonal": "#D62728",
        "cross_year_analog": "#9467BD",
        "interstation_regression": "#8C564B",
        "short_vector_linear": "#17BECF",
        "weekly_vector_seasonal": "#E377C2",
        "cross_year_vector_analog": "#BCBD22",
        "interstation_vector_regression": "#7F3C8D",
        "unimputable": "#7F7F7F",
    }

    for station in sorted(before.keys()):
        for var in variables:
            if var not in before[station].columns:
                continue

            s_before = before[station][var]
            s_after = after[station][var]
            gaps_log = log_df[(log_df["station"] == station) & (log_df["variable"] == var)]

            if gaps_log.empty and s_before.isna().sum() == 0:
                continue  # no hay nada que mostrar

            fig, (ax_bef, ax_aft) = plt.subplots(2, 1, figsize=(16, 7), sharex=True)

            # Panel superior — antes (gaps visibles como interrupciones)
            ax_bef.plot(
                s_before.index,
                s_before.values,
                color="#1F77B4",
                lw=0.8,
                label="Original",
            )
            ax_bef.set_ylabel(var, fontsize=9)
            ax_bef.set_title(f"{station} — {var} — Antes (con gaps)", fontsize=10)
            ax_bef.legend(fontsize=7, loc="upper right")

            # Panel inferior — después (serie completa + puntos coloreados por método)
            ax_aft.plot(
                s_after.index,
                s_after.values,
                color="#AAAAAA",
                lw=0.8,
                label="Serie imputada",
                zorder=2,
            )
            for _, row in gaps_log.iterrows():
                if row["method_used"] == "unimputable":
                    continue
                serialized = row.get("imputed_timestamps")
                if not isinstance(serialized, str) or not serialized:
                    continue
                # Un gap puede combinar métodos. El log conserva los timestamps
                # exactos para no colorear todo el intervalo como si una sola
                # estrategia hubiera producido cada punto.
                idx = pd.DatetimeIndex(pd.to_datetime(serialized.split("|")))
                vals = s_after.loc[idx]
                color = method_colors.get(row["method_used"], "#333333")
                ax_aft.scatter(
                    vals.index,
                    vals.values,
                    color=color,
                    s=6,
                    zorder=3,
                    label=row["method_used"],
                )

            ax_aft.set_ylabel(var, fontsize=9)
            ax_aft.set_title(f"{station} — {var} — Después (imputado)", fontsize=10)
            ax_aft.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
            ax_aft.xaxis.set_major_locator(mdates.MonthLocator())
            plt.xticks(rotation=45, ha="right", fontsize=7)

            # Leyenda sin duplicados en panel inferior
            handles, labels = ax_aft.get_legend_handles_labels()
            seen = {}
            for handle, label in zip(handles, labels):
                if label not in seen:
                    seen[label] = handle
            ax_aft.legend(seen.values(), seen.keys(), fontsize=7, loc="upper right", markerscale=2)

            plt.tight_layout()
            safe_var = var.replace(".", "_")
            out = plots_dir / f"serie_{station}_{safe_var}.png"
            plt.savefig(out, dpi=dpi, bbox_inches="tight")
            plt.close()

    log.info("Series temporales guardadas en %s", plots_dir)


def plot_gap_histogram(
    before: dict[str, pd.DataFrame],
    variables: list[str],
    short_gap_hours: float,
    plots_dir: Path,
    dpi: int,
) -> None:
    """
    Histograma de tamaños de gap por variable (escala log en X).
    Línea vertical roja en el umbral de 4 h.
    """
    fig, axes = plt.subplots(1, len(variables), figsize=(5 * len(variables), 4), sharey=False)
    if len(variables) == 1:
        axes = [axes]

    for ax, var in zip(axes, variables):
        all_durations = []
        for df in before.values():
            if var not in df.columns:
                continue
            gaps = find_gaps(df[var], df.get(eligibility_column(var)))
            if not gaps.empty:
                all_durations.extend(gaps["duration_h"].tolist())

        if not all_durations:
            ax.set_title(f"{var}\n(sin gaps)")
            continue

        bins = np.logspace(0, np.log10(max(all_durations) + 1), 30)
        ax.hist(all_durations, bins=bins, color="#4C72B0", edgecolor="white", lw=0.4)
        ax.axvline(
            short_gap_hours,
            color="red",
            lw=1.5,
            ls="--",
            label=f"Umbral {short_gap_hours:.0f} h",
        )
        ax.set_xscale("log")
        ax.set_xlabel("Duración del gap (horas, log)", fontsize=9)
        ax.set_ylabel("Frecuencia", fontsize=9)
        ax.set_title(f"Distribución de gaps — {var}", fontsize=10)
        ax.legend(fontsize=8)

    plt.tight_layout()
    out = plots_dir / "histograma_gaps.png"
    plt.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close()
    log.info("Gráfico guardado: %s", out)


def plot_methods_barplot(log_df: pd.DataFrame, plots_dir: Path, dpi: int) -> None:
    """
    Barplot: valores imputados por método y variable.
    """
    if log_df.empty:
        return

    summary = log_df.groupby(["variable", "method_used"])["n_values_imputed"].sum().reset_index()
    summary = summary[summary["n_values_imputed"] > 0]
    if summary.empty:
        return

    variables = summary["variable"].unique()
    n_columns = min(3, len(variables))
    n_rows = (len(variables) + n_columns - 1) // n_columns
    fig, axes = plt.subplots(
        n_rows,
        n_columns,
        figsize=(5 * n_columns, 4 * n_rows),
        squeeze=False,
    )
    flat_axes = axes.ravel()

    palette = sns.color_palette("tab10", n_colors=8)
    for ax, var in zip(flat_axes, variables):
        sub = summary[summary["variable"] == var].sort_values("n_values_imputed", ascending=False)
        bars = ax.bar(sub["method_used"], sub["n_values_imputed"], color=palette[: len(sub)])
        ax.set_title(f"Métodos de imputación — {var}", fontsize=10)
        ax.set_ylabel("Valores imputados", fontsize=9)
        ax.set_xlabel("")
        ax.tick_params(axis="x", labelrotation=30, labelsize=8)
        for bar, val in zip(bars, sub["n_values_imputed"]):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.5,
                str(int(val)),
                ha="center",
                va="bottom",
                fontsize=7,
            )

    for ax in flat_axes[len(variables) :]:
        ax.set_visible(False)

    plt.tight_layout()
    out = plots_dir / "metodos_imputacion.png"
    plt.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close()
    log.info("Gráfico guardado: %s", out)


def plot_interstation_r2_histogram(
    log_df: pd.DataFrame, plots_dir: Path, dpi: int, min_r2: float
) -> None:
    """Muestra la distribución de R² de regresiones realmente aceptadas.

    No se presenta un scatter predicho-versus-real para el gap porque, por
    definición, allí no existe una observación de referencia. Un gráfico así
    sugeriría una validación imposible. La evaluación fuera de muestra debe
    obtenerse mediante backtesting con gaps artificiales, descrito en la
    documentación.
    """
    if log_df.empty:
        return
    regression_rows = log_df[
        log_df["method_used"].isin(
            ["interstation_regression", "interstation_vector_regression"]
        )
        & log_df["accepted"]
    ]
    if regression_rows.empty:
        return

    r2_vals = regression_rows["interstation_r2"].dropna()
    if r2_vals.empty:
        return

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.hist(r2_vals, bins=10, color="#2CA02C", edgecolor="white")
    ax.axvline(
        min_r2,
        color="red",
        ls="--",
        lw=1.5,
        label=f"Umbral R²={min_r2:.2f}",
    )
    ax.set_xlabel("R² de validación temporal", fontsize=9)
    ax.set_ylabel("N° gaps", fontsize=9)
    ax.set_title("Regresión lineal entre estaciones", fontsize=10)
    ax.legend(fontsize=8)
    plt.tight_layout()
    out = plots_dir / "histograma_r2_entre_estaciones.png"
    plt.savefig(out, dpi=dpi, bbox_inches="tight")
    plt.close()
    log.info("Gráfico guardado: %s", out)


def circular_mean_degrees(series: pd.Series) -> float:
    """Calcula una media angular en grados y deja indefinido un vector nulo."""
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return float("nan")
    radians = np.radians(values % 360.0)
    mean_sine = float(np.sin(radians).mean())
    mean_cosine = float(np.cos(radians).mean())
    if np.hypot(mean_sine, mean_cosine) <= 1e-12:
        return float("nan")
    angle = float(np.degrees(np.arctan2(mean_sine, mean_cosine)) % 360.0)
    return 0.0 if np.isclose(angle, 360.0) else angle


def plot_diurnal_profile(
    before: dict[str, pd.DataFrame],
    after: dict[str, pd.DataFrame],
    variables: list[str],
    plots_dir: Path,
    dpi: int,
) -> None:
    """
    Perfil diurno medio (hora 0–23) antes y después de imputación.
    Original (azul sólido) vs Imputado (rojo sólido).
    Barras grises de fondo: cobertura de datos hora a hora en la serie
    original — muestra cuántas observaciones válidas hay por hora relativo
    al total de días, dejando visible dónde había gaps crónicos.
    """
    for var in variables:
        stations = [s for s in before if var in before[s].columns]
        n = len(stations)
        if n == 0:
            continue

        cols = min(3, n)
        rows = (n + cols - 1) // cols
        fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4 * rows), squeeze=False)

        for idx, station in enumerate(sorted(stations)):
            ax = axes[idx // cols][idx % cols]
            s_bef = before[station][var]
            s_aft = after[station][var]

            hours = np.arange(24)
            if var == WIND_DIRECTION:
                profile_bef = (
                    s_bef.groupby(s_bef.index.hour)
                    .apply(circular_mean_degrees)
                    .reindex(hours)
                )
                profile_aft = (
                    s_aft.groupby(s_aft.index.hour)
                    .apply(circular_mean_degrees)
                    .reindex(hours)
                )
            else:
                profile_bef = s_bef.groupby(s_bef.index.hour).mean().reindex(hours)
                profile_aft = s_aft.groupby(s_aft.index.hour).mean().reindex(hours)

            # Cobertura horaria: fracción de días con dato válido en cada hora
            coverage = (
                s_bef.groupby(s_bef.index.hour).apply(lambda x: x.notna().mean()).reindex(hours)
            )

            # Eje secundario: barras de cobertura en gris claro (fondo)
            ax2 = ax.twinx()
            ax2.bar(
                hours,
                coverage.values * 100,
                color="#CCCCCC",
                alpha=0.5,
                width=0.8,
                zorder=1,
                label="Cobertura original (%)",
            )
            ax2.set_ylim(0, 200)  # el 100% ocupa la mitad inferior del gráfico
            ax2.set_yticks([0, 25, 50, 75, 100])
            ax2.set_yticklabels(["0", "25", "50", "75", "100%"], fontsize=6, color="#888888")
            ax2.tick_params(axis="y", length=2)

            # Líneas de perfil en el eje principal (encima de las barras)
            line_style = "none" if var == WIND_DIRECTION else "-"
            ax.plot(
                hours,
                profile_bef.values,
                color="#1F77B4",
                lw=2.0,
                ls=line_style,
                marker="o",
                markersize=3,
                label="Original",
                zorder=3,
            )
            ax.plot(
                hours,
                profile_aft.values,
                color="#D62728",
                lw=2.0,
                ls=line_style,
                marker="s",
                markersize=3,
                label="Imputado",
                zorder=3,
            )

            # El eje principal debe quedar delante de las barras
            ax.set_zorder(ax2.get_zorder() + 1)
            ax.patch.set_visible(False)

            ax.set_title(station, fontsize=9)
            ax.set_xlabel("Hora del día", fontsize=8)
            ax.set_ylabel(var, fontsize=8)
            ax.set_xticks(range(0, 24, 3))
            ax.set_xlim(-0.5, 23.5)
            if var == WIND_DIRECTION:
                ax.set_ylim(0, 360)
                ax.set_yticks([0, 90, 180, 270, 360])

            # Leyenda combinada
            lines1, labels1 = ax.get_legend_handles_labels()
            from matplotlib.patches import Patch

            cov_patch = Patch(facecolor="#CCCCCC", alpha=0.6, label="Cobertura original (%)")
            ax.legend(
                handles=lines1 + [cov_patch],
                labels=labels1 + ["Cobertura (%)"],
                fontsize=6,
                loc="upper right",
            )

        # Ocultar subplots vacíos
        for idx in range(n, rows * cols):
            axes[idx // cols][idx % cols].set_visible(False)

        safe_var = var.replace(".", "_")
        fig.suptitle(f"Perfil diurno medio — {var}", fontsize=11)
        plt.tight_layout()
        out = plots_dir / f"perfil_diurno_{safe_var}.png"
        plt.savefig(out, dpi=dpi, bbox_inches="tight")
        plt.close()
        log.info("Gráfico guardado: %s", out)


def plot_diagnostics(
    before: dict[str, pd.DataFrame],
    after: dict[str, pd.DataFrame],
    log_df: pd.DataFrame,
    variables: list[str],
    output_dir: Path,
    dpi: int,
    short_gap_hours: float,
    interstation_min_r2: float,
) -> None:
    """Genera diagnósticos con los mismos umbrales usados al imputar."""
    plots_dir = _ensure_plots_dir(output_dir)
    log.info("Generando gráficos en %s …", plots_dir)

    for var in variables:
        plot_coverage_heatmap(before, after, var, plots_dir, dpi)

    plot_timeseries_with_imputed(before, after, log_df, variables, plots_dir, dpi)
    plot_gap_histogram(before, variables, short_gap_hours, plots_dir, dpi)
    plot_methods_barplot(log_df, plots_dir, dpi)
    plot_interstation_r2_histogram(log_df, plots_dir, dpi, interstation_min_r2)
    plot_diurnal_profile(before, after, variables, plots_dir, dpi)


# ──────────────────────────────────────────────────────────────────────────────
# Guardado de resultados
# ──────────────────────────────────────────────────────────────────────────────


def _accepted_imputation_timestamps(log_df: pd.DataFrame) -> dict[tuple[str, str], set]:
    """Valida el contrato del log y devuelve timestamps aceptados por serie."""
    accepted: dict[tuple[str, str], set] = {}
    if log_df.empty:
        return accepted
    missing_columns = set(IMPUTATION_LOG_COLUMNS) - set(log_df.columns)
    if missing_columns:
        raise ValueError(f"El log de imputación carece de columnas: {sorted(missing_columns)}")
    for _, row in log_df.iterrows():
        serialized = row.get("imputed_timestamps")
        timestamps = []
        if isinstance(serialized, str) and serialized:
            timestamps = list(pd.to_datetime(serialized.split("|")))
        expected = int(row.get("n_values_imputed", 0) or 0)
        if len(timestamps) != expected:
            raise ValueError(
                "El log no coincide con n_values_imputed para "
                f"{row.get('station')}.{row.get('variable')}"
            )
        if bool(row.get("accepted")) and expected == 0:
            raise ValueError("Una contribución aceptada no puede escribir cero valores")
        if expected and not bool(row.get("accepted")):
            raise ValueError("Una contribución rechazada no puede declarar valores imputados")
        key = (str(row.get("station")), str(row.get("variable")))
        accepted.setdefault(key, set()).update(timestamps)
    return accepted


def _imputation_details_by_cell(log_df: pd.DataFrame) -> dict[tuple[str, str, pd.Timestamp], dict]:
    """Expande contribuciones aceptadas a una atribución única por celda."""
    details: dict[tuple[str, str, pd.Timestamp], dict] = {}
    if log_df.empty:
        return details
    for _, row in log_df[log_df["accepted"]].iterrows():
        serialized = row.get("imputed_timestamps")
        if not isinstance(serialized, str) or not serialized:
            continue
        for timestamp in pd.to_datetime(serialized.split("|")):
            key = (str(row["station"]), str(row["variable"]), timestamp)
            if key in details:
                raise ValueError(f"Más de una contribución aceptada para {key}")
            details[key] = row.to_dict()
    return details


def _percentage(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def build_output_views(
    preserved: dict[str, pd.DataFrame],
    analytic: dict[str, pd.DataFrame],
    imputed: dict[str, pd.DataFrame],
    log_df: pd.DataFrame,
    variables: list[str],
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Combina vista reportada e imputaciones y crea estados internos por celda."""
    details = _imputation_details_by_cell(log_df)

    output: dict[str, pd.DataFrame] = {}
    state_frames: list[pd.DataFrame] = []
    for station, reported_frame in preserved.items():
        result = reported_frame.copy()
        source_row = (
            reported_frame["FECHA"].notna() & reported_frame["HORA"].notna()
            if {"FECHA", "HORA"}.issubset(reported_frame.columns)
            else pd.Series(True, index=reported_frame.index)
        )
        for variable in variables:
            raw = reported_frame[variable]
            after = imputed[station][variable]
            hard_invalid = _hard_invalid_mask(raw, variable)
            operational = ~hard_invalid & _operational_outlier_mask(raw, variable)
            eligible = analytic[station][eligibility_column(variable)].astype(bool)
            not_applicable = pd.Series(False, index=raw.index)
            if variable == WIND_DIRECTION and WIND_SPEED in reported_frame:
                not_applicable = imputed[station][WIND_SPEED].eq(0.0)

            original = raw.notna() & ~hard_invalid & ~operational & ~not_applicable
            structural = raw.isna() & ~eligible & ~not_applicable
            explicit = raw.isna() & eligible & source_row & ~not_applicable
            implicit = raw.isna() & eligible & ~source_row & ~not_applicable
            replaceable = (hard_invalid | raw.isna()) & ~not_applicable
            final_values = raw.copy()
            final_values.loc[replaceable] = after.loc[replaceable]
            final_values.loc[not_applicable] = np.nan

            origins = np.select(
                [
                    hard_invalid,
                    not_applicable,
                    operational,
                    original,
                    structural,
                    explicit,
                    implicit,
                ],
                [
                    "hard_invalid",
                    "not_applicable",
                    "operational_outlier",
                    "senamhi_observed",
                    "missing_structural",
                    "missing_explicit",
                    "missing_implicit",
                ],
                default="missing_explicit",
            )
            methods = pd.Series(pd.NA, index=raw.index, dtype="string")
            for timestamp in raw.index:
                detail = details.get((station, variable, timestamp))
                if detail is not None:
                    methods.at[timestamp] = str(detail["method_used"])
            imputed_cells = methods.notna()
            if (imputed_cells & ~replaceable).any():
                raise ValueError(f"El log intenta reemplazar una observación en {station}.{variable}")
            if (imputed_cells & final_values.isna()).any():
                raise ValueError(f"El log atribuye una imputación ausente en {station}.{variable}")

            sources = pd.Series(
                np.select(
                    [
                        not_applicable,
                        operational,
                        hard_invalid,
                        original,
                        structural,
                        explicit,
                        implicit,
                    ],
                    [
                        "not_applicable",
                        "senamhi_operational_outlier",
                        "missing_hard_invalid",
                        "senamhi_observed",
                        "missing_structural",
                        "missing_explicit",
                        "missing_implicit",
                    ],
                    default="missing_explicit",
                ),
                index=raw.index,
                dtype="string",
            )
            sources.loc[imputed_cells] = "imputed_" + methods.loc[imputed_cells]
            result[variable] = final_values
            result[f"{variable}_SOURCE"] = sources

            state_frames.append(
                pd.DataFrame(
                    {
                        "station": station,
                        "timestamp": raw.index,
                        "variable": variable,
                        "origin_state": origins,
                        "source": sources.to_numpy(),
                        "output_present": final_values.notna().to_numpy(dtype=bool),
                        "is_applicable": (~not_applicable).to_numpy(dtype=bool),
                    }
                )
            )
        output[station] = result
    return output, pd.concat(state_frames, ignore_index=True)


def build_execution_summary(
    states: pd.DataFrame,
    dataset: str,
    ifs_values: pd.DataFrame | None = None,
) -> str:
    """Genera el único resumen publicable a partir de estados internos."""
    def summarize(cells: pd.DataFrame, label: str) -> list[str]:
        applicable = cells["is_applicable"].astype(bool)
        denominator = int(applicable.sum())
        observed = int(cells["origin_state"].eq("senamhi_observed").sum())
        hard_invalid = int(cells["origin_state"].eq("hard_invalid").sum())
        operational = int(cells["origin_state"].eq("operational_outlier").sum())
        missing_before = int(
            (
                cells["origin_state"].eq("hard_invalid")
                | cells["origin_state"].astype(str).str.startswith("missing_")
            ).sum()
        )
        imputed_cells = cells["source"].astype(str).str.startswith("imputed_")
        imputed_count = int(imputed_cells.sum())
        remaining = int((applicable & ~cells["output_present"].astype(bool)).sum())
        coverage = int((applicable & cells["output_present"].astype(bool)).sum())
        lines = [label, f"  celdas_aplicables: {denominator}"]
        for name, count in [
            ("observaciones_senamhi_validas", observed),
            ("hard_invalid", hard_invalid),
            ("operational_outliers", operational),
            ("missing_antes", missing_before),
            ("imputados_total", imputed_count),
            ("remaining_missing", remaining),
            ("coverage_final", coverage),
        ]:
            lines.append(f"  {name}: {count} ({_percentage(count, denominator):.2f}%)")
        methods = cells.loc[imputed_cells, "source"].value_counts().sort_index()
        if methods.empty:
            lines.append("  imputados_por_metodo: ninguno")
        else:
            lines.append("  imputados_por_metodo:")
            lines.extend(
                f"    {method}: {int(count)} "
                f"({_percentage(int(count), imputed_count):.2f}% de imputados)"
                for method, count in methods.items()
            )
        return lines

    lines = [f"RESUMEN DE EJECUCION: {dataset}", ""]
    for (station, variable), cells in states.groupby(["station", "variable"], sort=True):
        lines.extend(summarize(cells, f"ESTACION={station} VARIABLE={variable}"))
        lines.append("")
    lines.extend(summarize(states, "TOTAL"))
    if ifs_values is not None:
        lines.extend(["", "PRECIPITACION EXTERNA ECMWF IFS (PP_IFS)"])
        for station, rows in ifs_values.groupby("ESTACION", sort=True):
            available = int(rows["PP_IFS"].notna().sum())
            total = len(rows)
            lines.append(
                f"  ESTACION={station}: {available}/{total} "
                f"({_percentage(available, total):.2f}%)"
            )
        available = int(ifs_values["PP_IFS"].notna().sum())
        total = len(ifs_values)
        lines.append(
            f"  TOTAL: {available}/{total} ({_percentage(available, total):.2f}%)"
        )
        lines.append(f"  FALTANTES: {total - available}")
        lines.append("  ORIGEN: ECMWF IFS; no es una observacion SENAMHI")
    return "\n".join(lines) + "\n"


def validate_processed_output(
    preserved: dict[str, pd.DataFrame],
    analytic: dict[str, pd.DataFrame],
    imputed: dict[str, pd.DataFrame],
    output_df: pd.DataFrame,
    log_df: pd.DataFrame,
    states: pd.DataFrame,
    variables: list[str],
) -> None:
    """Impone las postcondiciones del pipeline antes de publicar artefactos."""
    if output_df.duplicated(KEY_COLUMNS).any():
        raise ValueError("La salida contiene claves horarias duplicadas")
    if output_df[KEY_COLUMNS].isna().any().any():
        raise ValueError("La salida contiene claves horarias incompletas")
    if any(column.startswith(INTERNAL_COLUMN_PREFIX) for column in output_df.columns):
        raise ValueError("La salida expone columnas internas del pipeline")
    logged = _accepted_imputation_timestamps(log_df)
    if len(output_df) != sum(len(frame) for frame in imputed.values()):
        raise ValueError("La salida no conserva la grilla horaria de las estaciones")

    for station, before in analytic.items():
        after = imputed[station]
        output_station = output_df[output_df["ESTACION"].eq(station)].copy()
        output_station.index = parse_datetime_index(output_station)
        if not before.index.equals(after.index):
            raise ValueError(f"La imputación alteró el índice de {station}")
        for variable in variables:
            if variable not in before.columns:
                raise ValueError(f"Falta {variable} en la estación {station}")
            values = pd.to_numeric(after[variable], errors="coerce")
            if (values.notna() & ~np.isfinite(values)).any() or _hard_invalid_mask(
                values, variable
            ).any():
                raise ValueError(f"La salida contiene valores inválidos en {station}.{variable}")
            if _operational_outlier_mask(values, variable).any():
                raise ValueError(
                    f"La salida contiene valores fuera del rango operacional en {station}.{variable}"
                )
            original = before[variable].notna()
            if not np.allclose(
                before.loc[original, variable].astype(float),
                after.loc[original, variable].astype(float),
                equal_nan=True,
            ):
                raise ValueError(f"La imputación modificó originales válidos en {station}.{variable}")
            changed = before[variable].isna() & after[variable].notna()
            eligible = before.get(eligibility_column(variable))
            if eligible is not None and (changed & ~eligible.astype(bool)).any():
                raise ValueError(f"Se imputó ausencia estructural en {station}.{variable}")
            changed_timestamps = set(changed.index[changed])
            if changed.any():
                lo, hi = OPERATIONAL_RANGES[variable]
                if not after.loc[changed, variable].between(lo, hi).all():
                    raise ValueError(f"Se generó una predicción sospechosa en {station}.{variable}")
            if changed_timestamps != logged.get((station, variable), set()):
                raise ValueError(f"La salida y el log discrepan para {station}.{variable}")

            raw = preserved[station][variable]
            operational = ~_hard_invalid_mask(raw, variable) & _operational_outlier_mask(
                raw, variable
            )
            if (operational & before[eligibility_column(variable)]).any():
                raise ValueError(f"Un outlier operativo quedó elegible en {station}.{variable}")
            if not np.allclose(
                raw.loc[operational].astype(float),
                output_station.loc[operational, variable].astype(float),
                equal_nan=True,
            ):
                raise ValueError(f"La salida no preservó outliers de {station}.{variable}")

    for variable in variables:
        source_column = f"{variable}_SOURCE"
        if source_column not in output_df:
            raise ValueError(f"Falta procedencia embebida para {variable}")
        sources = output_df[source_column].astype(str)
        invalid_source = ~sources.isin(allowed_sources_for(variable))
        if invalid_source.any():
            raise ValueError(f"SOURCE desconocido para {variable}")

        missing_source = sources.str.startswith("missing_")
        if (missing_source & output_df[variable].notna()).any():
            raise ValueError(f"Una ausencia de {variable} conserva valor numerico")
        present_source = sources.eq("senamhi_observed") | sources.str.startswith(
            "imputed_"
        )
        if (present_source & output_df[variable].isna()).any():
            raise ValueError(f"Una fuente disponible de {variable} carece de valor")
        if variable != WIND_DIRECTION and sources.eq("not_applicable").any():
            raise ValueError(f"not_applicable no corresponde a {variable}")

    if {WIND_DIRECTION, WIND_SPEED}.issubset(variables):
        direction_not_applicable = output_df[
            f"{WIND_DIRECTION}_SOURCE"
        ].eq("not_applicable")
        calm = output_df[WIND_SPEED].eq(0.0)
        if (direction_not_applicable != calm).any():
            raise ValueError("Todo viento en calma requiere direccion not_applicable")
        if output_df.loc[calm, WIND_DIRECTION].notna().any():
            raise ValueError("La direccion debe estar ausente cuando el viento esta en calma")

    operational_states = states[states["origin_state"].eq("operational_outlier")]
    if operational_states["source"].astype(str).str.startswith("imputed_").any():
        raise ValueError("Un outlier operativo fue reemplazado por imputación")

    if "PP" in variables:
        if "PP_IFS" not in output_df:
            raise ValueError("La salida meteorológica no contiene PP_IFS")
        pp_ifs = pd.to_numeric(output_df["PP_IFS"], errors="coerce")
        malformed = output_df["PP_IFS"].notna() & (
            ~np.isfinite(pp_ifs) | ~pp_ifs.between(*OPERATIONAL_RANGES["PP"])
        )
        if malformed.any():
            raise ValueError("PP_IFS contiene valores externos no válidos")

    if {WIND_DIRECTION, WIND_SPEED}.issubset(variables):
        for station, frame in imputed.items():
            direction = frame[WIND_DIRECTION]
            generated = analytic[station][WIND_DIRECTION].isna() & direction.notna()
            if generated.any() and not direction.loc[generated].between(
                0.0, 360.0, inclusive="left"
            ).all():
                raise ValueError(f"Dirección imputada no canónica en {station}")


def save_outputs(
    imputed_df: pd.DataFrame,
    log_df: pd.DataFrame,
    summary_text: str,
    input_filepath: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Publica CSV, log y resumen mediante reemplazo de archivos completos."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = input_filepath.stem
    out_csv = output_dir / f"{stem}_imputed.csv"
    out_log = output_dir / f"{stem}_imputation_log.csv"
    out_summary = output_dir / f"{stem}_summary.txt"
    artifacts = {
        out_csv: imputed_df,
        out_log: log_df.reindex(columns=IMPUTATION_LOG_COLUMNS),
    }
    temporary: dict[Path, Path] = {}
    try:
        for destination, frame in artifacts.items():
            temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            frame.to_csv(temp, index=False)
            temporary[destination] = temp
        summary_temp = out_summary.with_name(f".{out_summary.name}.{os.getpid()}.tmp")
        summary_temp.write_text(summary_text, encoding="utf-8")
        temporary[out_summary] = summary_temp
        for destination, temp in temporary.items():
            os.replace(temp, destination)
    finally:
        for temp in temporary.values():
            temp.unlink(missing_ok=True)
    log.info("CSV imputado: %s", out_csv)
    log.info("Log: %s", out_log)
    log.info("Resumen: %s", out_summary)
    return out_csv, out_log, out_summary


# ──────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ──────────────────────────────────────────────────────────────────────────────


def process_file(
    filepath: Path, variables: list[str], config: ImputeConfig, dataset: str
) -> tuple[Path, Path, Path]:
    """Ejecuta carga, limpieza, imputación, diagnóstico y guardado de un CSV."""
    output_dir = RESULTS_ROOT / dataset

    log.info("=" * 60)
    log.info("Procesando: %s", filepath.name)
    log.info("=" * 60)

    station_dfs = load_and_reindex(filepath, variables)
    preserved = {station: frame.copy() for station, frame in station_dfs.items()}
    prepare_analytic_snapshot(station_dfs, variables)

    # Snapshot antes de imputar (copia profunda para gráficos)
    before = {st: df.copy() for st, df in station_dfs.items()}

    imputed_dfs, log_df = impute_file(station_dfs, variables, config)
    output_dfs, states = build_output_views(
        preserved, before, imputed_dfs, log_df, variables
    )
    out_df = reconstruct_output_df(output_dfs)

    ifs_values = None
    if dataset == "meteo":
        stations = out_df[["ESTACION", "LATITUD", "LONGITUD"]].drop_duplicates(
            "ESTACION"
        )
        timestamps = pd.DatetimeIndex(parse_datetime_index(out_df).unique()).sort_values()
        pp_ifs = acquire_pp_ifs(stations, timestamps)
        out_df = out_df.merge(
            pp_ifs, on=KEY_COLUMNS, how="left", validate="one_to_one"
        )
        if len(pp_ifs) != len(out_df) or "PP_IFS" not in out_df:
            raise ValueError("IFS no cubre la grilla meteorológica completa")
        ifs_values = out_df[[*KEY_COLUMNS, "PP_IFS"]]

    validate_processed_output(
        preserved, before, imputed_dfs, out_df, log_df, states, variables
    )
    summary_text = build_execution_summary(states, dataset, ifs_values)
    print(summary_text, end="")

    plot_diagnostics(
        before,
        imputed_dfs,
        log_df,
        variables,
        output_dir,
        PLOT_DPI,
        config.short_gap_hours,
        config.interstation_min_r2,
    )

    return save_outputs(
        out_df,
        log_df,
        summary_text,
        filepath,
        output_dir,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Imputación escalonada de datos horarios PM2.5 y meteorológicos."
    )
    parser.add_argument("--meteo", type=Path, default=METEO_PATH, help="Ruta al CSV meteorológico")
    parser.add_argument("--pm25", type=Path, default=PM25_PATH, help="Ruta al CSV de PM2.5")
    parser.add_argument(
        "--only",
        choices=["meteo", "pm25"],
        default=None,
        help="Procesa únicamente meteorología o PM2.5 (default: ambos)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = ImputeConfig()

    if args.only != "meteo":
        if args.pm25.exists():
            process_file(args.pm25, PM25_VARS, config, "pm25")
        else:
            parser.error(f"Archivo PM2.5 no encontrado: {args.pm25}")

    if args.only != "pm25":
        if args.meteo.exists():
            process_file(args.meteo, METEO_VARS, config, "meteo")
        else:
            parser.error(f"Archivo meteorológico no encontrado: {args.meteo}")


if __name__ == "__main__":
    main()
