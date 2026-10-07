# Metodología de preprocesamiento

## Alcance de R1.1

Se procesan por separado concentraciones de PM2.5 y variables meteorológicas de
SENAMHI. No se utilizan variables de una rama para imputar la otra. El periodo
del estudio es 2025-01-01 00:00 a 2026-06-30 23:00: 18 meses y 13 104 horas por
estación representada. Una grilla completa puede contener valores ausentes.

## Preparación y calidad

1. Validar columnas, clave única, fechas, horas y metadatos por estación.
2. Reindexar cada estación sobre la grilla horaria del producto.
3. Conservar una vista de datos reportados y construir una vista analítica.
4. Clasificar valores inválidos, atípicos y ausencias antes de imputar.

| Variable | Dominio duro | Rango operacional del proyecto |
|---|---|---|
| `PM2_5` | ≥ 0 | [0, 1000] µg/m³ |
| `TEMP` | ≥ −273,15 | [−10, 60] °C |
| `HR` | [0, 100] | [0, 100] % |
| `PP` | ≥ 0 | [0, 60] mm |
| `DIR_VIENTO` | [0, 360] | [0, 360]; 360° se normaliza a 0° |
| `VEL_VIENTO` | ≥ 0 | [0, 75] m/s |

La reconstrucción vectorial puede representar el norte como 360° por redondeo;
la comparación de direcciones considera equivalentes 0° y 360°.

Todas las mediciones deben ser finitas. Un valor fuera del dominio duro se
convierte en ausencia analítica y puede imputarse. Un atípico operacional
satisface el dominio duro pero excede el rango del proyecto: se conserva en
la salida, se etiqueta y queda excluido como objetivo y como evidencia.
Los rangos operacionales son decisiones analíticas del estudio, no límites
oficiales atribuidos a SENAMHI.

Solo se imputan faltantes internos entre la primera y la última observación
válida de cada estación-variable. Los extremos sin observaciones y las series
nunca observadas permanecen como ausencias estructurales. La duración original
del gap se mantiene durante toda la cascada.

## Cascada fija

| Etapa | Evidencia y condición |
|---|---|
| Gap de hasta 4 h | Interpolación lineal entre dos anclas originales válidas |
| Regla de `PP` | Cero solo si ambas anclas son cero; en otro caso, lineal |
| Media semanal | Misma hora en t±7 y t±14 días; al menos 3 de 4 donantes |
| Análogo anual | Primero t+1 año; t−1 año solo como alternativa |
| OLS entre estaciones | Cobertura ≥ 0,9 de horas pendientes; ≥ 48 pares; `TimeSeriesSplit(5)`; R² medio ≥ 0,5 |

Las estaciones candidatas a OLS se ordenan por distancia Haversine. La
distancia determina el orden, no es un predictor del modelo. Las estimaciones
fuera del rango operacional se rechazan; no se recortan. Cada método consulta
únicamente observaciones originales válidas: las imputaciones no se reutilizan.

Dirección y velocidad del viento se transforman en componentes cartesianas,
se imputan conjuntamente y se reconstruyen. Los campos originalmente observados
se conservan. La dirección durante calma es no aplicable y se excluye del
denominador de cobertura; las componentes internas no se publican.

## Precipitación externa

La rama meteorológica adquiere ECMWF IFS mediante Open-Meteo, valida unidad,
zona horaria y grilla, y añade `PP_IFS` después de la imputación SENAMHI. Un fallo
de descarga o una grilla incompleta impide publicar esa rama. Los valores IFS
fuera del rango de precipitación se representan como ausencias. `PP_IFS` no
actúa como donante, predictor ni sustituto de `PP`.

## Interpretación y límites

Las salidas conservan procedencia por celda y un log de imputaciones. Las tablas
de R1.1 describen disponibilidad y cumplimiento de criterios del dataset;
el incremento de cobertura no estima el error de las imputaciones.

La cascada es retrospectiva: admite observaciones posteriores al instante
reconstruido. No representa un procedimiento de pronóstico en tiempo real.
El R² usado para aceptar OLS es un criterio interno, no una validación empírica
de toda la cascada. No se dispone de backtesting integral sobre observaciones
ocultadas. Las ausencias pueden depender de estación y periodo, y los metadatos
instrumentales disponibles son limitados.

## Referencias técnicas

- [SENAMHI: estaciones](https://www.senamhi.gob.pe/?p=estaciones).
- [Open-Meteo: Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api).
- [scikit-learn: TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).
- [scikit-learn: LinearRegression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LinearRegression.html).
