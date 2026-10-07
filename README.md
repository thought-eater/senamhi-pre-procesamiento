# Datasets curados de PM2.5 y meteorología — Lima

**Resultado R1.1:** dos bases de datos separadas, curadas y georreferenciadas de
la red SENAMHI para Lima Metropolitana. Comprenden enero de 2025–junio de 2026,
con frecuencia horaria, metadatos geográficos y procedencia por variable.

## Datos y evidencia

| Producto | Dataset | Informe de preprocesamiento | Gráficos |
|---|---|---|---|
| PM2.5 | [CSV](resultados/pm25/pm25_lima_2025_2026_imputed.csv) | [Resumen](resultados/pm25/pm25_lima_2025_2026_summary.txt) | [Figuras](resultados/pm25/plots/) |
| Meteorología | [CSV](resultados/meteo/var_meteorologicas_lima_2025_2026_imputed.csv) | [Resumen](resultados/meteo/var_meteorologicas_lima_2025_2026_summary.txt) | [Figuras](resultados/meteo/plots/) |

- [Diccionario de variables y procedencia](docs/diccionario.md).
- [Metodología de limpieza e imputación](docs/metodologia.md).
- [Informe verificable de R1.1](validacion/r1_1/resultados/informe.md).
- [Protocolo de validación e indicadores](validacion/r1_1/README.md).
- [Correspondencia con tablas y figuras de la tesis](docs/evidencias.md).

Cada valor distingue observación, imputación o ausencia mediante `SOURCE`.
`PP_IFS` es precipitación modelada externa ECMWF IFS/Open-Meteo; permanece
separada de la precipitación SENAMHI `PP`.

## Reproducción

Requiere Python ≥ 3.10 y [uv](https://docs.astral.sh/uv/). Los CSV de entrada
proceden de los repositorios [PM2.5](https://github.com/thought-eater/scraper-pm2.5-lima)
y [meteorología](https://github.com/thought-eater/senamhi-var-metereologicas-lima).
Con ambos repositorios descargados como directorios hermanos:

```bash
uv sync --frozen
uv run --frozen python senamhi_preprocesamiento.py
```

El pipeline reemplaza las salidas de `resultados/`; la rama meteorológica
consulta Open-Meteo. La [guía de reproducción](docs/reproduccion.md) detalla
entradas, carga de datos y límites de reproducibilidad.

Para verificar los archivos publicados sin acceder a servicios externos:

```bash
uv run --frozen python -m validacion.r1_1.verificar_datasets \
  --fecha-referencia 2026-10-07
```

## Pruebas de software

```bash
uv run --frozen python -m unittest discover -s tests/software -v
```

Las [pruebas sintéticas](tests/README.md) comprueban el software. La validación
de R1.1 examina los datos publicados; el aumento de cobertura no demuestra la
exactitud de las imputaciones y la revisión del asesor es documental.
