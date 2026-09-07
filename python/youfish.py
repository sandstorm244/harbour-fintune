"""Youfish backend (FinTune cut): thin wrapper over an external yt-dlp binary + a local
media proxy — the AUDIO engine behind FinTune. Same engine as FinTube's, minus everything
a music player doesn't use (video ladders, comments, captions, subscriptions/feed); the
YouTube Music metadata layer (search / browse / lyrics / account) lives in ytm.py.

The app never pins a yt-dlp version — it shells out to whatever yt-dlp is on the
device, and the user updates that binary themselves. Every call is made from
PyOtherSide's worker thread, so blocking subprocess calls are fine here.

Playback note: googlevideo rejects GStreamer's libsoup HTTP stack with 403 (not a
fixable header — curl/urllib with identical headers get 206), so the audio track
streams through a tiny localhost proxy (below) that refetches the real URL with the
format's own User-Agent and serves byte ranges from a bounded, backpressured
on-disk download job.

PO tokens (2026 reality): YouTube increasingly binds a Proof-of-Origin token to the
stream URLs — without one many clients return nothing fetchable. The bgutil
provider (OPT-IN, user-installed; see install_pot_provider) mints them on a
sandboxed Deno sidecar. The common path avoids the mint entirely: resolve()'s
primary dump is the TOKEN-FREE tv_embedded client, run anonymously; the token
machinery is only the safety net for gated/restricted videos (see
_resolve_uncached / _default_client).
"""

import atexit
import calendar
import contextlib
import ctypes
import base64
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# Whether the DEVICE python can run the in-process fast-resolve zipapp at all. yt-dlp
# requires Python >= 3.10 (verified 2026-09: SFOS 5.1 ships 3.11 — fine; SFOS <= 5.0 ships
# 3.8 — never). The frozen binary bundles its OWN modern Python, which is exactly why it
# stays the universal default and fallback. Gates the in-process path and the launch-time
# zipapp autofetch; bump when upstream moves again — going stale is safe (the import just
# fails once and every resolve takes the binary).
_FAST_RESOLVE_PY_OK = sys.version_info >= (3, 10)

# Flags applied to every network-facing yt-dlp call. -4 forces IPv4: dual-stack connects
# can hang when a network advertises IPv6 routes it can't actually carry.
# (This is where PO-token / player-client args will accrue in M2.)
_COMMON_ARGS = ("-4",)


# --------------------------------------------------------------------------- #
# Authenticated extraction: hand yt-dlp the imported YouTube login as cookies.
# The session comes from the optional `ytm` module (import_browser_login reads the Sailfish
# Browser's cookie jar). It is materialised to an EPHEMERAL, owner-only temp file per yt-dlp call
# and removed straight after — there is never a persistent plaintext cookies file on disk, and a
# per-call file means parallel calls never share/clobber one cookie jar.
# --------------------------------------------------------------------------- #

def _write_cookies_temp():
    """Write the imported YouTube Music login (if any) to a fresh 0600 cookies.txt and return its
    path, or "" when signed out. The CALLER must remove the file when the yt-dlp call finishes."""
    text = ""
    try:
        import ytm
        text = ytm.netscape_cookies()
    except Exception:
        text = ""
    if not text:
        return ""
    fd, path = tempfile.mkstemp(prefix="ytdlp-ck-", suffix=".txt")   # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        return ""
    return path


@contextlib.contextmanager
def _cookies_args():
    """Yield ["--cookies", <ephemeral file>] for authenticated extraction (age-gated / members /
    premium content, fewer bot-wall 403s) when a YouTube login is imported, else []. The temp file
    lives only for the `with` block. Used to splice *cargs into a yt-dlp argv right after
    *_COMMON_ARGS. For a long-lived Popen (download) call _write_cookies_temp() directly instead."""
    path = _write_cookies_temp()
    try:
        yield (["--cookies", path] if path else [])
    finally:
        if path:
            try:
                os.remove(path)
            except Exception:
                pass

# Playback is AUDIO-ONLY: resolve() hands the player a fallback ladder of audio-only tracks
# (opus/AAC), selected by their PROPERTIES (the codec / bitrate / language yt-dlp reports on
# every format), NOT a hardcoded itag list — itags are undocumented and YouTube keeps
# rotating/adding them, so any fixed list silently misses variants. See _audio_candidates.
#
# Muxed fallback of last resort — a single combined stream the player can consume when every
# adaptive audio URL 403s. Prefer HLS (95/94/93, live streams) then progressive itag 18
# (360p H.264+AAC — being phased out by YouTube, so this rung self-deprecates; the player
# just plays its audio).
_MUXED_ITAGS = ("95", "94", "93", "18")

# FinTune reuses FinTube's app-managed yt-dlp (and its fast-resolve zipapp) if present, so a
# user who has both apps needn't download a second ~30 MB copy. FinTune's own managed bin
# still wins, and Install/Update always write FinTune's own copy — the sibling's is read-only
# to us and updated from FinTube.
_FINTUBE_DATA_DIR = os.path.expanduser("~/.local/share/harbour-fintube")
_CANDIDATE_PATHS = (
    os.path.join(_FINTUBE_DATA_DIR, "bin", "yt-dlp"),
)


def _is_manifest(f):
    """1 if this is an HLS/manifest variant (m3u8) rather than a direct progressive/DASH URL, else 0.
    tv_embedded exposes BOTH at every resolution; the direct URL is preferred as a pure tiebreaker —
    it needs no manifest round-trip before media (faster preroll) and rides the app's proven
    range-seekable proxy path (proxying a manifest is the fragile case the proxy code warns about).
    Without this the manifest variant can win the pick purely by list order (measured 2026-09-06:
    anonymous tv_embedded picked manifest v=617 over the direct equivalent)."""
    return 1 if ("m3u8" in (f.get("protocol") or "").lower()
                 or "manifest.googlevideo" in (f.get("url") or "")) else 0


def _audio_family(acodec):
    """'opus' | 'aac' | '' for a yt-dlp acodec string. '' = a codec we don't use (none / exotic)."""
    ac = (acodec or "").lower()
    if ac.startswith("opus"):
        return "opus"
    if ac.startswith(("mp4a", "aac")):
        return "aac"
    return ""


def _audio_orig_pref(f):
    """How much YouTube/yt-dlp prefers this track's LANGUAGE: >0 = original/default source audio,
    <0 = dubbed / descriptive. Uses yt-dlp's own `language_preference` (≈10 original / -1 dub /
    -10 descriptive) when present, else the format_note wording. Without this a same-bitrate DUB
    can outrank the source track (English video → Portuguese dub)."""
    lp = f.get("language_preference")
    if isinstance(lp, (int, float)):
        return lp
    note = (f.get("format_note") or "").lower()
    if "descriptive" in note or "description" in note:
        return -10
    if "original" in note or "default" in note:
        return 10
    return 0


def _is_drc(f):
    """1 for a DRC ('stable volume' / dynamic-range-compressed) audio track, else 0. YouTube marks
    these with a `-drc` format_id suffix (some clients — e.g. web_embedded — expose them alongside the
    normal tracks). Used only as a tie-break so the ORIGINAL dynamics win over the normalized variant
    when both are offered at the same language+codec; DRC is still picked if it's all that's on offer."""
    return 1 if ("drc" in (f.get("format_id") or "").lower()
                 or "drc" in (f.get("format_note") or "").lower()) else 0


# Bitrate-tier words yt-dlp appends to an audio format_note ("German, low"). Stripped to leave
# the bare language name for the audio picker.
_AUDIO_TIER_RE = re.compile(r",\s*(?:ultralow|low|medium|high)\s*$", re.I)


def _audio_candidates(formats):
    """Playable audio-only tracks (opus/AAC, with a direct URL), best-first. Ordered by bitrate
    high→low (opus preferred at a tie — better quality per bit; original/default language over
    dubs). Bitrate order naturally interleaves the codecs, so the music player's SABR-fallback
    ladder tries the best of each codec early. Property-based, so no hardcoded itag list to
    go stale."""
    cands = []
    for f in formats:
        if not f.get("url"):
            continue
        if (f.get("vcodec") or "none").lower() != "none":
            continue                                   # audio-only tracks only
        if not _audio_family(f.get("acodec")):
            continue                                   # exotic / none → skip
        cands.append(f)

    def key(f):
        codec_rank = 0 if _audio_family(f.get("acodec")) == "opus" else 1
        abr = f.get("abr") or f.get("tbr") or 0
        # LANGUAGE is the primary key so the SOURCE track always beats a dub regardless of its
        # bitrate; then CODEC (opus first), then bitrate. Opus is preferred OVER bitrate because
        # Opus/WebM audio flows through matroskademux, which PUSH-seeks over the range-seekable proxy
        # exactly like the WebM/VP9 video — whereas AAC/M4A goes through qtdemux, whose push-mode seek
        # returns FALSE on this SFOS/libhybris GStreamer (the "audio= 0" desync), forcing a whole-file
        # audio downloadbuffer that grinds before every preroll. Opus keeps BOTH branches push-mode:
        # fast preroll + A/V-synced seeks, no downloadbuffer. (Opus 251 ~160k >= AAC 140 ~128k, so
        # this rarely costs quality; falls back to AAC when no opus track exists.)
        # Then non-DRC before DRC: within the same language+codec, the ORIGINAL dynamics beat the
        # loudness-normalized ("stable volume") variant that some clients (web_embedded) also expose;
        # placed AFTER codec so we never trade the opus push-seek win for a non-DRC AAC track, and
        # a DRC track is still chosen when it's the only one offered.
        return (-_audio_orig_pref(f), codec_rank, _is_drc(f), -abr, _is_manifest(f))
    cands.sort(key=key)
    return cands


# --------------------------------------------------------------------------- #
# Local media proxy: injects a browser User-Agent and forwards byte ranges.
# --------------------------------------------------------------------------- #

_proxy_port = None
_proxy_lock = threading.Lock()
_ipv4_forced = False

# --- Download-backed streaming substrate ------------------------------------- #
# A per-(video,itag) job streams googlevideo bytes (in-process _DirectFetch by default; the
# yt-dlp child as fallback — see _spawn); the reader thread pwrites them into a temp file and
# advances an in-process `edge` counter; do_GET serves preads gated by `edge`. Backpressure is
# end-to-end either way: the reader only pulls when the read-ahead gate is open (GStreamer
# buffer full -> wfile.write blocks -> cursor stops advancing -> reader stops pulling -> the
# fetch pauses / the child blocks on its pipe), so disk stays bounded with no SIGSTOP /
# --limit-rate machinery. `edge` is OUR counter (bytes we actually pwrote), never getsize(),
# so a read can never see a byte we didn't place.
_SESS_CHUNK   = 256 << 10    # pipe read / pwrite unit
_READAHEAD    = 32 << 20     # download at most this far past the play cursor (the read-ahead cap)
_SEEK_SOON    = 4  << 20     # forward seek within this of edge -> block; beyond -> restart
_KEEPBACK     = 8  << 20     # bytes kept behind the cursor for cheap short backward seeks (D3)
_IDLE         = 25.0         # reap a stream idle (refs==0) this long
_REAP_EVERY   = 5.0
_STALL        = 120.0        # _wait gives up if edge hasn't advanced this long (D8 backup watchdog);
                             # must exceed the ~90s _ytdlp_formats re-resolve timeout — real in-download
                             # stalls are caught by --socket-timeout 30, not by this backstop.
_MAX_STREAMS  = 8
_MIN_FREE     = 300 << 20
_RESUME_TRIES = 3            # cap on CONSECUTIVE no-progress pipe deaths (reset on progress, D5/R9)
# FALLOC_FL_* literals (Linux; not exposed as os.* names) — reclaim the consumed prefix in place (D3)
_FALLOC_KEEP  = 0x01         # FALLOC_FL_KEEP_SIZE
_FALLOC_PUNCH = 0x02         # FALLOC_FL_PUNCH_HOLE
_PUNCH_OK     = True         # cleared on the first fallocate failure -> degrade to full-file
# Range-restart resume: on-device Range test PASSED 2026-09-03 — the frozen yt-dlp FORWARDS
# --add-header "Range: bytes=N-" on a direct-URL `-o -` download (reported total = clen - N, no 403),
# so resuming AT s.edge is safe and gives snappy seeks/resume. This is the shipped mode: the resume
# path spawns at s.edge and never resets edge/origin to 0, which by construction keeps disk bounded
# by the do_GET hole-punch during resume too (eliminates R8's balloon and R9's edge-reset problem).
# Keep the False branch as a DOCUMENTED FALLBACK ONLY: it re-downloads from 0 (offsets stay
# corruption-proof), can grow the temp file during a deep resume, and relies on the reader's
# free-space fail-safe to turn a would-be device-fill into a clean FAIL — never the shipped mode.
_RANGE_RESTART = True
# In-process direct fetch (default): the download job streams googlevideo via urllib INSIDE
# this process instead of spawning the frozen yt-dlp binary per job — the same ~1.3s spawn tax
# fast-resolve removed from resolve() was still paid on every playback start (twice: video +
# audio jobs) and on every mid-stream resume. _DirectFetch mirrors the child's proven behaviour
# (headers, IPv4, 30s socket timeout, bounded 10M Range chunks for the burst-window speedup)
# behind the exact proc surface _reader/_reap expect. Fallback doctrine (see _spawn/_reader): a
# stream whose direct fetch dies at byte 0 flips to the binary child for its remaining life —
# worst case is the status quo plus one failed HTTPS round-trip. Set False to force the child.
_DIRECT_STREAM = True
_DIRECT_CHUNK  = 10 << 20    # bounded Range chunk (== the child's --http-chunk-size 10M)
_streams = {}                # (video_id, itag) -> _Stream
_streams_lock = threading.Lock()
_reap_pending = []           # R3/R5: Popen zombies to wait() OFF-lock, drained by _reaper + atexit
_reap_lock = threading.Lock()
_STREAM_DIR = None           # <data_dir>/streamcache, set in _ensure_proxy


def _force_ipv4():
    """Make this process's socket lookups return IPv4 addresses only.

    googlevideo publishes AAAA records, but when a network advertises IPv6 it can't route,
    each connect stalls before falling back to IPv4 — longer than souphttpsrc's read timeout,
    so the pipeline errors out ("Socket I/O timed out") before the proxy, stuck in the same
    stall, can answer. This is the in-process equivalent of the `-4` flag passed to yt-dlp.
    yt-dlp runs in a separate
    process, and QtMultimedia's networking lives in the C++/Qt side, so patching
    getaddrinfo here only affects the proxy's own urllib fetches.
    """
    global _ipv4_forced
    if _ipv4_forced:
        return
    _orig = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only
    _ipv4_forced = True


# Proxy tracing is off unless YOUFISH_DEBUG is set in the environment, so no /tmp log file is
# written in normal use. Trace playback with `YOUFISH_DEBUG=1 harbour-youfish`.
_DEBUG = bool(os.environ.get("YOUFISH_DEBUG"))


_plog_t0 = None
def _plog(msg):
    # DIAG: prefix every line with seconds since the first log line, so REQ->DONE gaps expose
    # yt-dlp cold-start latency vs slow throughput (wrote / elapsed) directly.
    global _plog_t0
    if not _DEBUG:
        return
    try:
        if _plog_t0 is None:
            _plog_t0 = time.monotonic()
        with open("/tmp/youfish-proxy.log", "a") as fh:
            fh.write("[%7.2f] %s\n" % (time.monotonic() - _plog_t0, msg))
    except Exception:
        pass


def _tlog(msg):
    """Timing trace to stdout (visible under YOUFISH_DEBUG, like ytm's [ytm] lines) — for
    profiling start latency. Cheap; compiled out in normal use by the _DEBUG gate.
    Wall-clock stamped so field logs expose the *gaps between* lines (a stall in untimed
    code is invisible to per-step durations alone)."""
    if _DEBUG:
        try:
            now = time.time()
            print("[youfish/t %s.%03d] %s"
                  % (time.strftime("%H:%M:%S", time.localtime(now)), int(now % 1 * 1000), msg))
        except Exception:
            pass


def _timed_fn(label):
    """Decorator that logs a query function's total wall time (label + seconds) under YOUFISH_DEBUG.
    When debug is OFF it returns the function UNWRAPPED — literally zero overhead in normal use. Used
    to profile every user-facing yt-dlp/network query on-device (grep the log for `[youfish/t] q.`).
    Internal calls resolve to the wrapped module global too, so nested paths (feed workers) are timed.
    """
    def deco(fn):
        if not _DEBUG:
            return fn

        def wrapper(*a, **kw):
            _t0 = time.time()
            try:
                return fn(*a, **kw)
            finally:
                _tlog("%s %.2fs" % (label, time.time() - _t0))
        wrapper.__name__ = getattr(fn, "__name__", "fn")
        return wrapper
    return deco


def _spawn_tax_probe():
    """Measure the pure yt-dlp cold-start spawn tax: `yt-dlp --version` does ~no real work, so its
    wall time is almost entirely process launch (unpack the frozen binary + boot CPython + import the
    yt_dlp tree). Logged once per launch under YOUFISH_DEBUG so the log shows how much of EVERY query
    is just the spawn — the #1 number for deciding whether in-process / a daemon is worth it."""
    if not _DEBUG:
        return
    path = _ytdlp_path()
    if not path:
        _tlog("spawn_tax: yt-dlp not found")
        return
    best = None
    for _ in range(3):                       # min of a few runs → the warm-FS best case, the fair floor
        _t0 = time.time()
        try:
            subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        except Exception as ex:
            _tlog("spawn_tax: probe failed (%s)" % ex)
            return
        dt = time.time() - _t0
        best = dt if best is None else min(best, dt)
    _tlog("spawn_tax %.2fs  (min of 3x `yt-dlp --version`; ~pure process launch)" % best)


def _clen(url):
    """Total content length of a googlevideo stream, read straight from its URL.

    With query-param range requests the response is a 200 whose Content-Length is only
    the chunk size, so the URL's own clen= is how we learn the real total.
    """
    m = re.search(r"[?&]clen=(\d+)", url)
    return int(m.group(1)) if m else None


# The proxy exists only to refetch YouTube DASH/progressive media (googlevideo) with the right UA.
# Constrain it to https + Google hosts so it can't be turned into an open forward-proxy: reaching
# localhost services, file:// reads, or arbitrary hosts (SSRF) via a crafted u= parameter.
_PROXY_ALLOW_SUFFIXES = (".googlevideo.com", ".youtube.com", ".ytimg.com",
                         ".googleusercontent.com", ".google.com")


def _proxy_url_ok(url):
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if p.scheme != "https":
        return False
    host = (p.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in _PROXY_ALLOW_SUFFIXES)


