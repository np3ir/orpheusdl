"""Resolve platform share/short links to the real media URL.

Deezer's "Share" button gives ``https://link.deezer.com/s/<code>``. That is not a media URL: it
answers with a redirect whose ``dest``/``awf`` query parameter carries the real
``https://www.deezer.com/<type>/<id>`` link (plus tracking noise). We read the redirect without
following it blindly, and only ever accept a deezer.com address as the result, so a short link
cannot send us to some other host.
"""
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

SHORT_HOSTS = ('link.deezer.com', 'deezer.page.link')
_MAX_HOPS = 5
_UA = {'User-Agent': 'Mozilla/5.0 (compatible; orpheusdl-abq)'}


class ShortLinkError(Exception):
    pass


def is_short_link(url):
    host = urlparse(url if '://' in url else 'https://' + url).netloc.lower()
    return host in SHORT_HOSTS


def _is_deezer_media_host(host):
    host = host.lower()
    return host == 'deezer.com' or host.endswith('.deezer.com')


def _clean(url):
    """Drop the query/fragment (tracking parameters) of a deezer.com media URL."""
    u = urlparse(url)
    return f'https://{u.netloc}{u.path}'


def resolve(url, get=None, timeout=15):
    """Return the real media URL behind a Deezer short link; other URLs come back unchanged.

    Raises ShortLinkError when the link cannot be resolved to a deezer.com address.
    """
    if not is_short_link(url):
        return url
    get = get or requests.get
    current = url if '://' in url else 'https://' + url
    for _ in range(_MAX_HOPS):
        try:
            r = get(current, allow_redirects=False, timeout=timeout, headers=_UA)
        except requests.RequestException as e:
            raise ShortLinkError(f'could not open the short link ({e})') from e
        location = r.headers.get('Location') if r.status_code in (301, 302, 303, 307, 308) else None
        if not location:
            raise ShortLinkError(f'the short link did not redirect (HTTP {r.status_code})')
        location = urljoin(current, location)
        target = urlparse(location)
        # the real destination travels in these parameters of the intermediate redirect
        query = parse_qs(target.query)
        for key in ('dest', 'awf', 'gwf', 'iwf'):
            for candidate in query.get(key, []):
                cand = unquote(candidate)
                if _is_deezer_media_host(urlparse(cand).netloc) and not is_short_link(cand):
                    return _clean(cand)
        if _is_deezer_media_host(target.netloc) and not is_short_link(location):
            return _clean(location)
        if target.netloc.lower() not in SHORT_HOSTS:
            raise ShortLinkError(f'the short link points outside Deezer ({target.netloc})')
        current = location
    raise ShortLinkError('too many redirects')
