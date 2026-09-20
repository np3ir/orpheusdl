# Instalar y usar `orpheus URL` en Windows

El instalador deja disponible `orpheus` desde cualquier carpeta de PowerShell o
CMD. No necesitas escribir `python`, activar `.venv` ni instalar en C:.

## Si ya tienes OrpheusDL y su entorno `.venv`

Desde tu carpeta de instalación:

```powershell
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RegisterCommandOnly
```

Este modo solo registra el comando y escribe el informe local de instalación.
No cambia ajustes, credenciales, música ni módulos. Si no tienes `.venv`, usa el
instalador completo del siguiente apartado.

Cierra y abre la terminal. Si Windows Terminal conserva el PATH anterior, cierra
toda la aplicación y vuelve a abrirla. Después:

```powershell
orpheus "https://open.qobuz.com/track/441053229"
```

Pega una URL normal, **no** `[URL](URL)`.

## Instalación nueva

Instala primero Python 3.13 de 64 bits (incluyendo el lanzador `py`), Git y FFmpeg.
Deben estar disponibles en PATH; comprueba `py -3.13 --version`, `git --version`
y `ffmpeg -version`. [Enlaces y requisitos](INSTALL_WINDOWS.md#1-requisitos).

En PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE/Projects" | Out-Null
Set-Location "$env:USERPROFILE/Projects"
git clone https://github.com/np3ir/orpheusdl.git
Set-Location OrpheusDL
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

El instalador:

1. Crea `.venv` con Python 3.13 si no existe.
2. Clona los módulos Qobuz, Deezer, Tidal y Spotify desde los forks compatibles.
3. Instala `requirements-core.txt`, comprueba dependencias y arranque de la CLI.
4. Genera `config/settings.json` si falta, con calidad `lossless` y política FLAC.
5. Conserva los ajustes existentes y comprueba sus valores. No copia cuentas ni
   rutas de otra computadora, ni reemplaza módulos ya presentes.
6. Agrega la instalación al PATH de tu usuario conservando las demás entradas y
   evitando duplicados. No usa `setx` ni modifica el PATH del sistema.
7. Guarda el resultado en `config/install-report.json`, excluido de Git.

No requiere administrador. `-ExecutionPolicy Bypass` afecta solo al proceso del
instalador, no a tu política permanente. No uses el antiguo `install_orpheus.sh`:
es un instalador distinto para Termux.

## Tu cuenta y carpeta de música

```powershell
notepad .\config\settings.json
```

Completa las credenciales/token de cada servicio según su README y configura
`global.general.download_path`, por ejemplo `D:/Music`.
La calidad correcta es **`lossless`**; `LOSELESS` no existe. El instalador completo
avisa si encuentra esa calidad incorrecta en ajustes existentes, sin resetearlos.

Después de abrir una terminal nueva:

```powershell
Get-Command orpheus
orpheus --help
orpheus "https://open.qobuz.com/track/441053229"
orpheus -o D:/Music "URL_DEL_ALBUM_O_PLAYLIST"
```

El lanzador localiza `orpheus.py` junto a sí mismo y prefiere su `.venv`. Admite
rutas con espacios y devuelve el código de salida de Python. Instalaciones
antiguas sin `.venv` conservan el fallback a `python.exe`; se recomienda ejecutar
el instalador completo.

## Dedupe y actualizaciones

La instalación no inicia escaneos ni limpiezas. Consulta [índices y uso del
dedupe](INSTALL_WINDOWS.md#5-preparar-el-índice-y-descargar) y [plan, cuarentena y
rollback](DEDUPE_OPERATIONS.md). No copies bases SQLite entre computadoras: las
rutas e índices son locales y los bloqueos no coordinan distintos equipos.

Para actualizar, termina los procesos, usa los comandos de [actualización de
código y módulos](INSTALL_WINDOWS.md#7-actualizar-o-verificar) y vuelve a ejecutar
`install.ps1`. El instalador preserva los módulos existentes; no hace pull de
ellos ni descarta modificaciones locales.

## Problemas frecuentes

- **Comando no reconocido:** abre una terminal completamente nueva. Dentro de
  la instalación puedes comprobar `.\orpheus.cmd --help` sin depender del PATH.
- **Se abre otra copia:** `Get-Command orpheus -All` muestra los conflictos. No se
  borran lanzadores ajenos ni se cambian entradas del PATH del sistema.
- **Carpeta movida:** ejecuta el instalador en la nueva ubicación y retira del
  PATH la ubicación anterior cuando ya no la uses.
- **LOSELESS:** corrige `global.general.download_quality` a `lossless` y guarda.
- **Módulo incompleto:** revisa su carpeta; el instalador no la elimina.
- **FFmpeg ausente:** comprueba `ffmpeg -version` en una terminal nueva.

El modo `-NoUserPath` permite probar sin escribir el PATH persistente; solo
actualiza el proceso actual. Las pruebas automatizadas usan este modo.

## Verificación para colaboradores e IA

`tests/test_windows_install.py` verifica argumentos, espacios, códigos de salida,
registro sin duplicados, PATH persistente intacto durante pruebas y preservación
de la configuración existente. `scripts/configure_install.py` usa escritura
atómica y respaldo únicamente para aplicar valores de una instalación nueva.
Las cuentas y descargas reales no forman parte de estas pruebas.