def _probe_url_ok(url, ua, timeout=3):
    """Fast pre-flight for the token-free→token fallback (today: tv_embedded→mweb): does this
    googlevideo URL actually SERVE bytes, or 403 at byte 0? A gated token-free stream looks fine
    at resolve but 403s the instant the player fetches it, so resolve probes one chosen URL and,
    on a real 403, re-extracts with the token client BEFORE playback. Returns False ONLY on a definite HTTP 403 (the escalation trigger);
    True on 2xx AND on any ambiguous failure (timeout / DNS / other HTTP code) — we never escalate to
    the slower client on a maybe, so a flaky network can't make resolve pay for BOTH clients. One tiny
    Range: bytes=0-1 GET, forced IPv4, with the SAME UA the player will use (a mismatched UA 403s on
    its own and would be a false trigger)."""
    if not url:
        return True
    try:
        _force_ipv4()
        req = urllib.request.Request(url, headers={"User-Agent": ua or _BROWSER_UA,
                                                   "Range": "bytes=0-1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(2)
            return True
    except urllib.error.HTTPError as ex:
        return ex.code != 403
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Per-(video_id, itag) download job. The HTTP connection is ephemeral (every seek is a fresh
# do_GET, since we answer Connection: close); the download JOB persists here across connections,
# so a backward / nearby-forward seek is served from disk instead of re-fetching from zero.
# Invariant: [origin, edge) is always contiguous and fully valid; the reader only advances edge.
# Lock order is ALWAYS _streams_lock -> s.cond. do_GET takes s.cond ALONE (never nests
# _streams_lock under it). refs lives under s.cond. No proc.wait() ever runs under _streams_lock.
# --------------------------------------------------------------------------- #
class _Stream:
    def __init__(s, vid, itag, url, ua, total):
        s.vid, s.itag, s.url, s.ua = vid, itag, url, ua
        s.total = total                    # _clen(url): authoritative total, known up front
        s.path = os.path.join(_STREAM_DIR, "s-%s-%s-%d.dat"
                              % (vid, itag, int(time.time() * 1000)))
        s.fd = os.open(s.path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        s.origin = 0                       # first valid byte of the live segment (advances on reclaim)
        s.edge = 0                         # one past the last byte we've pwritten
        s.cursor = 0                       # furthest byte any connection has served (read-ahead gate)
        s.dl_start = 0                     # content offset the current yt-dlp proc streams FROM (D10)
        s.state = "RUN"                    # RUN | DONE | FAIL | DEAD
        s.refs = 0                         # R7/R10: mutated ONLY under s.cond
        s.last_active = time.time()
        s.edge_ts = time.time()            # last time edge advanced -> stall watchdog (D8)
        s.cursor_at_last_death = 0         # R9: cursor at the previous pipe death -> resets tries
        s.cond = threading.Condition()
        s.proc = None
        s.use_binary = False               # flipped when a direct fetch dies at byte 0 -> child
        s.gen = 0                          # fences a stale reader across a restart


def _free_bytes(path):
    """Free bytes on the filesystem holding `path`. On error return a huge number so a statvfs
    hiccup never wedges playback on a free-space guess (the reader's periodic re-check, D3, is the
    real device-full guard)."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except Exception:
        return 1 << 62


def _reap_proc(proc):
    """Best-effort wait() on an exited / killed child so it can't linger as a zombie. Called ONLY
    off any lock: _reader's inline resume reap (holds no lock), _reader's R4 self-kill, and the
    atexit sweep. _reap_locked NEVER calls this (R3/R5) — it queues to _reap_pending instead."""
    if not proc:
        return
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


class _DirectFetch:
    """In-process googlevideo streamer — a duck-typed stand-in for the yt-dlp child (_spawn):
    same `.stdout.read(n)` / `.kill()` / `.poll()` / `.wait()` surface, so _reader and the reap
    machinery run unchanged. Exists because every playback start (and every mid-stream resume)
    paid the same ~1.3s frozen-binary spawn tax that fast-resolve removed from resolve() — twice
    per video (video + audio jobs). Mirrors what the child did for an already-resolved DIRECT
    URL: the proven 6-header set with the format's own UA, forced IPv4, a 30s socket timeout,
    and — the load-bearing part — BOUNDED ~10M Range GETs per chunk (the --http-chunk-size
    trick: each bounded request re-enters googlevideo's full-speed burst window, where one
    open-ended GET gets paced down to ~playback bitrate; on-device 2026-09-03: 0.58->10 MB/s).

    Error surface: read() returns b"" at clean EOF *and* on any failure — exactly a child pipe
    closing — so _reader's existing resume machinery (re-resolve, R9 tries cap, D5) handles
    both; this object never retries what it can't fix (it has no way to re-resolve a URL).
    A mid-chunk truncation IS self-healed by reopening from the current offset (cheap — no
    process to relaunch), capped so a no-progress loop still dies into the resume path. kill()
    from the reap paths closes the live response, which unblocks a concurrent read(); a read
    blocked in connect() rides out its own <=30s timeout (the reader re-checks DEAD/gen right
    after, same as a slow child kill today)."""

    def __init__(self, url, ua, at, total):
        _force_ipv4()                # the in-process equivalent of the child's -4
        self.url, self.ua = url, ua
        self.pos = at                # next content offset to fetch (bytes are handed out in order)
        self.total = total           # from clen=; None -> learned from the first 206 Content-Range
        self.resp = None
        self.chunk_end = -1          # last offset of the open bounded chunk; None = open-ended 200
        self.reopens = 0             # consecutive ZERO-PROGRESS reopens (truncation guard)
        self.returncode = None       # duck: None while live, 0 clean EOF / killed, 1 error death
        self.stdout = self           # _reader drains proc.stdout.read(n)

    def _open_next(self):
        end = self.pos + _DIRECT_CHUNK - 1
        if self.total is not None:
            end = min(end, self.total - 1)
        req = urllib.request.Request(self.url, headers={
            "User-Agent": self.ua or _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Sec-Fetch-Mode": "navigate",
            "Accept-Encoding": "identity",
            "Range": "bytes=%d-%d" % (self.pos, end),
        })
        resp = urllib.request.urlopen(req, timeout=30)
        code = getattr(resp, "status", None) or resp.getcode()
        if code == 200:              # server ignored the Range: stream this one response to EOF
            self.chunk_end = None
        else:                        # 206: note the chunk bound; learn the total ("bytes a-b/N")
            self.chunk_end = end
            if self.total is None:
                m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", "") or "")
                if m:
                    self.total = int(m.group(1))
        self.resp = resp

    def read(self, n):
        """Next <=n bytes at self.pos; b"" at clean EOF or on any failure (= child pipe close)."""
        while self.returncode is None:
            if self.resp is None:
                if self.total is not None and self.pos >= self.total:
                    self.returncode = 0                   # everything delivered — clean EOF
                    return b""
                try:
                    self._open_next()
                except urllib.error.HTTPError as ex:
                    self.returncode = 0 if ex.code == 416 else 1   # 416: past EOF (no-clen case)
                    return b""
                except Exception:
                    self.returncode = 1                   # DNS / TLS / timeout / reset / ...
                    return b""
            try:
                buf = self.resp.read(n)
            except Exception:
                buf = b""
            if buf:
                self.pos += len(buf)
                self.reopens = 0
                return buf
            try:                                          # response exhausted (or died) — retire it
                self.resp.close()
            except Exception:
                pass
            self.resp = None
            if self.chunk_end is None:                    # open-ended 200 finished -> stream done
                self.returncode = 0
                return b""
            if self.pos > self.chunk_end:                 # bounded chunk fully consumed — normal;
                continue                                  # loop opens the next burst window
            self.reopens += 1                             # truncated mid-chunk: reopen from pos,
            if self.reopens > 3:                          # but never loop on zero progress
                self.returncode = 1
                return b""
        return b""

    # --- duck-typed child-process surface (for _reap_proc / _reap_locked / _reaper) --- #
    def kill(self):
        self.returncode = 0
        resp, self.resp = self.resp, None
        try:
            if resp is not None:
                resp.close()                              # unblocks a concurrent read()
        except Exception:
            pass

    terminate = kill

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def _spawn(s, at):
    """The download job for bytes [at, ...) of s.url — an in-process _DirectFetch by default,
    the yt-dlp CHILD when this stream flipped to the fallback (or _DIRECT_STREAM is off). Both
    expose the same duck surface (.stdout.read/.kill/.poll/.wait), so _reader and the reap
    machinery are agnostic. `s.url` is an already-resolved DIRECT googlevideo URL (from
    _proxied / _reresolve), so no cookies / PO-token / extractor args belong on either path.

    Child path: mirrors the EXACT 6-header set the retired _fetch proved on-device 2026-09-03
    (a bare request 403s at byte 0, empty body = bot-check) plus --socket-timeout so a stalled
    fetch dies into the resume path. preexec_fn makes the kernel SIGKILL the child if the
    worker dies. With _RANGE_RESTART=True `at` is s.edge on a resume; the frozen yt-dlp
    forwards the Range header (verified), so streamed content offset == at (pwrite offset
    stays correct). (D8, D10)"""
    if _DIRECT_STREAM and not s.use_binary:
        try:
            _plog("spawn direct itag=%s at=%d" % (s.itag, at))
            return _DirectFetch(s.url, s.ua, at, s.total)
        except Exception as ex:                # constructor is offline/lazy; belt-and-braces
            s.use_binary = True
            _plog("direct-fetch init failed (%r) -> binary child" % ex)
    _plog("spawn child itag=%s at=%d" % (s.itag, at))
    argv = [_ytdlp_path(), *_COMMON_ARGS, "--no-playlist",
            "--socket-timeout", "30",
            # googlevideo paces a single open-ended GET down to ~playback bitrate; --http-chunk-size
            # makes yt-dlp issue BOUNDED Range GETs per chunk, each re-entering its full-speed burst
            # window. On-device 2026-09-03: 0.58->10.09 MB/s WiFi, 0.55->1.30 MB/s 4G. Offset-safe:
            # the injected "Range: bytes=<at>-" (below) becomes HttpFD req_start and chunking continues
            # FROM there, so the reader's pwrite offset == content offset (D10) still holds (verified:
            # first 64KB byte-identical to the non-chunked Range fetch, no double-offset on this build).
            "--http-chunk-size", "10M",
            "--user-agent", s.ua,
            "--add-header", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "--add-header", "Accept-Language: en-us,en;q=0.5",
            "--add-header", "Sec-Fetch-Mode: navigate",
            "--add-header", "Accept-Encoding: identity"]
    if at > 0:
        argv += ["--add-header", "Range: bytes=%d-" % at]
    argv += ["-o", "-", "--", s.url]
    return subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            preexec_fn=_set_pdeathsig)


def _reader(s, gen):
    """The SOLE writer of s.fd. Drains yt-dlp's pipe into the temp file; pwrites at the offset the
    proc actually streamed from (dl_start + bytes-read this proc), so the offset ALWAYS equals the
    content offset in BOTH range modes (D10). The read-ahead cap doubles as the backpressure gate.
    On a mid-stream pipe death it re-resolves a fresh URL and resumes. In the SHIPPED mode
    (_RANGE_RESTART=True) resume spawns at s.edge and preserves origin/edge, so the do_GET hole-punch
    keeps disk bounded during resume just like steady playback. The False FALLBACK re-downloads from
    0 (bytes re-pwritten idempotently at the same offsets); it can grow the temp file during a deep
    resume, and only the per-8MiB free-space fail-safe below bounds it — a documented fallback limit."""
    tries = 0
    last_free_edge = 0                                       # edge at the last free-space check (D3)
    wfd = -1
    try:
        with s.cond:
            if s.state == "DEAD" or gen != s.gen:
                return
            try:
                wfd = os.dup(s.fd)
            except OSError:
                wfd = -1
        if wfd < 0:
            with s.cond:
                if s.state not in ("DONE", "DEAD"):
                    s.state = "FAIL"; s.cond.notify_all()
            return
        while True:
            proc = s.proc
            nproc = 0                                        # bytes THIS proc has produced (D10)
            while True:
                with s.cond:                                # read-ahead cap == backpressure
                    got = s.cond.wait_for(lambda: s.state == "DEAD"
                                          or gen != s.gen
                                          or s.edge - s.cursor < _READAHEAD,
                                          timeout=2.0)       # wake on DEAD/gen change or an open gate
                    if s.state == "DEAD" or gen != s.gen:
                        return
                    if not got:              # 2s timeout with the gate STILL closed: we're already
                        continue             # >= _READAHEAD past the play cursor (a paused / slow
                                             # reader). Loop and keep waiting — do NOT read another
                                             # chunk past the cap. Without this the cap was advisory:
                                             # a paused video kept pulling ~1 chunk/2s until the whole
                                             # file was on disk. The timeout now only re-checks DEAD/gen. (M2)
                buf = proc.stdout.read(_SESS_CHUNK)          # blocks on the network; never spins
                if not buf:
                    break
                try:
                    os.pwrite(wfd, buf, s.dl_start + nproc) # D10: offset == content offset streamed
                except OSError:                              # ENOSPC / bad fd -> clean FAIL
                    with s.cond:
                        if s.state not in ("DONE", "DEAD"):
                            s.state = "FAIL"; s.cond.notify_all()
                    return
                nproc += len(buf)
                with s.cond:
                    if s.state == "DEAD" or gen != s.gen:
                        return
                    new_edge = s.dl_start + nproc
                    if new_edge > s.edge:
                        s.edge = new_edge
                        s.edge_ts = time.time()              # D8: mark forward progress
                        s.cond.notify_all()                  # wake do_GETs blocked at the edge
                if s.edge - last_free_edge >= (8 << 20):     # D3 fail-safe: a growing stream can't
                    last_free_edge = s.edge                  #     fill the device (fallback-mode guard)
                    if _free_bytes(_STREAM_DIR) < _MIN_FREE:
                        with s.cond:
                            if s.state not in ("DONE", "DEAD"):
                                s.state = "FAIL"; s.cond.notify_all()
                        return
            # pipe closed: clean finish, our own reap, or mid-stream death (expired URL / 403)
            with s.cond:
                if s.state == "DEAD" or gen != s.gen:
                    return
                if s.total is not None and s.edge >= s.total:
                    s.state = "DONE"; s.cond.notify_all(); return
            if s.total is None:                              # R2: length-unknown (rare no-clen/bare-200)
                with s.cond:
                    if nproc > 0:                            # produced bytes then clean EOF == the end
                        s.state = "DONE"; s.cond.notify_all(); return
                    # nproc == 0 -> a real byte-0 death; fall through to the capped resume path
            with s.cond:
                s.edge_ts = time.time()   # B4: recovery in progress — don't let the stall watchdog abort re-resolve
            _reap_proc(proc)                                 # off-lock reap of the exited child
            if isinstance(proc, _DirectFetch) and nproc == 0:
                # The direct fetch produced NOTHING (403/expired/blocked at byte 0). Flip this
                # stream to the binary child before the resume respawn — the fallback doctrine:
                # worst case becomes exactly the pre-direct behaviour. (The re-resolve below may
                # also hand the next spawn a fresh URL; the child gets first go at it.)
                s.use_binary = True
                _plog("direct-fetch dead at byte 0 (itag=%s) -> binary child" % s.itag)
            if s.cursor > s.cursor_at_last_death:            # R9: credit REAL playback advance (cursor
                tries = 0                                    #     survives a False resume's edge=0),
            s.cursor_at_last_death = s.cursor                #     cap only genuinely stuck streams
            tries += 1
            if tries > _RESUME_TRIES:
                with s.cond:
                    s.state = "FAIL"; s.cond.notify_all()
                return
            fresh = _reresolve(s.vid, s.itag, s.url)         # reuse the rate-limited 403 refresh
            if fresh and _proxy_url_ok(fresh):
                s.url = fresh
            with s.cond:
                if s.state == "DEAD" or gen != s.gen:
                    return
                if _RANGE_RESTART:                           # shipped: trust the Range, resume at edge
                    s.dl_start = s.edge
                else:                                        # fallback: re-download from 0; bytes
                    s.origin = 0; s.edge = 0; s.dl_start = 0 #   re-pwritten at same offsets. cursor is
                    s.edge_ts = time.time()                  #   preserved (serve position).
            newproc = _spawn(s, s.dl_start)                  # R4: spawn into a local...
            with s.cond:                                     # ...then commit under the DEAD/gen re-check
                if s.state == "DEAD" or gen != s.gen:
                    try: newproc.kill()
                    except Exception: pass
                    _reap_proc(newproc)                      # reader holds no lock -> inline wait ok
                    return
                s.proc = newproc                             # now a reap either kills this or we did
    finally:
        # ANY unhandled path terminates the stream cleanly, so blocked do_GETs wake, refs drain, and
        # the reaper collects it — never leave state RUN behind a dead reader thread.
        if wfd >= 0:
            try: os.close(wfd)
            except OSError: pass
        with s.cond:
            if s.state not in ("DONE", "DEAD"):
                s.state = "FAIL"
                s.cond.notify_all()


def _acquire(vid, itag, url, ua, total, start):
    """Return the _Stream that will serve bytes from `start`, creating / restarting as needed.
    The ONLY place a seek (re)starts a yt-dlp process. Bumps refs (caller MUST drop it in a
    finally). Returns None at capacity / low disk / no yt-dlp, so do_GET can answer 503.
    refs is mutated under s.cond; acquire already holds _streams_lock and takes s.cond AFTER it,
    preserving the _streams_lock -> s.cond order (no deadlock). (D1, D7, R1, R6, R7)"""
    key = (vid, itag)
    with _streams_lock:
        s = _streams.get(key)
        if s and s.state in ("RUN", "DONE") and s.origin <= start <= s.edge + _SEEK_SOON:
            with s.cond:                                   # R1: reuse ONLY live/complete streams
                s.refs += 1                                # R7: refs under s.cond (a FAIL stream falls
                s.last_active = time.time()                #     through below and is rebuilt fresh)
            return s
        if s:                                              # DEAD/FAIL, far-forward, or below-origin
            _reap_locked(s)
            del _streams[key]
        if len(_streams) >= _MAX_STREAMS:
            _reap_one_idle_locked()
        if len(_streams) >= _MAX_STREAMS or _free_bytes(_STREAM_DIR) < _MIN_FREE:
            return None
        if not _ytdlp_path():                              # D7: never build a _Stream we can't feed
            return None
        s = None
        try:                                               # D7: guarded construction
            s = _Stream(vid, itag, url, ua, total)         #     no orphaned fd / tempfile / proc
            if _RANGE_RESTART:                             # responsive deep seek: download FROM start
                s.origin = s.edge = s.cursor = start
                s.dl_start = start
                if start:
                    os.ftruncate(s.fd, 0)                  # reclaim; [0,start) stays a free hole
            else:                                          # fallback: download from 0, gate waits for
                s.origin = s.edge = 0                      #   edge to reach the target. cursor=start
                s.dl_start = 0                             #   anchors the read-ahead gate at the play
                s.cursor = start                           #   position (D1) so the reader fills toward it
            s.refs = 1                                     # R7: fresh object, uncontended
            s.gen += 1
            s.edge_ts = time.time()
            s.proc = _spawn(s, s.dl_start)
            threading.Thread(target=_reader, args=(s, s.gen), daemon=True).start()
        except Exception as ex:
            _plog("acquire failed: %r" % ex)
            if s is not None:
                if s.proc is not None:                     # R6: kill+queue a child spawned before the
                    try: s.proc.kill()                     #     Thread.start() that raised (else it
                    except Exception: pass                 #     blocks on its pipe until pdeathsig)
                    with _reap_lock:
                        _reap_pending.append(s.proc)       # R3/R5: wait() off-lock in the reaper
                try: os.close(s.fd)
                except Exception: pass
                try: os.remove(s.path)
                except OSError: pass
            return None
        _streams[key] = s
        return s


def _wait(s, pos):
    """Bytes readable at `pos` right now, or None at clean EOF / failure / reap / stall. Blocks at
    the live edge until the reader advances past `pos` (woken by its notify; 1 s liveness fallback).
    The wait is ALWAYS timed and every terminal state returns None, so no path blocks forever (D8)."""
    with s.cond:
        while True:
            if s.state == "DEAD":
                return None
            if s.origin <= pos < s.edge:
                return s.edge - pos                        # on disk -> serve now
            if s.state in ("DONE", "FAIL"):
                return None                                # DONE: clean EOF; FAIL: short close (D11)
            if pos < s.origin:
                return None                                # below reclaimed origin (acquire restarts)
            if s.state == "RUN" and pos >= s.edge and time.time() - s.edge_ts > _STALL:
                return None                                # D8: edge stuck at the live edge -> give up
            s.cond.wait(timeout=1.0)


def _reap_locked(s):
    """Tear a stream down. Caller holds _streams_lock and removes the key afterwards. state=DEAD,
    the kill, AND the fd close all happen under s.cond, so a do_GET taking its per-connection os.dup
    under the same cond either dups a still-valid fd or sees DEAD and bails — never dups a closed /
    recycled fd. os.remove is immediate; POSIX keeps the inode alive for every outstanding dup.
    NO proc.wait() here (R3/R5): the killed child is QUEUED to _reap_pending and reaped off-lock by
    the reaper, so a wedged child never stalls the registry while _streams_lock is held. (D2, R3, R5)"""
    with s.cond:
        s.state = "DEAD"
        s.cond.notify_all()                                # wake the reader + every do_GET
        try:
            if s.proc:
                s.proc.kill()
        except Exception:
            pass
        try:
            os.close(s.fd)
        except Exception:
            pass
        try:
            os.remove(s.path)
        except OSError:
            pass
    if s.proc:                                             # R3/R5: append is atomic; no wait() on-lock
        with _reap_lock:
            _reap_pending.append(s.proc)


def _reap_one_idle_locked():
    """Reap the least-recently-active idle (refs==0) stream to free a slot. Caller holds the lock.
    refs / last_active are read under s.cond (R7); a stale read only delays a reap by one cycle."""
    victim = None
    for k, s in _streams.items():
        with s.cond:
            idle = s.refs <= 0
            la = s.last_active
        if idle and (victim is None or la < victim[2]):
            victim = (k, s, la)
    if victim:
        _reap_locked(victim[1])
        del _streams[victim[0]]


def _reaper():
    """Background: drain zombie children off-lock (R3/R5), then reap streams idle (no connection) past
    _IDLE. Steady playback (foreground or background audio) always holds >=1 connection, so it is never
    reaped; a seek drops refs to 0 for milliseconds << _IDLE. refs/last_active read under s.cond (R7)."""
    global _reap_pending
    while True:
        time.sleep(_REAP_EVERY)
        # R3/R5: reap SIGKILLed children here, holding NO lock, so a wedged child never stalls do_GET.
        with _reap_lock:
            pend = _reap_pending; _reap_pending = []
        keep = []
        for p in pend:
            try:
                if p.poll() is None: p.wait(timeout=1)     # brief; SIGKILL usually reaps in ms
            except Exception: pass
            try:
                if p.poll() is None: keep.append(p)        # still not dead -> retry next cycle
            except Exception: pass
        if keep:
            with _reap_lock:
                _reap_pending.extend(keep)
        now = time.time()
        with _streams_lock:
            for k, s in list(_streams.items()):
                with s.cond:                               # R7: read refs/last_active under s.cond
                    reap = s.refs <= 0 and now - s.last_active > _IDLE
                if reap:
                    _reap_locked(s)
                    del _streams[k]


def _sweep_all_streams():
    """Kill every live stream and delete all stream-cache temp files. Run at proxy startup (mop up a
    previous hard crash's leftovers) and via atexit (leave no orphaned yt-dlp child / temp file).
    Drains _reap_pending with a blocking wait too — the process is exiting, so a short wait is fine
    (R3/R5)."""
    with _streams_lock:
        for k, s in list(_streams.items()):
            _reap_locked(s)
            del _streams[k]
    with _reap_lock:
        pend = list(_reap_pending); _reap_pending[:] = []
    for p in pend:
        _reap_proc(p)
    if not _STREAM_DIR:
        return
    import glob
    for p in glob.glob(os.path.join(_STREAM_DIR, "s-*.dat")):
        try:
            os.remove(p)
        except OSError:
            pass


def release_playback(video_id, keep_itags=None):
    """PyOtherSide entry, called from QML teardown (Component.onDestruction, nowPlaying
    stopRequested, switchQuality / switchAudio). Reap every stream for this video whose itag is NOT
    in keep_itags. Reaps regardless of refs — safe because each live do_GET serves from its OWN dup
    (D2). Keyed off video_id, so it is correct even when the departing page tears down after the next
    page's resolve()."""
    keep = {str(i) for i in (keep_itags or [])}
    with _streams_lock:
        for k, s in list(_streams.items()):
            if k[0] == video_id and k[1] not in keep:
                _reap_locked(s)
                del _streams[k]
    return {"ok": True}


class _MediaProxyHandler(http.server.BaseHTTPRequestHandler):
    # libsoup (souphttpsrc) sends HTTP/1.1 requests; answer in kind. Bytes come from a per-itag
    # download job (_Stream): yt-dlp streams into a temp file, we serve preads gated by that file's
    # live edge, so every seek reuses one bounded, backpressured download instead of re-fetching
    # from zero. Body is close-delimited (Connection: close), the framing souphttpsrc accepts.
    # do_GET NEVER takes _streams_lock: it only ever takes s.cond (alone), so the sole global lock
    # ordering in the module stays _streams_lock -> s.cond with no hazard here (R7/R10).
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the app log quiet

    def do_GET(self):
        global _PUNCH_OK
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = q.get("u", [None])[0]
        video_id = q.get("v", [None])[0]
        itag = q.get("itag", [None])[0]
        ua = q.get("ua", [None])[0] or _BROWSER_UA  # the format's client UA (googlevideo is UA-bound)
        if not target:
            self.send_error(400, "missing target")
            return
        if not _proxy_url_ok(target):
            self.send_error(403, "blocked target")   # not an https Google/googlevideo host
            return
        raw_range = self.headers.get("Range", "")
        start = 0
        m = re.match(r"bytes=(\d+)-", raw_range)
        if m:
            start = int(m.group(1))
        total = _clen(target)  # googlevideo's full length, straight from the URL (authoritative)
        _plog("REQ itag=%s range=%s total=%s" % (itag, raw_range or "none", total))

        s = _acquire(video_id, itag, target, ua, total, start)
        if s is None:
            self.send_error(503, "no stream capacity")   # too many streams, low disk, or no yt-dlp
            return

        # D1 (WARM reuse): anchor the read-ahead gate at (or past) our start. A forward seek into
        # (edge, edge+_SEEK_SOON] reuses a stream whose cursor still sits below edge; without this
        # the reader stays pinned at its cap and we'd block forever.
        with s.cond:
            if start > s.cursor:
                s.cursor = start
                s.cond.notify_all()

        # D2: take our OWN dup of the fd, under s.cond, re-checking the stream wasn't just reaped.
        # Every os.pread / os.fallocate below uses cfd; POSIX keeps the inode alive until we close it
        # in finally, so a concurrent _reap_locked (release_playback / seek restart) can never make
        # us touch a recycled fd.
        cfd = -1
        with s.cond:
            if s.state != "DEAD":
                try:
                    cfd = os.dup(s.fd)
                except OSError:
                    cfd = -1
        if cfd < 0:
            with s.cond:                     # R7/R10: refs under s.cond (do_GET never takes _streams_lock)
                s.refs -= 1
                s.last_active = time.time()
            self.send_error(503, "stream gone")
            return

        try:
            # Framing: 206 + Content-Range/Content-Length when the client sent a Range and total is
            # known; 200 + Content-Length when total known and no Range; bare close-delimited 200
            # when total is unknown (no clen=, non-seekable). Content-Type is generic — decodebin
            # typefinds the container. NOTE (D11): if a stream goes FAIL after these headers are sent,
            # the body closes short (truncated 206); inherent, minimised by the D5/R9 resume logic.
            ctype = "application/octet-stream"
            if total is not None and raw_range:
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, total - 1, total))
                self.send_header("Content-Length", str(total - start))
            elif total is not None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(total - start))
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            pos = start
            written = 0
            while total is None or pos < total:
                n = _wait(s, pos)                # readable bytes at pos, or None at EOF/fail/reap/stall
                if n is None:
                    break
                data = os.pread(cfd, min(_SESS_CHUNK, n), pos)   # D2: from our private dup
                if not data:
                    break
                self.wfile.write(data)
                pos += len(data)
                written += len(data)
                with s.cond:
                    if pos > s.cursor:           # advance the read-ahead gate -> reader may fetch on
                        s.cursor = pos
                        s.cond.notify_all()
                    # R7/R10: reclaim the consumed prefix in place, but ONLY when THIS is the sole
                    # connection (s.refs == 1), checked atomically with the punch under s.cond. refs
                    # can only rise to 2 by another thread taking s.cond, so no second reader can
                    # appear mid-punch; with refs==1 there is exactly one reader (this do_GET) at
                    # pos==cursor, so punching [origin, cursor-_KEEPBACK) never touches a live byte.
                    # refs>1 (transient seek overlap) simply skips the punch until it drops back to 1:
                    # bounded extra disk, never zeros. Best-effort on cfd (a valid dup even if s.fd
                    # was just reaped); disabled permanently on first failure. (D3)
                    if _PUNCH_OK and s.refs == 1 and pos - s.origin > _KEEPBACK:
                        new_origin = pos - _KEEPBACK
                        try:
                            os.fallocate(cfd, _FALLOC_PUNCH | _FALLOC_KEEP,
                                         s.origin, new_origin - s.origin)
                            s.origin = new_origin
                        except Exception:
                            _PUNCH_OK = False   # degrade to full-file; never crash
                    s.last_active = time.time()
            self.wfile.flush()
            _plog("DONE start=%d wrote=%d" % (start, written))
        except (BrokenPipeError, ConnectionResetError):
            _plog("CLIENT-CLOSED start=%d" % start)   # player seeked/stopped — normal; stream kept
        except Exception as ex:
            _plog("proxy error start=%d: %r" % (start, ex))
            self.close_connection = True
        finally:
            try:
                os.close(cfd)                   # D2: release our dup; inode freed at the last close
            except OSError:
                pass
            with s.cond:                        # R7/R10: refs under s.cond, single-lock, no _streams_lock
                s.refs -= 1
                s.last_active = time.time()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _ensure_proxy():
    """Start the localhost proxy once; return its port. Also prepares the stream cache
    (<data_dir>/streamcache), sweeps any temp files a prior hard crash left, starts the idle reaper,
    and registers an atexit sweep so no yt-dlp child or temp file is ever orphaned. IPv4 pinning:
    _DirectFetch calls _force_ipv4() itself (the in-process equivalent of the child's -4)."""
    global _proxy_port, _STREAM_DIR
    with _proxy_lock:
        if _proxy_port:
            return _proxy_port
        if _DEBUG:
            try:
                open("/tmp/youfish-proxy.log", "w").close()  # fresh log each app run
            except Exception:
                pass
        _STREAM_DIR = os.path.join(_data_dir(), "streamcache")
        try:
            os.makedirs(_STREAM_DIR, exist_ok=True)
        except Exception:
            pass
        _sweep_all_streams()  # remove s-*.dat left behind by a previous hard crash
        server = _ThreadingHTTPServer(("127.0.0.1", 0), _MediaProxyHandler)
        _proxy_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=_reaper, daemon=True).start()
        atexit.register(_sweep_all_streams)
        return _proxy_port


def _proxied(url, video_id="", itag="", ua=""):
    """Wrap a stream URL so playback goes through the header-injecting proxy.

    video_id + itag ride along so the proxy can re-resolve a fresh URL if this one
    starts 403ing mid-stream (googlevideo throttles sustained streaming access). `ua`
    is the format's own User-Agent, forwarded so the proxy fetches googlevideo with the
    exact UA yt-dlp used (android-client URLs 403 under a mismatched UA).
    """
    if not url:
        return ""
    port = _ensure_proxy()
    q = "http://127.0.0.1:%d/play?u=%s" % (port, urllib.parse.quote(url, safe=""))
    if video_id and itag:
        q += "&v=%s&itag=%s" % (urllib.parse.quote(str(video_id), safe=""),
                                urllib.parse.quote(str(itag), safe=""))
    if ua:
        q += "&ua=" + urllib.parse.quote(ua, safe="")
    return q


# --- Fresh-URL refresh on mid-stream 403 -------------------------------------- #
# A googlevideo stream URL stops honouring sustained access after ~a minute and
# starts returning 403 partway through (throttle/session limit, not expiry). yt-dlp
# copes by re-extracting; the proxy does the same — on a 403 it re-resolves the
# video, swaps in the fresh URL for the same itag, and resumes at the identical byte
# offset (same itag => same encoding => byte-identical stream).
_url_cache = {}          # video_id -> {"ts": epoch, "fmts": {itag: fresh_url}}
_url_cache_lock = threading.Lock()
_URL_CACHE_TTL = 3600    # an entry only coordinates one playback; googlevideo URLs outlive it
_URL_CACHE_MAX = 64      # bound it so a long session can't stack refreshes without limit
# Rate-limit yt-dlp spawns triggered by the proxy's 403-refresh path, so a local caller can't
# hammer it with novel video_ids to force an unbounded stream of forks. Legit playback re-resolves
# rarely (only on a mid-stream 403), so a small burst is plenty. Guarded by _url_cache_lock.
_reresolve_spawns = []   # recent spawn timestamps
_RERESOLVE_WINDOW = 60.0
_RERESOLVE_BURST = 8


@_timed_fn("q.formats")
def _ytdlp_formats(video_id, anon=False):
    """Run yt-dlp and return {itag: direct_url} for every format that has a URL.

    anon=True mirrors resolve()'s PRIMARY dump — token-free AND cookie-free — so the refreshed
    map comes from the same client + auth posture that produced the playing URLs (same itag
    shapes) and dodges the authenticated-token-free gating the anonymous-primary fix proved
    (see _dump). anon=False is the reliable safety net: cookies (restricted content) + a
    minted PO token, exactly like resolve()'s fallback dumps."""
    path = _ytdlp_path()
    if not path or not video_id:
        return {}
    url = video_id if "://" in video_id else "https://www.youtube.com/watch?v=" + video_id
    _ensure_pot_server()  # a fresh URL is just as PO-gated; keep the token sidecar warm
    if _DEBUG and _pot_active():   # gate state on the WARM (self-heal) side, to compare with resolve's
        _tlog("reresolve gate: port=%s http=%r" % (_pot_ready_on_port(), _pot_http_ping(0.5)["ok"]))
    try:
        with (contextlib.nullcontext([]) if anon else _cookies_args()) as cargs:
            proc = subprocess.run([path, *_COMMON_ARGS, *cargs, *_pot_ytdlp_args(),
                                   *_yt_extractor_args(want_pot=not anon),
                                   "--dump-single-json", "--", url],
                                  capture_output=True, text=True, timeout=90,
                                  preexec_fn=_set_pdeathsig)   # D6: die with the app if abandoned
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return {f.get("format_id"): f.get("url")
                for f in data.get("formats", []) if f.get("format_id") and f.get("url")}
    except Exception:
        return {}


def _reresolve(video_id, itag, failed_url):
    """Fresh direct URL for (video_id, itag), re-running yt-dlp at most once per stale
    generation. Concurrent video+audio 403s share one refresh: whoever takes the lock
    first re-extracts; the other sees a cached URL that differs from its failed one and
    reuses it without a second yt-dlp run.

    Mirrors resolve()'s two-step strategy: an ANONYMOUS token-free dump first (the posture
    that produced the playing URLs on the common path — same itag shapes, and immune to the
    authenticated-token-free gating), then the cookie'd + PO-token dump only when the
    anonymous pass didn't yield THIS itag (restricted content, or URLs born from the mweb
    gated-fallback whose shapes a token-free dump may not reproduce). Both dumps count
    against the spawn rate-limit; worst case this holds _url_cache_lock across two 90s
    dumps — acceptable for a rare recovery path where reliability beats latency.
    """
    with _url_cache_lock:
        ent = _url_cache.get(video_id)
        if ent and time.time() - ent["ts"] < _URL_CACHE_TTL:
            cached = ent["fmts"].get(itag)
            if cached and cached != failed_url:
                return cached  # another track already refreshed this generation
        now = time.time()
        _reresolve_spawns[:] = [t for t in _reresolve_spawns if now - t < _RERESOLVE_WINDOW]
        if len(_reresolve_spawns) >= _RERESOLVE_BURST:
            _plog("reresolve rate-limited (%d in %.0fs)" % (len(_reresolve_spawns), _RERESOLVE_WINDOW))
            return None
        _reresolve_spawns.append(now)
        fresh = _ytdlp_formats(video_id, anon=True)
        if not fresh.get(itag):
            if len(_reresolve_spawns) < _RERESOLVE_BURST:      # the fallback spawn pays the limit too
                _reresolve_spawns.append(time.time())
                fresh2 = _ytdlp_formats(video_id)              # cookie'd + minted-token safety net
                if fresh2:
                    fresh = {**fresh, **fresh2}                # merged map still serves the other track
            else:
                _plog("reresolve token fallback rate-limited")
        if not fresh:
            return None
        if _DEBUG:   # the WARM re-resolve's token + client for the exact itag that 403'd
            _tlog("reresolve itag=%s %s client=%s"
                  % (itag, _pot_of(fresh.get(itag, "")), _default_client() or "auto"))
        _url_cache[video_id] = {"ts": time.time(), "fmts": fresh}
        if len(_url_cache) > _URL_CACHE_MAX:  # evict oldest beyond the cap
            for k, _ in sorted(_url_cache.items(),
                               key=lambda kv: kv[1]["ts"])[:len(_url_cache) - _URL_CACHE_MAX]:
                _url_cache.pop(k, None)
        return fresh.get(itag)


# --------------------------------------------------------------------------- #
# Resolve-RESULT cache + single-flight + speculative prefetch.  (REBUILD #1)
#
# Distinct from _url_cache (mid-stream 403 refresh). Stores the FULL {ok, info}
# resolve() payload keyed by (video_id + every setting that changes the output),
# stamped with a freshness deadline from the googlevideo `expire=` in the URLs.
# prefetch_resolve() fills it on a background thread; the cache-first resolve()
# front-door reads it (instant hit), JOINS an in-flight resolve rather than
# double-spawning, else resolves for real and caches a usefully-fresh success.
# --------------------------------------------------------------------------- #
_resolve_cache = {}                        # key -> {"payload": {ok,info}, "good_until": epoch, "ts": epoch}
_resolve_cache_lock = threading.Lock()
_resolve_inflight = {}                     # key -> threading.Event (leader signals joiners)
_RESOLVE_CACHE_MAX = 24
_RESOLVE_CACHE_MAX_TTL = 20 * 60           # never trust an entry longer than this, even if expire is hours out
_RESOLVE_SAFETY = 120                      # drop an entry this many secs BEFORE its URLs actually expire
# D1: join ceiling. _resolve_uncached runs up to THREE subprocess.run(timeout=90) dumps
# (primary + probe→mweb gated fallback + its SABR widen; the hard-fail widen is mutually
# exclusive with the probe path), so ~270s worst case. The joiner must wait PAST that,
# never time out early and launch a second resolve. 300s covers 3x90s + margin.
_RESOLVE_JOIN_TIMEOUT = 300

_prefetch_sema = threading.BoundedSemaphore(2)   # <=2 speculative yt-dlp jobs at once (no swarm)
_prefetch_pending = set()                  # keys queued/running as prefetch (debounce)
_prefetch_lock = threading.Lock()

_EXPIRE_RE = re.compile(r"(?:[?&]|%26|%3F|/)expire(?:=|/|%3D)(\d{9,11})", re.IGNORECASE)

# D10: settings keys that change resolve()'s OUTPUT — a change to any of these drops the cache.
_RESOLVE_OUTPUT_KEYS = ("player_client", "pot_provider")


def _expire_ts(u):
    """googlevideo `expire` unix-ts out of a URL — raw OR embedded/quoted in a proxied `u=`
    param (where the real validity clock lives). 0 if none (HLS / odd shape)."""
    if not u:
        return 0
    m = _EXPIRE_RE.search(u) or _EXPIRE_RE.search(urllib.parse.unquote(u))
    return int(m.group(1)) if m else 0


def _good_until(info):
    """Earliest picked-URL expiry minus a safety margin, capped at a sane max. resolve() never
    parses expire, so we do it here over the muxed/audio URLs."""
    now = time.time()
    exps = [e for e in (_expire_ts(info.get("muxed_url")),
                        _expire_ts(info.get("audio_url"))) if e]
    if not exps:                           # HLS-only / no parseable expire -> short conservative TTL
        return now + 5 * 60
    return min(min(exps) - _RESOLVE_SAFETY, now + _RESOLVE_CACHE_MAX_TTL)


def _signed_in():
    """Coarse login state for the cache key (a login change alters extraction -> invalidates)."""
    try:
        import ytm
        return bool(ytm.netscape_cookies())
    except Exception:
        return False


def _resolve_key(video_id):
    """video_id PLUS every hidden input that changes resolve()'s output. UI-taste settings
    (eq/boost/autoplay/…) are excluded — they don't affect the returned URLs."""
    return "\x1f".join((
        str(video_id),
        _default_client() or "auto",               # player_client (effective) — client + UA + ladder
        "1" if _pot_active() else "0",              # PO provider active -> flips client/token path
        "1" if _signed_in() else "0",               # login -> age/members/premium extraction
    ))


def _evict_resolve_cache_locked():
    if len(_resolve_cache) <= _RESOLVE_CACHE_MAX:
        return
    victims = sorted(_resolve_cache.items(), key=lambda kv: kv[1]["ts"])[
        :len(_resolve_cache) - _RESOLVE_CACHE_MAX]
    for k, _ in victims:
        _resolve_cache.pop(k, None)


def _resolve_cache_get(key):
    now = time.time()
    with _resolve_cache_lock:
        ent = _resolve_cache.get(key)
        if ent and ent["good_until"] > now:
            return ent["payload"]
        if ent:
            _resolve_cache.pop(key, None)          # expired -> drop
    return None


def invalidate_resolve_cache():
    """Clear the whole resolve cache. Called on any output-affecting settings change and on
    login/logout (cheap — small dict, refills on demand)."""
    with _resolve_cache_lock:
        _resolve_cache.clear()


def _resolve_and_cache(video_id, key=None, speculative=False):
    """The one place a resolve actually happens. Cache hit -> instant. An in-flight resolve for
    the SAME key -> JOINED (waited on), never double-spawned. Else run the real _resolve_uncached
    and cache a fresh, non-live, full-ladder success. Runs the subprocess on WHATEVER thread calls
    it, so the prefetch path MUST call it from a background thread (never the worker).

    `speculative` is threaded for triggers (b)/(c) (D9): unused in #1, behaviour identical. Later
    it will skip the widen retry (I9) and cap good_until (I12); do NOT branch on it yet."""
    if key is None:
        key = _resolve_key(video_id)

    hit = _resolve_cache_get(key)
    if hit is not None:
        return hit

    with _resolve_cache_lock:
        ev = _resolve_inflight.get(key)
        if ev is None:
            ev = threading.Event()
            _resolve_inflight[key] = ev
            leader = True
        else:
            leader = False

    if not leader:                                 # ---- JOIN the in-flight resolve (D1) ----
        if not ev.wait(_RESOLVE_JOIN_TIMEOUT):     # wait PAST the leader's 2x90s ceiling
            hit = _resolve_cache_get(key)          # timed out (near-impossible): re-check cache
            if hit is not None:
                return hit
            # NEVER launch a second subprocess. The leader is about to populate; a soft error
            # lets QML retry — cheaper than a 2x resolve. (D1)
            return {"ok": False, "error": "still resolving"}
        hit = _resolve_cache_get(key)
        if hit is not None:
            return hit
        # Leader finished but cached nothing (failure / live / degraded / stale key). Rare
        # single double-spawn on the non-cacheable path only — acknowledged, not a swarm leak.
        return _resolve_uncached(video_id)

    try:                                            # ---- LEADER ----
        payload = _resolve_uncached(video_id)
        if payload.get("ok"):
            info = payload.get("info") or {}
            key2 = _resolve_key(video_id)                       # D2: recompute AFTER the resolve
            usable_audio = bool(info.get("audio_urls") or info.get("audio_url")
                                or info.get("muxed_url"))        # D4: something the player can walk
            cacheable = (key2 == key                             # D2: world didn't move under us
                         and not info.get("is_live")             # D3: never cache live
                         and usable_audio)                       # D4: never cache an empty result
            if cacheable:
                gu = _good_until(info)
                if gu > time.time() + 5:                         # only store something worth serving
                    with _resolve_cache_lock:
                        _resolve_cache[key] = {"payload": payload,
                                               "good_until": gu, "ts": time.time()}
                        _evict_resolve_cache_locked()
        # Failure / live / degraded / stale-key: returned to the immediate caller, NOT cached
        # (a transient bot-wall or SABR-thin window must re-resolve fresh on the next tap).
        return payload
    finally:
        with _resolve_cache_lock:
            _resolve_inflight.pop(key, None)
        ev.set()


def prefetch_resolve(video_id, speculative=False):
    """PyOtherSide entry: kick a speculative resolve on a BACKGROUND thread, return instantly.
    Deduped (one per key), capped at 2 concurrent spawns. A key already fresh in cache, already
    in flight, or over the cap is a fast no-op. `speculative` is the (b)/(c) seam (D9)."""
    if not video_id:
        return {"ok": True, "queued": False}
    key = _resolve_key(video_id)

    if _resolve_cache_get(key) is not None:
        return {"ok": True, "queued": False, "cached": True}

    with _prefetch_lock:
        if key in _prefetch_pending:
            return {"ok": True, "queued": False, "inflight": True}
        _prefetch_pending.add(key)

    def _bg():
        # D7: a throwaway prefetch thread must NEVER be the one to START/restart the POT sidecar
        # — PR_SET_PDEATHSIG arms against THIS short-lived thread, so the kernel would SIGKILL the
        # sidecar the instant _bg returns, sabotaging the worker's token source. Defer to prewarm's
        # parked, correctly-armed thread and skip this speculative attempt (it warms on the next
        # prefetch or the real foreground tap).
        if _pot_active() and not _pot_ready_on_port():
            try:
                prewarm()
            except Exception:
                pass
            with _prefetch_lock:
                _prefetch_pending.discard(key)
            return
        # Non-blocking acquire = DROP at the 2-spawn ceiling (don't queue a swarm).
        if not _prefetch_sema.acquire(blocking=False):
            with _prefetch_lock:
                _prefetch_pending.discard(key)
            return
        try:
            _resolve_and_cache(video_id, key, speculative=speculative)
        except Exception:
            pass
        finally:
            _prefetch_sema.release()
            with _prefetch_lock:
                _prefetch_pending.discard(key)

    try:
        threading.Thread(target=_bg, daemon=True).start()
    except Exception:
        # D5: thread/FD exhaustion under a scroll burst — discard the key so a failed start can't
        # wedge this video as permanently "pending" (mirrors _bg's finally).
        with _prefetch_lock:
            _prefetch_pending.discard(key)
        return {"ok": False, "queued": False, "error": "spawn failed"}
    return {"ok": True, "queued": True}


# --------------------------------------------------------------------------- #
# yt-dlp wrappers
# --------------------------------------------------------------------------- #

def _managed_ytdlp():
    """The app-managed yt-dlp, living under our own data dir. This is the only spot that is
    both writable and reachable from inside the Sailjail sandbox — the user's ~/.local/bin
    and a trimmed PATH are masked from the jail — so it's checked first."""
    return os.path.join(_data_dir(), "bin", "yt-dlp")


def _system_binary(name):
    """A user/system copy of `name` to fall back on when the app has no managed copy of its own. A
    GUI-launched SFOS app runs with a TRIMMED PATH (no ~/.local/bin), so we consult PATH via `which`
    AND the standard user-local / system spots explicitly — the same approach as _DENO_CANDIDATES.
    Lets a user who keeps their own yt-dlp (e.g. in ~/.local/bin, shared with other apps) skip a
    second app-managed copy. A managed copy still WINS when present, so Install/Update always put the
    app back in control of exactly what it runs. Returns an executable path, or None."""
    found = shutil.which(name)
    if found and os.access(found, os.X_OK):
        return found
    for p in (os.path.expanduser("~/.local/bin/" + name),
              "/usr/local/bin/" + name, "/usr/bin/" + name):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _ytdlp_path():
    """yt-dlp for the app to run. Prefers the app-managed copy in our own bin/ (so Install/Update stay
    in control of what runs); then FinTube's app-managed copy (_CANDIDATE_PATHS — shared install,
    updated from FinTube); otherwise falls back to a user/system yt-dlp (PATH, ~/.local/bin,
    /usr/local/bin, /usr/bin — see _system_binary) so a user who keeps their own copy needn't have the
    app fetch a second one. Missing entirely → the UI prompts a download."""
    _ensure_deno_on_path()  # yt-dlp's bundled EJS challenge-solver needs Deno reachable on PATH
    managed = _managed_ytdlp()
    if os.path.isfile(managed) and os.access(managed, os.X_OK):
        return managed
    for p in _CANDIDATE_PATHS:                     # FinTube's managed copy (both apps installed)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return _system_binary("yt-dlp")


def ytdlp_version():
    """Installed yt-dlp version string, or '' if missing/broken."""
    path = _ytdlp_path()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def ytdlp_update():
    """Run yt-dlp's own self-updater and report the result, on the settings-chosen channel.

    This works for the standalone binary the user installed (it downloads the latest
    release from GitHub and replaces itself in place); a pip/package install refuses
    and says so, which we surface verbatim. Extraction is the part YouTube keeps
    breaking, so this is the app's main maintenance lever — no youfish rebuild needed.
    """
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found", "version": ""}
    channel = _ytdlp_channel()
    try:
        # --update-to <channel>@latest is unambiguous whichever channel the binary is on now;
        # it can pull ~30 MB over a phone link, so allow generous time.
        proc = subprocess.run([path, *_COMMON_ARGS, "--update-to", channel + "@latest"],
                              capture_output=True, text=True, timeout=300)
        out = (proc.stdout + proc.stderr).strip()
        return {"ok": proc.returncode == 0,
                "output": (out[-400:] if out else "yt-dlp reported nothing"),
                "version": ytdlp_version(), "channel": channel}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "update timed out", "version": ytdlp_version()}
    except Exception as ex:
        return {"ok": False, "error": str(ex), "version": ytdlp_version()}


