# Best-quality FLAC across services (by ISRC) · Mejor FLAC entre servicios (por ISRC)

**[English](#english) · [Español](#español)**

---

## English

`artist_best_quality.py` takes **one** link from Tidal, Deezer, Qobuz **or Spotify**
— an **artist, album, playlist or track** — and downloads it as the **best FLAC
available across the three FLAC services**, matching recordings by **ISRC** (the code
that identifies the exact recording, so there is no wrong-match guessing).

> A **Spotify** link is a *metadata-only source*: Spotify has no FLAC, so it is used
> only to read the songs' ISRCs, and the audio is downloaded from Qobuz/Tidal/Deezer.

### What it does

**For an artist link:**
1. Reads the artist's whole discography from the service in the link.
2. Finds the **same artist** on the other two services by matching ISRCs
   (falls back to a name search, verified by ISRC overlap).
3. Builds the **union** of every recording across the three services, deduplicated
   by ISRC — so a track that only exists on one service is still captured.

**For an album / playlist / track link:**
1. Reads the ISRCs contained in that album, playlist or track.
2. Looks each recording up on all three services directly by ISRC.

**Then, in both cases:**
3. **Skips what you already own**, checked against the library ISRC index.
4. For each remaining recording, picks the service with the **best FLAC**
   (24-bit hi-res > 16-bit) — or the quality you ask for — and downloads it there.
   If the requested quality isn't available, it falls back to the best FLAC that
   *is* available instead of skipping the track (unless `--exact`).

The download itself is handed to `orpheus.py`, so all existing behaviour applies
(FLAC-only, tagging, covers, lyrics, per-target-path dedup). Tracks that fail on
the best source are retried on the next-best service by ISRC.

### Usage

The install folder is on PATH, so from **any** directory use the short launcher
`abq` (Windows). It forwards all flags to the script:

```powershell
# Any link type works: artist, album, playlist or track
abq "https://tidal.com/artist/10411"
abq "https://www.deezer.com/album/1603029"
abq "https://www.deezer.com/playlist/908622995"
abq "https://tidal.com/browse/track/96594261"

# Ask for a specific quality
abq "https://www.deezer.com/en/artist/221" -q 16
abq "https://play.qobuz.com/artist/29674" -q 24

# Preview the plan (service + quality per track), download nothing
abq "https://tidal.com/artist/10411" --dry
```

Equivalent long form (from the install folder):

```bash
python artist_best_quality.py "https://tidal.com/artist/10411"
```

### Quality (`-q`)

| value              | meaning                                             | downloaded at |
|--------------------|-----------------------------------------------------|---------------|
| `best` / `max`     | highest FLAC anywhere (hi-res if it exists)         | hifi          |
| `hires` / `24` / `hifi` | want 24-bit hi-res; fall back to 16-bit if none | hifi          |
| `lossless` / `16` / `cd` | 16-bit / 44.1 kHz FLAC                         | lossless      |

- Requested quality **not available** → downloads the best FLAC that is, instead
  of skipping. Use `--exact` (with `hires`/`24`) to skip when no hi-res exists.

### Other flags

| flag | meaning |
|------|---------|
| `--services qobuz,tidal` | which services to use, and the tie-break order (default: config `prefer_order`) |
| `--credited` / `--no-credited` | (artist links) include albums the artist only appears on (default: off) |
| `--no-dedup` | do not skip tracks already in the library ISRC index |
| `-o PATH` | output path (default: `download_path` from settings) |
| `--limit N` | only process the first N recordings (testing) |
| `--max-albums N` | (artist links) only read the first N albums per service (sampling/testing) |
| `--dry` | show the plan, download nothing |

### Permanent configuration (settings.json)

Defaults live in `config/settings.json` → `global` → `artist_best_quality`
(registered in the Orpheus settings schema, so they persist across runs):

```json
"artist_best_quality": {
    "default_quality": "best",
    "prefer_order": ["qobuz", "tidal", "deezer"],
    "dedup_with_library": true,
    "library_root": "",
    "credited_albums": false
}
```

- `default_quality` — used when `-q` is not given.
- `prefer_order` — breaks quality ties and chooses the 16-bit source.
- `dedup_with_library` — skip ISRCs already in your library.
- `library_root` — folder to dedup against; empty = the download path.
- `credited_albums` — include appears-on albums by default (artist links).

The `-q` flag and the CLI flags override these for a single run.

### Dedup (skip what you already have)

Deduplication uses the same per-root ISRC index as the downloader. Build it once
per library root before relying on it:

```bash
python isrc_index_tool.py --dir "Z:\" --build
```

If the index isn't built, the tool downloads everything (it warns and does not
skip). Positive matches are re-verified against the real file on disk.

