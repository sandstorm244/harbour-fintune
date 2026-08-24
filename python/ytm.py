"""FinTune metadata layer: YouTube Music's InnerTube API over urllib.

This is the *what to show* half of FinTune. It never resolves streams — it hands
`videoId`s to the engine (`youfish.resolve`), which does the playback plumbing.

Design choices (see PLAN.md):
  * Pure `urllib` — no `requests`, no bundled ytmusicapi. Same dependency-light
    ethos as the engine. Renderer shapes are lifted from ytmusicapi (open source),
    but we own the parsers so there is nothing to keep in sync but our own code.
  * Runs on PyOtherSide's worker thread, so blocking HTTP is fine here.
  * Logged-out is a first-class state: search + generic browse work with no auth.
    Auth (OAuth device flow) unlocks the *personalized* home + library.

VALIDATE-ON-DEVICE: the OAuth section uses the public "YouTube on TV" limited-input
client — the same creds yt-dlp's oauth and MicroTube's login use. Google has
tightened this before; if device login stops working, this is the first suspect
(see PLAN.md "Auth model" for fallbacks). The unauth search/browse path does not
depend on any of it.
"""

import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_APP = "harbour-fintune"

_YTM_ORIGIN = "https://music.youtube.com"
_INNERTUBE = _YTM_ORIGIN + "/youtubei/v1/"
# WEB_REMIX InnerTube identity. These are the SHIPPED DEFAULTS (last-known-good) — the live key +
# client version are auto-detected from music.youtube.com's ytcfg blob and cached in the data dir,
# so a client-version rotation heals itself without an app update. See _ytm_config().
_DEFAULT_INNERTUBE_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"   # public site key, not a secret
_DEFAULT_CLIENT_VERSION = "1.20241127.01.00"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# "YouTube on TV" OAuth client (limited-input device flow). Public creds, same as
# yt-dlp's youtube oauth. See module docstring / PLAN.md for the risk note.
_OAUTH_CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
_OAUTH_CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
_OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"
_OAUTH_DEVICE_URL = "https://oauth2.googleapis.com/device/code"
_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_OAUTH_GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"

_DEBUG = bool(os.environ.get("YOUFISH_DEBUG"))


def _log(msg):
    if _DEBUG:
        try:
            print("[ytm] " + msg)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Paths (FinTune's own data dir — separate from FinTube's)
# --------------------------------------------------------------------------- #

def _data_dir():
    d = os.path.expanduser("~/.local/share/" + _APP)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _tokens_path():
    return os.path.join(_data_dir(), "ytm_tokens.json")


def _cookies_path():
    return os.path.join(_data_dir(), "ytm_cookies.json")


# --------------------------------------------------------------------------- #
# IPv4 pin (this device's IPv6 path is a black hole — see youfish._force_ipv4)
# --------------------------------------------------------------------------- #

_ipv4_forced = False


def _force_ipv4():
    global _ipv4_forced
    if _ipv4_forced:
        return
    _orig = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only
    _ipv4_forced = True


# --------------------------------------------------------------------------- #
# Low-level HTTP
# --------------------------------------------------------------------------- #

def _post_form(url, fields, timeout=30):
    _force_ipv4()
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _read_body(resp):
    """Read a response body, transparently gunzipping it when the server compressed it. InnerTube
    browse responses are large (a playlist can be several MB of JSON); gzip cuts the transfer
    ~5-8×, which is the bulk of a playlist's load time on a mobile connection. Works on both a
    normal response and an HTTPError (both expose .headers + .read())."""
    raw = resp.read()
    try:
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    except Exception:
        enc = ""
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw


