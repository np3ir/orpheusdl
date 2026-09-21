# Install and use `orpheus URL` on Windows · Instalar y usar `orpheus URL` en Windows

**[English](#english) · [Español](#español)**

---

## English

The installer makes `orpheus` available from any PowerShell or CMD folder. You do
not need to type `python`, activate `.venv`, or install on C:. The same applies to
`abq` (best-quality-across-services downloader).

### If you already have OrpheusDL and its `.venv`

From your install folder:

```powershell
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RegisterCommandOnly
```

This mode only registers the command and writes the local install report. It does
not change settings, credentials, music or modules. If you don't have `.venv`, use
the full installer in the next section.

Close and reopen the terminal. If Windows Terminal keeps the old PATH, close the
whole app and reopen it. Then:

```powershell
orpheus "https://open.qobuz.com/track/441053229"
```

Paste a plain URL, **not** `[URL](URL)`.

### Fresh installation

Install first Python 3.13 (64-bit, including the `py` launcher), Git and FFmpeg.
They must be on PATH; check `py -3.13 --version`, `git --version` and
`ffmpeg -version`. [Links and requirements](INSTALL_WINDOWS.md#español).

In PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE/Projects" | Out-Null
Set-Location "$env:USERPROFILE/Projects"
git clone https://github.com/np3ir/orpheusdl.git
Set-Location OrpheusDL
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The installer:

1. Creates `.venv` with Python 3.13 if it doesn't exist.
2. Clones the Qobuz, Deezer, Tidal and Spotify modules from the compatible forks.
3. Installs `requirements-core.txt`, checks dependencies and CLI startup.
4. Generates `config/settings.json` if missing, with `lossless` quality and FLAC policy.
5. Keeps existing settings and validates their values. It does not copy accounts or
   paths from another computer, nor replace modules already present.
6. Adds the install to your user PATH, keeping other entries and avoiding duplicates.
   It does not use `setx` or modify the system PATH.
7. Saves the result to `config/install-report.json`, excluded from Git.

No administrator needed. `-ExecutionPolicy Bypass` affects only the installer
process, not your permanent policy. Do not use the old `install_orpheus.sh`: it is
a different installer for Termux.

### Your account and music folder

```powershell
notepad .\config\settings.json
```

Fill in each service's credentials/token as per its README and set
`global.general.download_path`, e.g. `D:/Music`. The correct quality is
**`lossless`**; `LOSELESS` does not exist. The full installer warns if it finds that
wrong quality in existing settings, without resetting them.

After opening a new terminal:

```powershell
Get-Command orpheus
orpheus --help
orpheus "https://open.qobuz.com/track/441053229"
orpheus -o D:/Music "ALBUM_OR_PLAYLIST_URL"
```

The launcher finds `orpheus.py` next to itself and prefers its `.venv`. It handles
paths with spaces and returns Python's exit code. Old installs without `.venv` keep
the `python.exe` fallback; running the full installer is recommended.

### The `abq` command

`abq.cmd` sits next to `orpheus.cmd` in the same folder (already on PATH), so
`abq "URL"` works from anywhere too. It downloads an artist/album/playlist/track as
the best FLAC across services by ISRC — see [ARTIST_BEST_QUALITY.md](ARTIST_BEST_QUALITY.md).

### Dedup and updates

Installation does not start scans or cleanups. See [indexes and dedup usage](INSTALL_WINDOWS.md#español)
and [plan, quarantine and rollback](DEDUPE_OPERATIONS.md). Do not copy SQLite
databases between computers: paths and indexes are local and locks don't coordinate
across machines.

To update, stop the processes, use the [code and module update commands](INSTALL_WINDOWS.md#español)
and run `install.ps1` again. The installer preserves existing modules; it doesn't
pull them or discard local changes.

### Common problems

- **Command not recognized:** open a brand-new terminal. Inside the install you can
  check `.\orpheus.cmd --help` without relying on PATH.
- **A different copy opens:** `Get-Command orpheus -All` shows conflicts. It doesn't
  delete other launchers or change system PATH entries.
- **Moved folder:** run the installer in the new location and remove the old one
  from PATH when you no longer use it.
- **LOSELESS:** fix `global.general.download_quality` to `lossless` and save.
- **Incomplete module:** check its folder; the installer doesn't delete it.
- **Missing FFmpeg:** check `ffmpeg -version` in a new terminal.

The `-NoUserPath` mode lets you test without writing the persistent PATH; it only
updates the current process. Automated tests use this mode.

### Verification for contributors and AI

`tests/test_windows_install.py` checks arguments, spaces, exit codes, registration
without duplicates, persistent PATH left intact during tests, and preservation of
existing config. `scripts/configure_install.py` uses atomic write and backup only to
apply values for a fresh install. Real accounts and downloads are not part of these tests.

---

## Español

El instalador deja disponible `orpheus` desde cualquier carpeta de PowerShell o
CMD. No necesitas escribir `python`, activar `.venv` ni instalar en C:. Lo mismo
aplica a `abq` (descargador al mejor FLAC entre servicios).

### Si ya tienes OrpheusDL y su entorno `.venv`

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

### Instalación nueva

Instala primero Python 3.13 de 64 bits (incluyendo el lanzador `py`), Git y FFmpeg.
Deben estar disponibles en PATH; comprueba `py -3.13 --version`, `git --version`
y `ffmpeg -version`. [Enlaces y requisitos](INSTALL_WINDOWS.md#español).

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

### Tu cuenta y carpeta de música

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

### El comando `abq`

`abq.cmd` está junto a `orpheus.cmd` en la misma carpeta (ya en el PATH), así que
`abq "URL"` también funciona desde cualquier sitio. Baja artista/álbum/playlist/track
al mejor FLAC entre servicios por ISRC — ver [ARTIST_BEST_QUALITY.md](ARTIST_BEST_QUALITY.md).

### Dedupe y actualizaciones

La instalación no inicia escaneos ni limpiezas. Consulta [índices y uso del
dedupe](INSTALL_WINDOWS.md#español) y [plan, cuarentena y
rollback](DEDUPE_OPERATIONS.md). No copies bases SQLite entre computadoras: las
rutas e índices son locales y los bloqueos no coordinan distintos equipos.

Para actualizar, termina los procesos, usa los comandos de [actualización de
código y módulos](INSTALL_WINDOWS.md#español) y vuelve a ejecutar
`install.ps1`. El instalador preserva los módulos existentes; no hace pull de
ellos ni descarta modificaciones locales.

### Problemas frecuentes

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

### Verificación para colaboradores e IA

`tests/test_windows_install.py` verifica argumentos, espacios, códigos de salida,
registro sin duplicados, PATH persistente intacto durante pruebas y preservación
de la configuración existente. `scripts/configure_install.py` usa escritura
atómica y respaldo únicamente para aplicar valores de una instalación nueva.
Las cuentas y descargas reales no forman parte de estas pruebas.