# Standalone aarch64 build (self-contained — bundles its own Python, so it doesn't depend on
# the device's Python version). "latest" redirects to the current release asset; each release
# also publishes SHA2-256SUMS, which we verify the download against.
_YTDLP_ASSET = "yt-dlp_linux_aarch64"
# Release bases PER UPDATE CHANNEL. The binary hops channels via its own --update-to, but every
# direct download here (first binary install and — crucially — the importable ZIPAPP, which has
# no self-updater) must come from the repo matching the user's channel: a nightly binary next to
# a stable zipapp means the in-process fast path is missing the very breakage fix the user
# switched to nightly FOR — it fails (or goes SABR-thin) and every resolve silently pays a dead
# in-process attempt before the binary rescues it. Both repos publish the identical asset set
# (yt-dlp_linux_aarch64, the yt-dlp zipapp, SHA2-256SUMS); verified 2026-09-06.
_YTDLP_RELEASE_BASES = {
    "stable": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/",
    "nightly": "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/",
}


def _ytdlp_channel():
    """The user's yt-dlp update channel: "stable" unless explicitly "nightly"."""
    return "nightly" if (get_settings().get("ytdlp_channel") == "nightly") else "stable"


def _ytdlp_release_base():
    """GitHub release-asset base URL for the user's channel (binary, zipapp and sums alike)."""
    return _YTDLP_RELEASE_BASES[_ytdlp_channel()]