### Notes / limits

- Requires valid credentials for the services you use (Qobuz/Tidal/Deezer in
  `settings.json`). Hi-res from Qobuz/Tidal also requires a subscription that
  allows it; otherwise those services deliver 16-bit and the tool downloads that.
- Deezer FLAC is always 16-bit/44.1 kHz; Qobuz and Tidal can be hi-res.
- For an **artist** link, a recording is compared only on services where it appears
  in the resolved artist's discography; a copy hiding on an unrelated compilation
  isn't probed. Album/playlist/track links look every ISRC up directly on all
  services.
- Deezer tracks whose album/playlist payload carries no ISRC are skipped for
  *discovery* (they can still be a fallback source when found via another service).

---

## Español

`artist_best_quality.py` toma **un** enlace de Tidal, Deezer, Qobuz **o Spotify**
— de **artista, álbum, playlist o track** — y lo descarga con el **mejor FLAC
disponible entre los tres servicios FLAC**, emparejando las grabaciones por **ISRC**
(el código que identifica la grabación exacta, así que no hay emparejamientos
equivocados).

> Un enlace de **Spotify** es una *fuente solo de metadata*: Spotify no tiene FLAC,
> así que solo se usa para leer los ISRC de los temas, y el audio se baja de
> Qobuz/Tidal/Deezer.

### Qué hace

**Para un enlace de artista:**
1. Lee toda la discografía del artista en el servicio del enlace.
2. Encuentra al **mismo artista** en los otros dos servicios emparejando ISRC
   (con respaldo a búsqueda por nombre, verificada por solape de ISRC).
3. Construye la **unión** de todas las grabaciones entre los tres servicios,
   deduplicada por ISRC — así un tema que solo existe en un servicio también entra.

**Para un enlace de álbum / playlist / track:**
1. Lee los ISRC contenidos en ese álbum, playlist o track.
2. Busca cada grabación directamente por ISRC en los tres servicios.

**Después, en ambos casos:**
3. **Salta lo que ya tienes**, comparando con el índice ISRC de tu biblioteca.
4. Por cada grabación restante elige el servicio con el **mejor FLAC**
   (hi-res 24-bit > 16-bit) — o la calidad que pidas — y lo descarga de ahí.
   Si la calidad pedida no está, baja el mejor FLAC que **sí** haya en vez de
   saltarlo (salvo `--exact`).

La descarga la realiza `orpheus.py`, así que se mantiene todo lo existente
(solo FLAC, etiquetado, portadas, letras, dedup por ruta de destino). Los temas
que fallan en la mejor fuente se reintentan en el siguiente servicio por ISRC.

### Uso

La carpeta de instalación está en el PATH, así que desde **cualquier** carpeta
usa el lanzador corto `abq` (Windows). Pasa todos los flags al script:

```powershell
# Sirve cualquier tipo de enlace: artista, álbum, playlist o track
abq "https://tidal.com/artist/10411"
abq "https://www.deezer.com/album/1603029"
abq "https://www.deezer.com/playlist/908622995"
abq "https://tidal.com/browse/track/96594261"

# Pedir una calidad concreta
abq "https://www.deezer.com/en/artist/221" -q 16
abq "https://play.qobuz.com/artist/29674" -q 24

# Ver el plan (servicio + calidad por tema), sin descargar nada
abq "https://tidal.com/artist/10411" --dry
```

