# Instalar OrpheusDL en otra computadora (Windows)

Esta guía instala el fork de **np3ir**, sus módulos compatibles y las herramientas
ISRC. No hace falta copiar la instalación anterior ni sus credenciales.

## 1. Requisitos

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

## 2. Descargar código y módulos

Elige una carpeta nueva donde tengas permisos. Ejemplo:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE/Projects" | Out-Null
Set-Location "$env:USERPROFILE/Projects"
git clone https://github.com/np3ir/OrpheusDL.git
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

## 3. Entorno Python independiente

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

## 4. Crear y configurar los ajustes

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

## 5. Preparar el índice y descargar

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

## 6. Limpiar duplicados existentes

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

## 7. Actualizar o verificar

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

## Validación de esta guía

Comprobada el 19 de septiembre de 2026 en Windows y Python 3.13, con un entorno
virtual nuevo, sin vendor/, sin credenciales y con los cuatro módulos clonados.
Se comprueban ayuda de CLI, creación de settings.json, `pip check` y las 107 pruebas
sin descargar música. La autenticación de cada cuenta se realiza en el equipo nuevo.
