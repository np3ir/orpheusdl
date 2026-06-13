"""OrpheusDL GUI — Streamlit interface for OrpheusDL fork."""
import streamlit as st
import os, subprocess, json, sys, time, requests, shutil
from pathlib import Path
from datetime import datetime

ORPHEUS_DIR   = Path(__file__).parent
SETTINGS_FILE = ORPHEUS_DIR / "config" / "settings.json"
SESSION_FILE  = ORPHEUS_DIR / "session.json"
COOKIES_FILE  = ORPHEUS_DIR / "config" / "cookies.txt"

TIDAL_AUTH_BASE = "https://auth.tidal.com/v1/"

st.set_page_config(page_title="OrpheusDL", page_icon="🎵", layout="wide")
st.title("🎵 OrpheusDL")
st.caption("Download music from TIDAL, Apple Music, Deezer and more.")

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_settings():
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        return json.load(f)

def tidal_tv_credentials():
    """TIDAL TV device-auth credentials → (client_id, client_secret).

    Read from env vars first, else from config/settings.json
    (modules.tidal.tv_atmos_token / tv_atmos_secret). Kept out of source so this
    public repo ships no hardcoded client secret — settings.json is gitignored.
    Either value may be None if unconfigured.
    """
    try:
        tcfg = (load_settings().get("modules", {}) or {}).get("tidal", {})
    except Exception:
        tcfg = {}
    cid    = os.environ.get("TIDAL_TV_CLIENT_ID")     or tcfg.get("tv_atmos_token")
    secret = os.environ.get("TIDAL_TV_CLIENT_SECRET") or tcfg.get("tv_atmos_secret")
    return cid, secret

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def tidal_session_valid():
    if not SESSION_FILE.exists():
        return False
    try:
        s = json.loads(SESSION_FILE.read_text())
        exp = s.get("expiry_time", "")
        if not exp:
            return bool(s.get("access_token"))
        return datetime.fromisoformat(exp) > datetime.now()
    except:
        return False

def pfmt(t):
    sample = {
        "{artist_initials}": "A", "{album_artist}": "Aitana",
        "{artists}": "Aitana", "{year}": "2026",
        "{album_clean}": "CUARTO AZUL", "{release}": "(ALBUM)",
        "{track_number}": "01", "{title_clean}": "Las Babas", "{explicit}": "",
    }
    for k, v in sample.items():
        t = t.replace(k, v)
    return t

def status_badge(ok, label_ok, label_no):
    if ok:
        st.success(f"✅ {label_ok}")
    else:
        st.warning(f"❌ {label_no}")

settings = load_settings()
general  = settings["global"]["general"]
fmt      = settings["global"]["formatting"]
modules  = settings.get("modules", {})

tab_login, tab_dl, tab_settings = st.tabs(["🔐 Login", "⬇️ Download", "⚙️ Settings"])