def _https_open(url, ctx, timeout=60):
    """Open a URL, refusing anything that isn't HTTPS end-to-end (initial URL and, after
    GitHub's redirect to its asset host, the final URL too). Cert verification is on via ctx."""
    if not url.lower().startswith("https://"):
        raise ValueError("refusing non-HTTPS URL: " + url)
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    if not resp.geturl().lower().startswith("https://"):
        resp.close()
        raise ValueError("download redirected to a non-HTTPS URL")
    return resp


def _expected_sha256(ctx, asset=_YTDLP_ASSET):
    """The published SHA-256 for `asset` (the aarch64 binary by default; the arch-independent
    zipapp for fast resolve), from the CHANNEL repo's SHA2-256SUMS file (or None)."""
    with _https_open(_ytdlp_release_base() + "SHA2-256SUMS", ctx, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
            return parts[0].strip().lower()
    return None


def install_ytdlp():
    """Download yt-dlp into our data dir (the one place that's writable AND visible inside the
    Sailjail sandbox). HTTPS-only, checksum-verified against the release's SHA2-256SUMS. Runs
    in the background; progress + result go to QML via pyotherside."""
    import pyotherside

    def run():
        tmp = None
        try:
            _force_ipv4()  # pin IPv4 — avoid a stalled connect on unroutable-IPv6 networks
            ctx = ssl.create_default_context()  # verifies the server certificate
            expected = _expected_sha256(ctx)    # None if the sums file can't be parsed
            dest_dir = os.path.join(_data_dir(), "bin")
            os.makedirs(dest_dir, exist_ok=True)
            dest = _managed_ytdlp()
            tmp = dest + ".part"
            h = hashlib.sha256()
            with _https_open(_ytdlp_release_base() + _YTDLP_ASSET, ctx) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = done * 100.0 / total
                            if int(pct) != last:
                                last = int(pct)
                                pyotherside.send("ytdlp_install_progress", pct)
            if expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("ytdlp_install_done", False,
                                 "Checksum mismatch — download discarded, nothing installed", "")
                return
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
            ver = ytdlp_version()  # exercises the binary — confirms it actually runs
            if ver:
                note = "Installed yt-dlp " + ver
                if not expected:
                    note += " (checksum unavailable, not verified)"
                pyotherside.send("ytdlp_install_done", True, note, ver)
            else:
                pyotherside.send("ytdlp_install_done", False,
                                 "Downloaded + checksum OK, but the binary won't run here — "
                                 "the sandbox is likely blocking exec from the data dir", "")
        except Exception as ex:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            pyotherside.send("ytdlp_install_done", False, str(ex), "")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Fast resolve: run yt-dlp IN-PROCESS instead of spawning the frozen
# binary. That binary is a PyInstaller onefile — it re-unpacks to TMPDIR and re-imports
# yt_dlp on EVERY call (~1.3s spawn tax on this CPU). Importing yt-dlp ONCE and keeping
# a warm YoutubeDL in the worker removes that tax and keeps the player-JS / n-sig caches
# warm across resolves — the in-process advantage that keeps NewPipe fast. The binary
# stays the resilient DEFAULT (self-updating, needs no device Python/deps) AND the
# fallback: ANY failure here (missing/broken/incompatible zip, import or extraction
# error) falls through to the binary, so opting in can never make a resolve fail that the
# binary would have served. Scope: only the TOKEN-FREE hot dump (tv_embedded, no PO
# token) runs in-process; every token / widen / gated dump keeps using the binary, so the
# bgutil PO-token PLUGIN machinery is untouched and never needed in-process.
# --------------------------------------------------------------------------- #

_YTDLP_ZIPAPP_ASSET = "yt-dlp"   # the arch-independent zipapp in the same (channel) release


def _ytdlp_zipapp_path():
    """OUR OWN importable yt-dlp zipapp (fast-resolve only, never exec'd). Both reading and
    installing go through _ytdlp_zipapp_read_path, which prefers a shared FinTube copy."""
    return os.path.join(_data_dir(), "bin", "yt-dlp.zip")


def _ytdlp_zipapp_read_path():
    """The zipapp to IMPORT: the one living next to the ACTIVE binary, so the fast-resolve copy
    stays in lockstep with whichever yt-dlp actually runs. FinTube's binary (shared install) →
    FinTube's zipapp (FinTube's Update refreshes both together); otherwise our own. If the
    sibling has no zipapp we still fall back to our own — fast resolve keeps working and the
    version-skew label in Providers surfaces any mismatch."""
    active = _ytdlp_path()
    if active and active.startswith(_FINTUBE_DATA_DIR + os.sep):
        p = os.path.join(_FINTUBE_DATA_DIR, "bin", "yt-dlp.zip")
        if os.path.isfile(p):
            return p
    return _ytdlp_zipapp_path()


def _zipapp_version(path):
    """Read yt_dlp/version.py's __version__ out of the zipapp WITHOUT importing it (importing
    would pin this whole process to one yt_dlp for its lifetime). "" if it can't be read."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            src = z.read("yt_dlp/version.py").decode("utf-8", "replace")
        m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", src)
        return m.group(1) if m else ""
    except Exception:
        return ""


def fast_resolve_status():
    """For the Providers UI: is the in-process fast-resolve copy set up — and can this
    device's python run it at all (python_ok drives the honest why-not text). NOT a
    setting: fast resolve engages automatically wherever python_ok holds and the copy
    exists; the binary is always the fallback."""
    path = _ytdlp_zipapp_read_path()
    present = os.path.isfile(path)
    return {"installed": present,
            "version": _zipapp_version(path) if present else "",
            "python_ok": _FAST_RESOLVE_PY_OK,
            "python_version": "%d.%d" % sys.version_info[:2]}


def install_ytdlp_zipapp():
    """Download the arch-independent yt-dlp ZIPAPP for fast resolve. HTTPS-only, checksum-verified
    against the release's SHA2-256SUMS, then structurally validated (must be a zip exposing the
    yt_dlp package). Writes to the path the importer READS (_ytdlp_zipapp_read_path) — normally
    our own bin/, but when FinTune runs on FinTube's shared install it refreshes FinTube's copy
    instead, so Update keeps the binary and its fast-resolve copy in lockstep for both apps.
    Background; progress + result go to QML via pyotherside."""
    import pyotherside
    import zipfile

    # Engine-side gate, mirroring the QML pythonOk checks: on a too-old OS python the zipapp
    # can never run, so refuse the download outright. Covers the race where an install rides
    # along before fast_resolve_status() has told the UI that python_ok is false.
    if not _FAST_RESOLVE_PY_OK:
        pyotherside.send("ytdlp_zipapp_done", False,
                         "In-process yt-dlp needs OS Python 3.10+ — the binary is used instead.", "")
        return {"ok": False}

    def run():
        tmp = None
        try:
            _force_ipv4()
            ctx = ssl.create_default_context()
            expected = _expected_sha256(ctx, _YTDLP_ZIPAPP_ASSET)   # None if the sums can't be parsed
            dest = _ytdlp_zipapp_read_path()
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".part"
            h = hashlib.sha256()
            with _https_open(_ytdlp_release_base() + _YTDLP_ZIPAPP_ASSET, ctx) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = done * 100.0 / total
                            if int(pct) != last:
                                last = int(pct)
                                pyotherside.send("ytdlp_zipapp_progress", pct)
            if expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("ytdlp_zipapp_done", False,
                                 "Checksum mismatch — download discarded", "")
                return
            # zipimport reads the central directory past the shebang prefix, so a plain
            # ZipFile check is enough to confirm the yt_dlp package is importable from it.
            try:
                with zipfile.ZipFile(tmp) as z:
                    ok_shape = "yt_dlp/__init__.py" in z.namelist()
            except Exception:
                ok_shape = False
            if not ok_shape:
                os.remove(tmp)
                pyotherside.send("ytdlp_zipapp_done", False,
                                 "Downloaded file is not a yt-dlp zipapp — discarded", "")
                return
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
            ver = _zipapp_version(dest)
            note = "Installed yt-dlp zipapp " + (ver or "(unknown version)")
            if not expected:
                note += " (checksum unavailable, not verified)"
            if _YT_DLP_IMPORT_DONE:   # a copy is already imported (one-shot per process) — tell
                note += " — takes effect next app launch"    # the user why nothing changes yet
            pyotherside.send("ytdlp_zipapp_done", True, note, ver)
        except Exception as ex:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            pyotherside.send("ytdlp_zipapp_done", False, str(ex), "")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


_ZIPAPP_AUTOFETCH_DONE = False


def _autofetch_zipapp():
    """Self-heal the fast-resolve copy at launch, once per process: a capable device that
    already has the yt-dlp binary (so the user consented to yt-dlp — the zipapp is the same
    software in importable form, from the same release) but no zipapp fetches one in the
    background. A shared FinTube copy counts as present (that's the read path). A failure
    stays silent and leaves the binary in charge; the next launch or an Update tap retries.
    This is what keeps "the copy always exists" true for installs that predate it — there
    is no settings row to re-enable."""
    global _ZIPAPP_AUTOFETCH_DONE
    if _ZIPAPP_AUTOFETCH_DONE or not _FAST_RESOLVE_PY_OK:
        return
    _ZIPAPP_AUTOFETCH_DONE = True
    try:
        if os.path.isfile(_ytdlp_zipapp_read_path()) or not _ytdlp_path():
            return
    except Exception:
        return
    install_ytdlp_zipapp()


# ---- in-process extraction (the warm path) --------------------------------- #

_YT_DLP_MOD = None
_YT_DLP_IMPORT_DONE = False
_yt_dlp_import_lock = threading.Lock()
# One warm YoutubeDL per THREAD: the long-lived worker thread keeps its player-JS / n-sig
# caches warm across resolves; throwaway prefetch threads each get their own — so no two
# threads ever share a YoutubeDL, which is what makes this lock-free and race-free.
_inproc_tls = threading.local()


class _InprocLogger:
    """Swallow yt-dlp's chatter; surface errors only, and only when debugging."""
    def debug(self, m):
        pass

    def info(self, m):
        pass

    def warning(self, m):
        pass

    def error(self, m):
        if _DEBUG:
            _tlog("inproc yt-dlp error: " + str(m)[:200])


def _import_yt_dlp():
    """Import the yt-dlp ZIPAPP once, module-wide, and cache it (None if unavailable). Inserting
    the zipapp at the FRONT of sys.path makes zipimport load OUR yt_dlp regardless of any system
    copy. One-time and irreversible for the process — an updated zip takes effect next launch."""
    global _YT_DLP_MOD, _YT_DLP_IMPORT_DONE
    if not _FAST_RESOLVE_PY_OK:
        return None      # the device python can't run yt-dlp at all — the single gate point
    if _YT_DLP_IMPORT_DONE:
        return _YT_DLP_MOD
    with _yt_dlp_import_lock:
        if _YT_DLP_IMPORT_DONE:
            return _YT_DLP_MOD
        import sys
        zp = _ytdlp_zipapp_read_path()
        if not os.path.isfile(zp):
            return None          # not installed yet — stay RETRYABLE (don't cache), so a resolve
                                 # during the enable-and-download flow picks it up once it lands
        mod = None
        try:
            if zp not in sys.path:
                sys.path.insert(0, zp)
            _ensure_deno_on_path()   # in-process n-sig also uses Deno when it's present
            import yt_dlp as _m
            mod = _m
        except Exception as ex:
            _plog("fast-resolve: yt-dlp import failed (%s) — using binary" % ex)
            mod = None
        _YT_DLP_MOD = mod
        _YT_DLP_IMPORT_DONE = True   # the zip existed: cache the outcome (success, or a hard import
                                     # failure we shouldn't retry every resolve)
        return _YT_DLP_MOD


def _fast_resolve_ready():
    """True when the importable yt-dlp actually loaded (the device-python gate lives at the
    top of _import_yt_dlp). No user setting — in-process is automatic, the binary is the
    fallback, not an option."""
    return _import_yt_dlp() is not None


def _parse_extractor_args(extra):
    """Turn a ["--extractor-args", "youtube:k=v;k2=v2,v3"] argv fragment into YoutubeDL's
    extractor_args dict {"youtube": {"k": ["v"], "k2": ["v2", "v3"]}}. [] -> {}."""
    out = {}
    i = 0
    while i < len(extra):
        if extra[i] == "--extractor-args" and i + 1 < len(extra):
            ie, _, kvs = extra[i + 1].partition(":")
            d = {}
            for kv in kvs.split(";"):
                if not kv:
                    continue
                k, _, v = kv.partition("=")
                d[k.strip()] = [x for x in v.split(",") if x != ""] if v else []
            if ie.strip():
                out[ie.strip().lower()] = d
            i += 2
            continue
        i += 1
    return out


def _inproc_apply_cookies(ydl, anon=False):
    """Sync the warm YoutubeDL's cookie jar with the imported YouTube login (from ytm), reloading
    only when the cookie text actually changed (sign in/out) — a no-op on the common path. anon=True
    forces an EMPTY jar (the token-free primary resolves cookie-free to dodge auth gating)."""
    text = ""
    if not anon:
        try:
            import ytm
            text = ytm.netscape_cookies() or ""
        except Exception:
            text = ""
    if getattr(_inproc_tls, "cookie_hash", None) == hash(text):
        return
    _inproc_tls.cookie_hash = hash(text)
    try:
        jar = ydl.cookiejar
        jar.clear()
        if text:
            fd, p = tempfile.mkstemp(prefix="ytdlp-ck-", suffix=".txt")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                jar.load(p)
            finally:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


def _inproc_ydl(mod):
    """The warm, per-thread YoutubeDL (built once per thread; see _inproc_tls)."""
    ydl = getattr(_inproc_tls, "ydl", None)
    if ydl is None:
        ydl = mod.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "no_color": True,
            "source_address": "0.0.0.0",   # == yt-dlp -4 (force IPv4), matching _COMMON_ARGS
            # Bound each in-process request. The BINARY path has a hard 90s aggregate cap
            # (subprocess timeout=90) that extract_info lacks; per-request bounding is the
            # in-process stand-in, so one wedged socket can't pin the worker indefinitely.
            "socket_timeout": 20,
            # No explicit cachedir: use yt-dlp's default (~/.cache/yt-dlp), the SAME the frozen
            # binary uses (app is unsandboxed → writable). So the in-process path shares the
            # binary's warm n-sig / player-JS disk cache — a first-of-session resolve is warmer.
            "logger": _InprocLogger(),
        })
        _inproc_tls.ydl = ydl
        _inproc_tls.cookie_hash = None
    return ydl


