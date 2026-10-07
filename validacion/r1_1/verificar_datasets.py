"""Verifica R1.1 sobre archivos reales, sin red ni ejecución de la imputación.

Uso desde la raíz: python -m validacion.r1_1.verificar_datasets
    --fecha-referencia YYYY-MM-DD
"""

from __future__ import annotations

import argparse
import calendar
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys

from processing_rules import METEO_VARS, PM25_VARS, allowed_sources_for


ROOT = Path(__file__).resolve().parents[2]
START = datetime(2025, 1, 1)
END = datetime(2026, 6, 30, 23)
HOURS = int((END - START) / timedelta(hours=1)) + 1
MONTHS = [f"2025-{month:02d}" for month in range(1, 13)] + [
    f"2026-{month:02d}" for month in range(1, 7)
]
TABLE_FIELDS = [
    "variable", "celdas_aplicables", "observaciones", "atipicos",
    "ausencias_antes", "imputados", "ausencias_finales", "cobertura_final_pct",
]
MONTH_FIELDS = [
    "producto", "estacion", "variable", "mes", "horas_esperadas",
    "observaciones", "imputados", "disponibles", "observado_pct",
]


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f"Cabecera ausente o repetida: {path.name}")
        rows = list(reader)
    if not rows or any(None in row or None in row.values() for row in rows):
        raise ValueError(f"CSV vacío o filas incompatibles con la cabecera: {path.name}")
    return rows


