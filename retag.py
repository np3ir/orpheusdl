"""
retag.py — Rewrite title and artist tags in M4A files to match OrpheusDL filename format.
  - Title: removes feat./ft. from title if featured artist is already in artist tag
  - Artist: joins multiple artists with " / " separator

Usage:
  python retag.py Z:/              --dry-run      # preview changes only
  python retag.py Z:/ --apply                     # apply changes
  python retag.py Z:/ --apply --skip-playlists    # skip !playlists folder
"""

import sys
import os
import re
import unicodedata
import argparse
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

try:
    from mutagen.easymp4 import EasyMP4
    from mutagen.mp4 import MP4
except ImportError:
    print("Install mutagen: pip install mutagen")
    sys.exit(1)

_RE_FEAT = re.compile(
    r'\s*\(\s*(?:feat|ft|featuring|with|con)\.?\s+.*?\)',
    re.IGNORECASE
)


def _norm(s):
    d = unicodedata.normalize('NFD', s)
    return ''.join(c for c in d if unicodedata.category(c) != 'Mn').lower().strip()


def clean_title(title: str, artists: list[str]) -> str:
    """Remove feat. from title if featured artist is already in artist list."""
    if not title:
        return title
    artists_norm = ' '.join(_norm(a) for a in artists)
    match = _RE_FEAT.search(title)
    if match:
        inner = re.sub(r'^\s*\(\s*(?:feat|ft|featuring|with|con)\.?\s*', '',
                       match.group(0), flags=re.IGNORECASE).rstrip(')')
        if len(inner.strip()) > 2 and _norm(inner) in artists_norm:
            return title.replace(match.group(0), '').strip()
    return title


def process_file(path: Path, dry_run: bool) -> dict | None:
    """
    Returns dict with changes if any, None if no change needed.
    """
    try:
        audio = EasyMP4(str(path))
    except Exception as e:
        return {'path': str(path), 'error': str(e)}

    changes = {}

    # Current values
    cur_title = (audio.get('title') or [''])[0]
    cur_artist = audio.get('artist') or []

    # Normalize artist to list of strings
    if isinstance(cur_artist, list):
        artists = [str(a) for a in cur_artist]
    else:
        artists = [str(cur_artist)] if cur_artist else []

    # If artist is a single string with / or ; — split it
    if len(artists) == 1 and (' / ' in artists[0] or '; ' in artists[0]):
        sep = ' / ' if ' / ' in artists[0] else '; '
        artists = [a.strip() for a in artists[0].split(sep)]

    # 1. Clean title
    new_title = clean_title(cur_title, artists)
    if new_title != cur_title:
        changes['title'] = (cur_title, new_title)

    # 2. Join artists with " / " into a single tag value
    # Change needed if: multiple values in list, OR single value with wrong separator
    needs_join = isinstance(cur_artist, list) and len(cur_artist) > 1
    new_artist = ' / '.join(artists)
    cur_single = cur_artist[0] if isinstance(cur_artist, list) and len(cur_artist) == 1 else (cur_artist if not isinstance(cur_artist, list) else None)
    needs_sep_fix = cur_single and ('; ' in cur_single) and ' / ' not in cur_single

    if needs_join or needs_sep_fix:
        old_display = str(cur_artist)
        changes['artist'] = (old_display, new_artist)

    if not changes:
        return None

    if not dry_run:
        try:
            if 'title' in changes:
                audio['title'] = changes['title'][1]
            if 'artist' in changes:
                audio['artist'] = changes['artist'][1]
            audio.save()
        except Exception as e:
            return {'path': str(path), 'error': str(e)}

    return {'path': str(path), 'changes': changes}


def main():
    parser = argparse.ArgumentParser(description='Retag M4A files')
    parser.add_argument('root', help='Root directory to scan (e.g. Z:/)')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Preview changes without applying (default)')
    parser.add_argument('--apply', action='store_true',
                        help='Apply changes (disables dry-run)')
    parser.add_argument('--skip-playlists', action='store_true', default=True,
                        help='Skip !playlists folder (default: True)')
    args = parser.parse_args()

    dry_run = not args.apply
    root = Path(args.root)

    print(f"\n{'DRY RUN — no files will be modified' if dry_run else 'APPLYING CHANGES'}")
    print(f"Scanning: {root}\n")

    total = changed = errors = skipped = 0

    show_detail = 50  # show first N changes in detail
    for m4a in root.rglob('*.m4a'):
        if args.skip_playlists and '!playlists' in str(m4a):
            skipped += 1
            continue
        total += 1

        if total % 5000 == 0:
            print(f"  [{total:,}] scanned — {changed:,} changes, {errors} errors...")

        result = process_file(m4a, dry_run)
        if result is None:
            continue
        if 'error' in result:
            errors += 1
            if errors <= 10:
                print(f"  ERROR: {result['path']}: {result['error']}")
            continue

        changed += 1
        c = result['changes']
        if changed <= show_detail:
            print(f"  {m4a.name}")
            if 'title' in c:
                print(f"    title:  '{c['title'][0]}' -> '{c['title'][1]}'")
            if 'artist' in c:
                print(f"    artist: {c['artist'][0]} -> '{c['artist'][1]}'")
        elif changed == show_detail + 1:
            print(f"  (showing only first {show_detail} changes...)")

    if changed > show_detail:
        print(f"\n  Total changes: {changed:,}")

    print(f"\n{'=' * 60}")
    print(f"  Total scanned : {total:,}")
    print(f"  Would change  : {changed:,}" if dry_run else f"  Changed       : {changed:,}")
    print(f"  Errors        : {errors}")
    print(f"  Skipped (playlists): {skipped:,}")
    if dry_run and changed > 0:
        print(f"\n  Run with --apply to apply changes.")
    print()


if __name__ == '__main__':
    main()
