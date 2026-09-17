"""Shared local audio policy. Settings are read once per process; restart after edits."""
from functools import lru_cache
import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parents[1] / 'config' / 'settings.json'

@lru_cache(maxsize=1)
def flac_only():
    with SETTINGS_PATH.open(encoding='utf-8-sig') as source:
        settings = json.load(source)
    value = settings.get('global', {}).get('codecs', {}).get('flac_only', True)
    if not isinstance(value, bool):
        raise ValueError('global.codecs.flac_only must be true or false (without quotes)')
    return value
