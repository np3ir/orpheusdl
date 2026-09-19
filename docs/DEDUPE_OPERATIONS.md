# Operación de dedupe ISRC

Los siguientes comandos se ejecutan en PowerShell desde C:\OrpheusDL.
Usar rutas con barra final `/` en los argumentos evita problemas de escape con
programas nativos de Windows. Son ejemplos: no se ejecutaron sobre tu biblioteca.

## 1. Actualizar índice cuando haya cambios externos

```powershell
python isrc_index_tool.py --dir Z:/ --build --workers 8
```

Puede tardar en un NAS grande. No se ejecuta automáticamente en cada descarga.
Los índices ya construidos siguen siendo utilizables. Para consultar solo los
registros guardados:

```powershell
python isrc_index_tool.py --dir Z:/ --report --out config/dupes_Z_actual.csv
```

## 2. Crear un plan sin mover música

```powershell
python isrc_dedupe.py --dir Z:/ --workers 8 --out config/plan_Z_nuevo.csv
```

Genera `plan_Z_nuevo.csv` y `plan_Z_nuevo.json`. No sobreescribe un plan existente.
KEEP identifica la copia conservada, MOVE las candidatas y REVIEW los grupos
ambiguos que no serán movidos. Revisar el CSV antes de aplicar.
Repetir el plan con otro nombre si hubo cambios importantes en disco.

## 3. Aplicar el plan revisado

```powershell
python isrc_dedupe.py --dir Z:/ --apply --plan config/plan_Z_nuevo.json
```

Escribe `config/plan_Z_nuevo.journal.jsonl` y mueve a
`Z:/_DUPLICADOS/<id-del-plan>/...`. Conserva la ruta relativa original.
El código de salida 1 indica errores para revisión; 0 indica operación sin errores
reportados. No hay borrado permanente. Para reanudar, repetir el mismo comando.
No ejecutar descargadores antiguos sobre Z: durante esta operación.

## 4. Deshacer

```powershell
python isrc_dedupe.py --dir Z:/ --rollback config/plan_Z_nuevo.journal.jsonl
```

Restaura solo archivos cuya evidencia siga coincidiendo. Nunca pisa una ruta
ocupada. Se puede repetir tras una interrupción. Después de un rollback, generar
un plan nuevo para otra limpieza; no reutilizar el plan aplicado y revertido.
Guardar el JSON y journal hasta terminar la revisión.

## Descargas nuevas

La opción actual `global.general.isrc_library_dedup: true` habilita la prevención.
Las ejecuciones nuevas usan ambos caminos de descarga y comparten el índice.
Las ejecuciones ya abiertas mantienen su código anterior: cerrar/reiniciar de
forma normal para que todas cooperen con los nuevos bloqueos.

`global.general.isrc_library_upgrade: true` es opcional. Permite conservar una
nueva copia lossless de mejores características anunciadas, sin eliminar la vieja.
Por defecto permanece false. No confundir esta opción con la limpieza: el plan
siempre elige la mejor evidencia disponible en disco para los grupos compatibles.

## Verificación local

```powershell
python -m pytest tests -q -p no:cacheprovider
```

Detalles de implementación, riesgos conocidos y backup del código:
[DEDUPE_DESIGN.md](DEDUPE_DESIGN.md).
