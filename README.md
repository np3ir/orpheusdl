# OrpheusDL (np3ir fork)

Download music in **FLAC (top quality)** from **Tidal, Deezer, Qobuz and Spotify**.
Paste a link and it downloads. With one extra command (`abq`) it downloads a whole
**artist, album, playlist or track**, picking — track by track — the service with
the **best FLAC**.

**[English](#english) · [Español](#español)**

> You need your own accounts for those services (a subscription for hi-res).
> Necesitas tus propias cuentas de esos servicios (con suscripción para el hi-res).

---

## English

### 1) Install on Windows (step by step)

**Step A — install 3 programs** (once). Easiest: open **PowerShell** and paste:

```powershell
winget install Git.Git Python.Python.3.13 Gyan.FFmpeg
```

When it finishes, **close and reopen PowerShell** so it picks up what you installed.

> No `winget`? Download them by hand: [Git](https://git-scm.com/download/win),
> [Python 3.13](https://www.python.org/downloads/) (tick *"Add python.exe to PATH"*),
> [FFmpeg](https://www.gyan.dev/ffmpeg/builds/).

**Step B — download and install OrpheusDL.** Paste this in PowerShell:

```powershell
git clone https://github.com/np3ir/orpheusdl.git
cd orpheusdl
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

When it says **"Ready"**, **open a new terminal** and the commands work from any folder.

### 2) Add your accounts

Open the config file and fill in your details for each service:

```powershell
notepad "$HOME\orpheusdl\config\settings.json"
```

Find the `"modules"` section and fill in the user/password or tokens for Qobuz,
Tidal, Deezer and/or Spotify. Save and close. (That file is private and is **not**
uploaded to GitHub.)

### 3) Use

**`orpheus`** — download whatever the link points to (track, album, playlist or artist):

```powershell
orpheus "https://open.qobuz.com/track/441053229"
```

**`abq`** — download an **artist, album, playlist or track** as the best FLAC,
searching all three services by ISRC (it picks, per track, where the best quality is):

```powershell
abq "https://tidal.com/artist/10411"           # a whole artist
abq "https://www.deezer.com/album/1603029"     # an album
abq "https://www.deezer.com/playlist/123456"   # a playlist
abq "https://tidal.com/browse/track/96594261"  # a single track
```

Tip: add `--dry` to **preview** what it would download, without downloading anything.

Choose quality (optional): `-q best` (default, the best) · `-q 24` (hi-res) · `-q 16` (CD).
Full `abq` guide: [docs/ARTIST_BEST_QUALITY.md](docs/ARTIST_BEST_QUALITY.md).

Everything is saved to the folder in `download_path` (in the config). You can stop
with **Ctrl-C** anytime; running it again skips what's already downloaded.

### Update to the latest version

```powershell
cd $HOME\orpheusdl
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RegisterCommandOnly
```

### If something doesn't work

- **"orpheus/abq not recognized"** → open a **new terminal** (or restart Windows Terminal).
- Put the URL **in quotes** and without brackets: `"https://..."`, not `[text](url)`.
- Install **FFmpeg** and open a new terminal if it asks for it.
- More help: [docs/WINDOWS_COMMAND.md](docs/WINDOWS_COMMAND.md) ·
  [manual install](docs/INSTALL_WINDOWS.md).

---

## Español

### 1) Instalar en Windows (paso a paso)

**Paso A — Instala 3 programas** (una sola vez). Lo más fácil: abre **PowerShell** y pega:

```powershell
winget install Git.Git Python.Python.3.13 Gyan.FFmpeg
```

Cuando termine, **cierra y vuelve a abrir PowerShell** (para que reconozca lo instalado).

> ¿No tienes `winget`? Descárgalos a mano: [Git](https://git-scm.com/download/win),
> [Python 3.13](https://www.python.org/downloads/) (marca *"Add python.exe to PATH"*),
> [FFmpeg](https://www.gyan.dev/ffmpeg/builds/).

**Paso B — Descarga e instala OrpheusDL.** Pega esto en PowerShell:

```powershell
git clone https://github.com/np3ir/orpheusdl.git
cd orpheusdl
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Al terminar dirá **"Ready"**. **Abre una terminal nueva** y ya puedes usar los comandos
desde cualquier carpeta.

### 2) Poner tus cuentas

Abre el archivo de configuración y escribe tus datos de cada servicio:

```powershell
notepad "$HOME\orpheusdl\config\settings.json"
```

Busca la sección `"modules"` y rellena usuario/contraseña o tokens de Qobuz, Tidal,
Deezer y/o Spotify. Guarda y cierra. (Ese archivo es privado y **no** se sube a GitHub.)

### 3) Usar

**`orpheus`** — baja lo que sea del enlace (canción, álbum, playlist o artista):

```powershell
orpheus "https://open.qobuz.com/track/441053229"
```

**`abq`** — baja **artista, álbum, playlist o track** al mejor FLAC, buscando en los
3 servicios por ISRC (elige, tema por tema, dónde está la mejor calidad):

```powershell
abq "https://tidal.com/artist/10411"           # artista completo
abq "https://www.deezer.com/album/1603029"     # un álbum
abq "https://www.deezer.com/playlist/123456"   # una playlist
abq "https://tidal.com/browse/track/96594261"  # un solo tema
```

Truco: añade `--dry` para **ver primero** qué bajaría, sin descargar nada.

Elegir calidad (opcional): `-q best` (por defecto, la mejor) · `-q 24` (hi-res) · `-q 16` (CD).
Guía completa de `abq`: [docs/ARTIST_BEST_QUALITY.md](docs/ARTIST_BEST_QUALITY.md).

Todo se guarda en la carpeta que pusiste en `download_path` (en la config).
Puedes cortar con **Ctrl-C** cuando quieras; al volver a lanzarlo salta lo ya descargado.

### Actualizar a la última versión

```powershell
cd $HOME\orpheusdl
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RegisterCommandOnly
```

### Si algo no funciona

- **"orpheus/abq no se reconoce"** → abre una **terminal nueva** (o reinicia Windows Terminal).
- Pega la URL **entre comillas** y sin corchetes: `"https://..."`, no `[texto](url)`.
- Instala **FFmpeg** y ábrelo en una terminal nueva si te lo pide.
- Más ayuda: [docs/WINDOWS_COMMAND.md](docs/WINDOWS_COMMAND.md) ·
  [instalación manual](docs/INSTALL_WINDOWS.md).

---

<details>
<summary><b>Technical details (advanced) · Detalles técnicos</b></summary>

A hardened, quality-first fork of [OrpheusDL](https://github.com/bascurtiz/OrpheusDL)
(based on OrfiTeam/OrpheusDL), tuned for **building a large, clean, high-quality
library automatically**, where correctness and account safety matter more than speed.

### What this fork adds
- **Per-service rate limiting** (`utils/rate_limit.py`, `modules.<svc>.rate_limit_rpm`):
  spaces API requests so an account is not flagged. `0` = disabled. Only metadata is
  paced; audio streams at full speed.
- **FLAC-only** (`global.codecs.flac_only`, default `true`): the modules refuse
  non-FLAC transfers.
- **Cross-service ISRC de-duplication**: reuses an album folder when tracks match by
  ISRC (avoids `(2)/(3)` copies of the same recording).
- **Library-wide ISRC index** (opt-in): see [docs/DEDUPE_OPERATIONS.md](docs/DEDUPE_OPERATIONS.md)
  and [docs/DEDUPE_DESIGN.md](docs/DEDUPE_DESIGN.md).
- **Crash-safe config**: `settings.json` is written atomically with a `.bak` copy.
- **`artist_best_quality.py` (command `abq`)**: any Tidal/Deezer/Qobuz link
  (artist/album/playlist/track) → best FLAC across the three services by ISRC.
  Permanent options in `settings.json → global.artist_best_quality`. See
  [docs/ARTIST_BEST_QUALITY.md](docs/ARTIST_BEST_QUALITY.md).

### The two commands
`orpheus.cmd` and `abq.cmd` live in the install folder, which the installer adds to
PATH, so both commands work from anywhere. On Linux/macOS use the equivalents
`python orpheus.py <url>` / `python artist_best_quality.py <url>`.

### Service modules (separate repos)
The installer clones them into `modules/` automatically. For a manual install:

| Module | Repo | Branch |
|---|---|---|
| Qobuz  | [np3ir/orpheusdl-qobuz](https://github.com/np3ir/orpheusdl-qobuz)   | `feat/rate-limit-flac-only` |
| Deezer | [np3ir/OrpheusDL-deezer](https://github.com/np3ir/OrpheusDL-deezer) | `feat/rate-limit-flac-only` |
| Tidal  | [np3ir/orpheusdl-tidal](https://github.com/np3ir/orpheusdl-tidal)   | `feat/rate-limit-flac-only` |
| Spotify| [np3ir/orpheusdl-spotify](https://github.com/np3ir/orpheusdl-spotify) | `codex/flac-only` |

### Manual installation (Linux/macOS or without `install.ps1`)
```bash
git clone https://github.com/np3ir/orpheusdl.git
cd orpheusdl
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-qobuz.git  modules/qobuz
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/OrpheusDL-deezer.git modules/deezer
git clone --recurse-submodules -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-tidal.git modules/tidal
git clone -b codex/flac-only https://github.com/np3ir/orpheusdl-spotify.git modules/spotify
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-core.txt
python orpheus.py <url>            # the first run creates config/settings.json
```

### Key configuration (`config/settings.json`)
- `general.download_path` — library root (e.g. `Z:\`).
- `general.download_quality` — `lossless`.
- `codecs.flac_only` — refuse non-FLAC transfers.
- `global.artist_best_quality` — `abq` options (`default_quality`, `prefer_order`, …).
- `modules.<service>.rate_limit_rpm` — per-service request pacing.

### Credits
Upstream: [bascurtiz/OrpheusDL](https://github.com/bascurtiz/OrpheusDL) and
[OrfiTeam/OrpheusDL](https://github.com/OrfiTeam/OrpheusDL). For personal, lossless,
quality-first archival use — respect each platform's terms of service.

</details>
