# OrpheusDL (fork np3ir)

Descarga música en **FLAC (máxima calidad)** desde **Tidal, Deezer, Qobuz y Spotify**.
Pegas un enlace y lo baja. Con un comando extra (`abq`) baja **toda la discografía de
un artista** eligiendo, tema por tema, el servicio que tenga el **mejor FLAC**.

> Necesitas tus propias cuentas de esos servicios (con suscripción para el hi-res).

---

## 1) Instalar en Windows (paso a paso)

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

---

## 2) Poner tus cuentas

Abre el archivo de configuración y escribe tus datos de cada servicio:

```powershell
notepad "$HOME\orpheusdl\config\settings.json"
```

Busca la sección `"modules"` y rellena usuario/contraseña o tokens de Qobuz, Tidal,
Deezer y/o Spotify. Guarda y cierra. (Ese archivo es privado y **no** se sube a GitHub.)

---

## 3) Usar

**`orpheus`** — baja lo que sea del enlace (canción, álbum, playlist o artista):

```powershell
orpheus "https://open.qobuz.com/track/441053229"
```

**`abq`** — baja el **artista completo** al mejor FLAC, buscando en los 3 servicios por ISRC:

```powershell
abq "https://tidal.com/artist/10411"
```

Truco: añade `--dry` para **ver primero** qué bajaría, sin descargar nada:

```powershell
abq "https://tidal.com/artist/10411" --dry
```

Elegir calidad (opcional): `-q best` (por defecto, la mejor) · `-q 24` (hi-res) · `-q 16` (CD).
Guía completa de `abq`: [docs/ARTIST_BEST_QUALITY.md](docs/ARTIST_BEST_QUALITY.md).

Todo se guarda en la carpeta que pusiste en `download_path` (por defecto en la config).
Puedes cortar con **Ctrl-C** cuando quieras; al volver a lanzarlo salta lo ya descargado.

---

## Actualizar a la última versión

```powershell
cd $HOME\orpheusdl
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RegisterCommandOnly
```

## Si algo no funciona

- **"orpheus/abq no se reconoce"** → abre una **terminal nueva** (o reinicia Windows Terminal).
- Pega la URL **entre comillas** y sin corchetes: `"https://..."`, no `[texto](url)`.
- Instala **FFmpeg** y ábrelo en una terminal nueva si te lo pide.
- Más ayuda: [docs/WINDOWS_COMMAND.md](docs/WINDOWS_COMMAND.md) ·
  [instalación manual](docs/INSTALL_WINDOWS.md).

---

<details>
<summary><b>Detalles técnicos (avanzado)</b></summary>

Fork de [OrpheusDL](https://github.com/bascurtiz/OrpheusDL) (basado en OrfiTeam/OrpheusDL),
orientado a **construir una biblioteca grande, limpia y de alta calidad**, priorizando
corrección y seguridad de la cuenta sobre la velocidad.

### Lo que añade este fork
- **Límite de peticiones por servicio** (`utils/rate_limit.py`, `modules.<svc>.rate_limit_rpm`):
  espacia las peticiones de API para no disparar detección de la cuenta. `0` = desactivado.
  El audio se descarga a toda velocidad; solo se limita la metadata.
- **Solo FLAC** (`global.codecs.flac_only`, por defecto `true`): los módulos rechazan
  transferencias que no sean FLAC.
- **De-duplicación por ISRC entre servicios**: reutiliza la carpeta del álbum cuando los
  temas coinciden por ISRC (evita copias `(2)/(3)` de la misma grabación).
- **Índice ISRC de la biblioteca** (opt-in): ver [docs/DEDUPE_OPERATIONS.md](docs/DEDUPE_OPERATIONS.md)
  y [docs/DEDUPE_DESIGN.md](docs/DEDUPE_DESIGN.md).
- **Config a prueba de fallos**: `settings.json` se escribe de forma atómica con copia `.bak`.
- **`artist_best_quality.py` (comando `abq`)**: artista → mejor FLAC entre Qobuz/Tidal/Deezer
  por ISRC (unión de los 3, dedup y selección de calidad). Config permanente en
  `settings.json → global.artist_best_quality`. Ver [docs/ARTIST_BEST_QUALITY.md](docs/ARTIST_BEST_QUALITY.md).

### Los dos comandos
Los lanzadores `orpheus.cmd` y `abq.cmd` viven en la carpeta de instalación, que el
instalador añade a tu PATH; por eso ambos funcionan desde cualquier carpeta. En Linux/macOS
usa el equivalente `python orpheus.py <url>` / `python artist_best_quality.py <url>`.

### Módulos de servicio (repos separados)
El instalador los clona en `modules/` automáticamente. Para instalación manual:

| Módulo | Repo | Rama |
|---|---|---|
| Qobuz  | [np3ir/orpheusdl-qobuz](https://github.com/np3ir/orpheusdl-qobuz)   | `feat/rate-limit-flac-only` |
| Deezer | [np3ir/OrpheusDL-deezer](https://github.com/np3ir/OrpheusDL-deezer) | `feat/rate-limit-flac-only` |
| Tidal  | [np3ir/orpheusdl-tidal](https://github.com/np3ir/orpheusdl-tidal)   | `feat/rate-limit-flac-only` |
| Spotify| [np3ir/orpheusdl-spotify](https://github.com/np3ir/orpheusdl-spotify) | `codex/flac-only` |

### Instalación manual (Linux/macOS o sin `install.ps1`)
```bash
git clone https://github.com/np3ir/orpheusdl.git
cd orpheusdl
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-qobuz.git  modules/qobuz
git clone -b feat/rate-limit-flac-only https://github.com/np3ir/OrpheusDL-deezer.git modules/deezer
git clone --recurse-submodules -b feat/rate-limit-flac-only https://github.com/np3ir/orpheusdl-tidal.git modules/tidal
git clone -b codex/flac-only https://github.com/np3ir/orpheusdl-spotify.git modules/spotify
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-core.txt
python orpheus.py <url>            # el primer arranque crea config/settings.json
```

### Configuración clave (`config/settings.json`)
- `general.download_path` — carpeta de la biblioteca (p. ej. `Z:\`).
- `general.download_quality` — `lossless`.
- `codecs.flac_only` — rechazar lo que no sea FLAC.
- `global.artist_best_quality` — opciones de `abq` (`default_quality`, `prefer_order`, …).
- `modules.<servicio>.rate_limit_rpm` — ritmo de peticiones por servicio.

### Créditos
Upstream: [bascurtiz/OrpheusDL](https://github.com/bascurtiz/OrpheusDL) y
[OrfiTeam/OrpheusDL](https://github.com/OrfiTeam/OrpheusDL). Uso personal, sin ánimo de
lucro; respeta los términos de servicio de cada plataforma.

</details>