_orig_popen = None   # set once when the DEBUG Deno probe is installed


def _inproc_install_deno_probe():
    """DEBUG-only, idempotent: wrap subprocess.Popen so we can time how long each resolve spends in
    **Deno** (the n-signature solver) vs everything else (network + CPython extraction) — the one
    number that decides whether the JS runtime is worth touching. All subprocess spawns funnel
    through Popen (run/check_output call it), so this catches every Deno invocation. Transparent:
    it only instruments deno-named spawns from a thread that armed an accumulator (`_inproc_tls.deno`),
    counts each process once, and passes everything else straight through unchanged."""
    global _orig_popen
    if _orig_popen is not None:
        return
    _orig_popen = subprocess.Popen

    def _popen(cmd, *a, **k):
        p = _orig_popen(cmd, *a, **k)
        acc = getattr(_inproc_tls, "deno", None)
        if acc is None:
            return p
        try:
            a0 = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
            is_deno = "deno" in os.path.basename(str(a0)).lower()
        except Exception:
            is_deno = False
        if not is_deno:
            return p
        t = time.time()
        counted = [False]
        _oc, _ow = p.communicate, p.wait

        def _mark():
            if not counted[0]:
                counted[0] = True
                acc[0] += 1
                acc[1] += time.time() - t

        def communicate(*aa, **kk):
            try:
                return _oc(*aa, **kk)
            finally:
                _mark()

        def wait(*aa, **kk):
            try:
                return _ow(*aa, **kk)
            finally:
                _mark()

        p.communicate = communicate
        p.wait = wait
        return p

    subprocess.Popen = _popen


def _inproc_dump(url, extra, anon=False):
    """Extract `url` in-process with the warm YoutubeDL and return a dict shaped exactly like the
    binary's --dump-single-json (via sanitize_info). Raises on failure — the caller falls back.
    anon=True resolves cookie-free (the token-free primary; see _dump)."""
    mod = _import_yt_dlp()
    if mod is None:
        raise RuntimeError("yt-dlp zipapp not importable")
    ydl = _inproc_ydl(mod)
    ydl.params["extractor_args"] = _parse_extractor_args(extra)
    _inproc_apply_cookies(ydl, anon)
    if _DEBUG:                       # arm the Deno-share probe for THIS resolve (this thread)
        _inproc_install_deno_probe()
        _inproc_tls.deno = [0, 0.0]  # [n_spawns, total_seconds]
    info = ydl.extract_info(url, download=False)
    return ydl.sanitize_info(info)


# --------------------------------------------------------------------------- #
# PO-token provider (bgutil): an OPT-IN, user-installed sidecar.
#
# YouTube now binds a Proof-of-Origin token to each video id, so a token can't be
# pasted once and reused — it must be minted per video. The bgutil provider does this:
# a small Deno HTTP server keeps a BotGuard VM warm and mints a fresh token on demand,
# and a pure-Python yt-dlp plugin auto-calls it. We clone + set it up on request (like
# yt-dlp itself), never bundle it, and run the server under Deno's default-deny sandbox:
# network + env only, reads jailed to its own folder, and NO write / run / blanket-ffi —
# the capabilities an npm supply-chain worm would need. See install_pot_provider().
# --------------------------------------------------------------------------- #
_POT_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
_POT_TAG = "1.3.2"          # pinned KNOWN-GOOD release (matches the bundled yt-dlp bgutil plugin).
                            # The EFFECTIVE tag (see _pot_effective_tag) can be updated to the
                            # latest release from inside the app, with no rebuild.
_POT_PORT = 4416            # bgutil's default HTTP port; the plugin probes 127.0.0.1:4416

_DENO_CANDIDATES = (
    os.path.expanduser("~/.deno/bin/deno"),  # default deno install location
    os.path.expanduser("~/.local/bin/deno"),  # common user-local spot (a launcher's PATH omits it)
    "/usr/local/bin/deno",
    "/usr/bin/deno",
)

# Deno ships as a single self-contained binary (aarch64/glibc) from its GitHub releases, so — like
# yt-dlp — the app can fetch it into its own bin/ instead of needing a manual system install.
_DENO_ASSET = "deno-aarch64-unknown-linux-gnu.zip"
_DENO_DOWNLOAD_URL = "https://github.com/denoland/deno/releases/latest/download/" + _DENO_ASSET
_DENO_SUMS_URL = _DENO_DOWNLOAD_URL + ".sha256sum"


def _managed_deno():
    return os.path.join(_data_dir(), "bin", "deno")


_pot_proc = None
_pot_lock = threading.Lock()
_pot_last_error = ""     # human-readable reason the sidecar last failed to start/answer (diagnostics)
_pot_log_rotated = False  # server.log is rotated once per app launch (see _pot_rotate_log)


def _deno_path():
    """Deno binary, or None. Prefers the app-managed copy (install_deno) in our own bin/; then
    FinTube's managed copy (shared install — no second ~40 MB fetch); then a launcher's trimmed
    PATH; then ~/.deno/bin + ~/.local/bin."""
    managed = _managed_deno()
    if os.path.isfile(managed) and os.access(managed, os.X_OK):
        return managed
    sibling = os.path.join(_FINTUBE_DATA_DIR, "bin", "deno")
    if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
        return sibling
    found = shutil.which("deno")
    if found:
        return found
    for p in _DENO_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_deno_ejs_logged = False


def _ensure_deno_on_path():
    """Put the JS runtime on PATH for yt-dlp's child processes. yt-dlp solves YouTube's signature /
    `n` challenges by running its bundled yt-dlp-ejs scripts through a JS runtime (Deno); a launcher's
    trimmed PATH hides our managed/user Deno, so prepend its folder. Idempotent; a no-op when there's
    no Deno (yt-dlp still falls back to its built-in Python interpreter while that path survives)."""
    global _deno_ejs_logged
    deno = _deno_path()
    if not deno:
        return
    d = os.path.dirname(deno)
    if d and d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    if _DEBUG and not _deno_ejs_logged:   # confirm the EJS runtime wiring once (YOUFISH_DEBUG)
        _deno_ejs_logged = True
        print("[youfish] EJS: yt-dlp will use Deno at " + deno)


def _git_path():
    found = shutil.which("git")
    if found:
        return found
    for p in ("/usr/bin/git", "/usr/local/bin/git", os.path.expanduser("~/.local/bin/git")):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def deno_version():
    """Installed Deno version string, or '' if missing/broken."""
    path = _deno_path()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        first = (out.stdout or "").splitlines()[0] if out.stdout else ""
        m = re.search(r"deno (\S+)", first)
        return m.group(1) if m else (first[:40] if first else "")
    except Exception:
        return ""


def install_deno():
    """Download Deno (the PO-token provider's runtime) into our bin/ — a single self-contained
    binary, fetched + verified like yt-dlp, so the provider needs no manual runtime install.
    Background thread; progress + result via pyotherside (deno_install_progress / deno_install_done)."""
    import pyotherside
    import zipfile

    def run():
        tmp = None
        try:
            _force_ipv4()
            ctx = ssl.create_default_context()
            expected = None
            try:   # verify against the release's per-asset .sha256sum when present; else HTTPS-only
                with _https_open(_DENO_SUMS_URL, ctx, timeout=30) as resp:
                    parts = resp.read().decode("utf-8", "replace").split()
                    expected = parts[0].strip().lower() if parts else None
            except Exception:
                expected = None
            dest_dir = os.path.join(_data_dir(), "bin")
            os.makedirs(dest_dir, exist_ok=True)
            tmp = os.path.join(dest_dir, "deno-dl.zip.part")
            h = hashlib.sha256()
            with _https_open(_DENO_DOWNLOAD_URL, ctx) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = done * 100.0 / total
                            if int(pct) != last:
                                last = int(pct)
                                pyotherside.send("deno_install_progress", pct)
            if expected and h.hexdigest().lower() != expected:
                os.remove(tmp)
                pyotherside.send("deno_install_done", False,
                                 "Checksum mismatch — download discarded, nothing installed", "")
                return
            # The archive holds a single `deno` binary; extract just that (by basename) into bin/.
            dest = _managed_deno()
            got = False
            with zipfile.ZipFile(tmp) as zf:
                for name in zf.namelist():
                    if os.path.basename(name) == "deno" and not name.endswith("/"):
                        with zf.open(name) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                        os.chmod(dest, 0o755)
                        got = True
                        break
            os.remove(tmp)
            tmp = None
            if not got:
                pyotherside.send("deno_install_done", False,
                                 "Archive didn't contain a deno binary", "")
                return
            ver = deno_version()   # exercises the binary — confirms it actually runs
            if ver:
                note = "Installed Deno " + ver
                if not expected:
                    note += " (checksum unavailable, not verified)"
                pyotherside.send("deno_install_done", True, note, ver)
            else:
                pyotherside.send("deno_install_done", False,
                                 "Downloaded, but the binary won't run here", "")
        except Exception as ex:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            pyotherside.send("deno_install_done", False, str(ex), "")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def _pot_dir():
    return os.path.join(_data_dir(), "potprovider")


def _pot_repo_dir():
    return os.path.join(_pot_dir(), "bgutil-ytdlp-pot-provider")


def _pot_server_dir():
    return os.path.join(_pot_repo_dir(), "server")


def _pot_plugin_dir():
    # The directory handed to yt-dlp's --plugin-dirs. yt-dlp DISCOVERS plugins by globbing one subdir
    # level down (<dir>/*/yt_dlp_plugins — the same shape as its auto-scan of
    # ~/.config/yt-dlp/plugins/<name>/yt_dlp_plugins), NOT <dir>/yt_dlp_plugins directly. The bgutil
    # repo keeps the plugin at <repo>/plugin/yt_dlp_plugins, so we hand yt-dlp the REPO ROOT (it then
    # finds <repo>/plugin/yt_dlp_plugins). Pointing straight at plugin/ (whose yt_dlp_plugins is a
    # DIRECT child) matched the glob nothing → "Plugin directories: none", ZERO providers loaded, and
    # the app silently ran only on a user's stray ~/.config install if any (measured on-device
    # 2026-09-02 — our managed plugin had never loaded via --plugin-dirs).
    return _pot_repo_dir()


