# ISRC deduplication: design and operational limits

The index is a local SQLite cache per download root. Existing index filenames and
schema remain compatible. Roots must be used consistently; alternate drive/UNC
aliases are not merged. Each computer should build its own index.

## Download lifecycle

1. Resolve track metadata and destination root.
2. Acquire the process lock for root + ISRC (same computer/installation).
3. Query SQLite afresh and inspect candidate tags/metadata on disk.
4. Reuse an eligible existing file, or download/convert/tag the new audio.
5. Register the final path only after completion; release the lock on all exits.

Both synchronous and asynchronous routes use this lifecycle. Provider audio
acquisition is deferred until after the duplicate check, including Deezer's
temporary-file route. Cancellation settles active file workers before releasing
the claim. Positive matches are revalidated, not accepted from existence alone.
Missing/unreadable files never authorize skips. All known candidates are checked.

`global.general.isrc_library_dedup` enables this behavior (default false).
`global.general.isrc_library_upgrade` optionally allows an additive lossless quality
upgrade (default false); it never deletes the old copy. Advertised provider quality
is not a guarantee of actual/audible quality. Inspect the completed files afterward.

## Index refresh

Downloads register new completed files. External additions/moves require an explicit
`isrc_index_tool.py --build`. Reports and downloads never implicitly scan the NAS.
Builds use bounded worker batches and a per-root scan lock. Incomplete directory
walks do not prune the index or advance the completed-scan timestamp. Previously
valid but now unreadable evidence is retained for retry, and rejected by lookups.

## Cleanup

`isrc_dedupe.py` creates a JSON plan and readable CSV before any moves. It ranks
compatible recordings by lossless status, bit depth, sample rate and lossy bitrate.
Lossless compression ratio/file size is not treated as audio fidelity. Equal quality
prefers the shorter path, then lexical order. Duration differences above two seconds,
channel differences, unreadable metadata and changed tags require manual review.

Applying requires the saved plan. Evidence is rechecked before each move. Quarantine
is `_DUPLICADOS/<plan-id>/<relative-path>` on the same filesystem. A durable JSONL
journal records intent before rename and result afterward. Resume and rollback can
recover an interrupted rename; occupied destinations are never overwritten.
This tool has no permanent-delete option. See [commands](DEDUPE_OPERATIONS.md).

## Limits

- Metadata validation is not full audio decoding or acoustic identity verification.
- Locks coordinate updated processes on one PC, not other computers or old versions.
- Stop old download processes before cleanup; new processes load updated code.
- Cleanup does not rewrite existing M3U files or reorganize covers/lyrics. Removing
  repeated recordings can leave incomplete album folders and old playlist references.
- Verify any NAS/backup mirror policy before applying cleanup; a mirror may propagate
  the removal of original paths. Exclude quarantine only according to your backup policy.
- No index built: downloader warns and falls back to existing path-based deduplication.
- Cross-folder comparisons conservatively require ordinary codecs, known duration
  and a stereo candidate; ambiguous/spatial cases retain path-based behavior.

## Verification

`python -m pytest tests -q -p no:cacheprovider` exercises temporary tagged audio,
real process locks, sync/async integration, cancellation, quality decisions, root
containment, interrupted journals and rollback. Tests make no provider downloads.
