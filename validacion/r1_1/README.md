# Validación del resultado R1.1

**Resultado:** dos datasets separados, curados y georreferenciados de PM2.5 y
meteorología SENAMHI. **Medios de verificación:** archivos de datos, diccionario,
informe de preprocesamiento y código de adquisición y carga.

Este directorio contiene la verificación reproducible sobre los archivos reales
de la tesis. Las pruebas unitarias y de integración con datos sintéticos están
en [`tests/software/`](../../tests/software/).

## Ejecución

Desde la raíz del repositorio, con Python ≥ 3.10:

```bash
python -m validacion.r1_1.verificar_datasets --fecha-referencia 2026-10-07
```

También puede utilizarse `uv run --frozen python` en el entorno del proyecto.
El verificador usa la biblioteca estándar, lee las entradas y productos
existentes y no descarga datos ni ejecuta la imputación.

Por defecto busca los CSV brutos en los dos repositorios hermanos indicados en
la [guía de reproducción](../../docs/reproduccion.md), y los finales y logs en
`resultados/pm25/` y `resultados/meteo/`. Opciones:

- `--pm25-bruto CSV` y `--meteo-bruto CSV`: otras ubicaciones de los brutos.
- `--fecha-referencia YYYY-MM-DD`: fecha obligatoria para evaluar antigüedad.
- `--salida DIRECTORIO`: destino del informe; por defecto este `resultados/`.

El código de salida es 0 si cumplen las comprobaciones automáticas y 1 si
alguna incumple. La revisión del asesor se registra como pendiente documental
y no queda acreditada por el código de salida.

## Indicadores y alcance de las comprobaciones

| Criterio | Operacionalización | Evidencia |
|---|---|---|
| ≥ 6 meses consecutivos | Grilla completa del periodo de estudio y ≥ 6 meses calendario consecutivos con al menos una observación válida por variable en alguna estación | Informe y cobertura mensual |
| Antigüedad ≤ 10 años | Todas las fechas entre la fecha de referencia menos diez años y la fecha de referencia | Comprobación de antigüedad |
| Coordenadas | Latitud/longitud finitas dentro de sus dominios, metadatos constantes por estación y coordenadas coincidentes con el bruto | Catálogo de estaciones |
| Frecuencia definida | Clave única, horas válidas, orden cronológico y separación de una hora en cada estación | Comprobación de periodo y frecuencia |
| Faltantes reportados | Conteos y porcentajes por variable, estación y mes | Tablas de cobertura |
| Criterios documentados | Documentación pública disponible y trazabilidad de estados y logs | Metodología, diccionario e informe |
| Revisión del asesor | Requiere constancia documental de revisión | Pendiente documental |

El periodo de referencia del estudio es enero de 2025–junio de 2026:
13 104 horas por estación. El criterio de presencia mensual es una interpretación
operativa explícita del IOV; no equivale a exigir cobertura del 100 % ni a
certificar seis meses completos para todas las estaciones. Los porcentajes
mensuales permiten examinar esa diferencia sin ocultar series ausentes.

Se verifica además que las observaciones y los atípicos declarados correspondan
a los brutos (tolerancia absoluta 1e-9; 0° y 360° son equivalentes para dirección), que las claves
brutas se conserven y que cada celda imputada tenga exactamente una contribución
coherente en el log, con la misma estación, variable, hora y método.

El esquema separa las ramas y mantiene `PP_IFS` sin `SOURCE`. Su disponibilidad
se informa de forma independiente. Las coordenadas válidas numéricamente no
constituyen una certificación institucional de ubicación o datum.

## Cálculos de cobertura

- **Aplicables:** todas las celdas excepto `not_applicable` (dirección en calma).
- **Observaciones:** celdas `senamhi_observed`.
- **Atípicos:** celdas `senamhi_operational_outlier`, conservadas por el productor.
- **Imputados:** celdas con estados `imputed_*` admitidos para la variable.
- **Ausencias antes:** aplicables menos observaciones y atípicos. Se reconstruyen
  desde la procedencia final; representan ausencia analítica previa a imputar,
  incluyendo inválidos duros, no solo campos vacíos del CSV bruto.
- **Ausencias finales:** aplicables menos valores disponibles.
- **Cobertura final:** 100 × disponibles / aplicables.

Cuando no hay celdas aplicables, el porcentaje queda vacío. Para `PP_IFS`, las
observaciones SENAMHI e imputaciones no aplican; su ausencia antes y después
es la de la columna externa publicada. Los porcentajes se redondean a dos
decimales. La cobertura mensual observada usa todas las horas calendario del
mes como denominador, incluida la calma.

## Evidencia generada

- [`informe.md`](resultados/informe.md): resumen legible y estado de criterios.
- [`tabla_11.csv`](resultados/tabla_11.csv): cobertura global por variable,
  contrastada con [`referencia_tabla_11.csv`](referencia_tabla_11.csv).
- [`cobertura_estacion.csv`](resultados/cobertura_estacion.csv) y
  [`cobertura_mensual.csv`](resultados/cobertura_mensual.csv): desagregaciones.
- [`estaciones.csv`](resultados/estaciones.csv): coordenadas y filas por estación.
- [`metodos.csv`](resultados/metodos.csv): imputaciones por método y serie.
- [`comprobaciones.csv`](resultados/comprobaciones.csv): criterios y decisiones.
- [`manifiesto.json`](resultados/manifiesto.json): hashes SHA-256 de entradas,
  productos y archivos del protocolo, con fecha de referencia y versión Python.

Una discrepancia con la tabla de referencia se reporta; no se corrigen datos
para forzar coincidencia. La verificación no es un backtesting de exactitud,
no vuelve a evaluar donantes internos y no acredita aprobación del asesor.
