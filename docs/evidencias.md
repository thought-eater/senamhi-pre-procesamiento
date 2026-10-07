# Evidencia de R1.1

El [informe de validación](../validacion/r1_1/resultados/informe.md) vincula los
criterios del resultado con los datos publicados. Las comprobaciones sobre
archivos reales se ejecutan por separado de las [pruebas de software](../tests/README.md).

## Tablas de la sección 4.1 de la tesis

| Tabla | Contenido | Evidencia pública |
|---|---|---|
| 8 | Reglas de calidad | [Metodología](metodologia.md) y [reglas implementadas](../processing_rules.py) |
| 9 | Condiciones de imputación | [Metodología](metodologia.md) |
| 10 | Estados `SOURCE` | [Diccionario](diccionario.md) |
| 11 | Cobertura, observaciones, imputaciones y ausencias | [Tabla recalculada](../validacion/r1_1/resultados/tabla_11.csv) e [informe](../validacion/r1_1/resultados/informe.md) |
| 12 y 13 | Diccionarios de ambos productos | [Diccionario](diccionario.md) y cabeceras de los CSV |

La tabla de referencia de cobertura se conserva en
[`referencia_tabla_11.csv`](../validacion/r1_1/referencia_tabla_11.csv), para
contrastar las cifras declaradas con las recalculadas. Esta referencia no
alimenta la imputación ni reemplaza la lectura de los datos.

## Figuras de resultados

Las figuras 1–3 son esquemas conceptuales de adquisición, calidad y cascada,
descritos en la metodología. Las figuras cuantitativas corresponden a:

| Figura | Archivo |
|---|---|
| 4 | [Cobertura PM2.5](../resultados/pm25/plots/heatmap_cobertura_PM2_5.png) |
| 5 | [Cobertura HR](../resultados/meteo/plots/heatmap_cobertura_HR.png) |
| 6 | [Cobertura TEMP](../resultados/meteo/plots/heatmap_cobertura_TEMP.png) |
| 7 | [Cobertura PP](../resultados/meteo/plots/heatmap_cobertura_PP.png) |
| 8 | [Cobertura DIR_VIENTO](../resultados/meteo/plots/heatmap_cobertura_DIR_VIENTO.png) |
| 9 | [Cobertura VEL_VIENTO](../resultados/meteo/plots/heatmap_cobertura_VEL_VIENTO.png) |
| 10 | [Serie PM2.5, Campo de Marte](../resultados/pm25/plots/serie_CAMPO_DE_MARTE_PM2_5.png) |
| 11 | [Serie HR, Campo de Marte](../resultados/meteo/plots/serie_CAMPO_DE_MARTE_HR.png) |
| 12 | [Serie TEMP, Campo de Marte](../resultados/meteo/plots/serie_CAMPO_DE_MARTE_TEMP.png) |
| 13 | [Serie PP, Campo de Marte](../resultados/meteo/plots/serie_CAMPO_DE_MARTE_PP.png) |
| 14 | [Serie DIR_VIENTO, Campo de Marte](../resultados/meteo/plots/serie_CAMPO_DE_MARTE_DIR_VIENTO.png) |
| 15 | [Serie VEL_VIENTO, Campo de Marte](../resultados/meteo/plots/serie_CAMPO_DE_MARTE_VEL_VIENTO.png) |
| 16 | [Métodos PM2.5](../resultados/pm25/plots/metodos_imputacion.png) |
| 17 | [Métodos meteorológicos](../resultados/meteo/plots/metodos_imputacion.png) |

Los gráficos de disponibilidad diaria usan todas las horas del día; la tabla de
cobertura excluye los casos de dirección no aplicable durante calma. Una mayor
continuidad gráfica o cobertura no demuestra menor error de imputación.
