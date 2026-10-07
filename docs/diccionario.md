# Diccionario de los datasets curados

Cada fila representa una estación y una hora. PM2.5 y meteorología constituyen
productos independientes, con clave única `(ESTACION, FECHA, HORA)`. Los CSV
usan coma, UTF-8, cabecera y campos vacíos para valores no disponibles.

## Variables y metadatos

| Campo | Unidad / formato | Producto y significado |
|---|---|---|
| `ESTACION` | Texto | Ambos: nombre normalizado de estación |
| `FECHA` | Entero `YYYYMMDD` | Ambos: fecha local |
| `HORA` | Entero `HH*10000` | Ambos: hora; `130000` representa 13:00 |
| `LONGITUD`, `LATITUD` | Grados decimales | Ambos: coordenadas configuradas |
| `ALTITUD` | Metros o vacío | Ambos: altitud configurada |
| `DISTRITO` | Texto | Ambos: distrito |
| `PROVINCIA` | Texto | PM2.5: provincia |
| `RED` | Texto | Meteorología: red EMA |
| `PM2_5` | µg/m³ | Concentración de PM2.5 |
| `TEMP` | °C | Temperatura del aire |
| `HR` | % | Humedad relativa |
| `PP` | mm | Precipitación SENAMHI |
| `DIR_VIENTO` | Grados meteorológicos | Dirección de procedencia del viento |
| `VEL_VIENTO` | m/s | Velocidad del viento |
| `PP_IFS` | mm durante la hora precedente | Precipitación externa ECMWF IFS/Open-Meteo |

Las horas se interpretan como `America/Lima`; el CSV no almacena zona horaria.
Las unidades SENAMHI son las interpretadas de la fuente. La captura no contiene
metadatos completos de instrumentos ni confirmación institucional del intervalo
de acumulación de `PP`, la referencia vertical de altitud o la vigencia de las
coordenadas. La validación numérica de coordenadas no certifica su exactitud
geodésica.

## Procedencia por celda

Cada variable SENAMHI lleva una columna `<VAR>_SOURCE`. Esto comprende
`PM2_5_SOURCE` en PM2.5 y `TEMP_SOURCE`, `HR_SOURCE`, `PP_SOURCE`,
`DIR_VIENTO_SOURCE`, `VEL_VIENTO_SOURCE` en meteorología.

| Estado | Interpretación |
|---|---|
| `senamhi_observed` | Observación original admitida por los controles de calidad |
| `senamhi_operational_outlier` | Valor original conservado, excluido de la imputación |
| `missing_hard_invalid` | Valor inválido no resuelto por la cascada |
| `missing_explicit` | Faltante interno presente en una fila de adquisición |
| `missing_implicit` | Faltante interno cuya fila se creó al reindexar |
| `missing_structural` | Fuera de la vida observada o serie nunca observada |
| `not_applicable` | Dirección no definida durante viento en calma |

Una imputación aceptada usa `imputed_` seguido del método:

| Método | Aplicación |
|---|---|
| `short_linear` | Interpolación lineal de gaps cortos |
| `short_zero` | Cero entre dos anclas cero, solo para `PP` |
| `weekly_seasonal` | Media semanal escalar |
| `cross_year_analog` | Análogo anual escalar |
| `interstation_regression` | Regresión OLS escalar entre estaciones |
| `short_vector_linear` | Interpolación vectorial de viento |
| `weekly_vector_seasonal` | Media semanal vectorial |
| `cross_year_vector_analog` | Análogo anual vectorial |
| `interstation_vector_regression` | Regresión OLS vectorial de viento |

Solo `senamhi_observed` identifica observaciones originales válidas. Un campo
numérico disponible puede ser una imputación o un atípico conservado. Cero es
un valor posible, no una marca de ausencia.

`PP_IFS` tiene procedencia externa homogénea y no posee columna `SOURCE`.
No sustituye valores de `PP` ni cuenta como observación SENAMHI. Su resolución
espacial declarada por el proveedor es de 9 km; no es una medición puntual.

## Archivos complementarios

Cada producto incluye un log y un resumen de preprocesamiento:

- `<stem>_imputation_log.csv`: intentos de imputación por estación, variable y
  gap. `accepted`, `method_used`, `n_values_imputed` e `imputed_timestamps`
  identifican las contribuciones; las horas se separan con `|`. Los intentos
  rechazados incluyen `rejection_reason`. La regresión registra estación
  predictora, distancia, cobertura, pares, R² y coeficientes.
- `<stem>_summary.txt`: conteos y porcentajes por estación-variable, métodos y
  totales. `PP_IFS` se informa por separado.
- `plots/`: figuras de cobertura, series y métodos descritas en el
  [índice de evidencias](evidencias.md).

La [validación de R1.1](../validacion/r1_1/README.md) define los denominadores
de cobertura y genera tablas consultables en CSV y Markdown.