def timestamp(row: dict) -> datetime:
    day, hour = row["FECHA"], row["HORA"]
    if len(day) != 8 or not day.isascii() or not day.isdigit() or not hour.isdigit():
        raise ValueError("FECHA/HORA deben ser enteros YYYYMMDD y HH*10000")
    value = int(hour)
    if value % 10000 or not 0 <= value <= 230000:
        raise ValueError(f"Hora no horaria: {hour}")
    return datetime.strptime(day, "%Y%m%d").replace(hour=value // 10000)


def number(value: str) -> float | None:
    if value == "":
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Valor numérico no finito: {value}")
    return result


def index_rows(rows: list[dict]) -> dict:
    indexed = {}
    for row in rows:
        if not row["ESTACION"].strip():
            raise ValueError("Estación vacía")
        key = (row["ESTACION"], timestamp(row))
        if key in indexed:
            raise ValueError(f"Clave duplicada: {key}")
        indexed[key] = row
    return indexed


def longest_run(months: list[str]) -> int:
    """Cuenta meses calendario consecutivos con al menos una observación."""
    ordinals = sorted({int(m[:4]) * 12 + int(m[5:]) for m in months})
    best = run = 0
    previous = None
    for current in ordinals:
        run = run + 1 if previous is not None and current == previous + 1 else 1
        best = max(best, run)
        previous = current
    return best


def same_observation(variable: str, original: float | None, value: float | None) -> bool:
    if original is None or value is None:
        return False
    if variable == "DIR_VIENTO" and 0 <= original <= 360 and 0 <= value <= 360:
        # Norte puede representarse como 0° o 360° tras reconstrucción vectorial.
        difference = abs(original - value) % 360
        return min(difference, 360 - difference) <= 1e-9
    return math.isclose(original, value, rel_tol=0, abs_tol=1e-9)


def matches_reference(actual: dict, expected: dict) -> bool:
    for field, value in expected.items():
        if field == "variable" or value == "":
            if str(actual.get(field)) != value:
                return False
        else:
            try:
                if float(actual[field]) != float(value):
                    return False
            except (ValueError, KeyError, TypeError):
                return False
    return True


def table_row(variable: str, counts: Counter) -> dict:
    applicable = counts["aplicables"]
    external = variable == "PP_IFS"
    return {
        "variable": variable,
        "celdas_aplicables": applicable,
        "observaciones": "" if external else counts["observaciones"],
        "atipicos": 0 if external else counts["atipicos"],
        "ausencias_antes": (
            applicable - counts["disponibles"] if external else
            applicable - counts["observaciones"] - counts["atipicos"]
        ),
        "imputados": "" if external else counts["imputados"],
        "ausencias_finales": applicable - counts["disponibles"],
        "cobertura_final_pct": round(100 * counts["disponibles"] / applicable, 2)
        if applicable else "",
    }


def check_log(path: Path, expected: Counter) -> bool:
    logged = Counter()
    for row in read_csv(path):
        times = row["imputed_timestamps"].split("|") if row["imputed_timestamps"] else []
        count = int(row["n_values_imputed"])
        if row["accepted"] not in {"True", "False"} or count != len(times):
            return False
        if times and row["accepted"] != "True":
            return False
        for time in times:
            logged[(row["station"], row["variable"], datetime.fromisoformat(time),
                    f"imputed_{row['method_used']}")] += 1
    return logged == expected


def verify_product(product: str, final: Path, raw: Path, log: Path,
                   variables: list[str], reference: date) -> dict:
    rows, raw_rows = read_csv(final), read_csv(raw)
    meta = {"LONGITUD", "LATITUD", "ALTITUD", "DISTRITO"}
    meta.add("PROVINCIA" if product == "pm25" else "RED")
    required = {"ESTACION", "FECHA", "HORA", *meta, *variables}
    columns = required | {f"{v}_SOURCE" for v in variables}
    if product == "meteo":
        columns.add("PP_IFS")
    if set(rows[0]) != columns or set(raw_rows[0]) != required:
        raise ValueError("Esquema de columnas incompatible con el producto")
    indexed, originals = index_rows(rows), index_rows(raw_rows)
    checks = []

    def check(criterion: str, passed: bool, detail: str) -> None:
        checks.append({"producto": product, "criterio": criterion,
                       "estado": "CUMPLE" if passed else "NO_CUMPLE", "detalle": detail})

    check("esquema_y_clave", True, f"{len(rows)} filas; clave única; columnas separadas")
    station_times = defaultdict(list)
    for station, time in indexed:
        station_times[station].append(time)
    grid_ok = all(
        len(times) == HOURS and times == sorted(times)
        and times[0] == START and times[-1] == END
        and all(b - a == timedelta(hours=1) for a, b in zip(times, times[1:]))
        for times in station_times.values()
    )
    check("periodo_y_frecuencia", grid_ok,
          f"2025-01-01 a 2026-06-30; {HOURS} horas por estación; frecuencia 1 h")
    cutoff_year = reference.year - 10
    cutoff = reference.replace(
        year=cutoff_year,
        day=min(reference.day, calendar.monthrange(cutoff_year, reference.month)[1]),
    )
    check("antiguedad", all(cutoff <= time.date() <= reference for _, time in indexed),
          f"Fechas entre {cutoff} y {reference}, sin fechas futuras")
    check("conservacion_claves", set(originals) <= set(indexed),
          "Toda clave del CSV de adquisición permanece en el producto")
    counters, monthly = defaultdict(Counter), defaultdict(Counter)
    methods, expected_log, errors = Counter(), Counter(), Counter()
    stations, coordinates = [], defaultdict(set)
    valid_sources = {v: set(allowed_sources_for(v)) for v in variables}
    for (station, time), row in indexed.items():
        original = originals.get((station, time))
        lat, lon = number(row["LATITUD"]), number(row["LONGITUD"])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            errors["coordenadas"] += 1
        coordinates[station].add(tuple(row[field] for field in sorted(meta)))
        if original and any(number(original[f]) != number(row[f])
                            for f in ("LATITUD", "LONGITUD")):
            errors["coordenadas"] += 1
        for variable in [*variables, *(["PP_IFS"] if product == "meteo" else [])]:
            value = number(row[variable])
            source = row.get(f"{variable}_SOURCE", "external_ifs")
            counts = counters[(station, variable)]
            month = monthly[(station, variable, time.strftime("%Y-%m"))]
            external = variable == "PP_IFS"
            observed = source == "senamhi_observed"
            outlier = source == "senamhi_operational_outlier"
            imputed = source.startswith("imputed_")
            applicable = source != "not_applicable"
            if not external:
                if source not in valid_sources[variable]:
                    errors["source"] += 1
                if (observed or outlier or imputed) != (value is not None):
                    errors["source"] += 1
                if not applicable and (variable != "DIR_VIENTO" or number(row["VEL_VIENTO"]) != 0):
                    errors["source"] += 1
                if observed or outlier:
                    old = number(original[variable]) if original else None
                    if not same_observation(variable, old, value):
                        errors["observaciones"] += 1
                    elif variable == "DIR_VIENTO" and old != value:
                        errors["equivalencias_angulares"] += 1
                if imputed:
                    expected_log[(station, variable, time, source)] += 1
                    methods[(station, variable, source)] += 1
            for target in (counts, month):
                target.update({"aplicables": int(applicable), "observaciones": int(observed),
                               "atipicos": int(outlier), "imputados": int(imputed),
                               "disponibles": int(applicable and value is not None)})
    stable_meta = all(len(items) == 1 for items in coordinates.values())
    check("georreferenciacion", not errors["coordenadas"] and stable_meta,
          f"{len(coordinates)} estaciones; rangos geográficos, constancia y coincidencia con bruto")
    check("procedencia", not errors["source"], f"{errors['source']} incoherencias de SOURCE/valor")
    check("observaciones_preservadas", not errors["observaciones"],
          f"{errors['observaciones']} diferencias respecto al bruto (tolerancia 1e-9); "
          f"{errors['equivalencias_angulares']} representaciones angulares equivalentes (0°/360°)")
    check("log_imputacion", check_log(log, expected_log),
          f"{sum(expected_log.values())} celdas imputadas contrastadas por estación, variable, hora y método")
    totals = defaultdict(Counter)
    station_table, month_table = [], []
    for (station, variable), counts in sorted(counters.items()):
        totals[variable].update(counts)
        station_table.append({"producto": product, "estacion": station, **table_row(variable, counts)})
        for month in MONTHS:
            counts_month = monthly[(station, variable, month)]
            hours = calendar.monthrange(int(month[:4]), int(month[5:]))[1] * 24
            month_table.append({
                "producto": product, "estacion": station, "variable": variable, "mes": month,
                "horas_esperadas": hours, "observaciones": counts_month["observaciones"],
                "imputados": counts_month["imputados"], "disponibles": counts_month["disponibles"],
                "observado_pct": round(100 * counts_month["observaciones"] / hours, 2)
                if variable != "PP_IFS" else "",
            })
    for variable in variables:
        months = [month for (_, var, month), counts in monthly.items()
                  if var == variable and counts["observaciones"] > 0]
        run = longest_run(months)
        check(f"meses_con_observaciones_{variable}", run >= 6,
              f"{run} meses consecutivos con ≥1 observación en alguna estación; no implica cobertura completa")
    for station in sorted(coordinates):
        first = indexed[(station, station_times[station][0])]
        stations.append({"producto": product, "estacion": station,
                         "latitud": first["LATITUD"], "longitud": first["LONGITUD"],
                         "altitud": first["ALTITUD"], "filas": len(station_times[station])})
    if product == "meteo":
        ifs = totals["PP_IFS"]
        check("disponibilidad_ifs", ifs["disponibles"] == len(rows),
              f"{ifs['disponibles']}/{len(rows)} valores externos disponibles; separados de PP")
    return {"checks": checks, "table": [table_row(v, totals[v]) for v in
            [*variables, *(["PP_IFS"] if product == "meteo" else [])]],
            "stations": stations, "station_table": station_table, "monthly": month_table,
            "methods": [{"producto": product, "estacion": s, "variable": v,
                         "metodo": m, "celdas": n} for (s, v, m), n in sorted(methods.items())]}


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fecha-referencia", type=date.fromisoformat, required=True)
    parser.add_argument("--pm25-bruto", type=Path, default=ROOT.parent / "scraper-pm2.5-lima/pm25_lima_2025_2026.csv")
    parser.add_argument("--meteo-bruto", type=Path, default=ROOT.parent / "senamhi-var-metereologicas-lima/var_meteorologicas_lima_2025_2026.csv")
    parser.add_argument("--salida", type=Path, default=ROOT / "validacion/r1_1/resultados")
    args = parser.parse_args(argv)
    collected = {key: [] for key in ("checks", "table", "stations", "station_table", "monthly", "methods")}
    inputs = []
    for product, raw, variables, stem in (
        ("pm25", args.pm25_bruto, PM25_VARS, "pm25_lima_2025_2026"),
        ("meteo", args.meteo_bruto, METEO_VARS, "var_meteorologicas_lima_2025_2026"),
    ):
        base = ROOT / "resultados" / product
        final, log = base / f"{stem}_imputed.csv", base / f"{stem}_imputation_log.csv"
        inputs.extend([(f"entrada/{product}/{raw.name}", raw)])
        try:
            result = verify_product(product, final, raw, log, variables, args.fecha_referencia)
            for key in collected:
                collected[key].extend(result[key])
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Los errores de lectura también invalidan la verificación; no se omiten productos.
            detail = Path(error.filename).name if getattr(error, "filename", None) else str(error)
            collected["checks"].append({
                "producto": product,
                "criterio": "lectura_y_estructura",
                "estado": "NO_CUMPLE",
                "detalle": f"{type(error).__name__}: revisar entradas ({detail})",
            })
    reference_path = ROOT / "validacion/r1_1/referencia_tabla_11.csv"
    expected = read_csv(reference_path)
    actual = {row["variable"]: row for row in collected["table"]}
    for row in expected:
        variable = row["variable"]
        matches = variable in actual and matches_reference(actual[variable], row)
        collected["checks"].append({"producto": "R1.1", "criterio": f"tabla_11_{variable}",
                                    "estado": "CUMPLE" if matches else "NO_CUMPLE",
                                    "detalle": "Comparación con cifras de referencia; porcentajes redondeados a dos decimales"})
    documentation = [ROOT / "docs" / name for name in
                     ("diccionario.md", "metodologia.md", "reproduccion.md", "evidencias.md")]
    collected["checks"].extend([
        {"producto": "R1.1", "criterio": "documentacion", "estado": "CUMPLE" if all(p.is_file() for p in documentation) else "NO_CUMPLE",
         "detalle": "Diccionario, metodología, reproducción e índice de evidencias disponibles; contenido sujeto a revisión"},
        {"producto": "R1.1", "criterio": "revision_asesor", "estado": "PENDIENTE_DOCUMENTAL",
         "detalle": "La ejecución no acredita la revisión del asesor"},
    ])
    for path in sorted((ROOT / "resultados").rglob("*")):
        if path.is_file():
            inputs.append((path.relative_to(ROOT).as_posix(), path))
    for path in [*documentation, reference_path, Path(__file__), ROOT / "processing_rules.py",
                 ROOT / "senamhi_preprocesamiento.py", ROOT / "integrar_precipitacion_ifs.py",
                 ROOT / "pyproject.toml", ROOT / "uv.lock"]:
        inputs.append((path.relative_to(ROOT).as_posix(), path))
    manifest = {"fecha_referencia": str(args.fecha_referencia), "python": sys.version.split()[0],
                "archivos": [{"archivo": name, "bytes": path.stat().st_size,
                              "sha256": digest(path)} for name, path in inputs if path.is_file()]}
    args.salida.mkdir(parents=True, exist_ok=True)
    outputs = [
        ("tabla_11.csv", "table", TABLE_FIELDS),
        ("cobertura_estacion.csv", "station_table", ["producto", "estacion", *TABLE_FIELDS]),
        ("cobertura_mensual.csv", "monthly", MONTH_FIELDS),
        ("estaciones.csv", "stations", ["producto", "estacion", "latitud", "longitud", "altitud", "filas"]),
        ("metodos.csv", "methods", ["producto", "estacion", "variable", "metodo", "celdas"]),
        ("comprobaciones.csv", "checks", ["producto", "criterio", "estado", "detalle"]),
    ]
    for name, key, fields in outputs:
        write_csv(args.salida / name, collected[key], fields)
    (args.salida / "manifiesto.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failures = sum(row["estado"] == "NO_CUMPLE" for row in collected["checks"])
    lines = ["# Verificación de R1.1", "", f"Fecha de referencia: **{args.fecha_referencia}**.", "",
             f"Comprobaciones automáticas incumplidas: **{failures}**. Revisión del asesor: **pendiente documental**.", "",
             "## Cobertura recalculada (Tabla 11)", "",
             "| Variable | Aplicables | Observaciones | Atípicos | Ausencias antes | Imputados | Ausencias finales | Cobertura % |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in collected["table"]:
        values = ["No aplica" if row[field] == "" else
                  f"{row[field]:.2f}" if field == "cobertura_final_pct" else str(row[field])
                  for field in TABLE_FIELDS]
        lines.append("| " + " | ".join(values) + " |")
    lines.extend(["", "## Comprobaciones", "", "| Producto | Criterio | Estado | Detalle |", "|---|---|---|---|"])
    lines.extend("| " + " | ".join(str(row[f]).replace("|", "/").replace("\n", " ") for f in ("producto", "criterio", "estado", "detalle")) + " |" for row in collected["checks"])
    lines.extend(["", "## Archivos de evidencia", "",
                  "- [Cobertura por estación-variable](cobertura_estacion.csv).",
                  "- [Cobertura mensual observada](cobertura_mensual.csv).",
                  "- [Estaciones y coordenadas](estaciones.csv).",
                  "- [Imputaciones por método](metodos.csv).",
                  "- [Tabla 11 en CSV](tabla_11.csv) y [comprobaciones](comprobaciones.csv).",
                  "- [Identificación SHA-256 de entradas, productos y protocolo](manifiesto.json).", "",
                  "## Interpretación", "",
                  "Los meses con observaciones exigen al menos una observación válida por mes y variable",
                  "en alguna estación. La grilla y la cobertura efectiva se verifican por separado;",
                  "este criterio no significa seis meses completos sin ausencias en todas las estaciones.", "",
                  "La cobertura final incluye observaciones conservadas, atípicos e imputaciones.",
                  "Las celdas no aplicables de dirección en calma se excluyen del denominador.",
                  "PP_IFS es externa y se contabiliza por separado. El informe no estima exactitud",
                  "de imputación ni certifica la revisión del asesor. Véase el [protocolo](../README.md).", ""])
    (args.salida / "informe.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Verificación R1.1: {failures} incumplimientos automáticos; informe en {args.salida}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
