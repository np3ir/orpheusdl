"""Deezer public API (api.deezer.com) lookups used by artist_best_quality.py.

Per https://developers.deezer.com/api/errors a failed request returns
{"error": {"type", "message", "code"}}. Only code 800 (DATA_NOT_FOUND) means the
item does not exist; 4 (QUOTA) and 700 (SERVICE_BUSY) are temporary and are
retried with backoff. The edge in front of api.deezer.com can also answer with an
HTML "Access Denied" page instead of JSON; that is treated as temporary too.

A lookup that still fails after the retries raises DeezerLookupError, so callers
can report it instead of reading it as "not on Deezer".
"""
import time

import requests

BASE_URL = 'https://api.deezer.com'
DATA_NOT_FOUND = 800
RETRY_CODES = {4, 700}  # QUOTA, SERVICE_BUSY


class DeezerLookupError(Exception):
    """The request failed (after retries, or with a non-retryable API error)."""


def get(path, http=None, attempts=4, backoff=2.0, timeout=20, sleep=time.sleep):
    """GET BASE_URL + path. Returns the JSON body, or None for DATA_NOT_FOUND (800).

    http: a requests.Session (use a rate-gated one); defaults to the requests module.
    Raises DeezerLookupError when the request keeps failing or the API returns an
    error other than 800 / the retryable ones."""
    http = http or requests
    url = BASE_URL + path
    last = 'no attempt'
    for i in range(attempts):
        try:
            resp = http.get(url, timeout=timeout)
        except requests.RequestException as e:
            last = f'network error: {e}'
        else:
            try:
                body = resp.json()
            except ValueError:
                last = f'HTTP {resp.status_code}, non-JSON response (blocked by the edge?)'
            else:
                err = body.get('error') if isinstance(body, dict) else None
                if not err:
                    return body
                code = err.get('code') if isinstance(err, dict) else None
                desc = (f'{err.get("type")} {code}: {err.get("message")}'
                        if isinstance(err, dict) else str(err))
                if code == DATA_NOT_FOUND:
                    return None
                if code not in RETRY_CODES:
                    raise DeezerLookupError(f'{path}: {desc}')
                last = desc
        if i < attempts - 1:
            sleep(backoff * (2 ** i))
    raise DeezerLookupError(f'{path}: {last} (after {attempts} attempts)')
