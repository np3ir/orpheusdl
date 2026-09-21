# Install OrpheusDL on another computer (Windows) · Instalar OrpheusDL en otra computadora (Windows)

**[English](#english) · [Español](#español)**

---

## English

**Recommended:** use [the Windows installer](WINDOWS_COMMAND.md) to create the
environment, install the modules and make `orpheus URL` available from any folder.
The steps below are the manual alternative.

If you already followed this guide, just update and register the command:

```powershell
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RegisterCommandOnly
```

Close and reopen the terminal before using `orpheus "URL"`.

This guide installs the **np3ir** fork, its compatible modules and the ISRC tools.
You don't need to copy the previous install or its credentials.

### 1. Requirements

- Windows with Python **3.13 (64-bit)** (the version used in tests).
- Git, on PATH.
- FFmpeg, on PATH. `ffmpeg-python` is a library: it does not install the executable.
- Your own account/subscription and authentication for the services you use.

Install Python from [python.org](https://www.python.org/downloads/windows/),
Git from [git-scm.com](https://git-scm.com/downloads/win) and FFmpeg per
[its official page](https://ffmpeg.org/download.html). Reopen PowerShell and check:

```powershell
py -3.13 --version
git --version
ffmpeg -version
```

### 2. Download code and modules

Pick a new folder where you have permissions. Example:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE/Projects" | Out-Null
Set-Location "$env:USERPROFILE/Projects"
git clone https://github.com/np3ir/orpheusdl.git
Set-Location OrpheusDL

git clone -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-qobuz.git modules/qobuz
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/OrpheusDL-deezer.git modules/deezer
git clone --recurse-submodules -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-tidal.git modules/tidal
git clone -b codex/flac-only https://github.com/np3ir/orpheusdl-spotify.git modules/spotify
```

The modules live in separate repositories: cloning only OrpheusDL does not install
them. You can skip services you don't use; the full test suite needs all four. Do
not use `install_orpheus.sh` for this install: it's an old Termux installer pointing
to another repo that deletes an existing folder.

### 3. Independent Python environment

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-core.txt
.\.venv\Scripts\python.exe orpheus.py --help
```

The commands call the environment's Python directly; no need to activate scripts or
change PowerShell's execution policy. `requirements-core.txt` covers the CLI
described here. The original `requirements.txt` includes wider optional integrations
not needed for this basic install. Don't mix both files in the same environment: the
CLI profile pins the librespot/protobuf versions the Spotify module needs at startup.

### 4. Create and configure the settings

```powershell
.\.venv\Scripts\python.exe orpheus.py settings refresh
notepad config/settings.json
```

The first run may ask to update the config and exit; that's normal. The file is
generated with the installed modules' settings and fields. Fill in each service's
authentication as per its README; each provider uses a different mechanism. Don't
publish tokens, passwords or session files.

In the `global` object, adjust the existing keys; do NOT replace the whole file with
this fragment:

```json
{
  "general": {
    "download_path": "D:/Music",
    "download_quality": "lossless",
    "isrc_library_dedup": true,
    "isrc_library_upgrade": false
  },
  "codecs": {
    "flac_only": true
  }
}
```

Use a real folder on that computer. For a NAS, connect the drive first and check
permissions. `Z:` is only an example, not a drive the program creates. Spotify is
for metadata when FLAC-only is on: it does not convert lossy audio to FLAC. Rate
limits are per process; they don't avoid every 429.

### 5. Prepare the index and download

Create the music folder before building the index. Example:

```powershell
New-Item -ItemType Directory -Force D:/Music | Out-Null
.\.venv\Scripts\python.exe isrc_index_tool.py --dir D:/Music --build --workers 8
.\.venv\Scripts\python.exe orpheus.py -o D:/Music "ALBUM_OR_PLAYLIST_URL"
```

Replace the URL text with a real URL. An empty index is also fine: finished
downloads are added. If you already have music, the first scan may take a while. If
you download to two destinations, build one index per destination. On another
computer rebuild the index: don't copy the local databases with old paths. Instances
on different PCs don't share the local locks.

### 6. Clean up existing duplicates

Generate a plan first. It doesn't modify music:

```powershell
.\.venv\Scripts\python.exe isrc_dedupe.py --dir D:/Music --workers 8 --out config/plan_music.csv
```

After reviewing KEEP/MOVE/REVIEW, apply the plan:

```powershell
.\.venv\Scripts\python.exe isrc_dedupe.py --dir D:/Music --apply --plan config/plan_music.json
```

To undo:

```powershell
.\.venv\Scripts\python.exe isrc_dedupe.py --dir D:/Music --rollback config/plan_music.journal.jsonl
```

Details and limits: [operations](DEDUPE_OPERATIONS.md) and [design](DEDUPE_DESIGN.md).

### 7. Update or verify

With processes stopped and no pending local changes:

```powershell
git pull --ff-only
git -C modules/qobuz pull --ff-only
git -C modules/deezer pull --ff-only
git -C modules/tidal pull --ff-only
git -C modules/tidal submodule update --init --recursive
git -C modules/spotify pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements-core.txt
```

Optional tests (need all four modules):

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

`config/`, downloads, indexes and local backups are not part of normal commits.
Avoid `git add -f` on them. The verified commands don't replace an authentication
and download test on your new computer.

### `abq` command

Both `orpheus` and `abq` are registered by the installer (the launchers sit in the
install folder, which is added to PATH). `abq` downloads an artist/album/playlist/track
as the best FLAC across services by ISRC — see [ARTIST_BEST_QUALITY.md](ARTIST_BEST_QUALITY.md).

---

## Español

**Opción recomendada:** usa [el instalador de Windows](WINDOWS_COMMAND.md) para
crear el entorno, instalar los módulos y dejar disponible `orpheus URL` desde
cualquier carpeta. Los pasos siguientes son la alternativa manual.

Si ya seguiste esta guía, basta con actualizar y registrar el comando:

```powershell
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RegisterCommandOnly
```

Cierra y abre la terminal antes de usar `orpheus "URL"`.

Esta guía instala el fork de **np3ir**, sus módulos compatibles y las herramientas
ISRC. No hace falta copiar la instalación anterior ni sus credenciales.

### 1. Requisitos

- Windows con Python **3.13 de 64 bits** (versión usada en las pruebas).
- Git, disponible en PATH.
- FFmpeg, disponible en PATH. `ffmpeg-python` es una biblioteca: no instala el ejecutable.
- Cuenta/suscripción y autenticación propias para los servicios que utilices.

Instala Python desde [python.org](https://www.python.org/downloads/windows/),
Git desde [git-scm.com](https://git-scm.com/downloads/win) y FFmpeg siguiendo
[su página oficial](https://ffmpeg.org/download.html). Abre PowerShell de nuevo y comprueba:

```powershell
py -3.13 --version
git --version
ffmpeg -version
```

### 2. Descargar código y módulos

Elige una carpeta nueva donde tengas permisos. Ejemplo:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE/Projects" | Out-Null
Set-Location "$env:USERPROFILE/Projects"
git clone https://github.com/np3ir/orpheusdl.git
Set-Location OrpheusDL

git clone -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-qobuz.git modules/qobuz
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/OrpheusDL-deezer.git modules/deezer
git clone --recurse-submodules -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-tidal.git modules/tidal
git clone -b codex/flac-only https://github.com/np3ir/orpheusdl-spotify.git modules/spotify
```

Los módulos están en repositorios separados: clonar solo OrpheusDL no los instala.
Puedes omitir servicios que no uses; la suite completa de pruebas requiere los cuatro.
No uses `install_orpheus.sh` para esta instalación: es un instalador antiguo de Termux
que apunta a otro repositorio y elimina una carpeta existente.

### 3. Entorno Python independiente

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-core.txt
.\.venv\Scripts\python.exe orpheus.py --help
```

Los comandos llaman directamente al Python del entorno; no requieren activar scripts
ni cambiar la política de ejecución de PowerShell. `requirements-core.txt` cubre la
CLI descrita aquí. El `requirements.txt` original incluye integraciones y componentes
opcionales más amplios que no son necesarios para esta instalación básica.
No mezcles ambos archivos en el mismo entorno: el perfil CLI fija las versiones
compatibles de librespot y protobuf que necesita el módulo Spotify al arrancar.

### 4. Crear y configurar los ajustes

```powershell
.\.venv\Scripts\python.exe orpheus.py settings refresh
notepad config/settings.json
```

La primera ejecución puede pedir actualizar la configuración y salir; es normal.
El archivo se genera con los ajustes y campos de los módulos instalados. Completa
los campos de autenticación del servicio siguiendo su README; cada proveedor usa
un mecanismo distinto. No publiques tokens, contraseñas o archivos de sesión.

En el objeto `global`, ajusta las claves existentes; NO reemplaces todo el archivo
por este fragmento:

```json
{
  "general": {
    "download_path": "D:/Music",
    "download_quality": "lossless",
    "isrc_library_dedup": true,
    "isrc_library_upgrade": false
  },
  "codecs": {
    "flac_only": true
  }
}
```

Usa una carpeta real de esa computadora. Si usas NAS, conecta primero la unidad y
comprueba permisos. `Z:` es solo un ejemplo, no una unidad que el programa cree.
Spotify sirve para metadatos cuando FLAC-only está activo: no convierte audio con
pérdida en FLAC. Los límites de peticiones son por proceso; no evitan todos los 429.

### 5. Preparar el índice y descargar

Crea la carpeta de música antes de construir el índice. Ejemplo:

```powershell
New-Item -ItemType Directory -Force D:/Music | Out-Null
.\.venv\Scripts\python.exe isrc_index_tool.py --dir D:/Music --build --workers 8
.\.venv\Scripts\python.exe orpheus.py -o D:/Music "URL_DEL_ALBUM_O_PLAYLIST"
```

Sustituye el texto de la URL por una URL real. Un índice vacío también queda listo:
las descargas terminadas se añadirán. Si ya hay música, el primer escaneo puede tardar.
Si descargas a dos destinos, construye un índice para cada uno. En otra computadora
reconstruye el índice: no copies las bases de datos locales con rutas antiguas.
Las instancias de distintos PCs no comparten los bloqueos locales.

### 6. Limpiar duplicados existentes

Genera primero un plan. No modifica música:

```powershell
.\.venv\Scripts\python.exe isrc_dedupe.py --dir D:/Music --workers 8 --out config/plan_music.csv
```

Después de revisar KEEP/MOVE/REVIEW, aplica el plan:

```powershell
.\.venv\Scripts\python.exe isrc_dedupe.py --dir D:/Music --apply --plan config/plan_music.json
```

Para deshacer:

```powershell
.\.venv\Scripts\python.exe isrc_dedupe.py --dir D:/Music --rollback config/plan_music.journal.jsonl
```

Detalles y límites: [operación](DEDUPE_OPERATIONS.md) y [diseño](DEDUPE_DESIGN.md).

### 7. Actualizar o verificar

Con los procesos terminados y sin cambios locales pendientes:

```powershell
git pull --ff-only
git -C modules/qobuz pull --ff-only
git -C modules/deezer pull --ff-only
git -C modules/tidal pull --ff-only
git -C modules/tidal submodule update --init --recursive
git -C modules/spotify pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements-core.txt
```

Pruebas opcionales (requieren los cuatro módulos):

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

`config/`, descargas, índices y respaldos locales no se incluyen en los commits
normales. Evita `git add -f` sobre ellos. Los comandos verificados no sustituyen
una prueba de autenticación y descarga en tu nueva computadora.

### Comando `abq`

El instalador registra tanto `orpheus` como `abq` (los lanzadores están en la
carpeta de instalación, que se añade al PATH). `abq` baja artista/álbum/playlist/track
al mejor FLAC entre servicios por ISRC — ver [ARTIST_BEST_QUALITY.md](ARTIST_BEST_QUALITY.md).

### Validación de esta guía

Comprobada el 19 de septiembre de 2026 en Windows y Python 3.13, con un entorno
virtual nuevo, sin vendor/, sin credenciales y con los cuatro módulos clonados.
Se comprueban ayuda de CLI, creación de settings.json, `pip check` y las pruebas
sin descargar música. La autenticación de cada cuenta se realiza en el equipo nuevo.
