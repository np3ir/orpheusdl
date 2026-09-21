# Artist best-quality FLAC across services (by ISRC)

`artist_best_quality.py` takes **one** artist link from Tidal, Deezer or Qobuz and
downloads that artist as the **best FLAC available across all three services**,
matching recordings by **ISRC** (the code that identifies the exact recording, so
there is no wrong-match guessing).

## What it does

1. Reads the artist's whole discography from the service in the link.
2. Finds the **same artist** on the other two services by matching ISRCs
   (falls back to a name search if needed).
3. Builds the **union** of every recording across the three services, deduplicated
   by ISRC — so a track that only exists on one service is still captured.
4. **Skips what you already own**, checked against the library ISRC index.
5. For each remaining recording, picks the service with the **best FLAC**
   (24-bit hi-res > 16-bit) — or the quality you ask for — and downloads it there.
   If the requested quality isn't available, it falls back to the best FLAC that
   *is* available instead of skipping the track (unless `--exact`).

The download itself is handed to `orpheus.py`, so all existing behaviour applies
(FLAC-only, tagging, covers, lyrics, per-target-path dedup). Tracks that fail on
the best source are retried on the next-best service by ISRC.

## Usage

The install folder is on PATH, so from **any** directory use the short launcher
`abq` (Windows). It forwards all flags to the script:

```powershell
# Best FLAC anywhere (hi-res if it exists)
abq "https://tidal.com/artist/10411"

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

Any of the three artist-link formats works as the starting point; the other two
services are found automatically.

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
| `--credited` / `--no-credited` | include albums the artist only appears on (default: main discography only) |
| `--no-dedup` | do not skip tracks already in the library ISRC index |
| `-o PATH` | output path (default: `download_path` from settings) |
| `--limit N` | only process the first N recordings (testing) |
| `--dry` | show the plan, download nothing |

## Permanent configuration (settings.json)

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
- `credited_albums` — include appears-on albums by default.

The `-q` flag and the CLI flags override these for a single run.

## Dedup (skip what you already have)

Deduplication uses the same per-root ISRC index as the downloader. Build it once
per library root before relying on it:

```bash
python isrc_index_tool.py --dir "Z:\" --build
```

If the index isn't built, the tool downloads everything (it warns and does not
skip). Positive matches are re-verified against the real file on disk.

## Notes / limits

- Requires valid credentials for the services you use (Qobuz/Tidal/Deezer in
  `settings.json`). Hi-res from Qobuz/Tidal also requires a subscription that
  allows it; otherwise those services deliver 16-bit and the tool downloads that.
- Deezer FLAC is always 16-bit/44.1 kHz; Qobuz and Tidal can be hi-res.
- A recording is only compared on services where it appears in the resolved
  artist's discography. A copy hiding on an unrelated compilation isn't probed.
- Deezer tracks whose album payload carries no ISRC are skipped for *discovery*
  (they can still be a fallback source when found via another service's ISRC).