# ═══════════════════════════════════════════════════════════════════════════════
# LOGIN TAB
# ═══════════════════════════════════════════════════════════════════════════════
with tab_login:

    # ── TIDAL ────────────────────────────────────────────────────────────────
    with st.expander("🎵 TIDAL", expanded=True):
        if tidal_session_valid():
            info = json.loads(SESSION_FILE.read_text()) if SESSION_FILE.exists() else {}
            st.success("✅ Logged in to TIDAL")
            if info.get("expiry_time"):
                st.caption(f"Session expires: {info['expiry_time'][:19]}")
            if st.button("🚪 Logout TIDAL"):
                SESSION_FILE.unlink(missing_ok=True)
                st.rerun()
        else:
            st.warning("❌ Not logged in to TIDAL")
            if st.button("🔑 Login with TIDAL", type="primary", key="tidal_login"):
                st.session_state["tidal_auth_pending"] = True
                st.session_state["tidal_device_code"]  = None
                st.session_state["tidal_user_code"]    = None

        if st.session_state.get("tidal_auth_pending"):
            tidal_cid, tidal_secret = tidal_tv_credentials()
            if not tidal_cid or not tidal_secret:
                st.error("TIDAL TV credentials not configured — add "
                         "modules.tidal.tv_atmos_token / tv_atmos_secret to "
                         "config/settings.json (or set TIDAL_TV_CLIENT_ID / "
                         "TIDAL_TV_CLIENT_SECRET env vars).")
            if not st.session_state.get("tidal_device_code"):
                with st.spinner("Requesting device code..."):
                    r = requests.post(
                        TIDAL_AUTH_BASE + "oauth2/device_authorization",
                        data={"client_id": tidal_cid, "scope": "r_usr w_usr"},
                    )
                if r.status_code == 200:
                    st.session_state["tidal_device_code"] = r.json()["deviceCode"]
                    st.session_state["tidal_user_code"]   = r.json()["userCode"]
                else:
                    st.error("Failed to get device code.")
                    st.session_state["tidal_auth_pending"] = False

            if st.session_state.get("tidal_user_code"):
                code = st.session_state["tidal_user_code"]
                st.markdown(f"### Code: `{code}`")
                st.info(
                    f"1. Open **https://link.tidal.com/{code}** in your browser\n"
                    f"2. Log in with your TIDAL account\n"
                    f"3. Click **I've authorized** below"
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("✅ I've authorized"):
                        with st.spinner("Verifying..."):
                            for _ in range(30):
                                r = requests.post(
                                    TIDAL_AUTH_BASE + "oauth2/token",
                                    data={
                                        "client_id":     tidal_cid,
                                        "device_code":   st.session_state["tidal_device_code"],
                                        "client_secret": tidal_secret,
                                        "grant_type":    "urn:ietf:params:oauth:grant-type:device_code",
                                        "scope":         "r_usr w_usr",
                                    },
                                )
                                if r.status_code == 200:
                                    token = r.json()
                                    token["expiry_time"] = datetime.fromtimestamp(
                                        time.time() + token.get("expires_in", 86400)
                                    ).isoformat()
                                    SESSION_FILE.write_text(json.dumps(token, indent=2))
                                    for k in ["tidal_auth_pending", "tidal_device_code", "tidal_user_code"]:
                                        st.session_state.pop(k, None)
                                    st.success("✅ Logged in!")
                                    st.rerun()
                                time.sleep(1)
                            st.error("Timeout. Try again.")
                with c2:
                    if st.button("❌ Cancel", key="tidal_cancel"):
                        for k in ["tidal_auth_pending", "tidal_device_code", "tidal_user_code"]:
                            st.session_state.pop(k, None)
                        st.rerun()

    # ── APPLE MUSIC ──────────────────────────────────────────────────────────
    with st.expander("🍎 Apple Music"):
        if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 100:
            mtime = datetime.fromtimestamp(COOKIES_FILE.stat().st_mtime)
            st.success(f"✅ Cookies found (updated {mtime.strftime('%Y-%m-%d')})")
        else:
            st.warning("❌ No cookies found")

        st.caption("Export cookies from **music.apple.com** using the **Get cookies.txt LOCALLY** browser extension.")
        uploaded = st.file_uploader("Upload cookies.txt", type=["txt"], key="am_cookies")
        if uploaded:
            COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_FILE.write_bytes(uploaded.read())
            st.success("✅ Cookies saved!")
            st.rerun()

    # ── DEEZER ───────────────────────────────────────────────────────────────
    with st.expander("🎶 Deezer"):
        deezer_cfg = modules.get("deezer", {})
        current_arl = deezer_cfg.get("arl", "")
        status_badge(bool(current_arl), "ARL token configured", "ARL token not set")
        st.caption("Get your ARL from Deezer cookies: open deezer.com → DevTools → Application → Cookies → copy **arl** value.")
        new_arl = st.text_input("ARL token", value=current_arl, type="password", key="deezer_arl")
        if st.button("💾 Save Deezer ARL"):
            settings.setdefault("modules", {}).setdefault("deezer", {})["arl"] = new_arl
            save_settings(settings)
            st.success("✅ Saved!")
            st.rerun()

    # ── BEATPORT ─────────────────────────────────────────────────────────────
    with st.expander("🎛️ Beatport"):
        bp_cfg = modules.get("beatport", {})
        status_badge(bool(bp_cfg.get("username")), "Credentials configured", "Not logged in")
        c1, c2 = st.columns(2)
        with c1:
            bp_user = st.text_input("Username / Email", value=bp_cfg.get("username", ""), key="bp_user")
        with c2:
            bp_pass = st.text_input("Password", value=bp_cfg.get("password", ""), type="password", key="bp_pass")
        if st.button("💾 Save Beatport"):
            settings.setdefault("modules", {}).setdefault("beatport", {}).update(
                {"username": bp_user, "password": bp_pass}
            )
            save_settings(settings)
            st.success("✅ Saved!")
            st.rerun()

    # ── BEATSOURCE ───────────────────────────────────────────────────────────
    with st.expander("🎛️ Beatsource"):
        bs_cfg = modules.get("beatsource", {})
        status_badge(bool(bs_cfg.get("username")), "Credentials configured", "Not logged in")
        c1, c2 = st.columns(2)
        with c1:
            bs_user = st.text_input("Username / Email", value=bs_cfg.get("username", ""), key="bs_user")
        with c2:
            bs_pass = st.text_input("Password", value=bs_cfg.get("password", ""), type="password", key="bs_pass")
        if st.button("💾 Save Beatsource"):
            settings.setdefault("modules", {}).setdefault("beatsource", {}).update(
                {"username": bs_user, "password": bs_pass}
            )
            save_settings(settings)
            st.success("✅ Saved!")
            st.rerun()

    # ── IDAGIO ───────────────────────────────────────────────────────────────
    with st.expander("🎻 IDAGIO"):
        id_cfg = modules.get("idagio", {})
        status_badge(bool(id_cfg.get("username")), "Credentials configured", "Not logged in")
        c1, c2 = st.columns(2)
        with c1:
            id_user = st.text_input("Username / Email", value=id_cfg.get("username", ""), key="id_user")
        with c2:
            id_pass = st.text_input("Password", value=id_cfg.get("password", ""), type="password", key="id_pass")
        if st.button("💾 Save IDAGIO"):
            settings.setdefault("modules", {}).setdefault("idagio", {}).update(
                {"username": id_user, "password": id_pass}
            )
            save_settings(settings)
            st.success("✅ Saved!")
            st.rerun()

    # ── BUGS ─────────────────────────────────────────────────────────────────
    with st.expander("🐛 Bugs (Bugsmedia)"):
        bugs_cfg = modules.get("bugs", {})
        status_badge(bool(bugs_cfg.get("username")), "Credentials configured", "Not logged in")
        c1, c2 = st.columns(2)
        with c1:
            bugs_user = st.text_input("Username / Email", value=bugs_cfg.get("username", ""), key="bugs_user")
        with c2:
            bugs_pass = st.text_input("Password", value=bugs_cfg.get("password", ""), type="password", key="bugs_pass")
        if st.button("💾 Save Bugs"):
            settings.setdefault("modules", {}).setdefault("bugs", {}).update(
                {"username": bugs_user, "password": bugs_pass}
            )
            save_settings(settings)
            st.success("✅ Saved!")
            st.rerun()

    # ── SOUNDCLOUD ───────────────────────────────────────────────────────────
    with st.expander("☁️ SoundCloud"):
        sc_cfg = modules.get("soundcloud", {})
        current_token = sc_cfg.get("web_access_token", "")
        status_badge(bool(current_token), "Token configured", "Token not set")
        st.caption("Get your token from soundcloud.com → DevTools → Application → Local Storage → `sc_anonymous_id` or network request headers.")
        new_sc_token = st.text_input("Web Access Token", value=current_token, type="password", key="sc_token")
        if st.button("💾 Save SoundCloud"):
            settings.setdefault("modules", {}).setdefault("soundcloud", {})["web_access_token"] = new_sc_token
            save_settings(settings)
            st.success("✅ Saved!")
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD TAB
# ═══════════════════════════════════════════════════════════════════════════════
with tab_dl:
    if not tidal_session_valid():
        st.warning("⚠️ Not logged in to TIDAL. Go to the **Login** tab first.")

    c1, c2 = st.columns([3, 1])
    with c1:
        url = st.text_input(
            "URL",
            placeholder="https://tidal.com/album/...   or   https://music.apple.com/...",
        )
    with c2:
        quality = st.selectbox(
            "Quality", ["lossless", "hifi", "high", "low"],
            index=["lossless", "hifi", "high", "low"].index(general.get("download_quality", "lossless")),
        )

    if url:
        prev = (
            f"{general['download_path']}"
            f"{pfmt(fmt.get('album_format', ''))}"
            f"/{pfmt(fmt.get('track_filename_format', ''))}.flac"
        )
        st.info(f"📁 Output: `{prev}`")

    run_btn = st.button("⬇️ Download", type="primary", disabled=not url)
    output_area = st.empty()

    if run_btn and url:
        cmd = [sys.executable, str(ORPHEUS_DIR / "orpheus.py"), url]
        lines = []
        with st.spinner("Downloading..."):
            proc = subprocess.Popen(
                cmd, cwd=str(ORPHEUS_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    lines.append(line)
                    output_area.code("\n".join(lines[-25:]))
            proc.wait()
        if proc.returncode == 0:
            st.success("✅ Download complete!")
        else:
            st.error("❌ Download failed.")

# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS TAB
# ═══════════════════════════════════════════════════════════════════════════════
with tab_settings:
    st.subheader("📁 Paths")
    c1, c2 = st.columns(2)
    with c1:
        new_dl  = st.text_input("Download path", value=general["download_path"])
    with c2:
        new_vid = st.text_input("Video path",    value=general.get("video_download_path", ""))

    st.subheader("🎼 Templates")
    st.caption("Tokens: `{artist_initials}` `{album_artist}` `{artists}` `{year}` `{album_clean}` `{release}` `{track_number}` `{title_clean}` `{explicit}`")
    c3, c4 = st.columns(2)
    with c3:
        new_album_fmt = st.text_input("Album folder",   value=fmt.get("album_format", ""))
        new_track_fmt = st.text_input("Track filename", value=fmt.get("track_filename_format", ""))
    with c4:
        new_pl_fmt = st.text_input("Playlist format", value=fmt.get("playlist_format", ""))
        new_sg_fmt = st.text_input("Single path",     value=fmt.get("single_full_path_format", ""))

    if new_album_fmt and new_track_fmt:
        prev = f"{new_dl}{pfmt(new_album_fmt)}/{pfmt(new_track_fmt)}.flac"
        st.info(f"📁 Preview: `{prev}`")

    st.subheader("⚡ Performance")
    c5, c6 = st.columns(2)
    with c5:
        new_conc   = st.slider("Concurrent downloads", 1, 5,   int(general.get("concurrent_downloads", 1)))
        new_td_min = st.slider("Track delay min (s)",  0, 30,  int(general.get("inter_track_delay_min", 10)))
        new_td_max = st.slider("Track delay max (s)",  0, 60,  int(general.get("inter_track_delay_max", 20)))
    with c6:
        new_ad_min = st.slider("Album delay min (s)",  0, 60,  int(general.get("inter_album_delay_min", 30)))
        new_ad_max = st.slider("Album delay max (s)",  0, 120, int(general.get("inter_album_delay_max", 60)))

    st.subheader("🎚️ Quality & Codecs")
    c7, c8 = st.columns(2)
    with c7:
        new_q = st.selectbox(
            "Default quality", ["lossless", "hifi", "high", "low"],
            index=["lossless", "hifi", "high", "low"].index(general.get("download_quality", "lossless")),
            key="sq",
        )
    with c8:
        spatial     = st.checkbox("Spatial codecs (Atmos / 360)", value=settings["global"]["codecs"].get("spatial_codecs", True))
        proprietary = st.checkbox("Proprietary codecs",           value=settings["global"]["codecs"].get("proprietary_codecs", False))

    if st.button("💾 Save settings", type="primary"):
        general["download_path"]         = new_dl
        general["video_download_path"]   = new_vid
        general["download_quality"]      = new_q
        general["concurrent_downloads"]  = new_conc
        general["inter_track_delay_min"] = new_td_min
        general["inter_track_delay_max"] = new_td_max
        general["inter_album_delay_min"] = new_ad_min
        general["inter_album_delay_max"] = new_ad_max
        fmt["album_format"]              = new_album_fmt
        fmt["track_filename_format"]     = new_track_fmt
        fmt["playlist_format"]           = new_pl_fmt
        fmt["single_full_path_format"]   = new_sg_fmt
        settings["global"]["codecs"]["spatial_codecs"]     = spatial
        settings["global"]["codecs"]["proprietary_codecs"] = proprietary
        save_settings(settings)
        st.success("✅ Settings saved!")
        st.rerun()
