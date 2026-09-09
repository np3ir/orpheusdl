"""
Spotify device-flow authentication (sp_dc cookie based).

This presents as the Spotify desktop client to obtain a desktop access token.
It is used by the Spotify lyrics endpoint (color-lyrics). The previous
Spotify.dll / PlayPlay desktop download path has been removed; all audio
downloads now use the Librespot backend.
"""
# Client token used by the WebPlayer lyrics endpoint
DEVICE_CLIENT_TOKEN = "AAAyQwhc1wWtqYH7spRtLROv2auz6t7xi6xV0OIlc62hyvNrbjR3Lky8Lh2s7fi8jbjX1k31NBQ6d+mpEcAyXCvrNDmZSgTjuJ1QBVzqHOpP5t4E4kDvB36AfvXmcgZltN5dYgbiHal/R2LNupoZvT1fKocen24bUAHsInYgCtKy+kft4OWN1kaFo8LfNZymZzmXBXfxKfCiO1dKBQPz7Rv5hVPpcoyxkfAl4R5aNdap3iuRdAcaB4Udx28Eu98yrA=="
import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)


# ─── Desktop Client Identity ───────────────────────────────────────────────────
CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"
SP_VERSION = "128800483"
USER_AGENT = f"Spotify/{SP_VERSION} Win32_x86_64/Windows 10 (10.0.19044; x64)"
APP_PLATFORM = "Win32"

BASE_HEADERS = {
    "user-agent": USER_AGENT,
    "spotify-app-version": SP_VERSION,
    "app-platform": APP_PLATFORM,
}

TIMEOUT = 30

# ─── Device flow constants ─────────────────────────────────────────────────────
DEVICE_AUTH_URL = "https://accounts.spotify.com/oauth2/device/authorize"
DEVICE_TOKEN_URL = "https://accounts.spotify.com/api/token"
DEVICE_RESOLVE_URL = "https://accounts.spotify.com/pair/api/resolve"
DEVICE_SCOPE = (
    "app-remote-control,playlist-modify,playlist-modify-private,playlist-modify-public,"
    "playlist-read,playlist-read-collaborative,playlist-read-private,streaming,"
    "transfer-auth-session,ugc-image-upload,user-follow-modify,user-follow-read,"
    "user-library-modify,user-library-read,user-modify,user-modify-playback-state,"
    "user-modify-private,user-personalized,user-read-birthdate,user-read-currently-playing,"
    "user-read-email,user-read-play-history,user-read-playback-position,"
    "user-read-playback-state,user-read-private,user-read-recently-played,user-top-read"
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Device Flow Auth (sp_dc cookie based)
# ═══════════════════════════════════════════════════════════════════════════════

class SpotifyDeviceFlow:
    """Authenticate via device flow using sp_dc cookie."""

    def __init__(self, sp_dc: str) -> None:
        self._session = requests.Session()
        self._session.cookies.set("sp_dc", sp_dc, domain=".spotify.com")
        # Use desktop client headers for the device flow too
        self._session.headers.update(BASE_HEADERS)

    def get_token(self) -> dict:
        auth_data = self._initiate_device_authorization()
        device_code = auth_data["device_code"]
        user_code = auth_data["user_code"]
        verification_url = auth_data["verification_uri_complete"]

        flow_ctx, csrf_token = self._parse_verification_page(verification_url)
        self._submit_user_code(user_code, flow_ctx, csrf_token, verification_url)
        token_data = self._exchange_device_code(device_code)
        return token_data

    def _initiate_device_authorization(self) -> dict:
        response = requests.post(
            DEVICE_AUTH_URL,
            data={"client_id": CLIENT_ID, "scope": DEVICE_SCOPE},
            headers={**BASE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _parse_verification_page(self, verification_url: str) -> tuple:
        import urllib.parse
        response = self._session.get(verification_url, allow_redirects=True, timeout=TIMEOUT)
        try:
            flow_ctx_full = urllib.parse.parse_qs(
                urllib.parse.urlparse(response.url).query
            )["flow_ctx"][0]
            flow_ctx = flow_ctx_full.split(":")[0]
        except (KeyError, IndexError):
            raise ValueError("Failed to extract flow_ctx")

        pattern = r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>'
        match = re.search(pattern, response.text, re.DOTALL)
        try:
            json_data = json.loads(match.group(1))
            csrf_token = json_data["props"]["initialToken"]
        except Exception:
            raise ValueError("Failed to extract CSRF token")

        return flow_ctx, csrf_token

    def _submit_user_code(self, user_code: str, flow_ctx: str, csrf_token: str, referer_url: str) -> None:
        current_ts = int(time.time())
        response = self._session.post(
            DEVICE_RESOLVE_URL,
            params={"flow_ctx": f"{flow_ctx}:{current_ts}"},
            json={"code": user_code},
            headers={
                "x-csrf-token": csrf_token,
                "referer": referer_url,
                "origin": "https://accounts.spotify.com",
                "content-type": "application/json",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        if response.json().get("result") != "ok":
            raise ValueError("Failed to submit user code (result not ok)")

    def _exchange_device_code(self, device_code: str) -> dict:
        response = requests.post(
            DEVICE_TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={**BASE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