Forma larga equivalente (desde la carpeta de instalación):

```bash
python artist_best_quality.py "https://tidal.com/artist/10411"
```

### Calidad (`-q`)

| valor              | significado                                          | se baja a |
|--------------------|------------------------------------------------------|-----------|
| `best` / `max`     | el mejor FLAC en cualquier lado (hi-res si existe)   | hifi      |
| `hires` / `24` / `hifi` | quieres hi-res 24-bit; si no hay, baja a 16-bit | hifi      |
| `lossless` / `16` / `cd` | FLAC 16-bit / 44.1 kHz                          | lossless  |

- Si la calidad pedida **no está** → baja el mejor FLAC que sí haya, en vez de
  saltarlo. Usa `--exact` (con `hires`/`24`) para saltar cuando no haya hi-res.

### Otros flags

| flag | significado |
|------|-------------|
| `--services qobuz,tidal` | qué servicios usar y el orden de desempate (por defecto: `prefer_order` de la config) |
| `--credited` / `--no-credited` | (enlaces de artista) incluir álbumes donde solo aparece (por defecto: no) |
| `--no-dedup` | no saltar los temas ya presentes en el índice ISRC |
| `-o RUTA` | carpeta de salida (por defecto: `download_path` de la config) |
| `--limit N` | procesar solo las primeras N grabaciones (pruebas) |
| `--max-albums N` | (enlaces de artista) leer solo los primeros N álbumes por servicio (muestreo/pruebas) |
| `--dry` | mostrar el plan, sin descargar |

### Configuración permanente (settings.json)

Los valores por defecto viven en `config/settings.json` → `global` →
`artist_best_quality` (registrados en el esquema de Orpheus, así que persisten):

```json
"artist_best_quality": {
    "default_quality": "best",
    "prefer_order": ["qobuz", "tidal", "deezer"],
    "dedup_with_library": true,
    "library_root": "",
    "credited_albums": false
}
```

- `default_quality` — se usa cuando no pasas `-q`.
- `prefer_order` — rompe empates de calidad y elige la fuente de 16-bit.
- `dedup_with_library` — saltar ISRC ya presentes en tu biblioteca.
- `library_root` — carpeta contra la que deduplicar; vacío = la ruta de descarga.
- `credited_albums` — incluir álbumes de "aparece en" por defecto (enlaces de artista).

El flag `-q` y los flags de la CLI tienen prioridad para un run puntual.

### Dedup (saltar lo que ya tienes)

La deduplicación usa el mismo índice ISRC por raíz que el descargador. Constrúyelo
una vez por raíz de biblioteca antes de confiar en él:

```bash
python isrc_index_tool.py --dir "Z:\" --build
```

Si el índice no está construido, la herramienta descarga todo (avisa y no salta).
Los positivos se re-verifican contra el archivo real en disco.

### Notas / límites

- Requiere credenciales válidas de los servicios que uses (Qobuz/Tidal/Deezer en
  `settings.json`). El hi-res de Qobuz/Tidal también necesita una suscripción que
  lo permita; si no, esos servicios entregan 16-bit y eso se descarga.
- El FLAC de Deezer es siempre 16-bit/44.1 kHz; Qobuz y Tidal pueden ser hi-res.
- En un enlace de **artista**, una grabación se compara solo en los servicios donde
  aparece en la discografía del artista resuelto; una copia escondida en una
  recopilación ajena no se sondea. Los enlaces de álbum/playlist/track buscan cada
  ISRC directamente en todos los servicios.
- Los temas de Deezer cuyo álbum/playlist no traiga ISRC se saltan para el
  *descubrimiento* (aún pueden servir como fuente si se encuentran vía otro servicio).
