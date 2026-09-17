"""Shared proactive request rate-limiting for the download modules.

Quality-over-speed: the user prefers slow, gentle downloading so the services
(Tidal / Deezer / Qobuz) don't flag or block the account for fast bursts. This
spaces EVERY HTTP request that goes through a requests.Session at a configurable
requests-per-minute rate. One install call per module gates all of its traffic
(metadata calls + the audio-stream request).

    from utils.rate_limit import RateGate, install_gate_on_session, rpm_from_env
    install_gate_on_session(self.s, RateGate(rpm_from_env('ORPHEUS_QOBUZ_RPM', 60)))
"""
import os
import threading
import time

_DEFAULT_RPM = 60


class RateGate:
    """Thread-safe fixed-interval spacer: block until the next request may proceed."""

    def __init__(self, rpm=_DEFAULT_RPM):
        rpm = rpm if (rpm and rpm > 0) else _DEFAULT_RPM
        self._interval = 60.0 / rpm
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            delay = self._interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


def rpm_from_env(name, default=_DEFAULT_RPM):
    try:
        v = int(os.environ.get(name, str(default)))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _settings_path():
    # utils/rate_limit.py -> <OrpheusDL root>/config/settings.json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'config', 'settings.json')


_settings_cache = {'mtime': None, 'data': None}


def _load_settings_rpm(service):
    """Read modules.<service>.rate_limit_rpm from settings.json (cached by mtime).
    Fully guarded: any problem -> None (caller falls back to env/default)."""
    try:
        path = _settings_path()
        mtime = os.path.getmtime(path)
        if _settings_cache['mtime'] != mtime:
            import json
            with open(path, encoding='utf-8') as f:
                _settings_cache['data'] = json.load(f)
            _settings_cache['mtime'] = mtime
        data = _settings_cache['data'] or {}
        val = data.get('modules', {}).get(service, {}).get('rate_limit_rpm')
        if val is None:
            return None
        return int(val)  # may be 0 (= disabled) or negative (default upstream)
    except Exception:
        return None


def rpm_for(service, env_name, default=_DEFAULT_RPM):
    """Resolve requests-per-minute for a service. Priority:
    1. environment variable (one-off override, e.g. ORPHEUS_QOBUZ_RPM),
    2. settings.json -> modules.<service>.rate_limit_rpm (the persistent knob),
    3. the built-in default.
    A value of exactly 0 (env or settings) means DISABLED (no gate). A negative or
    unparseable value falls back to the default so a typo can't silently disable
    the account protection.
    """
    env_val = os.environ.get(env_name)
    if env_val:
        try:
            v = int(env_val)
            return v if v >= 0 else default
        except (TypeError, ValueError):
            return default
    from_settings = _load_settings_rpm(service)
    if from_settings is None:
        return default
    return from_settings if from_settings >= 0 else default


def install_gate_on_session(session, gate):
    """Wrap session.request so every get/post/... is spaced by the gate. Idempotent:
    a session is never double-wrapped. Returns the session for chaining."""
    if session is None or getattr(session, '_rate_gate_installed', False):
        return session
    _original_request = session.request

    def _throttled_request(method, url, *args, **kwargs):
        gate.wait()
        return _original_request(method, url, *args, **kwargs)

    session.request = _throttled_request
    session._rate_gate_installed = True
    return session


def install_service_gate(session, service, env_name, default=_DEFAULT_RPM):
    """Resolve the service's RPM (env > settings.json > default) and install a
    proactive gate on the session — UNLESS the resolved RPM is 0, which means the
    user disabled pacing for that service (no gate installed at all). Returns the
    session for chaining."""
    rpm = rpm_for(service, env_name, default)
    if rpm and rpm > 0:
        install_gate_on_session(session, RateGate(rpm))
    return session
