#!/usr/bin/env python3
"""Build the library ISRC index and/or report cross-folder duplicates.

The downloader's opt-in `isrc_library_dedup` uses the SAME per-root index this
builds, so run --build once per library root before enabling it (the first scan
of a big NAS root can take a while; later runs are incremental by mtime/size).

    python isrc_index_tool.py --dir "Z:\\" --build            # build/refresh (read-only on your music)
    python isrc_index_tool.py --dir "Z:\\" --report           # list same-ISRC files in >1 folder
    python isrc_index_tool.py --dir "Z:\\" --report --out dupes_Z.csv
    python isrc_index_tool.py --dir "D:\\Music" --build --force  # re-read every tag

Reports use the existing index without rescanning; --build explicitly refreshes.
Nothing is ever moved or deleted. This only reads tags and writes a local SQLite
index under ./config. Use it to SEE duplicates; deletion stays manual (or via the
reviewed-plan workflow in isrc_dedupe.py).
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orpheus.isrc_library_index import IsrcLibraryIndex

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config')


def main():
    ap = argparse.ArgumentParser(description='Library ISRC index / duplicate report')
    ap.add_argument('--dir', required=True, help='Library root (e.g. "Z:\\" or "D:\\Music")')
    ap.add_argument('--build', action='store_true', help='Build/refresh the index (read-only on music)')
    ap.add_argument('--force', action='store_true', help='Re-read every tag, not just changed files')
    ap.add_argument('--report', action='store_true', help='List ISRCs present in more than one file')
    ap.add_argument('--workers', type=int, default=8, help='Threads for reading tags (raise to 24-32 for a NAS/SMB share)')
    ap.add_argument('--out', help='Write the report to this CSV instead of stdout')
    args = ap.parse_args()

    if not (args.build or args.report):
        ap.error('choose --build and/or --report')

    idx = IsrcLibraryIndex(args.dir, CONFIG_DIR, print_fn=print)
    print(f'Index db: {idx.db_path}')
    print(f'Root:     {idx.root}')

    if args.build:
        if not idx.is_built():
            print('Primer build (leyendo tags del disco, puede tardar en un NAS grande)...')
        stats = idx.build(force=args.force, workers=args.workers)

        def _read_warnings():
            # Benign per-file read failures (do not block completion).
            samples = stats.get('read_error_samples') or []
            if stats.get('read_warnings'):
                print(f'  AVISO: {stats["read_warnings"]} archivo(s) no se pudieron leer/verificar '
                      f'esta vez; quedan sin indexar hasta el próximo build.')
                print('  Note: some files could not be read/verified this run; they will be retried '
                      'next build.')
                for e in samples:
                    print(f'    - {e}')

        if stats.get('error') == 'incomplete_scan':
            errs = stats.get('errors') or []
            print('\nAVISO: recorrido incompleto / incomplete traversal.')
            print('  No se pudo listar alguna carpeta, así que NO se marcó el escaneo como completo')
            print('  ni se purgaron archivos borrados (podrían no haberse visto). Los tags leídos')
            print('  SÍ se guardaron y el índice previo sigue en uso.')
            print('  A directory could not be listed, so the scan was NOT marked complete and no')
            print('  deleted files were pruned. Read tags WERE saved; the previous index stays in use.')
            other = {k: v for k, v in stats.items()
                     if k not in ('error', 'errors', 'read_error_samples') and not isinstance(v, list)}
            print('  ' + ', '.join(f'{k}={v}' for k, v in other.items()))
            if errs:
                print('  carpetas/errores de recorrido / traversal errors:')
                for e in errs:
                    print(f'    - {e}')
            _read_warnings()
            idx.close(); return 2
        if stats.get('error'):
            print(f'ERROR: {stats["error"]}'); idx.close(); return 2
        scalar = {k: v for k, v in stats.items() if isinstance(v, (str, int, float, bool))}
        print('Build OK: ' + ', '.join(f'{k}={v}' for k, v in scalar.items()))
        _read_warnings()
    else:
        if not idx.is_built():
            print('ERROR: index not built; run --build explicitly.')
            idx.close()
            return 2
        print(f'Cached report; last completed scan (Unix UTC): {idx.last_scan()}')

    if args.report:
        dups = idx.duplicates()
        n_groups = len(dups)
        n_extra = sum(len(v) - 1 for v in dups.values())
        print(f'\nISRCs duplicados (mismo ISRC en >1 archivo): {n_groups} '
              f'| copias sobrantes: {n_extra}')
        if args.out:
            with open(args.out, 'w', newline='', encoding='utf-8-sig') as fh:
                w = csv.writer(fh)
                w.writerow(['isrc', 'copias', 'ruta'])
                for isrc, paths in dups.items():
                    for p in paths:
                        w.writerow([isrc, len(paths), p])
            print(f'Reporte escrito: {args.out}')
        else:
            shown = 0
            for isrc, paths in dups.items():
                print(f'\n{isrc}  ({len(paths)} copias)')
                for p in paths:
                    print(f'    {p}')
                shown += 1
                if shown >= 40:
                    print(f'\n... y {n_groups - shown} grupos más (usa --out para el CSV completo)')
                    break
    idx.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
