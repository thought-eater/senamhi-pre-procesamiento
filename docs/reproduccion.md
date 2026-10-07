# Reproducción de los resultados

## 1. Obtener la misma versión de los archivos

Descargar los tres repositorios y conservar esta disposición:

```text
directorio-de-trabajo/
  scraper-pm2.5-lima/
    pm25_lima_2025_2026.csv
  senamhi-var-metereologicas-lima/
    var_meteorologicas_lima_2025_2026.csv
  senamhi-pre-procesamiento/
    resultados/
```

Repositorios de entrada: [PM2.5](https://github.com/thought-eater/scraper-pm2.5-lima)
y [meteorología](https://github.com/thought-eater/senamhi-var-metereologicas-lima).
El [manifiesto de la verificación](../validacion/r1_1/resultados/manifiesto.json)
identifica mediante SHA-256 los archivos examinados. Para citar un resultado,
utilizar la revisión concreta del repositorio que contiene esos archivos.

## 2. Preparar el entorno

Desde la raíz del preprocesador, con Python ≥ 3.10 y uv:

```bash
uv sync --frozen
```

`uv.lock` fija las dependencias. Cada extractor utiliza su propio entorno;
el meteorológico requiere Python ≥ 3.14. La validación de archivos publicados
utiliza únicamente la biblioteca estándar de Python y `processing_rules.py`.

## 3. Verificar la evidencia publicada, sin red

```bash
uv run --frozen python -m validacion.r1_1.verificar_datasets \
  --fecha-referencia 2026-10-07
```

Se leen los CSV brutos, los CSV finales y los logs. Se recalculan las tablas y
se escribe un informe en `validacion/r1_1/resultados/`. La fecha explícita fija
la evaluación de antigüedad para que sea reproducible. Se puede escribir en otra
carpeta con `--salida ruta`; `--pm25-bruto` y `--meteo-bruto` permiten seleccionar
capturas ubicadas fuera de la disposición predeterminada.

El [protocolo](../validacion/r1_1/README.md) especifica las comprobaciones y sus
límites. Esta operación no vuelve a ejecutar la imputación ni descarga IFS.

## 4. Regenerar los productos

```bash
uv run --frozen python senamhi_preprocesamiento.py
```

La ejecución reemplaza CSV finales, logs, resúmenes y gráficos dentro de
`resultados/pm25/` y `resultados/meteo/`. Para mantener una referencia de la
publicación, ejecutar la regeneración en otra copia del repositorio.

También se puede seleccionar una rama y rutas de entrada:

```bash
uv run --frozen python senamhi_preprocesamiento.py --only pm25 --pm25 ruta/pm25.csv
uv run --frozen python senamhi_preprocesamiento.py --only meteo --meteo ruta/meteo.csv
```

La rama PM2.5 trabaja sobre archivos locales. Meteorología descarga `PP_IFS`
en memoria desde Open-Meteo: necesita conexión y depende de la disponibilidad
y versión del contenido remoto. No se conservan las respuestas IFS originales,
por lo que una nueva ejecución no garantiza identidad binaria con el producto
publicado. La comparación de tablas sí puede hacerse offline sobre ese producto.

## 5. Cargar y seleccionar los datos

Ejemplo desde la raíz, usando pandas, incluido en el entorno:

```python
import pandas as pd

datos = pd.read_csv("resultados/pm25/pm25_lima_2025_2026_imputed.csv")
datos["tiempo"] = pd.to_datetime(datos["FECHA"].astype(str), format="%Y%m%d")
datos["tiempo"] += pd.to_timedelta(datos["HORA"] // 10000, unit="h")
datos["tiempo"] = datos["tiempo"].dt.tz_localize("America/Lima")
observaciones = datos.loc[datos["PM2_5_SOURCE"].eq("senamhi_observed")]
```

Para incluir imputaciones se debe declarar esa decisión y seleccionar sus
estados `imputed_*`. Los atípicos conservados y `PP_IFS` tienen naturalezas
distintas de las observaciones originales. Véase el [diccionario](diccionario.md).
