"""Merge the per-service artist CSVs (artistas_qobuz/tidal/deezer.csv) into one
combined list with just two columns: Nombre, URL.

No deduplication: every (Nombre, URL) row from each service is kept as-is, so an
artist present on several platforms appears once per platform (each with its own
URL). Appearances are dropped. Rows are sorted by name for a clean single list.

    python merge_artists.py --out artistas_combinado.csv
"""
import csv, os, argparse, unicodedata

ap = argparse.ArgumentParser()
ap.add_argument('--out', default='artistas_combinado.csv')
ap.add_argument('--inputs', default='artistas_qobuz.csv,artistas_tidal.csv,artistas_deezer.csv')
args = ap.parse_args()


def sort_key(name):
    d = unicodedata.normalize('NFKD', str(name))
    d = ''.join(c for c in d if not unicodedata.combining(c))
    return ' '.join(d.casefold().split())


rows = []
for path in [p.strip() for p in args.inputs.split(',') if p.strip()]:
    if not os.path.exists(path):
        print(f'  (aviso: falta {path}, se omite)')
        continue
    with open(path, encoding='utf-8-sig') as f:
        r = csv.reader(f)
        next(r, None)  # header
        for row in r:
            if len(row) < 2 or not row[0].strip():
                continue
            rows.append((row[0].strip(), row[1]))  # (Nombre, URL) - no dedup

rows.sort(key=lambda nu: (sort_key(nu[0]), nu[1]))
with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['Nombre', 'URL'])
    w.writerows(rows)
print(f'LISTO: {len(rows)} filas (sin deduplicar) -> {args.out}')