def _http_get_json(url, timeout=15):
    """Plain GET → JSON (for non-InnerTube services like LRCLIB). IPv4-pinned like the rest."""
    _force_ipv4()
    req = urllib.request.Request(url, headers={
        "User-Agent": "FinTune (harbour-fintune; SailfishOS YouTube Music client)",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(_read_body(resp).decode())


# --------------------------------------------------------------------------- #
# Self-healing InnerTube identity (key + client version)
#
# YTM ships its current InnerTube API key and WEB_REMIX client version inside the ytcfg blob on
# every page. We scrape those, cache them in the data dir, and use them in place of the shipped
# defaults — so when YouTube rotates the client version (the value that actually drifts and starts
# 400ing) the app follows along on its own, no rebuild. The request path NEVER blocks on the
# network: it returns cached-or-default immediately and refreshes in the background.
# --------------------------------------------------------------------------- #

_YTM_CFG_TTL = 12 * 3600            # re-scrape the live identity at most this often
_ytm_cfg_cache = None              # in-memory {"key","version","ts"}
_ytm_cfg_lock = threading.Lock()
_ytm_cfg_warming = False


def _ytm_config_path():
    return os.path.join(_data_dir(), "ytm_config.json")


def _ytm_cfg_load():
    try:
        with open(_ytm_config_path()) as f:
            d = json.load(f)
        return {"key": d.get("key") or _DEFAULT_INNERTUBE_KEY,
                "version": d.get("version") or _DEFAULT_CLIENT_VERSION,
                "ts": float(d.get("ts", 0))}
    except Exception:
        return {"key": _DEFAULT_INNERTUBE_KEY, "version": _DEFAULT_CLIENT_VERSION, "ts": 0}


def _ytm_config():
    """Live InnerTube identity, self-healing. Returns the cached auto-detected values when fresh,
    the shipped defaults when cold, and kicks a background refresh when stale. Never makes a
    network call on the request path — a browse/search always has a usable identity at once."""
    global _ytm_cfg_cache
    cfg = _ytm_cfg_cache
    if cfg is None:
        cfg = _ytm_cfg_load()
        _ytm_cfg_cache = cfg
    if time.time() - cfg.get("ts", 0) > _YTM_CFG_TTL:
        _warm_ytm_config()
    return cfg


def _fetch_ytm_identity(timeout=15):
    """Scrape the current InnerTube key + WEB_REMIX client version from music.youtube.com's ytcfg.
    Returns (key, version); either may be None if not found."""
    _force_ipv4()
    req = urllib.request.Request(_YTM_ORIGIN + "/", headers={
        "User-Agent": _BROWSER_UA,
        "Accept-Encoding": "gzip",
        "Cookie": "SOCS=CAI;",          # skip the EU consent interstitial (serves the real page)
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = _read_body(resp).decode("utf-8", "replace")
    except Exception:
        return (None, None)
    m_key = re.search(r'"INNERTUBE_API_KEY":\s*"([^"]+)"', html)
    m_ver = re.search(r'"INNERTUBE_CLIENT_VERSION":\s*"([^"]+)"', html)
    key = m_key.group(1) if m_key else None
    ver = m_ver.group(1) if m_ver else None
    if ver and not re.match(r"^\d+\.\d{6}", ver):   # sanity: looks like 1.YYYYMMDD.xx.xx
        ver = None
    return (key, ver)


def _warm_ytm_config():
    """Refresh the cached identity from the live site, in the background (deduped)."""
    global _ytm_cfg_warming
    with _ytm_cfg_lock:
        if _ytm_cfg_warming:
            return
        _ytm_cfg_warming = True

    def _bg():
        global _ytm_cfg_cache, _ytm_cfg_warming
        try:
            key, ver = _fetch_ytm_identity()
            if ver:      # the version is the part that drifts; only commit when we actually got one
                cfg = {"key": key or _DEFAULT_INNERTUBE_KEY, "version": ver, "ts": time.time()}
                try:
                    with open(_ytm_config_path(), "w") as f:
                        json.dump(cfg, f)
                except Exception:
                    pass
                _ytm_cfg_cache = cfg
                _log("innertube identity refreshed: version=%s" % ver)
        except Exception:
            pass
        finally:
            _ytm_cfg_warming = False

    threading.Thread(target=_bg, daemon=True).start()


def _context(auth=False):
    """InnerTube request context. Personalized calls need a logged-in context flag."""
    ctx = {
        "client": {
            "clientName": "WEB_REMIX",
            "clientVersion": _ytm_config()["version"],
            "hl": "en",
            "gl": "US",
        },
        "user": {"lockedSafetyMode": False},
    }
    return ctx


def _innertube(endpoint, body, use_auth=True, timeout=30):
    """POST an InnerTube endpoint. When `use_auth`, authenticates with the imported browser
    cookies (SAPISIDHASH) if present, else an OAuth Bearer token if present; otherwise makes an
    anonymous (public-key) request."""
    _force_ipv4()
    payload = {"context": _context()}
    payload.update(body or {})
    mode = _auth_mode() if use_auth else "none"
    headers = {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/json",
        "Origin": _YTM_ORIGIN,
        "Referer": _YTM_ORIGIN + "/",
        "Accept-Encoding": "gzip",
    }
    # Cookie auth keeps ?key= (like the web client); OAuth Bearer must drop it (key + token
    # together is a 400 INVALID_ARGUMENT); anonymous uses the key.
    if mode == "cookie":
        ck = _load_cookies()
        headers["Cookie"] = ck.get("cookie", "")
        headers["Authorization"] = _sapisidhash(ck.get("sapisid", ""), _YTM_ORIGIN)
        headers["X-Goog-AuthUser"] = "0"
        # Read the visitor id from cache only — never block a browse/search on the homepage
        # fetch. If it's cold, warm it in the background for next time (get_home pre-warms it, so
        # personalized home still carries it). Authed calls are fine without it — the account
        # cookies do the authenticating.
        vid = _visitor_id_cached()
        if vid:
            headers["X-Goog-Visitor-Id"] = vid
        else:
            _warm_visitor_id()
        url = _INNERTUBE + endpoint + "?key=" + _ytm_config()["key"] + "&prettyPrint=false"
    elif mode == "oauth":
        tok = _access_token()
        if tok:
            headers["Authorization"] = "Bearer " + tok
        url = _INNERTUBE + endpoint + "?prettyPrint=false"
    else:
        url = _INNERTUBE + endpoint + "?key=" + _ytm_config()["key"] + "&prettyPrint=false"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(_read_body(resp).decode())
            if mode == "cookie":
                _absorb_rotations(resp, req)    # keep the session fresh off our own traffic
            return data
    except urllib.error.HTTPError as he:
        body = ""
        try:
            body = _read_body(he).decode()[:300]
        except Exception:
            pass
        _log("innertube %s mode=%s HTTP %s: %s" % (endpoint, mode, he.code, body))
        if he.code == 400:
            _warm_ytm_config()   # a 400 is often a stale client version — re-scrape for next time
        raise


# --------------------------------------------------------------------------- #
# OAuth device flow
# --------------------------------------------------------------------------- #

_tokens_lock = threading.Lock()


def _load_tokens():
    try:
        with open(_tokens_path()) as f:
            t = json.load(f)
        return t if isinstance(t, dict) else {}
    except Exception:
        return {}


def _save_tokens(t):
    with _tokens_lock:
        try:
            path = _tokens_path()
            with open(path, "w") as f:
                json.dump(t, f)
            os.chmod(path, 0o600)     # OAuth refresh token — owner-only
        except Exception:
            pass


def is_logged_in():
    return _auth_mode() != "none"


def logout():
    for p in (_tokens_path(), _cookies_path()):
        try:
            os.remove(p)
        except Exception:
            pass
    return {"logged_in": False}


def account_status():
    """For QML: are we signed in, and by which method (cookie / oauth / none)."""
    return {"logged_in": is_logged_in(), "method": _auth_mode()}


def _access_token():
    """A valid access token, refreshing from the stored refresh token if expired.
    Returns None when logged out or refresh fails."""
    t = _load_tokens()
    if not t.get("refresh_token"):
        return None
    if t.get("access_token") and t.get("expires_at", 0) > time.time() + 60:
        return t["access_token"]
    try:
        r = _post_form(_OAUTH_TOKEN_URL, {
            "client_id": _OAUTH_CLIENT_ID,
            "client_secret": _OAUTH_CLIENT_SECRET,
            "refresh_token": t["refresh_token"],
            "grant_type": "refresh_token",
        })
    except Exception as ex:
        _log("refresh failed: %s" % ex)
        return None
    if "access_token" not in r:
        return None
    t["access_token"] = r["access_token"]
    t["expires_at"] = time.time() + int(r.get("expires_in", 3600))
    _save_tokens(t)
    return t["access_token"]


def login_begin():
    """Kick off OAuth device login on a background thread.

    Emits (pyotherside):
      ytm_login_code {user_code, verification_url}   — show these to the user
      ytm_login_done {ok, error}                     — terminal result
    """
    import pyotherside

    def worker():
        try:
            dev = _post_form(_OAUTH_DEVICE_URL, {
                "client_id": _OAUTH_CLIENT_ID,
                "scope": _OAUTH_SCOPE,
            })
        except Exception as ex:
            pyotherside.send("ytm_login_done", False, "Couldn't start login: %s" % ex)
            return
        code = dev.get("user_code", "")
        url = dev.get("verification_url") or dev.get("verification_uri") or "https://google.com/device"
        device_code = dev.get("device_code", "")
        interval = int(dev.get("interval", 5)) or 5
        expires = int(dev.get("expires_in", 1800)) or 1800
        if not device_code:
            pyotherside.send("ytm_login_done", False, "Login service gave no device code.")
            return
        pyotherside.send("ytm_login_code", code, url)

        deadline = time.time() + expires
        while time.time() < deadline:
            time.sleep(interval)
            try:
                r = _post_form(_OAUTH_TOKEN_URL, {
                    "client_id": _OAUTH_CLIENT_ID,
                    "client_secret": _OAUTH_CLIENT_SECRET,
                    "device_code": device_code,
                    "grant_type": _OAUTH_GRANT_DEVICE,
                })
            except urllib.error.HTTPError as he:
                # authorization_pending / slow_down come back as 4xx with a JSON body.
                try:
                    r = json.loads(he.read().decode())
                except Exception:
                    r = {"error": "http_%s" % he.code}
            except Exception as ex:
                pyotherside.send("ytm_login_done", False, str(ex))
                return

            if "access_token" in r:
                t = {
                    "refresh_token": r.get("refresh_token", ""),
                    "access_token": r["access_token"],
                    "expires_at": time.time() + int(r.get("expires_in", 3600)),
                }
                _save_tokens(t)
                pyotherside.send("ytm_login_done", True, "")
                return
            err = r.get("error", "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err in ("access_denied", "expired_token"):
                pyotherside.send("ytm_login_done", False,
                                 "Login " + ("denied" if err == "access_denied" else "expired") + ".")
                return
            # Unknown error — stop rather than hammer.
            pyotherside.send("ytm_login_done", False, err or "Login failed.")
            return
        pyotherside.send("ytm_login_done", False, "Login timed out.")

    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------- #
# Browser-cookie ("SAPISIDHASH") auth — the reliable path. FinTune is unsandboxed, so we read
# the Sailfish browser's own cookie jar after the user signs into music.youtube.com there (a
# real browser, so Google's embedded-webview login block never applies). No copy-paste, no
# Google Cloud client. Mirrors ytmusicapi's "browser" auth.
# --------------------------------------------------------------------------- #

_BROWSER_COOKIE_PATHS = [
    "~/.local/share/org.sailfishos/browser/.mozilla/cookies.sqlite",
    "~/.mozilla/mozembed/cookies.sqlite",
    "~/.local/share/org.sailfishos/sailfish-browser/.mozilla/cookies.sqlite",
]

# The SAPISID (for the auth hash) plus the session cookies YouTube checks; we rebuild a Cookie
# header from whichever of these the jar has.
_AUTH_COOKIE_NAMES = ("__Secure-3PAPISID", "SAPISID", "__Secure-1PAPISID",
                      "__Secure-3PSID", "__Secure-1PSID", "SID", "HSID", "SSID",
                      "APISID", "SIDCC", "__Secure-3PSIDCC", "__Secure-1PSIDCC",
                      "LOGIN_INFO", "PREF", "VISITOR_INFO1_LIVE",
                      "__Secure-3PSIDTS", "__Secure-1PSIDTS")


def _load_cookies():
    try:
        with open(_cookies_path()) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


# Serialises the read-modify-write done by the rotation write-back (and any other save), so two
# concurrent InnerTube calls can't lose each other's rotation via last-writer-wins.
_cookies_lock = threading.Lock()


def _save_cookies(c):
    # Atomic + private: write a fresh temp in the same dir (mkstemp creates it 0600, so there's no
    # world-readable create window), then os.replace() over the target. os.replace is atomic on
    # POSIX, so a crash mid-write can't truncate the live cookie file and log the user out.
    try:
        d = _data_dir()
        fd, tmp = tempfile.mkstemp(prefix=".ytm_cookies.", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(c, f)
            os.chmod(tmp, 0o600)      # session cookies — owner-only
            os.replace(tmp, _cookies_path())
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass


def _read_cookie_jar(path):
    """Read google/youtube cookie rows from a Firefox/Gecko cookies.sqlite. Returns a list of
    (name, value, host, cpath, expiry, secure). Copies the DB first so a running browser's
    lock/WAL doesn't block us."""
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
        shutil.copyfile(path, tmp)
        for ext in ("-wal", "-shm"):     # bring recent writes (a fresh login) along
            if os.path.exists(path + ext):
                shutil.copyfile(path + ext, tmp + ext)
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT name, value, host, path, expiry, isSecure FROM moz_cookies "
                "WHERE host LIKE '%youtube.com' OR host LIKE '%google.com'").fetchall()
        finally:
            con.close()
        return rows
    except Exception as ex:
        _log("cookie read failed (%s): %s" % (path, ex))
        return []
    finally:
        for p in ((tmp, tmp + "-wal", tmp + "-shm") if tmp else ()):
            try:
                os.remove(p)
            except Exception:
                pass


def _netscape_from_rows(rows):
    """Build Netscape/Mozilla cookies.txt TEXT from cookie rows (for yt-dlp --cookies). Kept as
    text inside the 0600 cookie store; the engine materialises it to an ephemeral, private file
    only for the duration of a single yt-dlp call — never a persistent cookies file on disk."""
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value, host, cpath, expiry, secure in rows:
        if not host:
            continue
        lines.append("\t".join([host, "TRUE" if host.startswith(".") else "FALSE",
                                cpath or "/", "TRUE" if secure else "FALSE",
                                str(int(expiry) if expiry else 0), name, value]))
    return "\n".join(lines) + "\n"


def netscape_cookies():
    """The imported session as Netscape cookies.txt text, or '' when not signed in. Consumed by
    the engine (youfish) to write an ephemeral, private cookies file per yt-dlp call."""
    return _load_cookies().get("ytdlp", "")


# --------------------------------------------------------------------------- #
# Cookie write-back. A browser session never goes stale because the browser keeps talking to
# Google and persists the rotated cookies (SIDCC / __Secure-*SIDTS / …) that come back as
# Set-Cookie on every authenticated response. FinTune makes those same authenticated calls, so
# we do the same: fold any rotations from our OWN responses back into the 0600 store. That keeps
# the signed-in session alive off the app's own traffic — no dependency on the user re-opening
# the Sailfish browser. Account-level events (sign-out, password change, forced re-auth) still
# need a fresh Import; nothing client-side can refresh those.
# --------------------------------------------------------------------------- #

def _refresh_netscape(text, updates):
    """Rewrite the values of any rotated cookies inside a Netscape cookies.txt blob, so the
    yt-dlp cookie text tracks the same rotations we fold into the InnerTube header."""
    if not text or not updates:
        return text
    out = []
    for line in text.split("\n"):
        if line and not line.startswith("#") and "\t" in line:
            cols = line.split("\t")
            if len(cols) == 7 and cols[5] in updates:   # cols[5]=name, cols[6]=value
                cols[6] = updates[cols[5]]
                out.append("\t".join(cols))
                continue
        out.append(line)
    return "\n".join(out)


def _persist_rotations(updates):
    """Merge cookies Google rotated back into the 0600 store — both the InnerTube Cookie header
    and the yt-dlp Netscape text. Existing cookies are updated in place; a rotated cookie we
    don't yet have is added only if it's one we track. Writes only when a value actually changed
    (so the common no-rotation response is a cheap no-op). The whole read-modify-write is under
    _cookies_lock so overlapping InnerTube calls can't lose each other's rotation."""
    with _cookies_lock:
        stored = _load_cookies()
        if not stored.get("cookie"):
            return
        pairs, idx = [], {}
        for part in stored["cookie"].split(";"):
            part = part.strip()
            if "=" in part:
                n, v = part.split("=", 1)
                idx[n] = len(pairs)
                pairs.append([n, v])
        changed = 0
        for n, v in updates.items():
            if n in idx:
                if pairs[idx[n]][1] != v:
                    pairs[idx[n]][1] = v
                    changed += 1
            elif n in _AUTH_COOKIE_NAMES:
                idx[n] = len(pairs)
                pairs.append([n, v])
                changed += 1
        if not changed:
            return
        stored["cookie"] = "; ".join("%s=%s" % (n, v) for n, v in pairs)
        sap = updates.get("__Secure-3PAPISID") or updates.get("SAPISID")
        if sap:
            stored["sapisid"] = sap
        if stored.get("ytdlp"):
            stored["ytdlp"] = _refresh_netscape(stored["ytdlp"], updates)
        stored["rotated_at"] = int(time.time())
        _save_cookies(stored)
        _log("cookie rotation persisted (%d refreshed)" % changed)


def _absorb_rotations(resp, req):
    """After an authenticated InnerTube call, capture any Set-Cookie rotations Google returned
    and fold them into the store. Best-effort and never fatal — a failure here just means the
    session ages a bit faster, exactly as it did before this existed."""
    try:
        if not (resp.headers.get_all("Set-Cookie") or []):
            return                              # fast path: nothing rotated on this response
        jar = http.cookiejar.CookieJar()
        jar.extract_cookies(resp, req)          # stdlib parses Set-Cookie (quoting, attrs, …)
        updates = dict((c.name, c.value) for c in jar if c.value is not None)
        if updates:
            _persist_rotations(updates)
    except Exception as ex:
        _log("cookie rotation absorb skipped: %s" % ex)


def import_browser_login():
    """Import the signed-in YouTube Music session from the Sailfish browser's cookie jar.
    Returns {ok, error?, count}. Called from QML (Settings -> Import login from browser)."""
    rows, used = [], ""
    for cand in _BROWSER_COOKIE_PATHS:
        p = os.path.expanduser(cand)
        if os.path.isfile(p):
            rows = _read_cookie_jar(p)
            used = p
            if rows:
                break
    if not rows:
        return {"ok": False, "count": 0,
                "error": "No browser cookies found. Open the Sailfish browser, sign in at "
                         "music.youtube.com, then try Import again."}
    jar = {name: value for (name, value, host, cpath, expiry, secure) in rows}
    sapisid = jar.get("__Secure-3PAPISID") or jar.get("SAPISID")
    if not sapisid:
        return {"ok": False, "count": 0,
                "error": "Found browser cookies, but not a signed-in Google session. Sign in "
                         "at music.youtube.com in the Sailfish browser first."}
    pairs = [(n, jar[n]) for n in _AUTH_COOKIE_NAMES if n in jar]
    cookie_header = "; ".join("%s=%s" % (n, v) for n, v in pairs)
    # Store everything in the single 0600 cookie file: the Cookie header + SAPISID for InnerTube
    # auth, and the Netscape text for yt-dlp (age-gated tracks, fewer 403s) — the latter is only
    # ever materialised to an ephemeral file by the engine, never persisted as plaintext.
    _save_cookies({"cookie": cookie_header, "sapisid": sapisid,
                   "ytdlp": _netscape_from_rows(rows),
                   "source": used, "imported_at": int(time.time())})
    _log("imported %d cookies (%d rows) from %s" % (len(pairs), len(rows), used))
    return {"ok": True, "count": len(pairs)}


def _sapisidhash(sapisid, origin):
    """The web client's SAPISIDHASH auth value: 'SAPISIDHASH <ts>_<sha1(ts sapisid origin)>'."""
    ts = str(int(time.time()))
    digest = hashlib.sha1(("%s %s %s" % (ts, sapisid, origin)).encode()).hexdigest()
    return "SAPISIDHASH %s_%s" % (ts, digest)


_visitor_cache = {"id": "", "at": 0}


def _visitor_id():
    """A visitor id from the YTM homepage ytcfg, cached ~1h. Best-effort (omitted on failure)."""
    if _visitor_cache["id"] and time.time() - _visitor_cache["at"] < 3600:
        return _visitor_cache["id"]
    try:
        _force_ipv4()
        req = urllib.request.Request(_YTM_ORIGIN, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
        m = (re.search(r'"VISITOR_DATA":"(.*?)"', html)
             or re.search(r'"visitorData":"(.*?)"', html))
        vid = m.group(1) if m else ""
        if vid:
            _visitor_cache.update({"id": vid, "at": int(time.time())})
        return vid
    except Exception as ex:
        _log("visitor id fetch failed: %s" % ex)
        return ""


def _visitor_id_cached():
    """The visitor id if it's already cached and fresh, else '' — never fetches (non-blocking)."""
    if _visitor_cache["id"] and time.time() - _visitor_cache["at"] < 3600:
        return _visitor_cache["id"]
    return ""


_visitor_warm_lock = threading.Lock()
_visitor_warming = False


def _warm_visitor_id():
    """Populate the visitor-id cache in the background (single-flight) so the next InnerTube call
    has it, without blocking this one on the homepage fetch."""
    global _visitor_warming
    with _visitor_warm_lock:
        if _visitor_warming:
            return
        _visitor_warming = True

    def _w():
        global _visitor_warming
        try:
            _visitor_id()
        finally:
            with _visitor_warm_lock:
                _visitor_warming = False

    threading.Thread(target=_w, daemon=True).start()


def _auth_mode():
    """Which auth we'll use for personalized requests, in preference order."""
    if _load_cookies().get("sapisid"):
        return "cookie"
    if _load_tokens().get("refresh_token"):
        return "oauth"
    return "none"


# --------------------------------------------------------------------------- #
# Renderer navigation helpers (defensive — YouTube reshuffles these)
# --------------------------------------------------------------------------- #

def _nav(obj, path, default=None):
    """Walk a nested dict/list by a path of keys/indices; return default on any miss."""
    cur = obj
    for k in path:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


def _runs_text(node):
    """Join a {runs:[{text}...]} text node into a plain string."""
    runs = _nav(node, ["runs"], [])
    return "".join(r.get("text", "") for r in runs) if runs else (node.get("simpleText", "") if isinstance(node, dict) else "")


def _artist_id_from_runs(node):
    """Find the artist channel browseId inside a byline/subtitle {runs:[...]} node. Artist
    ("channel") browseIds start with UC; album (MPRE) / playlist links in the same byline don't,
    so the prefix picks the artist out cleanly. '' when the byline carries no artist link."""
    for r in (_nav(node, ["runs"], []) or []):
        bid = _nav(r, ["navigationEndpoint", "browseEndpoint", "browseId"])
        if bid and bid.startswith("UC"):
            return bid
    return ""


def _best_thumb(node):
    thumbs = (_nav(node, ["musicThumbnailRenderer", "thumbnail", "thumbnails"])
              or _nav(node, ["croppedSquareThumbnailRenderer", "thumbnail", "thumbnails"])
              or _nav(node, ["thumbnail", "thumbnails"])
              or _nav(node, ["thumbnails"]) or [])
    return thumbs[-1]["url"] if thumbs else ""


def _endpoint_target(nav_endpoint):
    """Classify a navigationEndpoint into a playable/browsable target."""
    vid = _nav(nav_endpoint, ["watchEndpoint", "videoId"])
    if vid:
        return {"kind": "song", "videoId": vid,
                "playlistId": _nav(nav_endpoint, ["watchEndpoint", "playlistId"], "")}
    bid = _nav(nav_endpoint, ["browseEndpoint", "browseId"])
    if bid:
        page = _nav(nav_endpoint, ["browseEndpoint", "browseEndpointContextSupportedConfigs",
                                   "browseEndpointContextMusicConfig", "pageType"], "")
        kind = {"MUSIC_PAGE_TYPE_ALBUM": "album",
                "MUSIC_PAGE_TYPE_ARTIST": "artist",
                "MUSIC_PAGE_TYPE_PLAYLIST": "playlist"}.get(page, "browse")
        return {"kind": kind, "browseId": bid}
    return {"kind": "none"}


def _parse_two_row(item):
    """musicTwoRowItemRenderer — the card shape used in carousels."""
    tgt = _endpoint_target(_nav(item, ["navigationEndpoint"], {}))
    tgt.update({
        "title": _runs_text(_nav(item, ["title"], {})),
        "subtitle": _runs_text(_nav(item, ["subtitle"], {})),
        "thumb": _best_thumb(_nav(item, ["thumbnailRenderer"], {})),
        "artistId": _artist_id_from_runs(_nav(item, ["subtitle"], {})),
    })
    return tgt


def _parse_responsive(item):
    """musicResponsiveListItemRenderer — the row shape (search results, lists)."""
    cols = _nav(item, ["flexColumns"], [])

    def col_text(i):
        return _runs_text(_nav(cols, [i, "musicResponsiveListItemFlexColumnRenderer", "text"], {}))

    vid = (_nav(item, ["overlay", "musicItemThumbnailOverlayRenderer", "content",
                       "musicPlayButtonRenderer", "playNavigationEndpoint",
                       "watchEndpoint", "videoId"])
           or _nav(item, ["playlistItemData", "videoId"])
           or _nav(cols, [0, "musicResponsiveListItemFlexColumnRenderer", "text",
                          "runs", 0, "navigationEndpoint", "watchEndpoint", "videoId"]))
    out = {"title": col_text(0), "subtitle": col_text(1),
           "thumb": _best_thumb(_nav(item, ["thumbnail"], {})),
           "artistId": _artist_id_from_runs(
               _nav(cols, [1, "musicResponsiveListItemFlexColumnRenderer", "text"], {}))}
    if vid:
        out["kind"] = "song"
        out["videoId"] = vid
    else:
        out.update(_endpoint_target(_nav(item, ["navigationEndpoint"], {})))
    return out


def _parse_shelf_items(contents):
    out = []
    for c in contents or []:
        if "musicTwoRowItemRenderer" in c:
            out.append(_parse_two_row(c["musicTwoRowItemRenderer"]))
        elif "musicResponsiveListItemRenderer" in c:
            out.append(_parse_responsive(c["musicResponsiveListItemRenderer"]))
    return [i for i in out if i.get("title")]


def _collect_renderers(node, key, out):
    """Depth-first collect every value stored under `key` anywhere in the response. Lets us
    pull all song rows out of a search result without depending on its exact section layout."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                out.append(v)
            else:
                _collect_renderers(v, key, out)
    elif isinstance(node, list):
        for it in node:
            _collect_renderers(it, key, out)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _parse_shelves_from(contents):
    shelves = []
    for sec in contents or []:
        shelf = (sec.get("musicCarouselShelfRenderer")
                 or sec.get("musicImmersiveCarouselShelfRenderer"))
        if not shelf:
            continue
        title = _runs_text(_nav(shelf, ["header", "musicCarouselShelfBasicHeaderRenderer",
                                        "title"], {}))
        items = _parse_shelf_items(shelf.get("contents"))
        if items:
            shelves.append({"title": title, "items": items})
    return shelves


def _home_page(data):
    """(shelves, continuation_token) from a home response — initial or continuation shape."""
    sl = (_nav(data, ["contents", "singleColumnBrowseResultsRenderer", "tabs", 0,
                      "tabRenderer", "content", "sectionListRenderer"])
          or _nav(data, ["continuationContents", "sectionListContinuation"])
          or _nav(data, ["contents", "sectionListRenderer"]))
    if not sl:
        return [], None
    contents = _nav(sl, ["contents"], [])
    shelves = _parse_shelves_from(contents)
    token = (_nav(sl, ["continuations", 0, "nextContinuationData", "continuation"])
             or _nav(sl, ["continuations", 0, "reloadContinuationData", "continuation"]))
    if not token:                          # newer style: continuationItemRenderer at the tail
        for sec in contents:
            tok = _nav(sec, ["continuationItemRenderer", "continuationEndpoint",
                             "continuationCommand", "token"])
            if tok:
                token = tok
                break
    return shelves, token


def get_home(max_shelves=25):
    """Home shelves. Personalized when signed in (browser-cookie / SAPISIDHASH auth); generic
    otherwise. The home is PAGINATED — the first response holds the top shelves plus a
    continuation token; we follow it to pull in the lower shelves (New releases, Forgotten
    favorites, Your daily discover…). Falls back to the anonymous home if the authed request
    returns nothing. Returns {logged_in, shelves, error?, fallback?}."""
    logged = is_logged_in()
    if logged:
        _visitor_id()          # blocking warm here (once at startup) so home carries the visitor
                               # id; every later browse/search then reads it from cache for free
    try:
        data = _innertube("browse", {"browseId": "FEmusic_home"}, use_auth=logged)
    except Exception as ex:
        _log("home request failed: %s" % ex)
        data = None
    shelves, token = _home_page(data) if data else ([], None)
    guard = 0
    while token and len(shelves) < max_shelves and guard < 12:
        guard += 1
        try:
            cont = _innertube("browse", {"continuation": token}, use_auth=logged)
        except Exception as ex:
            _log("home continuation failed: %s" % ex)
            break
        more, token = _home_page(cont)
        shelves.extend(more)
        if not more and not token:
            break
    _log("home: mode=%s pages=%d shelves=%d"
         % (_auth_mode() if logged else "anon", guard + 1, len(shelves)))
    fallback = False
    if logged and not shelves:
        try:
            d2 = _innertube("browse", {"browseId": "FEmusic_home"}, use_auth=False)
        except Exception:
            d2 = None
        fb, _t = _home_page(d2) if d2 else ([], None)
        _log("home fallback(anon): shelves=%d" % len(fb))
        if fb:
            shelves, fallback = fb, True
    if shelves:                                   # cache for an instant load next launch
        try:
            with open(_home_cache_path(), "w") as f:
                json.dump({"shelves": shelves, "logged_in": logged}, f)
        except Exception:
            pass
    return {"logged_in": logged, "shelves": shelves, "fallback": fallback}


def _home_cache_path():
    return os.path.join(_data_dir(), "home_cache.json")


def cached_home():
    """The last successfully loaded home shelves (instant, from disk) — the app shows these
    immediately on launch, then refreshes in the background via get_home()."""
    try:
        with open(_home_cache_path()) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {"shelves": []}
    except Exception:
        return {"shelves": []}


# --------------------------------------------------------------------------- #
# Play history — recently-played tracks for the History page. A list, newest last, LRU-capped;
# re-playing a track moves it to the most-recent slot rather than duplicating it.
# --------------------------------------------------------------------------- #
def _play_history_path():
    return os.path.join(_data_dir(), "play_history.json")


def record_play(video_id, title="", subtitle="", thumb="", artist_id=""):
    """Append a played track to the music history (dedup-to-front on repeat)."""
    if not video_id:
        return
    try:
        with open(_play_history_path()) as f:
            hist = json.load(f)
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    hist = [h for h in hist if isinstance(h, dict) and h.get("videoId") != video_id]
    hist.append({"videoId": video_id, "title": title or video_id, "subtitle": subtitle or "",
                 "thumb": thumb or "", "artistId": artist_id or "", "ts": int(time.time())})
    if len(hist) > 400:
        hist = hist[-400:]
    try:
        with open(_play_history_path(), "w") as f:
            json.dump(hist, f)
    except Exception:
        pass


def play_history(limit=200):
    """Recently-played tracks, newest first: [{videoId,title,subtitle,thumb,artistId,ts}]."""
    try:
        with open(_play_history_path()) as f:
            hist = json.load(f)
        if not isinstance(hist, list):
            return []
    except Exception:
        return []
    rows = [h for h in hist if isinstance(h, dict) and h.get("videoId")]
    rows.reverse()   # newest first
    return rows[:max(1, int(limit))]


def clear_play_history():
    try:
        p = _play_history_path()
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
    return {"ok": True}


def _disliked_path():
    return os.path.join(_data_dir(), "disliked.json")


def _load_disliked():
    try:
        with open(_disliked_path()) as f:
            d = json.load(f)
        return set(d) if isinstance(d, list) else set()
    except Exception:
        return set()


def _save_disliked(s):
    try:
        with open(_disliked_path(), "w") as f:
            json.dump(sorted(s), f)
    except Exception:
        pass


def disliked_ids():
    """The set of disliked videoIds (for the 'skip disliked' feature), as a sorted list."""
    return sorted(_load_disliked())


def _update_disliked(video_id, rating):
    """Track dislikes locally so playback can skip them. DISLIKE adds; LIKE/INDIFFERENT clears."""
    s = _load_disliked()
    if rating == "DISLIKE":
        s.add(video_id)
    else:
        s.discard(video_id)
    _save_disliked(s)


def rate_song(video_id, rating="LIKE"):
    """Like / Dislike / clear a song's rating (requires sign-in).
    rating: LIKE | DISLIKE | INDIFFERENT (clear). Returns {ok}."""
    if not video_id:
        return {"ok": False, "error": "no video"}
    if not is_logged_in():
        return {"ok": False, "error": "not signed in"}
    endpoint = {"LIKE": "like/like", "DISLIKE": "like/dislike",
                "INDIFFERENT": "like/removelike"}.get(rating, "like/like")
    try:
        _innertube(endpoint, {"target": {"videoId": video_id}})
        _update_disliked(video_id, rating)
        _log("rate %s -> %s ok" % (video_id, rating))
        return {"ok": True}
    except Exception as ex:
        _log("rate %s %s failed: %s" % (video_id, rating, ex))
        return {"ok": False, "error": str(ex)}


def search(query, limit=40):
    """Combined music search — songs + artists + albums + playlists (works logged out). Returns
    {items:[{kind, videoId|browseId, title, subtitle, thumb}]}.

    Layout-robust: deep-collect every musicResponsiveListItemRenderer and keep each item that is
    either playable (videoId) or browsable (browseId), in the order YouTube returns them (top
    result, then Songs, Artists, Albums, Playlists…)."""
    q = (query or "").strip()
    if not q:
        return {"items": []}
    try:
        data = _innertube("search", {"query": q}, use_auth=False)
    except Exception as ex:
        _log("search failed: %s" % ex)
        return {"items": [], "error": str(ex)}
    rows = []
    _collect_renderers(data, "musicResponsiveListItemRenderer", rows)
    items, seen = [], set()
    for r in rows:
        it = _parse_responsive(r)
        key = it.get("videoId") or it.get("browseId")
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(it)
        if len(items) >= limit:
            break
    _log("search '%s': rows=%d items=%d" % (q, len(rows), len(items)))
    return {"items": items[:limit]}


# The page header (album/playlist/artist) carries the title, cover art and primary artist.
# It lives in different places across layouts: older responses put it under a top-level `header`
# key; the newer two-column album/playlist layout makes it the FIRST item of the section list
# (`…twoColumnBrowseResultsRenderer…sectionListRenderer.contents[0].musicResponsiveHeaderRenderer`).
# So we deep-search for the header renderer by name rather than guessing its path.
_HEADER_KEYS = ("musicResponsiveHeaderRenderer", "musicDetailHeaderRenderer",
                "musicVisualHeaderRenderer", "musicImmersiveHeaderRenderer")


def _find_header(data):
    """The page's header renderer dict, wherever it lives. '' -> {}."""
    for k in _HEADER_KEYS:
        found = []
        _collect_renderers(data, k, found)
        if found:
            return found[0]
    # Editable-playlist pages wrap the real header a level down.
    ed = []
    _collect_renderers(data, "musicEditablePlaylistDetailHeaderRenderer", ed)
    if ed:
        return _nav(ed[0], ["header", "musicResponsiveHeaderRenderer"], {}) or ed[0]
    return {}


def _header_title(data):
    """Album/playlist/artist page title (from its header renderer, wherever it is)."""
    return _runs_text(_nav(_find_header(data), ["title"], {}))


def _header_thumb(data):
    """Cover art from the page header — album/playlist track rows don't repeat it per row."""
    return _best_thumb(_nav(_find_header(data), ["thumbnail"], {}))


def _header_artist(data):
    """(name, browseId) of the page's primary artist, from the header's subtitle/strapline runs.
    The artist run is the one linking to a channel (UC… browseId); '' when there isn't one."""
    h = _find_header(data)
    for key in ("subtitle", "straplineTextOne", "secondSubtitle"):
        for r in (_nav(h, [key, "runs"], []) or []):
            bid = _nav(r, ["navigationEndpoint", "browseEndpoint", "browseId"])
            if bid and bid.startswith("UC"):
                return r.get("text", ""), bid
    return "", ""


def get_playlist(browse_id, limit=200, params=""):
    """Tracks of a playlist / album / mix / artist page (works logged out for public ones;
    signed-in for your own). Layout-robust: deep-collect the track rows. `params` supports the
    artist "Show all songs" target (a browseId+params endpoint). Returns
    {ok, title, tracks:[{videoId,title,subtitle,thumb}]}."""
    if not browse_id:
        return {"ok": False, "error": "no id", "tracks": []}
    body = {"browseId": browse_id}
    if params:
        body["params"] = params
    try:
        data = _innertube("browse", body)
    except Exception as ex:
        _log("get_playlist %s failed: %s" % (browse_id, ex))
        return {"ok": False, "error": str(ex), "tracks": []}
    rows = []
    _collect_renderers(data, "musicResponsiveListItemRenderer", rows)
    header_thumb = _header_thumb(data)                 # album cover — track rows omit their own
    header_artist, header_artist_id = _header_artist(data)
    tracks, seen = [], set()
    for r in rows:
        it = _parse_responsive(r)
        vid = it.get("videoId")
        if vid and vid not in seen:
            if not it.get("thumb"):
                it["thumb"] = header_thumb             # fall back to the album/playlist cover
            if not it.get("subtitle"):
                it["subtitle"] = header_artist
            if not it.get("artistId"):
                it["artistId"] = header_artist_id
            seen.add(vid)
            tracks.append(it)
        if len(tracks) >= limit:
            break
    _log("get_playlist %s: rows=%d tracks=%d" % (browse_id, len(rows), len(tracks)))
    return {"ok": True, "title": _header_title(data), "tracks": tracks}


def get_artist(browse_id):
    """A YouTube Music artist page: header + top songs + carousels (Albums, Singles & EPs, Videos,
    Featured on, Fans might also like…). Returns
    {ok, name, thumb, songs:[{videoId,…}], songs_more:{browseId,params}, shelves:[{title,items}]}.
    The top-songs shelf is a musicShelfRenderer (rows); the rest are musicCarouselShelfRenderers
    (cards) — parsed by the same helpers used for search/home."""
    if not browse_id:
        return {"ok": False, "error": "no id", "songs": [], "shelves": []}
    try:
        data = _innertube("browse", {"browseId": browse_id})
    except Exception as ex:
        _log("get_artist %s failed: %s" % (browse_id, ex))
        return {"ok": False, "error": str(ex), "songs": [], "shelves": []}
    sl = (_nav(data, ["contents", "singleColumnBrowseResultsRenderer", "tabs", 0,
                      "tabRenderer", "content", "sectionListRenderer", "contents"])
          or _nav(data, ["contents", "sectionListRenderer", "contents"]) or [])
    songs, songs_more, shelves = [], {"browseId": "", "params": ""}, []
    for sec in sl:
        ms = sec.get("musicShelfRenderer")
        if ms:                                    # the "Songs" (top tracks) shelf
            rows = []
            _collect_renderers(ms.get("contents"), "musicResponsiveListItemRenderer", rows)
            for r in rows:
                it = _parse_responsive(r)
                if it.get("videoId"):
                    if not it.get("artistId") and browse_id.startswith("UC"):
                        it["artistId"] = browse_id
                    songs.append(it)
            ep = (_nav(ms, ["title", "runs", 0, "navigationEndpoint", "browseEndpoint"])
                  or _nav(ms, ["bottomEndpoint", "browseEndpoint"]) or {})
            if ep.get("browseId"):
                songs_more = {"browseId": ep.get("browseId", ""), "params": ep.get("params", "")}
            continue
        shelf = (sec.get("musicCarouselShelfRenderer")
                 or sec.get("musicImmersiveCarouselShelfRenderer"))
        if shelf:                                 # Albums / Singles / Videos / … carousels
            hdr = _nav(shelf, ["header", "musicCarouselShelfBasicHeaderRenderer"], {})
            items = _parse_shelf_items(shelf.get("contents"))
            if items:
                shelves.append({"title": _runs_text(_nav(hdr, ["title"], {})), "items": items})
    _log("get_artist %s: songs=%d shelves=%d" % (browse_id, len(songs), len(shelves)))
    return {"ok": True, "name": _header_title(data), "thumb": _header_thumb(data),
            "songs": songs, "songs_more": songs_more, "shelves": shelves}


def _parse_panel_video(r):
    """playlistPanelVideoRenderer — a track row in a watch/radio queue."""
    return {
        "kind": "song",
        "videoId": r.get("videoId", ""),
        "title": _runs_text(_nav(r, ["title"], {})),
        "subtitle": (_runs_text(_nav(r, ["longBylineText"], {}))
                     or _runs_text(_nav(r, ["shortBylineText"], {}))),
        "thumb": _best_thumb(_nav(r, ["thumbnail"], {})),
        "artistId": (_artist_id_from_runs(_nav(r, ["longBylineText"], {}))
                     or _artist_id_from_runs(_nav(r, ["shortBylineText"], {}))),
    }


def get_radio(video_id, limit=25):
    """A song radio (autoplay continuation) for a videoId — the related-track queue YouTube
    Music generates. Returns {tracks:[{videoId,title,subtitle,thumb}]} (includes the seed
    track first; the caller dedupes). Works logged out; personalized when signed in."""
    if not video_id:
        return {"tracks": []}
    body = {
        "videoId": video_id,
        "playlistId": "RDAMVM" + video_id,       # song-radio playlist id
        "isAudioOnly": True,
        "enablePersistentPlaylistPanel": True,
        "tunerSettingValue": "AUTOMIX_SETTING_NORMAL",
    }
    try:
        data = _innertube("next", body)
    except Exception as ex:
        _log("radio %s failed: %s" % (video_id, ex))
        return {"tracks": [], "error": str(ex)}
    rows = []
    _collect_renderers(data, "playlistPanelVideoRenderer", rows)
    tracks, seen = [], set()
    for r in rows:
        vid = r.get("videoId")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        tracks.append(_parse_panel_video(r))
        if len(tracks) >= limit:
            break
    _log("radio %s: tracks=%d" % (video_id, len(tracks)))
    return {"tracks": tracks}


# --------------------------------------------------------------------------- #
# Lyrics (LRCLIB — free, no auth). Synced (LRC) when available, else plain text.
# --------------------------------------------------------------------------- #

_LRCLIB = "https://lrclib.net/api"
_LYRIC_TYPES = {"song", "video", "playlist", "album", "artist", "single", "ep"}


def _clean_artist(artist):
    """YTM bylines vary ('Song • Artist', 'Artist • Album • Year', or a bare name). Take the first
    segment that isn't a type keyword as the primary artist for lyric matching."""
    parts = [p.strip() for p in re.split(r"[•·|]", artist or "") if p.strip()]
    parts = [p for p in parts if p.lower() not in _LYRIC_TYPES]
    return parts[0] if parts else (artist or "").strip()


def _parse_lrc(text):
    """Parse LRC synced lyrics into [{t, line}] (t = ms), sorted by time."""
    out = []
    for raw in (text or "").split("\n"):
        tags = re.findall(r"\[(\d+):(\d+)(?:[.:](\d+))?\]", raw)
        if not tags:
            continue
        line = re.sub(r"\[[^\]]*\]", "", raw).strip()
        for mm, ss, frac in tags:
            ms = (int(mm) * 60 + int(ss)) * 1000
            if frac:
                ms += int(frac[:2].ljust(2, "0")) * 10 if len(frac) <= 2 \
                      else int(frac[:3].ljust(3, "0"))
            out.append({"t": ms, "line": line})
    out.sort(key=lambda x: x["t"])
    return out


def _lyrics_result(data):
    synced = _parse_lrc(data.get("syncedLyrics") or "")
    plain = data.get("plainLyrics") or ""
    return {"ok": bool(synced or plain), "synced": synced, "plain": plain,
            "instrumental": bool(data.get("instrumental"))}


def _best_lyrics_match(arr, dur):
    """Pick the best LRCLIB search hit: synced first, then closest duration."""
    if not isinstance(arr, list) or not arr:
        return None
    def score(e):
        d = abs((e.get("duration") or 0) - dur) if dur else 0
        return (0 if e.get("syncedLyrics") else 1, d)
    return sorted(arr, key=score)[0]


def get_lyrics(title, artist, duration_sec=0, album=""):
    """Lyrics for a track from LRCLIB. Tries an exact signature match, then search. Returns
    {ok, synced:[{t,line}], plain, instrumental, error?}. Synced is empty when only plain text
    exists; the caller shows plain then."""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "synced": [], "plain": "", "error": "No track"}
    artist_clean = _clean_artist(artist)
    dur = int(duration_sec or 0)

    # 1) exact get (needs a close album+duration; 404s otherwise → fall through to search).
    if artist_clean and dur:
        try:
            qs = urllib.parse.urlencode({"artist_name": artist_clean, "track_name": title,
                                         "album_name": album or title, "duration": dur})
            return _lyrics_result(_http_get_json(_LRCLIB + "/get?" + qs))
        except urllib.error.HTTPError as he:
            if he.code != 404:
                _log("lyrics get failed: %s" % he.code)
        except Exception as ex:
            _log("lyrics get error: %s" % ex)

    # 2) search by track + artist, then by track alone — pick the best by sync + duration.
    for params in ({"track_name": title, "artist_name": artist_clean} if artist_clean else None,
                   {"q": (title + " " + artist_clean).strip()}):
        if not params:
            continue
        try:
            arr = _http_get_json(_LRCLIB + "/search?" + urllib.parse.urlencode(params))
            best = _best_lyrics_match(arr, dur)
            if best:
                return _lyrics_result(best)
        except Exception as ex:
            _log("lyrics search error: %s" % ex)

    return {"ok": False, "synced": [], "plain": "", "error": "No lyrics found"}


def _lyrics_cache_dir():
    d = os.path.join(_data_dir(), "lyrics")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _lyrics_cache_path(video_id):
    vid = re.sub(r"[^\w-]", "", video_id or "")[:64]   # sanitise: it becomes a filename
    return os.path.join(_lyrics_cache_dir(), vid + ".json")


def cache_lyrics(video_id, title, artist, duration_sec=0, album=""):
    """Fetch a track's lyrics and store them next to its offline download so LyricsPage can show
    the synced view with NO network later. Called when an audio download finishes. Best-effort —
    a miss just means no offline lyrics for that track (it'll still try online when connected)."""
    if not video_id:
        return {"ok": False}
    res = get_lyrics(title, artist, duration_sec, album)
    if res.get("ok"):
        try:
            with open(_lyrics_cache_path(video_id), "w") as f:
                json.dump({"synced": res.get("synced") or [], "plain": res.get("plain") or "",
                           "instrumental": bool(res.get("instrumental"))}, f)
        except Exception:
            pass
    return res


def cached_lyrics(video_id):
    """Lyrics saved alongside a download, or {ok:False} if none. Read-only, offline-friendly."""
    if not video_id:
        return {"ok": False}
    try:
        with open(_lyrics_cache_path(video_id)) as f:
            d = json.load(f)
        return {"ok": bool(d.get("synced") or d.get("plain")),
                "synced": d.get("synced") or [], "plain": d.get("plain") or "",
                "instrumental": bool(d.get("instrumental")), "cached": True}
    except Exception:
        return {"ok": False}


def clear_cached_lyrics(video_id):
    """Drop a track's cached lyrics (called when its download is deleted). Best-effort."""
    try:
        os.remove(_lyrics_cache_path(video_id))
    except Exception:
        pass


def add_to_playlist(playlist_id, video_id):
    """Add a track to one of your playlists (auth required). `playlist_id` may be a raw PL… id or
    a VL… library browseId (the VL prefix is stripped). Returns {ok, error?}."""
    if not is_logged_in():
        return {"ok": False, "error": "Not signed in"}
    if not playlist_id or not video_id:
        return {"ok": False, "error": "Missing playlist or track"}
    pid = playlist_id[2:] if playlist_id.startswith("VL") else playlist_id
    body = {"playlistId": pid,
            "actions": [{"action": "ACTION_ADD_VIDEO", "addedVideoId": video_id}]}
    try:
        res = _innertube("browse/edit_playlist", body)
        status = res.get("status", "") if isinstance(res, dict) else ""
        if status and status != "STATUS_SUCCEEDED":
            _log("add_to_playlist %s -> %s" % (pid, status))
            return {"ok": False, "error": "Couldn't add (playlist may not be editable)"}
        _log("add_to_playlist %s ok" % pid)
        return {"ok": True}
    except Exception as ex:
        _log("add_to_playlist %s failed: %s" % (pid, ex))
        return {"ok": False, "error": str(ex)}


def get_library_playlists():
    """Your library playlists (requires sign-in) — includes the Liked Music card. Returns
    {logged_in, playlists:[{title, browseId, subtitle, thumb}]}."""
    if not is_logged_in():
        return {"logged_in": False, "playlists": []}
    try:
        data = _innertube("browse", {"browseId": "FEmusic_liked_playlists"})
    except Exception as ex:
        _log("library failed: %s" % ex)
        return {"logged_in": True, "playlists": [], "error": str(ex)}
    cards = []
    _collect_renderers(data, "musicTwoRowItemRenderer", cards)
    out = []
    for c in cards:
        it = _parse_two_row(c)
        if it.get("browseId") and it.get("title"):
            out.append(it)
    _log("library: playlists=%d" % len(out))
    return {"logged_in": True, "playlists": out}
