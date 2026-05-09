# OrpheusDL — np3ir Fork

A heavily modified fork of [OrfiDev/OrpheusDL](https://github.com/OrfiDev/OrpheusDL) and [bascurtiz/OrpheusDL](https://github.com/bascurtiz/OrpheusDL) with major improvements to the Apple Music module and cross-platform file organization.

---

## Key Improvements Over Upstream

### Apple Music Module (`modules/applemusic/`)
- **Separated artists** — uses the `artists` relationship from the Apple Music API instead of splitting the `artistName` string (`"A & B"` → `["A", "B"]`)
- **Correct album artist** — primary artist from catalog relationship, not the combined collaboration string
- **isSingle/isCompilation detection** — uses boolean fields instead of unreliable `playParams.kind`
- **Album suffix stripping** — removes ` - Single`, ` - EP` from album names (type goes in `{release}`)
- **Full tags** — `total_tracks`, `total_discs`, `upc`, `copyright`, `label`, `release_date` from album data
- **LRC lyrics** — via gamdl TTML→LRC conversion (`ModuleModes.lyrics`)
- **Credits** — via `/songs/{id}/credits` endpoint (`ModuleModes.credits`)
- **167-storefront scan** — scans all Apple Music regions in parallel to find region-exclusive releases
- **Library playlists** — supports `p.xxx` personal library playlist URLs with full pagination (1943+ tracks)
- **Pre-flight availability check** — skips albums from foreign storefronts that are unavailable for streaming
- **Race condition fix** — removed `_set_session()` call from concurrent download path (was causing 401 errors)

### Cross-Platform File Organization
- **`{album_artist}`** template variable — always the primary album artist (consistent across Tidal, Apple Music, Deezer)
- **`{year}`** in folder names — clean 4-digit year instead of full date
- **Deezer single detection** — `TYPE=0` with 1 track is correctly identified as `SINGLE`
- **Fuzzy folder matching** — prevents duplicate folders differing only in diacritics/accents (e.g. `Los Angeles Azules` vs `Los Ángeles Azules` downloaded from different platforms point to the same folder)
- **Cross-extension file detection** — detects existing `.flac` when downloading `.m4a` and vice versa, preventing re-downloads across platforms

### Core (`orpheus/music_downloader.py`)
- **Error TrackInfo guard** — no longer creates garbage files when `get_track_info` fails silently
- **`_check_db` file verification** — if a tracked file was deleted from disk, it re-downloads correctly
- **Short track threshold** — lowered to 4KB minimum (handles interludes and skits under 1MB)
- **Playlist download fix** — `m3u_playlist` parameter no longer causes silent failures for all tracks
- **`{album_artist}` in templates** — populated from `Tags.album_artist` field
- **`artist_initials` from album artist** — folder initials based on album artist, consistent with Tidal behavior

---

## Recommended Settings

```json
"formatting": {
    "album_format": "{artist_initials}/{album_artist}/({year}) {album_clean} {release}",
    "track_filename_format": "{track_number}. {artists} - {title_clean}{explicit}{dolby: [atmos]}",
    "single_full_path_format": "{artist_initials}/{album_artist}/({year}) {album_clean} {release}/{track_number}. {artists} - {title_clean}{explicit}",
    "playlist_format": "Z:/!playlists/{name} ||| {artists} - {title_clean}{explicit}{dolby: [atmos]}",
    "enable_zfill": true,
    "force_album_format": true
}
```

This mirrors the folder structure used by [tiddl](https://github.com/oskvr37/tiddl) for Tidal downloads, keeping multi-platform collections consistent in a single library.

---

## Apple Music Setup

1. Install gamdl dependencies (bundled in `modules/applemusic/gamdl/`)
2. Export your Apple Music cookies to `config/cookies.txt` in Netscape format (use a browser extension)
3. Cookies expire approximately every 30 days — re-export when 401 errors appear
4. Configure in `config/settings.json`:

```json
"applemusic": {
    "cookies_path": "./config/cookies.txt",
    "language": "en-US",
    "codec": "aac",
    "quality": "high"
}
```

> **Note:** ALAC (lossless) requires a device-level Widevine CDM. The bundled generic CDM only supports AAC 256kbps (legacy path).

---

## Supported URLs

| Service | Example |
|---------|---------|
| Apple Music artist | `https://music.apple.com/us/artist/noah-kahan/328583953` |
| Apple Music album | `https://music.apple.com/us/album/stick-season/1641076676` |
| Apple Music song | `https://music.apple.com/us/song/stick-season/1641076689` |
| Apple Music playlist (catalog) | `https://music.apple.com/us/playlist/dale-play/pl.4b364b8b...` |
| Apple Music playlist (library) | `https://music.apple.com/library/playlist/p.ldvAJK1coEp23Y` |
| Tidal | `https://tidal.com/album/387249452` |
| Deezer | `https://www.deezer.com/album/643003011` |

---

## Related Tools

- [ammon-cli](https://github.com/np3ir/ammon-cli) — Apple Music Monitor: follow artists and playlists, auto-download new releases via OrpheusDL
- [odesli-cli](https://github.com/np3ir/odesli-cli) — Cross-platform artist ID lookup (MusicBrainz + Apple Music direct search + Songlink)