def _pot_marker():
    return os.path.join(_pot_dir(), ".installed")


def _pot_installed():
    return (os.path.isfile(_pot_marker())
            and os.path.isfile(os.path.join(_pot_server_dir(), "src", "main.ts")))


def _pot_active():
    """Installed AND enabled — the gate for both the sidecar and the yt-dlp plugin args."""
    return _pot_installed() and bool(get_settings().get("pot_provider", False))


def _canvas_node_path():
    """Absolute path to node-canvas's native addon, if it was ever built (only when the
    tight, no-native-code setup turned out to need it). Empty otherwise."""
    import glob
    hits = glob.glob(os.path.join(_pot_server_dir(), "node_modules", "**", "canvas.node"),
                     recursive=True)
    return hits[0] if hits else ""


def _pot_server_flags():
    """Deno argv for the token server — least privilege.

    Denied outright: write, run (subprocess), and blanket ffi — the powers a compromised
    npm dependency would need to steal files, plant a backdoor, or run native code. Reads
    are jailed to the server's own tree (jsdom loads a bundled stylesheet + resolves
    node_modules from there); network + env are all it legitimately needs. jsdom degrades
    gracefully without node-canvas, so no native addon is built or loaded. (If some future
    build genuinely needs canvas, ffi is granted to that ONE .node file — never wholesale.)
    """
    flags = [
        _deno_path(), "run",
        # --allow-net is UNRESTRICTED on purpose. We tried scoping it to the loopback listen + a
        # fixed list of Google/BotGuard hosts, but the token generator's network targets shift as
        # YouTube reworks BotGuard (new attestation hosts, redirects to the current challenge page,
        # plus Node-compat sockets that bind 0.0.0.0:0 locally before connecting out). Every miss
        # made Deno kill the server mid-request with NotCapable — the connection just closes with
        # no response — so NO PO token was ever produced and every video hit the "confirm you're
        # not a bot" wall. The list was unmaintainable against YouTube's changes. Exfiltration
        # defence now rests on the powers that actually matter and stay locked below: the server
        # still can't WRITE files, RUN processes, or load native code (FFI), and can only READ its
        # own tree — so a compromised npm dep can't steal files, persist, or execute anything.
        # Broad outbound network is the acceptable price of a token generator that keeps working.
        "--allow-net",
        "--allow-env",                        # server reads PORT / token-TTL (+ open-ended) from env
        "--allow-read=" + _pot_server_dir(),  # jsdom CSS + node_modules, confined to our dir
        "--deny-write",
        "--deny-run",
        "--v8-flags=--max-old-space-size=8192",  # BotGuard VM warmup peaks above the 2 GB default
    ]
    canvas = _canvas_node_path() if get_settings().get("pot_needs_ffi") else ""
    flags.append(("--allow-ffi=" + canvas) if canvas else "--deny-ffi")
    flags.append(os.path.join(_pot_server_dir(), "src", "main.ts"))
    return flags


def _pot_ytdlp_args():
    """yt-dlp args to load ONLY the app's own bundled bgutil plugin when the provider is active; else
    []. `--no-plugin-dirs` FIRST empties yt-dlp's plugin search list — otherwise yt-dlp ALSO scans the
    default ~/.config/yt-dlp/plugins and ~/.local/share dirs, and a user's stray manual bgutil install
    there SHADOWS our managed copy (namespace import is first-match-wins, no warning — measured: a
    stray 1.3.1 silently beat our 1.3.2, and 1.3.1 wouldn't mint the web_embedded token). Then
    `--plugin-dirs` adds only our repo. Order matters: --no-plugin-dirs MUST come first, or it also
    wipes our dir. Keeps yt-dlp untouched whenever the provider isn't set up/enabled."""
    return ["--no-plugin-dirs", "--plugin-dirs", _pot_plugin_dir()] if _pot_active() else []


def _pot_bind_localhost():
    """Patch the cloned server to bind 127.0.0.1 instead of all interfaces.

    Upstream main.ts hardcodes host "::" (fallback "0.0.0.0") with no env/flag — its own
    comment says a localhost default is planned 'in the next major version', so we make that
    change early. The SUCCESS LOG is patched too: upstream prints a HARDCODED "[::]:<port>"
    address string regardless of the actual bind, so an unpatched log reads as an
    all-interfaces bind even when the rebind worked — a recurring false alarm when reading
    device logs (verify for real with `ss -tlnp | grep 4416`). The "address " prefix keeps
    the error lines ("Could not listen on [::]…") untouched. Best-effort + idempotent: if
    the source shape ever changes, the replaces are no-ops and the server just keeps
    upstream behaviour (the low-severity status quo). Deno runs the .ts directly, so the
    rewrite takes effect on the next server start."""
    main_ts = os.path.join(_pot_server_dir(), "src", "main.ts")
    try:
        with open(main_ts) as f:
            src = f.read()
        patched = (src.replace('host: "::"', 'host: "127.0.0.1"')
                      .replace('host: "0.0.0.0"', 'host: "127.0.0.1"')
                      .replace('address [::]:', 'address 127.0.0.1:')
                      .replace('address 0.0.0.0:', 'address 127.0.0.1:'))
        if patched != src:
            with open(main_ts, "w") as f:
                f.write(patched)
    except Exception:
        pass


def _pot_disable_webgpu():
    """Neutralize Deno's WebGPU in the cloned server before BotGuard can touch it.

    Deno exposes navigator.gpu, but on the libhybris/Mali GL stack the native
    GPU.requestAdapter() SEGFAULTS the whole process (YouTube's newer webpage-challenge flow
    fingerprints the GPU; a headless x86 server just gets a null adapter and moves on). We prepend
    a one-liner to src/main.ts that makes requestAdapter() return null — the normal 'no WebGPU'
    result — so BotGuard falls back to the software fingerprint instead of crashing. Idempotent
    (marker-guarded) + best-effort: if the entry file ever moves, it's a no-op and the server runs
    as it does today. Uses defineProperty so it also wins if the method is non-writable."""
    main_ts = os.path.join(_pot_server_dir(), "src", "main.ts")
    marker = "/* youfish:no-webgpu */"
    shim = (marker + ' try{if(globalThis.GPU&&globalThis.GPU.prototype)'
            'Object.defineProperty(globalThis.GPU.prototype,"requestAdapter",'
            '{value:async()=>null,configurable:true});}catch(_e){}\n')
    try:
        with open(main_ts) as f:
            src = f.read()
        if marker in src:
            return
        with open(main_ts, "w") as f:
            f.write(shim + src)
    except Exception:
        pass


