"""Adquisición en memoria de precipitación ECMWF IFS mediante Open-Meteo."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from processing_rules import (
    KEY_COLUMNS,
    OPERATIONAL_RANGES,
    QUALITY_RULES,
    TIMEZONE_ASSUMPTION,
)

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
TIMEZONE = TIMEZONE_ASSUMPTION


def open_meteo_url(latitude: float, longitude: float, start: str, end: str) -> str:
    """Construye la consulta horaria explícita al modelo ECMWF IFS."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start,
        "end_date": end,
        "hourly": "precipitation",
        "timezone": TIMEZONE,
        "models": "ecmwf_ifs",
        "cell_selection": "nearest",
        "elevation": "nan",
    }
    return f"{OPEN_METEO_ARCHIVE}?{urlencode(params)}"


def fetch_open_meteo(url: str, retries: int = 3) -> dict[str, Any]:
    """Obtiene una respuesta remota en memoria con reintentos acotados."""
    request = Request(url, headers={"User-Agent": "tesis-senamhi-ifs/1.0"})
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError) as error:
            if attempt + 1 == retries:
                raise RuntimeError(
                    f"Open-Meteo falló tras {retries} intentos: {error}"
                ) from error
            time.sleep(2**attempt)
    raise AssertionError("bucle de reintentos inalcanzable")


def normalize_open_meteo_payload(station: str, payload: dict[str, Any]) -> pd.DataFrame:
    """Valida zona, unidad, grilla y rango antes de exponer ``PP_IFS``."""
    required = {"latitude", "longitude", "timezone", "hourly", "hourly_units"}
    if not required.issubset(payload):
        raise ValueError("Respuesta Open-Meteo incompleta")
    if payload["timezone"] != TIMEZONE:
        raise ValueError(f"Zona horaria inesperada: {payload['timezone']}")
    if not isinstance(payload["hourly_units"], dict) or (
        payload["hourly_units"].get("precipitation") != "mm"
    ):
        raise ValueError("Open-Meteo no devolvió precipitación en milímetros")
    if not isinstance(payload["hourly"], dict):
        raise ValueError("Respuesta Open-Meteo incompleta")
    cell_latitude = pd.to_numeric(pd.Series([payload["latitude"]]), errors="coerce").iloc[0]
    cell_longitude = pd.to_numeric(pd.Series([payload["longitude"]]), errors="coerce").iloc[0]
    if not np.isfinite(cell_latitude) or not -90.0 <= cell_latitude <= 90.0:
        raise ValueError("Latitud de celda IFS inválida")
    if not np.isfinite(cell_longitude) or not -180.0 <= cell_longitude <= 180.0:
        raise ValueError("Longitud de celda IFS inválida")

    times_raw = payload["hourly"].get("time", [])
    values_raw = payload["hourly"].get("precipitation", [])
    if not times_raw or len(times_raw) != len(values_raw):
        raise ValueError("Series horarias Open-Meteo vacías o de distinta longitud")
    times = pd.DatetimeIndex(pd.to_datetime(times_raw, errors="raise"))
    if times.tz is not None:
        raise ValueError("Open-Meteo debe devolver horas locales sin offset")
    if times.duplicated().any() or not times.is_monotonic_increasing:
        raise ValueError("Timestamps Open-Meteo duplicados o desordenados")
    if len(times) > 1 and not (times[1:] - times[:-1] == pd.Timedelta(hours=1)).all():
        raise ValueError("Open-Meteo no devolvió una grilla estrictamente horaria")

    reported = pd.Series(values_raw).notna()
    values = pd.to_numeric(pd.Series(values_raw), errors="coerce")
    # PP de estación y PP_IFS comparten límites globales de precipitación. IFS
    # sigue siendo una salida externa y nunca interviene en la imputación de PP.
    hard_invalid = reported & (
        ~np.isfinite(values) | values.lt(QUALITY_RULES["PP"].hard_min)
    )
    operational_outlier = values.notna() & np.isfinite(values) & ~values.between(
        *OPERATIONAL_RANGES["PP"]
    )
    values = values.mask(hard_invalid | operational_outlier)
    return pd.DataFrame(
        {
            "ESTACION": station,
            "TIMESTAMP_LOCAL": times,
            "PP_IFS": values.to_numpy(dtype=float),
        }
    )


def acquire_pp_ifs(
    stations: pd.DataFrame, timestamps: pd.DatetimeIndex
) -> pd.DataFrame:
    """Descarga y alinea ``PP_IFS`` para todas las estaciones y horas pedidas."""
    required = {"ESTACION", "LATITUD", "LONGITUD"}
    if not required.issubset(stations.columns):
        raise ValueError(f"Faltan columnas de estación para IFS: {sorted(required - set(stations))}")
    station_rows = stations[["ESTACION", "LATITUD", "LONGITUD"]].drop_duplicates(
        "ESTACION"
    )
    if station_rows.empty or station_rows["ESTACION"].duplicated().any():
        raise ValueError("La lista de estaciones IFS debe ser única y no vacía")
    for coordinate, lower, upper in [
        ("LATITUD", -90.0, 90.0),
        ("LONGITUD", -180.0, 180.0),
    ]:
        values = pd.to_numeric(station_rows[coordinate], errors="coerce")
        if values.isna().any() or not values.between(lower, upper).all():
            raise ValueError(f"{coordinate} inválida para adquisición IFS")

    grid = pd.DatetimeIndex(timestamps).sort_values().unique()
    if grid.empty or grid.tz is not None or grid.has_duplicates:
        raise ValueError("La grilla IFS debe contener horas locales únicas")
    if len(grid) > 1 and not (grid[1:] - grid[:-1] == pd.Timedelta(hours=1)).all():
        raise ValueError("La grilla solicitada a IFS no es estrictamente horaria")
    start = grid.min().date().isoformat()
    end = grid.max().date().isoformat()

    frames: list[pd.DataFrame] = []
    for station in station_rows.itertuples(index=False):
        url = open_meteo_url(station.LATITUD, station.LONGITUD, start, end)
        received = normalize_open_meteo_payload(station.ESTACION, fetch_open_meteo(url))
        indexed = received.set_index("TIMESTAMP_LOCAL")
        missing_hours = grid.difference(indexed.index)
        if not missing_hours.empty:
            raise ValueError(
                f"Open-Meteo omitió {len(missing_hours)} horas para {station.ESTACION}"
            )
        aligned = indexed.reindex(grid)
        frame = pd.DataFrame(
            {
                "ESTACION": station.ESTACION,
                "FECHA": grid.year * 10000 + grid.month * 100 + grid.day,
                "HORA": grid.hour * 10000,
                "PP_IFS": aligned["PP_IFS"].to_numpy(dtype=float),
            }
        )
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True)
    if result.duplicated(KEY_COLUMNS).any():
        raise ValueError("IFS produjo claves horarias duplicadas")
    return result
