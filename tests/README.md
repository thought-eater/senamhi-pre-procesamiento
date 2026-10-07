# Pruebas de software

Desde la raíz del repositorio, con las dependencias instaladas:

```bash
uv run --frozen python -m unittest discover -s tests/software -v
```

Estas pruebas unitarias y de integración utilizan series sintéticas, respuestas
simuladas y directorios temporales:

- `test_preprocesamiento.py`: calidad, cascada, viento, procedencia y publicación.
- `test_integrar_precipitacion_ifs.py`: solicitudes y validación IFS con mocks.
- `test_validacion_r1_1.py`: detección de inconsistencias en el verificador.

No descargan datos ni sustituyen la [verificación sobre los datasets reales](../validacion/r1_1/README.md).
Un resultado satisfactorio demuestra comportamiento del software, no exactitud
empírica de las imputaciones.
