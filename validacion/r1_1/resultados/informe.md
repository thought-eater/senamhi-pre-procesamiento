# Verificación de R1.1

Fecha de referencia: **2026-10-07**.

Comprobaciones automáticas incumplidas: **0**. Revisión del asesor: **pendiente documental**.

## Cobertura recalculada (Tabla 11)

| Variable | Aplicables | Observaciones | Atípicos | Ausencias antes | Imputados | Ausencias finales | Cobertura % |
|---|---:|---:|---:|---:|---:|---:|---:|
| PM2_5 | 117936 | 85016 | 0 | 32920 | 5862 | 27058 | 77.06 |
| TEMP | 91728 | 84498 | 0 | 7230 | 1416 | 5814 | 93.66 |
| HR | 91728 | 71195 | 0 | 20533 | 1545 | 18988 | 79.30 |
| PP | 91728 | 50360 | 0 | 41368 | 744 | 40624 | 55.71 |
| DIR_VIENTO | 88404 | 81618 | 0 | 6786 | 5233 | 1553 | 98.24 |
| VEL_VIENTO | 91728 | 80241 | 0 | 11487 | 7734 | 3753 | 95.91 |
| PP_IFS | 91728 | No aplica | 0 | 0 | No aplica | 0 | 100.00 |

## Comprobaciones

| Producto | Criterio | Estado | Detalle |
|---|---|---|---|
| pm25 | esquema_y_clave | CUMPLE | 117936 filas; clave única; columnas separadas |
| pm25 | periodo_y_frecuencia | CUMPLE | 2025-01-01 a 2026-06-30; 13104 horas por estación; frecuencia 1 h |
| pm25 | antiguedad | CUMPLE | Fechas entre 2016-10-07 y 2026-10-07, sin fechas futuras |
| pm25 | conservacion_claves | CUMPLE | Toda clave del CSV de adquisición permanece en el producto |
| pm25 | georreferenciacion | CUMPLE | 9 estaciones; rangos geográficos, constancia y coincidencia con bruto |
| pm25 | procedencia | CUMPLE | 0 incoherencias de SOURCE/valor |
| pm25 | observaciones_preservadas | CUMPLE | 0 diferencias respecto al bruto (tolerancia 1e-9); 0 representaciones angulares equivalentes (0°/360°) |
| pm25 | log_imputacion | CUMPLE | 5862 celdas imputadas contrastadas por estación, variable, hora y método |
| pm25 | meses_con_observaciones_PM2_5 | CUMPLE | 18 meses consecutivos con ≥1 observación en alguna estación; no implica cobertura completa |
| meteo | esquema_y_clave | CUMPLE | 91728 filas; clave única; columnas separadas |
| meteo | periodo_y_frecuencia | CUMPLE | 2025-01-01 a 2026-06-30; 13104 horas por estación; frecuencia 1 h |
| meteo | antiguedad | CUMPLE | Fechas entre 2016-10-07 y 2026-10-07, sin fechas futuras |
| meteo | conservacion_claves | CUMPLE | Toda clave del CSV de adquisición permanece en el producto |
| meteo | georreferenciacion | CUMPLE | 7 estaciones; rangos geográficos, constancia y coincidencia con bruto |
| meteo | procedencia | CUMPLE | 0 incoherencias de SOURCE/valor |
| meteo | observaciones_preservadas | CUMPLE | 0 diferencias respecto al bruto (tolerancia 1e-9); 0 representaciones angulares equivalentes (0°/360°) |
| meteo | log_imputacion | CUMPLE | 16672 celdas imputadas contrastadas por estación, variable, hora y método |
| meteo | meses_con_observaciones_TEMP | CUMPLE | 18 meses consecutivos con ≥1 observación en alguna estación; no implica cobertura completa |
| meteo | meses_con_observaciones_HR | CUMPLE | 18 meses consecutivos con ≥1 observación en alguna estación; no implica cobertura completa |
| meteo | meses_con_observaciones_PP | CUMPLE | 18 meses consecutivos con ≥1 observación en alguna estación; no implica cobertura completa |
| meteo | meses_con_observaciones_DIR_VIENTO | CUMPLE | 18 meses consecutivos con ≥1 observación en alguna estación; no implica cobertura completa |
| meteo | meses_con_observaciones_VEL_VIENTO | CUMPLE | 18 meses consecutivos con ≥1 observación en alguna estación; no implica cobertura completa |
| meteo | disponibilidad_ifs | CUMPLE | 91728/91728 valores externos disponibles; separados de PP |
| R1.1 | tabla_11_PM2_5 | CUMPLE | Comparación con cifras de referencia; porcentajes redondeados a dos decimales |
| R1.1 | tabla_11_TEMP | CUMPLE | Comparación con cifras de referencia; porcentajes redondeados a dos decimales |
| R1.1 | tabla_11_HR | CUMPLE | Comparación con cifras de referencia; porcentajes redondeados a dos decimales |
| R1.1 | tabla_11_PP | CUMPLE | Comparación con cifras de referencia; porcentajes redondeados a dos decimales |
| R1.1 | tabla_11_DIR_VIENTO | CUMPLE | Comparación con cifras de referencia; porcentajes redondeados a dos decimales |
| R1.1 | tabla_11_VEL_VIENTO | CUMPLE | Comparación con cifras de referencia; porcentajes redondeados a dos decimales |
| R1.1 | tabla_11_PP_IFS | CUMPLE | Comparación con cifras de referencia; porcentajes redondeados a dos decimales |
| R1.1 | documentacion | CUMPLE | Diccionario, metodología, reproducción e índice de evidencias disponibles; contenido sujeto a revisión |
| R1.1 | revision_asesor | PENDIENTE_DOCUMENTAL | La ejecución no acredita la revisión del asesor |

## Archivos de evidencia

- [Cobertura por estación-variable](cobertura_estacion.csv).
- [Cobertura mensual observada](cobertura_mensual.csv).
- [Estaciones y coordenadas](estaciones.csv).
- [Imputaciones por método](metodos.csv).
- [Tabla 11 en CSV](tabla_11.csv) y [comprobaciones](comprobaciones.csv).
- [Identificación SHA-256 de entradas, productos y protocolo](manifiesto.json).

## Interpretación

Los meses con observaciones exigen al menos una observación válida por mes y variable
en alguna estación. La grilla y la cobertura efectiva se verifican por separado;
este criterio no significa seis meses completos sin ausencias en todas las estaciones.

La cobertura final incluye observaciones conservadas, atípicos e imputaciones.
Las celdas no aplicables de dirección en calma se excluyen del denominador.
PP_IFS es externa y se contabiliza por separado. El informe no estima exactitud
de imputación ni certifica la revisión del asesor. Véase el [protocolo](../README.md).
