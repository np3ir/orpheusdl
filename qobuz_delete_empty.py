"""Delete the AUTHENTICATED user's OWN empty (0-track) Qobuz playlists.

Safety:
  - DRY-RUN by default: prints what it would delete and changes nothing.
  - Only deletes playlists OWNED by the logged-in user (owner.id == my id).
    Empty playlists you merely FOLLOW are never deleted (listed as skipped).
  - Deletion is irreversible on Qobuz; pass --apply to actually delete.

    python qobuz_delete_empty.py           # DRY-RUN
    python qobuz_delete_empty.py --apply    # really delete owned empties
"""
import argparse
import os
import sys

sys.path.insert(0, r'C:\OrpheusDL')
os.chdir(r'C:\OrpheusDL')
from orpheus.core import Orpheus

ap = argparse.ArgumentParser()
ap.add_argument('--apply', action='store_true', help='actually delete (default: dry-run)')
args = ap.parse_args()

sess = Orpheus().load_module('qobuz').session

me = sess.api_call('user/get')
my_id = me.get('id')
print(f"Authenticated as: {me.get('display_name') or me.get('login')} (id={my_id})\n")

# collect all playlists (paginated)
items, offset, total = [], 0, None
while total is None or offset < total:
    page = sess.api_call('playlist/getUserPlaylists', params={'limit': 500, 'offset': offset})
    block = page.get('playlists') or {}
    batch = block.get('items') or []
    total = block.get('total', offset + len(batch))
    items.extend(batch)
    if not batch:
        break
    offset += len(batch)

empty = [it for it in items if not (it.get('tracks_count') or 0)]
owned_empty = [it for it in empty if (it.get('owner') or {}).get('id') == my_id]
followed_empty = [it for it in empty if (it.get('owner') or {}).get('id') != my_id]

print(f"Total playlists: {len(items)} | empty: {len(empty)} "
      f"| owned-empty (deletable): {len(owned_empty)} | followed-empty (skip): {len(followed_empty)}\n")

print("=== OWNED empty (would be DELETED) ===")
for it in owned_empty:
    print(f"  {it.get('id')}  {it.get('name')!r}")
if followed_empty:
    print("\n=== FOLLOWED empty (NOT deleted; only you could unfollow) ===")
    for it in followed_empty:
        print(f"  {it.get('id')}  {it.get('name')!r}  (owner: {(it.get('owner') or {}).get('name')})")

if not args.apply:
    print(f"\nDRY-RUN: nothing deleted. Re-run with --apply to delete the "
          f"{len(owned_empty)} owned empty playlist(s).")
    sys.exit(0)

print(f"\nAPPLY: deleting {len(owned_empty)} owned empty playlist(s)...")
ok = fail = 0
for it in owned_empty:
    pid = it.get('id')
    try:
        sess.api_call('playlist/delete', params={'playlist_id': pid}, post=True)
        print(f"  deleted {pid}  {it.get('name')!r}")
        ok += 1
    except Exception as e:
        print(f"  FAILED  {pid}  {it.get('name')!r} -> {type(e).__name__}: {e}")
        fail += 1
print(f"\nDone. deleted={ok} failed={fail}")