def _pot_ready_on_port(timeout=0.25):
    try:
        with socket.create_connection(("127.0.0.1", _POT_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _pot_http_ping(timeout=1.5):
    """Confirm the token server is actually ANSWERING HTTP, not just holding the port open — a
    wedged Deno process can keep the socket bound while replying to nothing, which a bare TCP
    connect (_pot_ready_on_port) can't tell apart from healthy. Hits the bgutil server's /ping
    route; any HTTP reply (even an error status) means it's alive and processing. Returns
    {ok, version} — version comes from /ping's JSON when present, else ''."""
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/ping" % _POT_PORT,
                                     headers={"User-Agent": "youfish"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(8192)
        try:
            ver = str(json.loads(body.decode("utf-8", "replace")).get("version") or "")
        except Exception:
            ver = ""
        return {"ok": True, "version": ver}
    except urllib.error.HTTPError:
        return {"ok": True, "version": ""}   # server answered with an HTTP error → it IS alive
    except Exception:
        return {"ok": False, "version": ""}


def _pot_of(u):
    """DEBUG: the streaming PO-token (`pot=`) state of a googlevideo URL, WITHOUT leaking the
    token — 'no-url' when the pick has no URL at all (a genuinely dead pick), 'no-pot' when the
    URL just carries no pot= param (NORMAL for token-free clients like the anonymous tv_embedded
    primary — only suspect when that same stream 403s at byte 0), else the token's length +
    8-char prefix. The old single 'MISSING' label conflated those two very different states."""
    if not u:
        return "no-url"
    try:
        p = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("pot", [""])[0]
        return ("len=%d pfx=%s" % (len(p), p[:8])) if p else "no-pot"
    except Exception:
        return "?"


def _pot_server_log_tail(n=30):
    """Last n non-empty lines of the provider server's log (potprovider/server.log), or '' if
    there's none. This is where a Deno crash / NotCapable / OOM prints its reason."""
    try:
        with open(os.path.join(_pot_dir(), "server.log"), "r", errors="replace") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        return "\n".join(lines[-max(1, int(n)):])
    except Exception:
        return ""


def _pot_plugin_probe(timeout=30):
    """Does the installed yt-dlp actually RESOLVE our bgutil plugin directory? The clone keeps
    server+plugin in lockstep, but the plugin must ALSO be loadable by whatever yt-dlp binary is
    installed — and when it isn't, everything else looks healthy (server up, /ping answering)
    while every gated video quietly runs token-less. One OFFLINE binary run answers it:
    `--simulate` on a dummy scheme fails before any network, and the verbose debug header is
    where yt-dlp reports plugin-dir resolution — the exact line that caught the 2026-09-02
    'Plugin directories: none' incident. (NB `--version` short-circuits BEFORE plugin loading
    and prints none of this.) Costs one spawn (~1.3s on-device); diagnostics-only.

    Returns {checked, loaded, detail, js_runtimes, bgutil_lines}:
      loaded  True  → a 'Plugin directories' line names our repo (the historical failure mode
                      is ruled out);
              False → the line exists WITHOUT our repo (e.g. 'none') — yt-dlp runs unplugged;
              None  → no such line (very old yt-dlp / probe inconclusive) — no false alarms.
      js_runtimes   yt-dlp's own '[debug] JS runtimes' view — names Deno when reachable for the
                    n-sig solver, 'none' when EJS would fall back to the slow built-in.
      bgutil_lines  any output mentioning bgutil — a plugin that RESOLVES but fails to import
                    surfaces its warning/traceback here, which dir resolution alone can't see."""
    path = _ytdlp_path()
    if not path or not _pot_installed():
        return {"checked": False, "loaded": None, "detail": "",
                "js_runtimes": "", "bgutil_lines": ""}
    try:
        proc = subprocess.run(
            [path, "--no-plugin-dirs", "--plugin-dirs", _pot_plugin_dir(),
             "-v", "--simulate", "--", "youfish-probe:"],
            capture_output=True, text=True, timeout=timeout)
        out = (proc.stderr or "") + "\n" + (proc.stdout or "")
    except Exception as ex:
        return {"checked": False, "loaded": None, "detail": "probe failed: %s" % ex,
                "js_runtimes": "", "bgutil_lines": ""}
    lines = [ln.strip() for ln in out.splitlines()]
    dir_line = next((ln for ln in lines if "Plugin directories" in ln), "")
    js_line = next((ln for ln in lines if "JS runtimes" in ln), "")
    # The repo PATH itself contains "bgutil", so exclude the lines that merely echo it (the
    # argv dump and the dir line) — what's left is genuine plugin chatter (warnings/tracebacks).
    bgutil = "\n".join(ln for ln in lines
                       if "bgutil" in ln.lower()
                       and ln != dir_line and "Command-line config" not in ln)
    return {"checked": True,
            "loaded": (_pot_repo_dir() in dir_line) if dir_line else None,
            "detail": dir_line,
            "js_runtimes": js_line.split(":", 1)[-1].strip() if js_line else "",
            "bgutil_lines": bgutil}


def _set_pdeathsig():
    """Ask the kernel to SIGKILL the child if the app dies, so a sidecar/download can never be
    left orphaned (Linux PR_SET_PDEATHSIG = 1). Best-effort; runs in the forked child."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)
    except Exception:
        pass


def _ensure_pot_server(wait=25.0):
    """Start the token server if the provider is active and it isn't already listening.
    Returns True once something is listening on the port. No-op (returns False) when the
    provider isn't installed/enabled, so normal calls are entirely unaffected. `wait`
    bounds the TOTAL time this caller may spend here (lock join + port wait): the resolve
    hot path passes a short grace so it never owns a slow boot — False there just means
    the dump runs token-free (its gate re-checks the port), while the in-flight boot keeps
    going on the owner thread and serves the next call."""
    if not _pot_active():
        return False
    if _pot_ready_on_port():
        return True
    global _pot_proc, _pot_last_error
    deadline = time.time() + wait
    # A boot may already be in flight under the lock (prewarm's call, usually). Join it only
    # within this caller's budget: the 2026-09-08 FinTube field log showed a resolve on a
    # CPU-starved device blocking ~11s on the lock, then burning its OWN full window on a server
    # that never came up — 40s spent to conclude "proceed token-free". Timing out here abandons
    # nothing: the holder's boot continues regardless.
    if not _pot_lock.acquire(timeout=max(0.1, deadline - time.time())):
        return _pot_ready_on_port()
    try:
        if _pot_ready_on_port():
            return True
        if not _deno_path():
            _pot_last_error = "Deno runtime not found — install it from Providers → Download Deno."
            return False
        if not (_pot_proc and _pot_proc.poll() is None):
            if _pot_proc is not None:   # a tracked child died — record its exit code so the log tail
                try:                    # (and diagnostics) show WHY, e.g. -11 SIGSEGV / -9 SIGKILL(OOM)
                    with open(os.path.join(_pot_dir(), "server.log"), "a") as _lf:
                        _lf.write("[youfish] previous provider server exited (code %s)\n"
                                  % _pot_proc.poll())
                except Exception:
                    pass
            _pot_bind_localhost()   # ensure a fresh spawn binds 127.0.0.1, not all interfaces
            _pot_disable_webgpu()   # stub WebGPU — its native requestAdapter segfaults on Mali/libhybris
            env = dict(os.environ)
            env["PORT"] = str(_POT_PORT)
            try:
                logf = open(os.path.join(_pot_dir(), "server.log"), "ab", buffering=0)
            except Exception:
                logf = subprocess.DEVNULL
            # Spawn on a DEDICATED long-lived daemon thread that then parks on the child for its
            # whole life. PR_SET_PDEATHSIG is armed against the THREAD that forks the child, not the
            # process — so if the sidecar were Popen'd on a short-lived caller (the install thread, a
            # download thread, or a reader-thread re-resolve) the kernel would SIGKILL it the instant
            # that caller returned: the "server dies just after Provider ready" bug. Parking here
            # keeps pdeathsig armed to fire only when the app itself exits, whoever asked to start it.
            spawned = threading.Event()
            def _own_pot_server():
                global _pot_proc, _pot_last_error
                try:
                    proc = subprocess.Popen(
                        _pot_server_flags(), cwd=_pot_server_dir(), env=env,
                        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                        preexec_fn=_set_pdeathsig)
                except Exception as ex:
                    _pot_last_error = "Couldn't launch the Deno server: " + str(ex)
                    _pot_proc = None
                    spawned.set()
                    return
                _pot_proc = proc
                atexit.register(stop_pot_server)
                spawned.set()
                try:
                    proc.wait()          # park for the child's whole life (pdeathsig stays armed here)
                except Exception:
                    pass
            threading.Thread(target=_own_pot_server, daemon=True,
                             name="pot-server-owner").start()
            spawned.wait(5)              # the Popen is near-instant; let it happen before we poll
            if _pot_proc is None:
                return False             # Popen failed — _pot_last_error already set by the owner
        # The server LISTENS quickly; the BotGuard VM warms on the first token request,
        # which the yt-dlp plugin waits out itself — so we only wait for the port to open.
        while time.time() < deadline:
            if _pot_ready_on_port():
                _pot_last_error = ""
                return True
            if _pot_proc is None or _pot_proc.poll() is not None:
                _pot_last_error = ("Provider server exited (code %s) just after starting — see the "
                                   "server log in the diagnostics below."
                                   % (_pot_proc.poll() if _pot_proc is not None else "?"))
                return False   # died during startup — see potprovider/server.log
            time.sleep(0.3)
        _pot_last_error = "Provider server didn't open port %d within %.0fs." % (_POT_PORT, wait)
        return _pot_ready_on_port()
    finally:
        _pot_lock.release()


def _pot_rotate_log():
    """Start each app launch with a fresh server.log so it doesn't accumulate stale 'Started POT
    server' lines across runs (the log is otherwise append-only and never trimmed). Keeps exactly
    ONE previous log as server.log.prev, so the last session is still inspectable. Idempotent per
    process (guarded) and best-effort. os.replace is atomic; any process still holding the old fd
    keeps writing to the renamed inode, so this is safe even if a server were mid-write."""
    global _pot_log_rotated
    if _pot_log_rotated:
        return
    _pot_log_rotated = True
    try:
        log = os.path.join(_pot_dir(), "server.log")
        if os.path.isfile(log):
            os.replace(log, log + ".prev")   # overwrites an older .prev
    except Exception:
        pass


def prewarm():
    """Start the PO-token server in the background at app launch, so the first resolve doesn't pay
    the ~2s Deno startup on its critical path. No-op unless the provider is installed + enabled.
    Runs on its OWN daemon thread so the PyOtherSide worker (and the UI behind it) never blocks on
    the port wait — fire-and-forget from QML at startup."""
    if _DEBUG:          # profiling: log the isolated yt-dlp spawn tax once per launch, off-thread
        threading.Thread(target=_spawn_tax_probe, daemon=True).start()
    # Fast resolve: import the yt-dlp zipapp NOW, on a throwaway thread, so the first resolve of the
    # session doesn't pay the ~1.5s `import yt_dlp` on its critical path. The import is module-global
    # (see _import_yt_dlp), so any thread warms it; a no-op on a too-old device python, before the
    # zipapp arrives, or once already imported. _autofetch_zipapp then backfills a missing copy in
    # the background (once per launch) so fast resolve just exists wherever it can run.
    threading.Thread(target=_import_yt_dlp, daemon=True, name="ytdlp-import-prewarm").start()
    _autofetch_zipapp()
    _pot_rotate_log()   # fresh server.log per launch (keeps the previous one as server.log.prev)
    if not _pot_active():
        return
    # _ensure_pot_server() now brings the sidecar up on its OWN dedicated owner thread that parks on
    # the child (PR_SET_PDEATHSIG is armed against the forking thread, so it must be a long-lived
    # one) — so prewarm just has to TRIGGER it off the UI path. A throwaway daemon thread is fine: it
    # returns as soon as the port is up (or the 25s start times out), and the owner thread it spun up
    # keeps the sidecar alive until app exit.
    threading.Thread(target=_ensure_pot_server, daemon=True, name="pot-prewarm").start()


def stop_pot_server():
    """Terminate the token sidecar (called on app exit + when the user disables it)."""
    global _pot_proc
    p, _pot_proc = _pot_proc, None
    if not p:
        return
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
    except Exception:
        pass


def pot_status():
    """Provider state for the Providers UI. `running` = the port is open; `responding` = the
    server actually answers HTTP (the real 'it's working' signal — a wedged process can hold the
    port without replying). `last_error` carries the reason it isn't working, when there is one."""
    deno = _deno_path()
    running = _pot_ready_on_port()
    ping = _pot_http_ping() if running else {"ok": False, "version": ""}
    return {
        "installed": _pot_installed(),
        "enabled": bool(get_settings().get("pot_provider", False)),
        "deno": bool(deno),
        "deno_path": deno or "",
        "running": running,
        "responding": bool(ping["ok"]),
        "server_version": ping["version"],
        "tag": _pot_effective_tag(),
        "default_tag": _POT_TAG,
        "updated": bool((get_settings().get("pot_tag") or "").strip()),
        "last_error": _pot_last_error,
        # True when the Deno in use is the APP-MANAGED copy — the one that has no other
        # updater, so the UI offers "Update Deno" for it (a system/user Deno is theirs).
        "deno_managed": bool(deno) and deno == _managed_deno(),
    }


def pot_diagnostics():
    """A copy-pasteable health report for the PO-token provider and everything it depends on —
    for the Providers 'Run diagnostics' action, so a stuck user (or someone helping them) can see
    at a glance which piece is missing. Reports the resolved binaries + versions, the provider's
    install/enable state, whether the Deno server is alive and answering, and the tail of its log.
    Returns {report: <multiline text>, ...structured flags}."""
    installed = _pot_installed()
    enabled = bool(get_settings().get("pot_provider", False))
    # Snapshot the tracked child BEFORE any restart below, so a prior crash's exit code isn't masked
    # by a fresh spawn. poll() is None while alive, an int once exited (0 clean; negative = signal).
    prev = _pot_proc
    prev_code = prev.poll() if prev is not None else None
    # Actively TRY to (re)start when enabled + installed but nothing is listening — so "Run diagnostics"
    # reflects a real start ATTEMPT (and populates _pot_last_error when the server can't come up at all),
    # instead of a passive snapshot that can't tell "won't start" from "not tried".
    restart_tried = False
    restart_ok = None
    if enabled and installed and not _pot_ready_on_port():
        restart_tried = True
        restart_ok = _ensure_pot_server()

    deno = _deno_path()
    git = _git_path()
    ytdlp = _ytdlp_path()
    running = _pot_ready_on_port()
    ping = _pot_http_ping() if running else {"ok": False, "version": ""}
    # The axis nothing else checks: does THIS yt-dlp binary actually load our plugin? A healthy
    # server + an unloaded plugin still means token-less gated videos. One offline spawn.
    probe = (_pot_plugin_probe() if (installed and ytdlp)
             else {"checked": False, "loaded": None, "detail": "",
                   "js_runtimes": "", "bgutil_lines": ""})

    L = []
    L.append("FinTune — PO-token provider diagnostics")
    L.append("app data dir: " + _data_dir())
    L.append("")
    L.append("Deno   : " + ((deno + "  (v" + (deno_version() or "?") + ")") if deno else "NOT FOUND"))
    L.append("git    : " + (git or "NOT FOUND"))
    L.append("yt-dlp : " + ((ytdlp + "  (" + (ytdlp_version() or "?") + ")") if ytdlp else "NOT FOUND"))
    L.append("")
    L.append("provider installed : " + (("yes (" + _pot_effective_tag() + ")") if installed else "no"))
    L.append("provider enabled   : " + ("yes" if enabled else "no"))
    # Distinguish a server that STARTED-THEN-DIED (with its exit code) from one never started this
    # session — the key clue, since the sidecar can open its port fine and only crash later on the
    # first token mint (BotGuard warmup), which leaves the status "on" but nothing listening.
    if prev is not None and prev_code is None:
        L.append("server process     : alive (started by this app)")
    elif prev is not None:
        L.append("server process     : STARTED, then EXITED (code %s) — it opened its port, then the "
                 "process ended (negative = fatal signal: -11 SIGSEGV, -9 SIGKILL/OOM); see the log "
                 "below" % prev_code)
    else:
        L.append("server process     : not started in this app session")
    if restart_tried:
        L.append("restart attempt    : " + ("server came up" if restart_ok
                                             else "FAILED — " + (_pot_last_error or "unknown reason")))
    L.append("port %d listening  : %s" % (_POT_PORT, "yes" if running else "no"))
    L.append("answering HTTP     : " + ("yes" + (" (server v" + ping["version"] + ")"
                                                 if ping["version"] else "")
                                        if ping["ok"] else "no"))
    if probe["checked"]:
        if probe["loaded"] is True:
            L.append("plugin in yt-dlp   : loads (" + probe["detail"] + ")")
        elif probe["loaded"] is False:
            L.append("plugin in yt-dlp   : NOT LOADED — " + (probe["detail"] or "not resolved")
                     + " — yt-dlp runs WITHOUT the token plugin; reinstall the provider or "
                       "update yt-dlp")
        else:
            L.append("plugin in yt-dlp   : undetermined ("
                     + (probe["detail"] or "no plugin report from this yt-dlp") + ")")
        L.append("yt-dlp JS runtime  : " + (probe["js_runtimes"] or "(not reported)"))
        if probe["bgutil_lines"]:
            L.append("bgutil mentions    : " + probe["bgutil_lines"][:300])
    # A confirmed-unloaded plugin overrides "working": the server answering is irrelevant if
    # yt-dlp never calls it.
    verdict = ("working" if (enabled and ping["ok"] and probe["loaded"] is not False)
               else "NOT working" if enabled else "installed but switched off" if installed
               else "not set up")
    L.append("verdict            : " + verdict)
    if _pot_last_error:
        L.append("last error         : " + _pot_last_error)
    L.append("")
    L.append("note: /ping only proves the HTTP server answers; the real proof is a token mint — look "
             "for 'Generating POT' / 'poToken:' in the log below, which means it's genuinely working.")
    tail = _pot_server_log_tail(30)
    if tail:
        L.append("")
        L.append("--- server.log (last lines) ---")
        L.append(tail)

    return {
        "report": "\n".join(L),
        "deno": bool(deno), "git": bool(git), "ytdlp": bool(ytdlp),
        "installed": installed, "enabled": enabled,
        "running": running, "responding": bool(ping["ok"]),
        "plugin_loaded": probe["loaded"], "js_runtimes": probe["js_runtimes"],
        "prev_exit": prev_code,
        "last_error": _pot_last_error,
    }


def set_pot_enabled(on):
    """Turn the provider on/off (keeps the install either way) and start/stop the sidecar."""
    set_setting("pot_provider", bool(on))
    if on:
        _ensure_pot_server()
    else:
        stop_pot_server()
    return pot_status()


def _pot_effective_tag():
    """The provider release to install: a stored override if the user updated it, else the pinned
    known-good default. This is what makes the version no longer a hardcoded dead-end."""
    t = (get_settings().get("pot_tag") or "").strip()
    return t or _POT_TAG


def _pot_latest_tag():
    """Latest provider release tag from GitHub ('' on any failure). Used only by the explicit
    update action — never auto-applied, so the Deno sidecar can't silently drift out of step with
    the installed yt-dlp bgutil plugin (the two speak a versioned protocol)."""
    try:
        _force_ipv4()
        ctx = ssl.create_default_context()
        url = ("https://api.github.com/repos/Brainicism/"
               "bgutil-ytdlp-pot-provider/releases/latest")
        req = urllib.request.Request(url, headers={
            "User-Agent": _BROWSER_UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return (json.loads(resp.read().decode()).get("tag_name") or "").strip()
    except Exception:
        return ""


def install_pot_provider(tag=None, persist_tag=False):
    """Clone + set up the bgutil PO-token provider (opt-in). Background thread; progress and
    the final result go to QML via pyotherside, mirroring install_ytdlp(). `persist_tag` records
    the tag as the chosen `pot_tag` ONLY once the install succeeds (set by update_pot_provider).

    Deps are installed WITHOUT --allow-scripts, so npm lifecycle scripts never run during
    setup (node-canvas's native binary is skipped — jsdom degrades gracefully without it,
    which is what lets the server run with ffi fully denied)."""
    import pyotherside

    def run():
        global _pot_last_error
        the_tag = tag or _pot_effective_tag()
        try:
            deno = _deno_path()
            if not deno:
                _pot_last_error = "Deno runtime not found — install it from Providers → Download Deno."
                pyotherside.send("pot_install_done", False,
                                 "Deno runtime not found. Tap Download Deno, then retry.")
                return
            git = _git_path()
            if not git:
                _pot_last_error = "git not found on device (needed to clone the provider)."
                pyotherside.send("pot_install_done", False, "git not found on device.")
                return
            os.makedirs(_pot_dir(), exist_ok=True)
            repo = _pot_repo_dir()
            # Build into a STAGING dir and swap it in only once everything succeeds, so a failure
            # partway through (network drop, bad tag, dep-install error) leaves any EXISTING working
            # install untouched instead of destroying it up-front. (M3)
            staging = repo + ".new"
            shutil.rmtree(staging, ignore_errors=True)    # clear a stale temp from a prior failed run
            pyotherside.send("pot_install_progress", "Cloning provider (" + the_tag + ")…")
            cp = subprocess.run(
                [git, "clone", "--depth", "1", "--branch", the_tag, "--single-branch",
                 _POT_REPO, staging],
                capture_output=True, text=True, timeout=240)
            if cp.returncode != 0:
                shutil.rmtree(staging, ignore_errors=True)
                _pot_last_error = "Clone failed: " + (cp.stderr.strip()[-200:] or "git error")
                pyotherside.send("pot_install_done", False,
                                 "Clone failed: " + (cp.stderr.strip()[-200:] or "git error"))
                return
            pyotherside.send("pot_install_progress", "Installing dependencies (Deno)…")
            server = os.path.join(staging, "server")
            lock = os.path.join(server, "deno.lock")
            base = [deno, "install"]
            if get_settings().get("pot_needs_ffi"):
                base.append("--allow-scripts")   # only if node-canvas's native build is needed
            cmd = base + (["--frozen"] if os.path.isfile(lock) else [])
            dp = subprocess.run(cmd, cwd=server, capture_output=True, text=True, timeout=900)
            if dp.returncode != 0 and "--frozen" in cmd:   # lock mismatch? retry unlocked
                dp = subprocess.run(base, cwd=server, capture_output=True, text=True, timeout=900)
            if dp.returncode != 0:
                shutil.rmtree(staging, ignore_errors=True)
                _pot_last_error = "Dependency install failed: " + (dp.stderr.strip()[-200:] or "deno error")
                pyotherside.send("pot_install_done", False,
                                 "Dependency install failed: " + (dp.stderr.strip()[-200:] or "deno error"))
                return
            if not os.path.isfile(os.path.join(server, "src", "main.ts")):
                shutil.rmtree(staging, ignore_errors=True)
                pyotherside.send("pot_install_done", False,
                                 "Setup finished but the server entry is missing.")
                return
            # Staging built cleanly. Stop the OLD sidecar FIRST — otherwise it keeps serving on the
            # port and _ensure_pot_server() below would see the port open and never restart, so an
            # UPDATE would silently keep running the old server/plugin version. (M3)
            stop_pot_server()
            # Swap the new tree in with two same-filesystem renames (sub-millisecond window; the
            # previous install stays recoverable under .old until the new one is promoted).
            old = repo + ".old"
            shutil.rmtree(old, ignore_errors=True)
            if os.path.isdir(repo):
                os.rename(repo, old)
            os.rename(staging, repo)
            shutil.rmtree(old, ignore_errors=True)
            with open(_pot_marker(), "w") as f:
                f.write(the_tag)
            if persist_tag:
                set_setting("pot_tag", the_tag)  # remember the updated tag ONLY after a clean install
            set_setting("pot_provider", True)    # installed → enabled
            _ensure_pot_server()                 # fresh start (old one stopped above) so the new
                                                 # server + plugin version actually takes effect
            pyotherside.send("pot_install_done", True,
                             "Provider ready (" + the_tag + "). Videos now fetch a per-video token.")
        except subprocess.TimeoutExpired:
            shutil.rmtree(_pot_repo_dir() + ".new", ignore_errors=True)
            pyotherside.send("pot_install_done", False, "Setup timed out.")
        except Exception as ex:
            shutil.rmtree(_pot_repo_dir() + ".new", ignore_errors=True)
            pyotherside.send("pot_install_done", False, str(ex))

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def update_pot_provider():
    """Resolve the latest provider release and (re)install it, remembering it as the chosen tag.
    User-initiated, like ytdlp_update() — the sidecar only moves on an explicit request, so it
    stays in step with the yt-dlp bgutil plugin. Reports via the same pot_install_* events."""
    latest = _pot_latest_tag()
    if not latest:
        import pyotherside
        pyotherside.send("pot_install_done", False,
                         "Couldn't reach GitHub to find the latest provider release.")
        return {"ok": False}
    # Persist the new tag only AFTER a successful install (inside install_pot_provider) — otherwise a
    # failed update would leave the setting claiming a version that was never actually installed. (M3)
    return install_pot_provider(latest, persist_tag=True)


def resolve(video_id):
    """Cache-first resolve. Returns a fresh cached result instantly; joins an in-flight prefetch
    for the SAME key instead of spawning a second yt-dlp; else resolves and caches. Same
    {ok, info|error} shape — every caller (Backend.resolve) is unchanged."""
    return _resolve_and_cache(video_id)


def _resolve_uncached(video_id):
    """Resolve a track to playable AUDIO stream URLs.

    The player gets `audio_urls` — the full fallback ladder, best first (see
    _audio_candidates) — plus `muxed_url` (single combined stream) as the rung of last
    resort. One yt-dlp pass on the common path: the primary dump nearly always carries the
    whole audio ladder, so the client net is only widened when it returned literally nothing
    an audio player can use (FinTube's HD-pair hunt would roughly double resolve time here
    and buys music nothing).
    """
    path = _ytdlp_path()
    if not path:
        return {"ok": False, "error": "yt-dlp not found"}
    url = video_id
    if "://" not in url:
        url = "https://www.youtube.com/watch?v=" + video_id
    _t0 = time.time()
    # Bring the PO-token sidecar up (no-op unless installed+enabled) — with a short grace, never
    # owning the boot: the primary tv_embedded path is token-free, so when the server isn't up
    # in time the dump proceeds without it and the still-booting server serves the next resolve.
    _ensure_pot_server(wait=4)
    _tlog("pot_ensure %.2fs" % (time.time() - _t0))
    def _dump(extra, anon=False):
        """Run yt-dlp --dump-single-json with extra args; return (data, error).

        Fast resolve (opt-in): the TOKEN-FREE hot dump runs IN-PROCESS via the warm YoutubeDL,
        skipping the ~1.3s frozen-binary spawn tax; ANY failure falls through to the binary below.
        The token path (fetch_pot=always baked into `extra`) always takes the binary — that's
        where the bgutil PO-token plugin lives — so the in-process path never needs it.
        (Provider INACTIVE → a widen retry carries no fetch_pot marker and may run in-process
        too; equivalent by construction, since without the provider the binary loads no plugin.)

        anon=True resolves WITHOUT the login cookies. YouTube gates token-free clients (tv_embedded)
        HARD for AUTHENTICATED requests but not anonymous ones — confirmed on-device 2026-09-05: the
        same videos that 403'd (→ ~10s mweb+token fallback) signed-in resolved token-free in ~1.3s
        signed OUT. So the primary dump goes anonymous (public videos skip the gate entirely) and only
        the fallback re-runs WITH cookies, for genuinely restricted content (age-gated/members/private)."""
        _td = time.time()
        if _DEBUG and _pot_active():   # was the token server actually ANSWERING when we extracted?
            _tlog("dump gate: port=%s http=%r" % (_pot_ready_on_port(), _pot_http_ping(0.5)["ok"]))
        if "fetch_pot=always" not in " ".join(extra) and _fast_resolve_ready():
            try:
                data = _inproc_dump(url, extra, anon)
                if _DEBUG:   # break the wall time into Deno (n-sig) vs the rest (network + CPU)
                    _dn = getattr(_inproc_tls, "deno", None) or [0, 0.0]
                    _tot = time.time() - _td
                    _tlog("dump(inproc) %.2fs [deno %dx %.2fs | rest %.2fs]"
                          % (_tot, _dn[0], _dn[1], max(0.0, _tot - _dn[1])))
                else:
                    _tlog("dump(inproc) %.2fs" % (time.time() - _td))
                return data, ""
            except Exception as ex:
                _tlog("dump(inproc) failed %.2fs → binary: %s"
                      % (time.time() - _td, str(ex)[:120]))
        with (contextlib.nullcontext([]) if anon else _cookies_args()) as cargs:
            proc = subprocess.run(
                [path, *_COMMON_ARGS, *cargs, *_pot_ytdlp_args(), *extra,
                 "--dump-single-json", "--", url],
                capture_output=True, text=True, timeout=90,
                preexec_fn=_set_pdeathsig)   # D6: SIGKILL an orphaned prefetch child with the app
        _tlog("dump %.2fs rc=%d%s" % (time.time() - _td, proc.returncode, " anon" if anon else ""))
        if proc.returncode != 0:
            return None, (proc.stderr.strip()[:300] or "resolve failed")
        try:
            return json.loads(proc.stdout), ""
        except Exception as ex:
            return None, str(ex)

    def _audio_playable(d):
        fs = d.get("formats", [])
        return bool(_pick_audio(fs) or _pick(fs, _MUXED_ITAGS))

    try:
        _client_used = _default_client() or "auto"   # which client actually produced the URLs (debug)
        # Primary = token-free AND cookie-free. Authenticated token-free requests get gated by YouTube;
        # anonymous ones don't. Public videos resolve here fast + un-gated; a restricted video fails
        # this and drops to the cookie'd (+token) fallback below. (on-device confirmed 2026-09-05)
        data, err = _dump(_yt_extractor_args(), anon=True)
        # A hard failure (data is None) is usually YouTube's "confirm you're not a bot" check
        # tripping this client — retry once with the wider set. tv/android_vr use different
        # attestation and often pass where web/web_embedded get bot-checked.
        if data is None:
            data2, err2 = _dump(_yt_extractor_args(client_override=_RETRY_CLIENTS, want_pot=True))
            if data2 is not None:
                data = data2; _client_used = _RETRY_CLIENTS + "(widen)"
            else:
                err = err or err2
        # Music path: the primary (tv_embedded) almost always carries the full audio ladder
        # (+ muxed), so make do with ONE pass. Only widen the client net when it gave us
        # literally nothing an audio player can use.
        elif not _audio_playable(data):
            data2, _ = _dump(_yt_extractor_args(client_override=_RETRY_CLIENTS, want_pot=True))
            if data2 is not None and _audio_playable(data2):
                data = data2; _client_used = _RETRY_CLIENTS + "(widen)"
        if data is None:
            return {"ok": False, "error": err}
        formats = data.get("formats", [])
        # Token-free fast-path probe: tv_embedded is the fast default and needs NO token, but token-free
        # clients are the ones YouTube gates unpredictably — when gated the stream 403s the instant
        # playback fetches it. Probe one chosen URL; on a real 403 re-extract with the reliable TOKEN
        # path (mweb + a minted PO token) — done HERE, before playback, so the itags stay consistent
        # (a mid-stream client switch can't: clients emit different itag shapes). Only when tv_embedded
        # is OUR default choice (provider set up, no user-set player_client, no widen fired) — a user who
        # explicitly picks a client keeps it, and a widen result already left _client_used != it.
        _manual_client = (get_settings().get("player_client") or "").strip().lower() not in ("", "auto")
        if _pot_active() and not _manual_client and _client_used == "tv_embedded":
            _pt = (_pick_audio(formats) or _pick(formats, _MUXED_ITAGS) or {})
            if _pt.get("url") and "m3u8" not in (_pt.get("protocol") or ""):
                _tp = time.time()
                _ok = _probe_url_ok(_pt["url"], (_pt.get("http_headers") or {}).get("User-Agent", ""))
                if _DEBUG:
                    _tlog("probe %.2fs ok=%s%s" % (time.time() - _tp, _ok,
                                                   "" if _ok else " → mweb fallback"))
                if not _ok:
                    data2, _ = _dump(_yt_extractor_args(client_override="mweb", want_pot=True))
                    if data2 is not None and _audio_playable(data2):
                        data = data2
                        formats = data.get("formats", [])
                        _client_used = "mweb(gated-fallback)"
        audio = _pick_audio(formats)
        muxed = _pick(formats, _MUXED_ITAGS)
        if not muxed and not audio:
            return {"ok": False,
                    "error": "No playable format — try a different Player client in "
                             "Settings (some tracks need a PO token)."}
        # The proxy must fetch googlevideo with the SAME User-Agent yt-dlp used for these
        # formats — android-client URLs 403 under a mismatched UA. All picked formats come
        # from one client, so a single UA covers them.
        http_ua = ((audio or muxed or {}).get("http_headers") or {}).get(
            "User-Agent", "") or _BROWSER_UA
        if _DEBUG:   # instant-403 probe: which client, and does the COLD dump carry a valid pot= token?
            _tlog("resolve picks [client=%s]: a=%s %s | m=%s %s"
                  % (_client_used,
                     (audio or {}).get("format_id"), _pot_of((audio or {}).get("url", "")),
                     (muxed or {}).get("format_id"), _pot_of((muxed or {}).get("url", ""))))
            if _pot_active():   # a first-mint crash/OOM shows here as an exit-code line between dumps
                _tlog("pot log: " + ((_pot_server_log_tail(6) or "(none)").replace("\n", " | ")))
        # HLS (m3u8) plays fine directly, and proxying the manifest breaks segment
        # resolution; only progressive URLs (itag 18) need the UA-injecting proxy.
        muxed_url = ""
        if muxed:
            if "m3u8" in muxed.get("protocol", ""):
                muxed_url = muxed["url"]
            else:
                muxed_url = _proxied(muxed["url"], video_id, muxed.get("format_id"), http_ua)
        # Audio fallback ladder (best first) — the heart of the music resolve. YouTube
        # SABR-gates codecs per-video and unpredictably (one track 403s m4a but serves opus,
        # another the reverse), so hand the player EVERY available audio URL to try in turn
        # before it drops to muxed. Dedup by codec+bitrate so a rung's per-language variants
        # collapse to one; _audio_candidates already ordered original-language first, highest
        # bitrate, opus preferred. Route each through the proxy like the main track (DASH
        # audio 403s GStreamer's libsoup stack — souphttpsrc fetches localhost, urllib does
        # the real request).
        audio_url = _proxied(audio["url"], video_id, audio.get("format_id"), http_ua) if audio else ""
        audio_urls = []
        seen_tiers = set()
        for af in _audio_candidates(formats):
            tier = (_audio_family(af.get("acodec")), round(af.get("abr") or af.get("tbr") or 0))
            if tier in seen_tiers:
                continue
            seen_tiers.add(tier)
            audio_urls.append(_proxied(af["url"], video_id, af.get("format_id"), http_ua))
        _tlog("resolve TOTAL %.2fs" % (time.time() - _t0))
        return {"ok": True, "info": {
            "title": data.get("title", ""),
            "is_live": bool(data.get("is_live")) or (data.get("live_status") == "is_live"),
            "uploader": data.get("uploader") or data.get("channel") or "",
            "channel_id": data.get("channel_id") or data.get("uploader_id") or "",
            "channel_url": data.get("channel_url") or data.get("uploader_url") or "",
            "duration": data.get("duration") or 0,
            "muxed_url": muxed_url,
            # The ladder the player walks (audio_urls[0] == audio_url); audio_url stays for
            # any caller that only wants the single best track.
            "audio_url": audio_url,
            "audio_urls": audio_urls,
            "http_ua": http_ua,
            "muxed_itag": muxed.get("format_id", "") if muxed else "",
            "muxed_proto": muxed.get("protocol", "") if muxed else "",
            "audio_itag": audio.get("format_id", "") if audio else "",
        }}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def _pick(formats, itags):
    """First format (in itag-preference order) that exists and has a direct URL."""
    by_itag = {f.get("format_id"): f for f in formats}
    for itag in itags:
        f = by_itag.get(itag)
        if f and f.get("url"):
            return f
    return None


def _pick_audio(formats):
    """Best audio track with a URL — the top of the property-based audio ladder (see
    _audio_candidates: original/default language first, then highest bitrate, opus preferred
    at a tie). Music has no dub picker, so no language preference here."""
    cands = _audio_candidates(formats)
    return cands[0] if cands else None


# --------------------------------------------------------------------------- #
# On-disk state (JSON files the app owns).
# --------------------------------------------------------------------------- #

_dir_ready = False


def _atomic_write_json(path, obj):
    """Write obj as JSON to `path` atomically: serialise to a private (0600) temp file in the same
    directory, then os.replace() it over the target — atomic on POSIX, so a crash / battery-pull /
    ENOSPC mid-write can never truncate the live store (a truncated store loads as {} and the next
    save would then persist the wipe). Mirrors ytm.py's _save_cookies. Raises on failure, leaving
    the existing file untouched, so callers' current try/except still reports it."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def _data_dir():
    """FinTune's own data dir (separate from FinTube's — no shared settings/tokens). We still
    *reuse* FinTube's downloaded binaries read-only where they exist (see _CANDIDATE_PATHS /
    _ytdlp_zipapp_read_path) so a FinTube user needn't refetch them."""
    global _dir_ready
    d = os.path.expanduser("~/.local/share/harbour-fintune")
    if _dir_ready:
        return d
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    _dir_ready = True
    return d


# --------------------------------------------------------------------------- #
# Settings (a small JSON file the app owns).
# --------------------------------------------------------------------------- #
_SETTINGS_DEFAULTS = {"player_client": "",
                      # yt-dlp update channel: "stable" (default) or "nightly" (YouTube fixes
                      # land days sooner, less tested). Drives ytdlp_update()'s --update-to target.
                      "ytdlp_channel": "stable",
                      # home_backdrop: blurred now-playing art behind the home carousels (UI taste
                      # setting; on by default, toggled from Settings → Appearance).
                      "home_backdrop": True,
                      # 10-band equalizer: off by default, flat. eq_bands = per-band gain in dB
                      # (-24..+12), applied by the C++ player's equalizer-10bands.
                      "eq_enabled": False,
                      "eq_bands": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                      # Volume boost (linear gain, 1.0 = none) above system max; a soft limiter in
                      # the player keeps the extra gain from hard-clipping. For quiet BT output.
                      "boost_gain": 1.0,
                      # autoplay: when the queue ends, keep playing related songs (radio).
                      # skip_disliked: auto-skip songs you've disliked during autoplay.
                      "autoplay": True,
                      "skip_disliked": False,
                      # download_dir: where downloaded tracks are written. "" = the app's own
                      # downloads folder (default); a picked folder (e.g. ~/Music, an SD card)
                      # overrides it, validated writable before use (see _downloads_dir).
                      "download_dir": "",
                      # PO-token provider (bgutil): opt-in, user-installed. pot_needs_ffi
                      # stays False unless a build genuinely needs node-canvas's native addon
                      # (jsdom degrades gracefully without it).
                      "pot_provider": False, "pot_needs_ffi": False}
# Fast resolve (in-process yt-dlp) deliberately has NO settings key: it is not a preference
# but plumbing. It engages automatically wherever the device python can run it and the
# importable copy exists (installed with the binary, refreshed by Update, backfilled at
# launch by _autofetch_zipapp); the binary stays the fallback for every failure. A stale
# "fast_resolve" key from older builds may linger in settings.json — it is simply ignored.

# Widened client net, tried in ONE extra yt-dlp pass when the primary (tv_embedded, or
# yt-dlp's auto pick when no provider is set up) hard-fails on a bot-wall or returned
# nothing audio-playable. yt-dlp queries them all and merges formats; the url-presence
# filter in _pick keeps only the ones a SABR client can't serve. Unknown names are
# skipped with a warning, never a hard error, so a broad net here is safe.
_RETRY_CLIENTS = "tv,mweb,android,android_vr"


def _settings_path():
    return os.path.join(_data_dir(), "settings.json")


def _load_settings():
    try:
        with open(_settings_path()) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def get_settings():
    """Current settings merged over defaults — for the QML settings UI."""
    s = dict(_SETTINGS_DEFAULTS)
    s.update(_load_settings())
    return s


def set_setting(key, value):
    s = _load_settings()
    s[key] = value
    try:
        path = _settings_path()
        _atomic_write_json(path, s)
        os.chmod(path, 0o600)     # owner-only (privacy)
    except Exception:
        pass
    if key in _RESOLVE_OUTPUT_KEYS:      # D10: an output-affecting change drops cached resolves
        invalidate_resolve_cache()
    return get_settings()


def _default_client():
    """Which YouTube client resolve() uses by default.

    A user-set player_client always wins. Otherwise, when the PO-token provider is set up we default
    to `tv_embedded` — a TOKEN-FREE client: it returns the full range-fetchable HD ladder with the
    ORIGINAL audio (no DRC variants) and, crucially, needs NO Proof-of-Origin token, so resolve skips
    the ~4-5s on-device BotGuard mint entirely (measured 2026-09-05: tv_embedded ≈1.5s incl. spawn +
    HTTP 206 fetchable, vs web_embedded's ~5.5s dump — this is how NewPipe stays fast). Token-free
    clients are the ones YouTube gates unpredictably, so resolve PROBES tv_embedded's URL once and,
    only on a real 403 (gated), falls back to the reliable token path (`mweb` + a minted token). The
    provider is thus a SAFETY NET for the rare gated video, not a per-resolve tax. (History: default was
    web_embedded, then mweb-as-blanket-default on 2026-09-03 which put the mint on EVERY resolve — the
    "slow as of late" reports; tv_embedded-first removes it from the common path.) With no provider set
    up we leave yt-dlp on its own auto pick. resolve() also widens to _RETRY_CLIENTS if SABR-thin."""
    c = (get_settings().get("player_client") or "").strip()
    if c and c.lower() != "auto":
        return c
    return "tv_embedded" if _pot_active() else ""


def _yt_extractor_args(client_override=None, want_pot=False):
    """`--extractor-args` for yt-dlp built from settings (or []).

    player_client picks a YouTube client. client_override lets resolve() widen the client set
    on a retry without touching the saved preference. want_pot forces a PO-token mint (see below).
    """
    parts = []
    client = client_override if client_override is not None else _default_client()
    if client and client.lower() != "auto":
        parts.append("player_client=" + client)
    # fetch_pot=always forces yt-dlp to actually mint a PO token even for clients it marks GVS-token
    # OPTIONAL. Our default (mweb) marks it required=True and would fetch anyway, but this is kept as a
    # belt-and-braces safety net: a client with NO GVS-token policy (e.g. web_embedded — the former
    # default) defaults to required=False, and under fetch_pot=auto yt-dlp EARLY-RETURNS without ever
    # contacting the provider → no token → the URLs 403 under YouTube's "bind GVS PO Token to video id"
    # experiment (yt-dlp PR #14471). fetch_pot=always defeats that gate for any such client (incl. the
    # _RETRY_CLIENTS widen). Only on the paths that build/stream formats (resolve, re-resolve, download)
    # and only when the provider is active — NOT the --flat-playlist metadata passes, where a mint is
    # pure wasted BotGuard latency.
    if want_pot and _pot_active():
        parts.append("fetch_pot=always")
    return ["--extractor-args", "youtube:" + ";".join(parts)] if parts else []


# --------------------------------------------------------------------------- #
# Downloads: audio only (itag 140 → a single .m4a file — no ffmpeg, no merging; this is
# why FinTune manages no ffmpeg at all). yt-dlp runs in a background thread; progress +
# completion go to QML via pyotherside.send events. Metadata is tracked in downloads.json,
# including the track's artist/cover (`meta`) so the Downloads list and offline playback
# keep them.
# --------------------------------------------------------------------------- #
def _downloads_dir():
    """Where completed downloads are written. Defaults to a 'downloads' folder in the app's data
    dir; a user-set download_dir (Settings) overrides it when that folder exists and is writable —
    so media can land in ~/Videos, ~/Music, an SD card, etc. Falls back to the default if the chosen
    folder can't be created/written (e.g. an unmounted card), so a download never goes nowhere."""
    custom = (get_settings().get("download_dir") or "").strip()
    if custom:
        p = os.path.expanduser(custom)
        try:
            os.makedirs(p, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
        except Exception:
            pass
    d = os.path.join(_data_dir(), "downloads")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def download_location():
    """Where downloads go, for the Settings UI: the configured value ('' = app default), the
    effective absolute dir actually in use, and whether a custom dir is set."""
    configured = (get_settings().get("download_dir") or "").strip()
    return {"configured": configured, "effective": _downloads_dir(),
            "custom": bool(configured)}


def set_download_dir(path):
    """Choose the download folder (Settings → folder picker). '' resets to the app's own folder.
    Validates that the folder can be created + written and REFUSES (keeps the previous value) if
    not, so a bad pick can't silently send downloads nowhere. Accepts a plain path or a file:// URL.
    Returns download_location() plus {ok, error?}."""
    p = (path or "").strip()
    if p.startswith("file://"):
        p = p[len("file://"):]
    p = os.path.expanduser(p)
    if not p:
        set_setting("download_dir", "")
        return dict(download_location(), ok=True)
    try:
        os.makedirs(p, exist_ok=True)
        if not os.access(p, os.W_OK):
            return dict(download_location(), ok=False, error="That folder isn't writable.")
    except Exception as ex:
        return dict(download_location(), ok=False, error=str(ex))
    set_setting("download_dir", p)
    return dict(download_location(), ok=True)


def _downloads_path():
    return os.path.join(_data_dir(), "downloads.json")


def _safe_name(s):
    s = re.sub(r"[^\w\-. ]+", "_", s or "")[:80].strip()
    return s or "track"


def list_downloads():
    """Completed downloads: [{id, title, kind, path, subtitle?, thumb?, artistId?}, ...].
    Drops entries whose file is gone."""
    try:
        with open(_downloads_path()) as f:
            lst = json.load(f)
        if not isinstance(lst, list):
            return []
    except Exception:
        return []
    live = [d for d in lst if d.get("path") and os.path.exists(d["path"])]
    if len(live) != len(lst):
        _save_downloads(live)
    return live


def _save_downloads(lst):
    try:
        _atomic_write_json(_downloads_path(), lst)
    except Exception:
        pass


def download(video_id, title, kind="audio", meta=None):
    """Kick off a background audio download (itag 140 → a single .m4a, no ffmpeg). `kind` is
    kept for the QML call/entry shape (delete_download matches on it) but is always "audio" —
    the music app downloads nothing else.

    `meta` (optional dict) is stored alongside the entry so a downloaded track keeps its
    artist (`subtitle`), cover (`thumb`) and artist channel (`artistId`) for the Downloads
    list and offline playback."""
    import pyotherside
    kind = "audio"
    fmt, ext = "140", "m4a"
    binp = _ytdlp_path()
    if not binp:
        pyotherside.send("download_done", video_id, kind, False, "yt-dlp not found")
        return {"ok": False}
    # Sanitise the id before it reaches the -o output template and the URL: strip anything
    # outside [\w-] so a crafted id can't traverse out of downloads/ (../) or inject a yt-dlp
    # output-template field (%(...)s). Real YouTube ids are 11 chars of [\w-], so this is a
    # no-op for them. The stored entry + progress events still use the original id for UI matching.
    vid = re.sub(r"[^\w-]", "", video_id)[:64]
    url = "https://www.youtube.com/watch?v=" + vid
    base = os.path.join(_downloads_dir(), "%s [%s] %s" % (_safe_name(title), vid, kind))

    def run():
        ck = _write_cookies_temp()   # authenticated download (age-gated / members); rm in finally
        try:
            _ensure_pot_server()  # a download is just as PO-gated as playback
            proc = subprocess.Popen(
                [binp, *_COMMON_ARGS, *(["--cookies", ck] if ck else []),
                 *_yt_extractor_args(want_pot=True), *_pot_ytdlp_args(),
                 "--no-playlist", "-f", fmt, "--no-part", "--newline",
                 "-o", base + ".%(ext)s", "--", url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                # Die with the app: the result can only be REGISTERED (downloads.json + the
                # download_done event) while the app lives, so an app-exit orphan would keep
                # burning network/CPU writing a file the app can never list. Same doctrine
                # as every other child in this module (D6).
                preexec_fn=_set_pdeathsig)
            last = -1
            tail = []                              # keep the last lines to explain a failure
            for line in proc.stdout:
                s = line.rstrip()
                if s:
                    tail.append(s)
                    if len(tail) > 15:
                        tail.pop(0)
                m = re.search(r"\[download\]\s+([\d.]+)%", line)
                if m:
                    pct = float(m.group(1))
                    if int(pct) != last:
                        last = int(pct)
                        pyotherside.send("download_progress", video_id, kind, pct)
            proc.wait()
            fpath = base + "." + ext
            if proc.returncode == 0 and not os.path.exists(fpath):
                import glob
                cand = glob.glob(base + ".*")
                fpath = cand[0] if cand else fpath
            if proc.returncode == 0 and os.path.exists(fpath):
                lst = [d for d in list_downloads()
                       if not (d.get("id") == video_id and d.get("kind") == kind)]
                entry = {"id": video_id, "title": title or video_id,
                         "kind": kind, "path": fpath}
                if isinstance(meta, dict):        # artist/cover for the Downloads list + player
                    for k in ("subtitle", "thumb", "artistId"):
                        if meta.get(k):
                            entry[k] = meta[k]
                lst.insert(0, entry)
                _save_downloads(lst)
                pyotherside.send("download_done", video_id, kind, True, "")
            else:
                # Surface yt-dlp's own tail output so a failure is diagnosable, not a shrug.
                msg = ("\n".join(tail))[-400:] or "download failed"
                pyotherside.send("download_done", video_id, kind, False, msg)
        except Exception as ex:
            pyotherside.send("download_done", video_id, kind, False, str(ex))
        finally:
            if ck:
                try:
                    os.remove(ck)
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def delete_download(video_id, kind):
    keep = []
    for d in list_downloads():
        if d.get("id") == video_id and d.get("kind") == kind:
            try:
                if d.get("path") and os.path.exists(d["path"]):
                    os.remove(d["path"])
            except Exception:
                pass
        else:
            keep.append(d)
    _save_downloads(keep)
    return {"ok": True, "downloads": keep}


