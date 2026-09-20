# OrpheusDL (np3ir fork)

A hardened, quality-first fork of [OrpheusDL](https://github.com/bascurtiz/OrpheusDL)
(itself based on OrfiTeam/OrpheusDL) — a modular music archival tool that downloads
from Tidal, Deezer, Qobuz and Spotify.

**Windows: instala y usa `orpheus URL` desde cualquier carpeta.**

```powershell
git clone https://github.com/np3ir/orpheusdl.git
cd orpheusdl
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Requiere Git, Python 3.13 y FFmpeg. Configura tus credenciales locales y abre una
terminal nueva al terminar:

```powershell
orpheus "https://open.qobuz.com/track/441053229"
```

Para una instalación existente con `.venv`, ejecuta `git pull --ff-only` y luego
el instalador con `-RegisterCommandOnly`.
[Guía del instalador y solución de problemas](docs/WINDOWS_COMMAND.md).
[Instalación manual alternativa](docs/INSTALL_WINDOWS.md).

This fork is tuned for **building a large, clean, high-quality library automatically**,
where correctness and account safety matter more than raw speed.

> ℹ️ Los módulos de servicio viven en repos separados (ver más abajo). El código de los
> módulos **no** está incluido en este repo — clónalos dentro de `modules/`.

---

## What this fork adds

### Gentle, per-service rate limiting
A shared proactive request gate (`utils/rate_limit.py`) spaces **every** API request so the
services never see bursts (which is what gets an account flagged). Configured per service in
`config/settings.json`:

```jsonc
"modules": {
  "qobuz":  { ..., "rate_limit_rpm": 120 },   // requests per minute
  "deezer": { ..., "rate_limit_rpm": 60  },
  "tidal":  { ..., "rate_limit_rpm": 45  }
}
```

- Lower number = gentler / slower. `0` = **disabled** (no gate). A negative/invalid value
  falls back to the default so a typo can't silently drop the protection.
- Env override (one-off, wins over settings): `ORPHEUS_QOBUZ_RPM`, `ORPHEUS_DEEZER_RPM`,
  `ORPHEUS_TIDAL_RPM`.
- The gate only paces the **API/metadata** traffic — the audio itself streams from each
  service's CDN at full speed. Tidal additionally honours `Retry-After` on a 429 and has a
  run-wide 429 circuit breaker (`ORPHEUS_TIDAL_429_ABORT`).

### FLAC-only policy
`utils/audio_policy.py` (`global.codecs.flac_only`, default `true`) makes the Deezer, Qobuz and
Tidal modules refuse non-FLAC transfers, so the library stays lossless. (The Spotify fork refuses lossy track/episode transfers while FLAC-only is enabled;
metadata remains available.)

### Cross-service ISRC de-duplication
The same album reported with **different release years** by Deezer/Tidal/Qobuz used to land in
separate `(YYYY) Album` folders. `orpheus/music_downloader.py` now reuses an existing album
folder when the tracks match by **ISRC** (with a small-album-aware threshold so 1–2 track
singles dedup correctly too). Numbered `(2)/(3)` copies of the same recording (same ISRC) are
avoided.

### Crash-safe configuration
`orpheus/core.py` writes `settings.json` atomically and keeps a `settings.json.bak`, recovering
from it if the file is ever emptied/corrupted (e.g. a power loss or a concurrent run), so the
whole config is never reset to defaults.

### Tooling
| Script | What it does |
|---|---|
| `merge_dupe_albums.py` | One-time cleanup: merge existing duplicate album folders by ISRC (keeps the folder with most tracks, drops same-ISRC dups). `--dir "Z:\\"` dry-run, add `--apply`. |
| `dedup_numbered.py` | Remove `<name> (N).ext` files that are ISRC-identical to `<name>.ext`. |
| `isrc_recover.py` | Recover failed playlist tracks by ISRC across services (Deezer → Tidal). |
| `qobuz_delete_empty.py` | Delete the authenticated user's **own** empty Qobuz playlists (dry-run by default; never touches followed/editorial ones). |
| `playlist_artists.py`, `merge_artists.py`, `qobuz_playlists.py`, `list_playlists.py`, `spotify_playlists.py`, `qobuz_artists.py` | Export playlists / extract artist name+URL lists via public/app-level APIs. |

---

## Service modules (separate repos)

Each service module is its own git repository. Clone them into `modules/`. The forks below
carry this fork's rate-limit + FLAC-only changes on the `feat/rate-limit-flac-only` branch:

| Module | Repo | Branch |
|---|---|---|
| Qobuz  | [np3ir/orpheusdl-qobuz](https://github.com/np3ir/orpheusdl-qobuz)   | `feat/rate-limit-flac-only` |
| Deezer | [np3ir/OrpheusDL-deezer](https://github.com/np3ir/OrpheusDL-deezer) | `feat/rate-limit-flac-only` |
| Tidal  | [np3ir/orpheusdl-tidal](https://github.com/np3ir/orpheusdl-tidal)   | `feat/rate-limit-flac-only` |
| Spotify | [np3ir/orpheusdl-spotify](https://github.com/np3ir/orpheusdl-spotify) | `codex/flac-only` |

> The module changes import `utils.rate_limit` / `utils.audio_policy` from this repo, so they
> are meant to run **inside** an OrpheusDL checkout (not as standalone module clones).

---

## Manual installation (alternative)

Windows users can use `install.ps1` above instead of these manual steps.

```bash
git clone https://github.com/np3ir/orpheusdl.git
cd orpheusdl

# service modules (into modules/)
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-qobuz.git  modules/qobuz
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/OrpheusDL-deezer.git modules/deezer
git clone --recurse-submodules -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-tidal.git  modules/tidal
git clone -b codex/flac-only https://github.com/np3ir/orpheusdl-spotify.git modules/spotify

python -m venv .venv
# Bash on Linux/macOS (Windows commands are in the guide above):
source .venv/bin/activate
python -m pip install -r requirements-core.txt
# See the Windows guide for commands that always select the correct interpreter.

python orpheus.py <url>            # first run generates config/settings.json
```

Fill in your credentials in `config/settings.json` (git-ignored — never committed).

### Download to a different location per run
```bash
python orpheus.py -o "D:\OtherPlace" <url>
```
`-o/--output` overrides `download_path` without touching the config, so you can run two
instances to two destinations (each process has its own per-PID temp dir and its own rate gate).

---

## Configuration highlights (`config/settings.json`)

- `general.download_path` — library root (e.g. `Z:\\`).
- `general.download_quality` — `lossless`.
- `artist_downloading.merge_same_name_albums` — enables the cross-service ISRC folder reuse.
- `artist_downloading.explicit_content` — `both` downloads the clean and explicit editions.
- `codecs.flac_only` — refuse non-FLAC transfers.
- `formatting.album_format` — e.g. `{album_artist}/({release_year}) {name}`.
- `modules.<service>.rate_limit_rpm` — per-service request pacing (see above).

`config/settings.json` is git-ignored to exclude credentials from normal Git commits. It is written
atomically with a `.bak` recovery copy.

---

## Credits

- Upstream: [bascurtiz/OrpheusDL](https://github.com/bascurtiz/OrpheusDL) and
  [OrfiTeam/OrpheusDL](https://github.com/OrfiTeam/OrpheusDL).
- This fork's additions are for personal, lossless, quality-first archival use. Respect the
  terms of service of each streaming platform.

## Library-wide ISRC deduplication (2026-09-19)

The opt-in library index now coordinates synchronous/asynchronous downloads on this PC,
verifies on-disk tags, and registers only completed audio. Index refresh is explicit.
Cleanup requires a saved plan and supports a durable quarantine journal and rollback.
See [operating instructions](docs/DEDUPE_OPERATIONS.md) and
[design and limitations](docs/DEDUPE_DESIGN.md).
