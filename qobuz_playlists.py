"""Export the authenticated Qobuz user's playlists to CSV."""
import csv
import json
from pathlib import Path

from orpheus.core import Orpheus


def flatten(value, prefix=""):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            result.update(flatten(item, name))
    elif isinstance(value, list):
        result[prefix] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        result[prefix] = value
    return result


def main():
    session = (Orpheus().load_module("qobuz")).session
    rows = []
    offset = 0
    total = None
    while total is None or offset < total:
        payload = session.api_call(
            "playlist/getUserPlaylists",
            params={"limit": 500, "offset": offset},
        )
        block = payload.get("playlists") or {}
        items = block.get("items") or []
        total = block.get("total", offset + len(items))
        rows.extend(flatten(item) for item in items)
        if not items:
            break
        offset += len(items)

    rows = [
        {
            "Nombre": row.get("name", ""),
            "Tracks": row.get("tracks_count", ""),
            "URL": f"https://open.qobuz.com/playlist/{row.get('id')}" if row.get("id") else "",
        }
        for row in rows
    ]
    fields = ["Nombre", "Tracks", "URL"]
    output = Path(__file__).with_name("qobuz_playlists.csv")
    output_plain = Path(__file__).with_name("qobuz_playlists_utf8.csv")
    for destination, encoding in ((output, "utf-8-sig"), (output_plain, "utf-8")):
        with destination.open("w", encoding=encoding, newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    print(f"Qobuz playlists: {len(rows)}")
    print(f"CSV: {output}")
    print(f"CSV UTF-8 sin BOM: {output_plain}")
    print("Columns:", ", ".join(fields))


if __name__ == "__main__":
    main()
