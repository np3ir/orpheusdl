# Settings guide · Guía de ajustes (`config/settings.json`)

Plain-language explanation of every option. You don't need to understand all of
them — the defaults work. Change one, save the file, done.

Explicación en lenguaje sencillo de cada opción. No necesitas entenderlas todas:
los valores por defecto funcionan. Cambia una, guarda el archivo, y listo.

**[English](#english) · [Español](#español)**

> Open it with: `notepad "$HOME\orpheusdl\config\settings.json"`
> Ábrelo con: `notepad "$HOME\orpheusdl\config\settings.json"`

---

## English

The file has two big parts: **`global`** (how the program behaves) and
**`modules`** (your accounts for each service).

### general — the basics
| Option | What it does |
|---|---|
| `download_path` | The folder where your music is saved (e.g. `Z:\` or `D:/Music`). |
| `download_quality` | The quality to download. Use `lossless` (16-bit CD FLAC) or `hifi` (hi-res). |
| `search_limit` | How many results to show when you search. |
| `disabled_search_platforms` | Services to hide from search (a list, e.g. `["spotify"]`). |
| `concurrent_downloads` | How many tracks download at the same time. Higher = faster but heavier. |
| `progress_bar` | Show a progress bar while downloading (`true`/`false`). |
| `throttle_batch_size` | Pause after this many tracks to avoid hammering a service (`0` = off). |
| `throttle_pause_seconds` | How long that pause lasts, in seconds. |
| `create_platform_folder` | Put downloads in a subfolder named after the service. |
| `disable_subscription_checks` | Skip checking your plan before downloading (leave `false`). |
| `ignore_existing_files` | Skip files you already downloaded (recommended `true`). |
| `reverify_existing_files` | Re-check existing files every time (slower; usually `false`). |
| `isrc_library_dedup` | Don't download a song you already have, even in another folder (matched by ISRC). |
| `isrc_library_upgrade` | Replace an existing file if a higher-quality version is found. |

### artist_downloading — when you download a whole artist
| Option | What it does |
|---|---|
| `return_credited_albums` | Also include albums where the artist only "appears on" (not their own). |
| `separate_tracks_skip_downloaded` | Skip singles you already have when grabbing an artist. |
| `prefer_highest_quality_edition` | If an album has several editions, pick the best-quality one. |
| `merge_same_name_albums` | Put different editions of the same album in one folder. |
| `explicit_content` | Which version to get: `both`, `prefer_explicit`, or clean only. |

### artist_best_quality — options for the `abq` command
| Option | What it does |
|---|---|
| `default_quality` | Quality used when you don't pass `-q`: `best`, `hires`/`24`, or `lossless`/`16`. |
| `prefer_order` | Which service wins when quality ties (list, e.g. `["qobuz","tidal","deezer"]`). |
| `dedup_with_library` | Skip songs you already own (by ISRC). |
| `library_root` | Folder to compare against; empty = your download folder. |
| `credited_albums` | Include "appears on" albums by default for artist links. |
| `own_albums_only` | `true` = only the artist's own albums (skips compilations and collabs). Default `false` keeps every song the artist performs (including features) and drops only other artists' songs. |

### codecs — audio format
| Option | What it does |
|---|---|
| `flac_only` | Only download lossless FLAC; refuse lower-quality formats (recommended `true`). |
| `proprietary_codecs` | Allow special formats like MQA (usually `false`). |
| `spatial_codecs` | Allow surround/spatial audio like Dolby Atmos (usually `false`). |
| `include_dolby_atmos` | Also grab Atmos versions when available. |

### formatting — folder and file names
These are name patterns; the words in `{ }` are filled in automatically (artist,
album, year, track number, title…). You rarely need to change them.
| Option | What it does |
|---|---|
| `album_format` | Folder pattern for an album, e.g. `{album_artist}/({release_year}) {name}`. |
| `discography_format` | Folder pattern when downloading a whole artist. |
| `playlist_format` | Folder pattern for a playlist. |
| `track_filename_format` | File name pattern for each song. |
| `playlist_track_filename_format` | File name pattern for songs inside a playlist. |
| `single_full_path_format` | Path pattern for a single track download. |
| `metadata_separator` | Symbol between multiple artists/genres inside the tags. |
| `filename_separator` | Symbol between multiple artists in file names. |
| `split_metadata` | Store multiple artists/genres as separate values. |
| `enable_zfill` | Pad track numbers with a leading zero (01, 02…). |
| `force_album_format` | Always use the album folder pattern. |
| `use_album_artist_for_discography` | Group an artist's discography by album-artist. |
| `use_playlist_position` | Number files by their position in the playlist. |
| `use_album_position` | Number files by their position in the album. |
| `isrc_fallback` | If a track fails, try to get the same recording from another service (by ISRC). |
| `spotify_metadata_only` | Never stream audio from Spotify; use it only to read song info. |

### codec_conversion — converting after download
| Option | What it does |
|---|---|
| `codec_conversions` | Rules to convert one format into another (e.g. `alac` → `flac`). |
| `conversion_flags` | Extra quality settings for the conversion. |
| `conversion_keep_original` | Keep the original file after converting. |
| `ffmpeg_path` | Where FFmpeg is (leave `ffmpeg` if it's on your PATH). |
| `enable_undesirable_conversions` | Allow conversions that could lower quality (usually `false`). |

### module_defaults — where extra info comes from
| Option | What it does |
|---|---|
| `lyrics` | Which service to get lyrics from (`default` = the same as the download). |
| `covers` | Which service to get cover art from. |
| `credits` | Which service to get credits from. |

### lyrics — song lyrics
| Option | What it does |
|---|---|
| `embed_lyrics` | Save lyrics inside the music file. |
| `embed_synced_lyrics` | Save time-synced (karaoke) lyrics inside the file. |
| `save_synced_lyrics` | Also save a separate `.lrc` lyrics file. |

### covers — album art
| Option | What it does |
|---|---|
| `embed_cover` | Put the cover image inside the music file. |
| `main_compression` | Quality of the embedded cover (`high`/`low`). |
| `main_resolution` | Size in pixels of the embedded cover. |
| `save_original_cover_size` | Keep the full-size original cover too. |
| `save_external` | Also save the cover as a separate image file. |
| `external_format` | Format of that separate image (`jpg`/`png`). |
| `external_compression` | Quality of that separate image. |
| `external_resolution` | Size in pixels of that separate image. |
| `save_animated_cover` | Save the animated cover when a service has one. |

### playlist — playlist files
| Option | What it does |
|---|---|
| `save_m3u` | Create an `.m3u` playlist file. |
| `paths_m3u` | Use `absolute` or `relative` paths inside the playlist. |
| `extended_m3u` | Include extra info (title/artist) in the playlist file. |
| `group_by_album` | Organize the playlist's songs by album. |
| `m3u_only` | Only make the playlist file, don't download the songs. |
| `sync` | Keep a local folder matching the playlist. |
| `sync_remove_orphaned` | When syncing, delete local songs no longer in the playlist. |

### advanced
| Option | What it does |
|---|---|
| `advanced_login_system` | An alternative login handling (leave `false` unless you know you need it). |
| `debug_mode` | Print extra technical detail for troubleshooting. |

### modules — your accounts
Each service (`qobuz`, `tidal`, `deezer`, `spotify`) has its own fields for your
login (user/password, tokens or keys). Fill them in following each service's
README. One shared option:
| Option | What it does |
|---|---|
| `rate_limit_rpm` | Max requests per minute to that service, to protect your account (lower = gentler; `0` = no limit). |

Your accounts are private and this file is **not** uploaded to GitHub.

---

## Español

El archivo tiene dos partes grandes: **`global`** (cómo se comporta el programa) y
**`modules`** (tus cuentas de cada servicio).

### general — lo básico
| Opción | Qué hace |
|---|---|
| `download_path` | La carpeta donde se guarda tu música (p. ej. `Z:\` o `D:/Music`). |
| `download_quality` | La calidad a descargar. Usa `lossless` (FLAC de CD, 16-bit) o `hifi` (hi-res). |
| `search_limit` | Cuántos resultados mostrar al buscar. |
| `disabled_search_platforms` | Servicios a ocultar en la búsqueda (una lista, p. ej. `["spotify"]`). |
| `concurrent_downloads` | Cuántos temas se bajan a la vez. Más = más rápido pero más pesado. |
| `progress_bar` | Mostrar barra de progreso al descargar (`true`/`false`). |
| `throttle_batch_size` | Pausar tras esta cantidad de temas para no saturar el servicio (`0` = desactivado). |
| `throttle_pause_seconds` | Cuánto dura esa pausa, en segundos. |
| `create_platform_folder` | Guardar las descargas en una subcarpeta con el nombre del servicio. |
| `disable_subscription_checks` | Saltar la comprobación de tu plan antes de bajar (déjalo en `false`). |
| `ignore_existing_files` | Saltar archivos que ya descargaste (recomendado `true`). |
| `reverify_existing_files` | Volver a comprobar los archivos existentes cada vez (más lento; normalmente `false`). |
| `isrc_library_dedup` | No bajar una canción que ya tienes, aunque esté en otra carpeta (por ISRC). |
| `isrc_library_upgrade` | Reemplazar un archivo si aparece una versión de mayor calidad. |

### artist_downloading — al descargar un artista completo
| Opción | Qué hace |
|---|---|
| `return_credited_albums` | Incluir también álbumes donde el artista solo "aparece" (no son suyos). |
| `separate_tracks_skip_downloaded` | Saltar singles que ya tienes al bajar un artista. |
| `prefer_highest_quality_edition` | Si un álbum tiene varias ediciones, elegir la de mejor calidad. |
| `merge_same_name_albums` | Juntar distintas ediciones del mismo álbum en una carpeta. |
| `explicit_content` | Qué versión bajar: `both`, `prefer_explicit`, o solo limpia. |

### artist_best_quality — opciones del comando `abq`
| Opción | Qué hace |
|---|---|
| `default_quality` | Calidad cuando no pasas `-q`: `best`, `hires`/`24` o `lossless`/`16`. |
| `prefer_order` | Qué servicio gana cuando hay empate de calidad (lista, p. ej. `["qobuz","tidal","deezer"]`). |
| `dedup_with_library` | Saltar canciones que ya tienes (por ISRC). |
| `library_root` | Carpeta con la que comparar; vacío = tu carpeta de descargas. |
| `credited_albums` | Incluir álbumes de "aparece en" por defecto en enlaces de artista. |
| `own_albums_only` | `true` = solo los álbumes propios del artista (salta recopilatorios y colaboraciones). Por defecto `false` conserva cada canción que el artista interpreta (incluidas colaboraciones) y descarta solo las ajenas. |

### codecs — formato de audio
| Opción | Qué hace |
|---|---|
| `flac_only` | Descargar solo FLAC sin pérdida; rechazar formatos peores (recomendado `true`). |
| `proprietary_codecs` | Permitir formatos especiales como MQA (normalmente `false`). |
| `spatial_codecs` | Permitir audio envolvente/espacial como Dolby Atmos (normalmente `false`). |
| `include_dolby_atmos` | Bajar también versiones Atmos cuando existan. |

### formatting — nombres de carpetas y archivos
Son patrones de nombre; las palabras entre `{ }` se rellenan solas (artista,
álbum, año, número de pista, título…). Rara vez hace falta cambiarlos.
| Opción | Qué hace |
|---|---|
| `album_format` | Patrón de carpeta de un álbum, p. ej. `{album_artist}/({release_year}) {name}`. |
| `discography_format` | Patrón de carpeta al descargar un artista completo. |
| `playlist_format` | Patrón de carpeta de una playlist. |
| `track_filename_format` | Patrón de nombre de archivo de cada canción. |
| `playlist_track_filename_format` | Patrón de nombre para canciones dentro de una playlist. |
| `single_full_path_format` | Patrón de ruta al bajar un tema suelto. |
| `metadata_separator` | Símbolo entre varios artistas/géneros dentro de las etiquetas. |
| `filename_separator` | Símbolo entre varios artistas en el nombre del archivo. |
| `split_metadata` | Guardar varios artistas/géneros como valores separados. |
| `enable_zfill` | Rellenar el número de pista con un cero delante (01, 02…). |
| `force_album_format` | Usar siempre el patrón de carpeta de álbum. |
| `use_album_artist_for_discography` | Agrupar la discografía por artista del álbum. |
| `use_playlist_position` | Numerar los archivos por su posición en la playlist. |
| `use_album_position` | Numerar los archivos por su posición en el álbum. |
| `isrc_fallback` | Si una canción falla, intentar la misma grabación en otro servicio (por ISRC). |
| `spotify_metadata_only` | No transmitir audio de Spotify; usarlo solo para leer la info de las canciones. |

### codec_conversion — convertir tras descargar
| Opción | Qué hace |
|---|---|
| `codec_conversions` | Reglas para convertir un formato en otro (p. ej. `alac` → `flac`). |
| `conversion_flags` | Ajustes extra de calidad para la conversión. |
| `conversion_keep_original` | Conservar el archivo original tras convertir. |
| `ffmpeg_path` | Dónde está FFmpeg (deja `ffmpeg` si está en el PATH). |
| `enable_undesirable_conversions` | Permitir conversiones que podrían bajar la calidad (normalmente `false`). |

### module_defaults — de dónde sale la info extra
| Opción | Qué hace |
|---|---|
| `lyrics` | De qué servicio sacar las letras (`default` = el mismo de la descarga). |
| `covers` | De qué servicio sacar las portadas. |
| `credits` | De qué servicio sacar los créditos. |

### lyrics — letras
| Opción | Qué hace |
|---|---|
| `embed_lyrics` | Guardar las letras dentro del archivo de música. |
| `embed_synced_lyrics` | Guardar letras sincronizadas (karaoke) dentro del archivo. |
| `save_synced_lyrics` | Guardar además un archivo de letras `.lrc` aparte. |

### covers — portadas
| Opción | Qué hace |
|---|---|
| `embed_cover` | Poner la portada dentro del archivo de música. |
| `main_compression` | Calidad de la portada incrustada (`high`/`low`). |
| `main_resolution` | Tamaño en píxeles de la portada incrustada. |
| `save_original_cover_size` | Guardar también la portada original a tamaño completo. |
| `save_external` | Guardar además la portada como imagen aparte. |
| `external_format` | Formato de esa imagen aparte (`jpg`/`png`). |
| `external_compression` | Calidad de esa imagen aparte. |
| `external_resolution` | Tamaño en píxeles de esa imagen aparte. |
| `save_animated_cover` | Guardar la portada animada cuando el servicio la tenga. |

### playlist — archivos de playlist
| Opción | Qué hace |
|---|---|
| `save_m3u` | Crear un archivo de playlist `.m3u`. |
| `paths_m3u` | Usar rutas `absolute` (absolutas) o `relative` (relativas) en la playlist. |
| `extended_m3u` | Incluir info extra (título/artista) en el archivo de playlist. |
| `group_by_album` | Organizar las canciones de la playlist por álbum. |
| `m3u_only` | Crear solo el archivo de playlist, sin descargar las canciones. |
| `sync` | Mantener una carpeta local igual a la playlist. |
| `sync_remove_orphaned` | Al sincronizar, borrar canciones locales que ya no están en la playlist. |

### advanced — avanzado
| Opción | Qué hace |
|---|---|
| `advanced_login_system` | Un manejo de login alternativo (déjalo en `false` salvo que lo necesites). |
| `debug_mode` | Mostrar detalle técnico extra para diagnosticar problemas. |

### modules — tus cuentas
Cada servicio (`qobuz`, `tidal`, `deezer`, `spotify`) tiene sus propios campos de
acceso (usuario/contraseña, tokens o claves). Rellénalos siguiendo el README de
cada servicio. Una opción común:
| Opción | Qué hace |
|---|---|
| `rate_limit_rpm` | Máximo de peticiones por minuto a ese servicio, para proteger tu cuenta (menor = más suave; `0` = sin límite). |

Tus cuentas son privadas y este archivo **no** se sube a GitHub.
