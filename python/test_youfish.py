#!/usr/bin/env python3
"""Offline regression tests for FinTune's audio engine (youfish.py).

Runs with NO device, network, yt-dlp, or PO-token server — resolve()'s externals are all
mocked, so this exercises the pure format-selection + resolve wiring in isolation.
Run:  python3 test_youfish.py

The engine is FinTube's, cut down to audio: the music resolve contract (audio ladder +
muxed fallback), the FinTube binary/zipapp reuse layers, audio-only downloads with track
metadata, and the shared provider machinery (fast resolve, direct-fetch proxy, PO tokens)
are pinned here. youfish.py imports `pyotherside` only inside functions, so
`import youfish` is safe here.
"""

import base64
import json
import re
import os
import shutil
import threading
import time
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import youfish  # noqa: E402
import innertube  # noqa: E402

# The InnerTube fast path would hit the network, so the suite runs with it OFF by default (resolve
# then takes the yt-dlp fallback, which the fixtures mock). The Innertube* tests below switch it
# back on against canned responses.
_REAL_IT = youfish._it
youfish._it = lambda: None


def vf(fid, height, vcodec, fps=30, url="v", note=None):
    """A video-only format."""
    f = {"format_id": fid, "height": height, "vcodec": vcodec, "acodec": "none",
         "fps": fps, "url": url, "http_headers": {"User-Agent": "UA"}}
    if note is not None:
        f["format_note"] = note
    return f


def af(fid, abr, acodec, url="a", note=None, lang_pref=None, lang=None):
    """An audio-only format."""
    f = {"format_id": fid, "abr": abr, "acodec": acodec, "vcodec": "none", "url": url,
         "http_headers": {"User-Agent": "UA"}}
    if note is not None:
        f["format_note"] = note
    if lang_pref is not None:
        f["language_preference"] = lang_pref
    if lang is not None:
        f["language"] = lang
    return f


def muxed(fid="18", url="m"):
    return {"format_id": fid, "height": 360, "vcodec": "avc1", "acodec": "mp4a.40.2",
            "url": url, "protocol": "https", "http_headers": {"User-Agent": "UA"}}


# --- pure helpers ----------------------------------------------------------------------------- #

class AudioOrigPref(unittest.TestCase):
    def test_language_preference_field_wins(self):
        self.assertEqual(youfish._audio_orig_pref({"language_preference": 10}), 10)
        self.assertEqual(youfish._audio_orig_pref({"language_preference": -1}), -1)

    def test_note_fallback(self):
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English original (default)"}), 10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English descriptive"}), -10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "Portuguese"}), 0)
        self.assertEqual(youfish._audio_orig_pref({}), 0)


class AudioCandidates(unittest.TestCase):
    def test_ladder_prefers_opus_then_bitrate(self):
        # The ranking is opus-family-first (best on-device playback), each family then ordered by
        # bitrate desc — NOT a single global bitrate sort. So every opus rung (251,250,249,600)
        # precedes every AAC rung (140,139,599), each group high-bitrate first.
        fmts = [af("250", 70, "opus"), af("140", 128, "mp4a.40.2"), af("251", 160, "opus"),
                af("599", 31, "mp4a"), af("249", 50, "opus"), af("139", 48, "mp4a.40.5"),
                af("600", 35, "opus")]
        got = [f["format_id"] for f in youfish._audio_candidates(fmts)]
        self.assertEqual(got, ["251", "250", "249", "600", "140", "139", "599"])

    def test_source_beats_same_bitrate_dub(self):
        fmts = [af("251-3", 160, "opus", lang_pref=-1),    # dub
                af("251-0", 160, "opus", lang_pref=10)]    # source
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-0")

    def test_source_beats_higher_bitrate_dub(self):
        fmts = [af("251-3", 160, "opus", lang_pref=-1),        # dub, higher bitrate
                af("140-0", 128, "mp4a.40.2", lang_pref=10)]   # source, lower bitrate
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "140-0")

    def test_original_beats_same_codec_drc(self):
        # web_embedded exposes a DRC ("stable volume") variant beside the original; same language +
        # codec + bitrate, so only the DRC tie-break separates them — the ORIGINAL must win.
        fmts = [af("251-drc", 120, "opus", lang_pref=10),   # loudness-normalized
                af("251-7", 120, "opus", lang_pref=10)]     # original dynamics
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-7")

    def test_drc_used_when_only_option(self):
        # If DRC is all that's offered, it's still picked (no regression to "no audio").
        fmts = [af("251-drc", 120, "opus", lang_pref=10)]
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-drc")

    def test_opus_drc_kept_over_nondrc_aac(self):
        # The DRC penalty sits AFTER codec: we never trade the opus push-seek win for a non-DRC AAC.
        fmts = [af("251-drc", 120, "opus", lang_pref=10),        # opus, DRC
                af("140-7", 128, "mp4a.40.2", lang_pref=10)]     # aac, original
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-drc")

    def test_excludes_video_only_and_empty(self):
        self.assertEqual(youfish._audio_candidates([vf("137", 1080, "avc1")]), [])
        self.assertIsNone(youfish._pick_audio([vf("137", 1080, "avc1")]))

    def test_prefers_direct_over_manifest(self):
        # Same language/codec/bitrate → the manifest (m3u8) audio must lose to the direct URL.
        m = af("233", 160, "opus", lang_pref=10, url="https://manifest.googlevideo.com/a")
        m["protocol"] = "m3u8_native"
        d = af("251", 160, "opus", lang_pref=10, url="https://rr1---sn.googlevideo.com/a")
        self.assertEqual(youfish._audio_candidates([m, d])[0]["format_id"], "251")


# --- resolve() smoke tests (externals mocked) ------------------------------------------------- #

class ResolveSmoke(unittest.TestCase):
    """Mocked resolve() wiring for the MUSIC contract: audio ladder + muxed fallback, no video
    keys, the audio-playable widen, and error surfacing."""
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args",
                     "_yt_extractor_args", "_proxied", "get_settings", "_import_yt_dlp"):
            self._saved[name] = getattr(youfish, name)
        self._saved["run"] = youfish.subprocess.run

        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {}
        youfish._import_yt_dlp = lambda: None   # pin the BINARY path (no zipapp), whatever's on disk
        youfish.invalidate_resolve_cache()   # resolve() is cache-first (keyed by video id); every
                                             # test reuses id "vid", so isolate each test's fixture

    def tearDown(self):
        for name, fn in self._saved.items():
            if name == "run":
                youfish.subprocess.run = fn
            else:
                setattr(youfish, name, fn)

    def _mock_ytdlp(self, formats, title="T"):
        data = {"title": title, "formats": formats, "duration": 100}
        def fake_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def test_resolve_returns_audio_ladder(self):
        # Language-split itags (the shape that once crashed by_itag): the ladder dedups the
        # same-tier dub away, and playback starts on the SOURCE track.
        self._mock_ytdlp([af("251-0", 160, "opus", lang_pref=10),
                          af("251-3", 160, "opus", lang_pref=-1),    # dub, same tier
                          af("140-0", 128, "mp4a.40.2", lang_pref=10),
                          muxed()])
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        info = res["info"]
        self.assertTrue(info["audio_url"])                    # single best pick present
        self.assertEqual(len(info["audio_urls"]), 2)          # opus + aac tiers; dub collapsed
        self.assertEqual(info["audio_itag"], "251-0")         # source, not the dub
        self.assertEqual(info["muxed_itag"], "18")            # last-resort rung present

    def test_video_formats_ignored(self):
        # A dump full of video tracks must produce a pure-audio result — no video keys at all.
        self._mock_ytdlp([vf("137", 1080, "avc1"), vf("136", 720, "avc1"),
                          af("251", 160, "opus", lang_pref=10), muxed()])
        info = youfish.resolve("vid")["info"]
        self.assertEqual(info["audio_itag"], "251")
        for gone in ("video_itag", "video_url", "qualities", "audio_tracks"):
            self.assertNotIn(gone, info)

    def test_muxed_only_still_playable(self):
        self._mock_ytdlp([muxed()])
        info = youfish.resolve("vid")["info"]
        self.assertEqual(info["audio_url"], "")
        self.assertEqual(info["audio_urls"], [])
        self.assertEqual(info["muxed_url"], "m")              # the player's last rung still works

    def test_nothing_playable_is_an_error(self):
        self._mock_ytdlp([vf("137", 1080, "avc1")])           # video-only, no audio, no muxed
        res = youfish.resolve("vid")
        self.assertFalse(res.get("ok"))
        self.assertIn("No playable format", res.get("error", ""))

    def test_widen_fires_only_when_nothing_audio_playable(self):
        # First dump: video-only (unusable for music) -> ONE widen retry that has audio.
        thin = {"title": "T", "formats": [vf("137", 1080, "avc1")]}
        rich = {"title": "T", "formats": [af("251", 160, "opus", lang_pref=10)]}
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            data = thin if len(calls) == 1 else rich
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(calls), 2)                       # exactly one widen pass
        self.assertEqual(res["info"]["audio_itag"], "251")

    def test_no_widen_when_audio_already_playable(self):
        calls = []
        data = {"title": "T", "formats": [af("251", 160, "opus", lang_pref=10), muxed()]}
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run
        self.assertTrue(youfish.resolve("vid").get("ok"))
        self.assertEqual(len(calls), 1)                       # music path: ONE yt-dlp pass

    def test_resolve_error_surfaces(self):
        def fail_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm")
        youfish.subprocess.run = fail_run
        res = youfish.resolve("vid")
        self.assertFalse(res.get("ok"))
        self.assertIn("Sign in", res.get("error", ""))

    def test_final_error_skips_the_widen_retry(self):
        # A removed video is YouTube's final word: one extraction, not the widen re-run too.
        calls = []
        def fail_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(
                returncode=1, stdout="",
                stderr="WARNING: [youtube] vid: some warning\n"
                       "ERROR: [youtube] vid: Video unavailable. This video has been removed "
                       "by the uploader")
        youfish.subprocess.run = fail_run
        res = youfish.resolve("vid")
        self.assertFalse(res.get("ok"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["error"], "Video unavailable. This video has been removed by the uploader")

    def test_unclear_error_still_widens(self):
        calls = []
        def fail_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="ERROR: [youtube] vid: Sign in to confirm you're not a bot")
        youfish.subprocess.run = fail_run
        youfish.resolve("vid")
        self.assertEqual(len(calls), 2)

    def test_ytdlp_err_keeps_the_reason_only(self):
        e = youfish._ytdlp_err
        self.assertEqual(e("ERROR: [youtube] Tq92D6wQ1mg: Sign in to confirm your age. Use "
                           "--cookies-from-browser or --cookies for the authentication. See  "
                           "https://github.com/yt-dlp/yt-dlp/wiki/FAQ for how", "x"),
                         "Sign in to confirm your age.")
        self.assertEqual(e("ERROR: Unable to download webpage: HTTP Error 429", "x"),
                         "Unable to download webpage: HTTP Error 429")
        self.assertEqual(e("", "resolve failed"), "resolve failed")
        self.assertFalse(youfish._final_error("Video unavailable. This content isn't available, "
                                              "try again later."))


class ResolveCache(ResolveSmoke):
    """resolve() is cache-first: a repeat resolve is served from cache (no second yt-dlp spawn);
    invalidate_resolve_cache() forces a refetch."""

    def _counting_ytdlp(self, formats):
        self.calls = 0
        data = {"title": "T", "formats": formats, "duration": 100}
        def fake_run(cmd, **kwargs):
            self.calls += 1
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def test_second_resolve_hits_cache(self):
        self._counting_ytdlp([af("251", 160, "opus", lang_pref=10,
                                 url="https://r/videoplayback?expire=1990000000"), muxed()])
        self.assertTrue(youfish.resolve("vid").get("ok"))
        youfish.resolve("vid")
        self.assertEqual(self.calls, 1)          # second call served from cache — no new spawn

    def test_invalidate_forces_refetch(self):
        self._counting_ytdlp([af("251", 160, "opus", lang_pref=10,
                                 url="https://r/videoplayback?expire=1990000000"), muxed()])
        youfish.resolve("vid")
        youfish.invalidate_resolve_cache()
        youfish.resolve("vid")
        self.assertEqual(self.calls, 2)


class BinaryResolution(unittest.TestCase):
    """yt-dlp resolution order: FinTune's own managed copy WINS; then FinTube's app-managed copy
    (the shared install); then a user/system yt-dlp (PATH / ~/.local/bin / … via _system_binary).
    Each layer is pinned so none regresses."""
    def setUp(self):
        self._mo, self._which = youfish._managed_ytdlp, youfish.shutil.which
        self._tmp = tempfile.mkdtemp(prefix="binres-")
        p = os.path.join(self._tmp, "yt-dlp")           # a discoverable user/system copy
        with open(p, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
        self._sys = {"yt-dlp": p}
        self.which_calls = []

        def _spy(name):
            self.which_calls.append(name)
            return self._sys.get(name)                  # only yt-dlp resolves; deno → None

        youfish.shutil.which = _spy
        youfish._managed_ytdlp = lambda: os.path.join(self._tmp, "absent", "yt-dlp")
        # Point the FinTube-reuse layer somewhere controllable (absent by default).
        self._cand = youfish._CANDIDATE_PATHS
        self._fdd = youfish._FINTUBE_DATA_DIR
        youfish._FINTUBE_DATA_DIR = os.path.join(self._tmp, "fintube")
        youfish._CANDIDATE_PATHS = (os.path.join(self._tmp, "fintube", "bin", "yt-dlp"),)

    def tearDown(self):
        youfish._managed_ytdlp, youfish.shutil.which = self._mo, self._which
        youfish._CANDIDATE_PATHS = self._cand
        youfish._FINTUBE_DATA_DIR = self._fdd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_fintube_copy(self):
        d = os.path.join(self._tmp, "fintube", "bin")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "yt-dlp")
        with open(p, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
        return p

    def test_ytdlp_falls_back_to_system(self):
        # No managed copy anywhere, but a user/system copy is discoverable → resolved (NOT
        # None), and the system-fallback path was actually taken (which consulted for yt-dlp).
        self.assertEqual(youfish._ytdlp_path(), self._sys["yt-dlp"])
        self.assertIn("yt-dlp", self.which_calls)

    def test_fintube_copy_beats_system(self):
        ftp = self._make_fintube_copy()
        self.assertEqual(youfish._ytdlp_path(), ftp)
        self.assertNotIn("yt-dlp", self.which_calls)    # sibling copy short-circuits `which`

    def test_managed_wins_over_fintube_and_system(self):
        self._make_fintube_copy()
        managed = os.path.join(self._tmp, "managed-yt-dlp")
        with open(managed, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(managed, 0o755)
        youfish._managed_ytdlp = lambda: managed
        self.assertEqual(youfish._ytdlp_path(), managed)
        self.assertNotIn("yt-dlp", self.which_calls)    # managed short-circuits the fallback

    def test_zipapp_read_path_follows_active_binary(self):
        # FinTube's binary is the active one AND its zipapp exists → import THAT zipapp
        # (lockstep with the binary that runs); FinTube zipapp absent → our own path.
        ftp = self._make_fintube_copy()
        own = youfish._ytdlp_zipapp_path()
        self.assertEqual(youfish._ytdlp_zipapp_read_path(), own)     # sibling has no zipapp yet
        ftz = os.path.join(os.path.dirname(ftp), "yt-dlp.zip")
        with open(ftz, "w") as f:
            f.write("zip")
        self.assertEqual(youfish._ytdlp_zipapp_read_path(), ftz)     # now lockstep with FinTube
        # Our own managed binary appears → active binary is ours → back to our own zipapp.
        managed = os.path.join(self._tmp, "managed-yt-dlp")
        with open(managed, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(managed, 0o755)
        youfish._managed_ytdlp = lambda: managed
        self.assertEqual(youfish._ytdlp_zipapp_read_path(), own)


class PotTag(unittest.TestCase):
    """The PO-token provider version is no longer a hardcoded dead-end: a stored override wins,
    else the pinned known-good default. (The GitHub 'latest' lookup needs the network, so it
    isn't exercised offline here.)"""
    def setUp(self):
        self._gs = youfish.get_settings

    def tearDown(self):
        youfish.get_settings = self._gs

    def test_defaults_to_pinned_when_no_override(self):
        youfish.get_settings = lambda: {}
        self.assertEqual(youfish._pot_effective_tag(), youfish._POT_TAG)

    def test_override_wins(self):
        youfish.get_settings = lambda: {"pot_tag": "1.4.0"}
        self.assertEqual(youfish._pot_effective_tag(), "1.4.0")

    def test_blank_override_ignored(self):
        youfish.get_settings = lambda: {"pot_tag": "   "}
        self.assertEqual(youfish._pot_effective_tag(), youfish._POT_TAG)


# --- YouTube login (cookies) + subscription import ------------------------------------------- #

def _fake_ytm(text):
    """Install a fake `ytm` module exposing netscape_cookies() → text, for the engine's lazy
    `import ytm`. Returns the previous sys.modules entry (or None) so the caller can restore it."""
    prev = sys.modules.get("ytm")
    m = types.ModuleType("ytm")
    m.netscape_cookies = lambda: text
    sys.modules["ytm"] = m
    return prev


class CookiesArgs(unittest.TestCase):
    def setUp(self):
        self._prev = sys.modules.get("ytm")

    def tearDown(self):
        if self._prev is not None:
            sys.modules["ytm"] = self._prev
        else:
            sys.modules.pop("ytm", None)

    def test_signed_out_yields_empty(self):
        _fake_ytm("")                               # netscape_cookies() == "" → no --cookies
        with youfish._cookies_args() as cargs:
            self.assertEqual(cargs, [])

    def test_signed_in_yields_ephemeral_file_removed_after(self):
        _fake_ytm("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tv\n")
        with youfish._cookies_args() as cargs:
            self.assertEqual(cargs[0], "--cookies")
            path = cargs[1]
            self.assertTrue(os.path.exists(path))   # exists during the with
            with open(path) as f:
                self.assertIn("SAPISID", f.read())
        self.assertFalse(os.path.exists(path))      # and is removed after


# --- fast resolve (in-process yt-dlp) --------------------------------------------------------- #

class ExtractorArgParse(unittest.TestCase):
    """_parse_extractor_args turns the yt-dlp argv fragment into YoutubeDL's extractor_args dict."""
    def test_empty(self):
        self.assertEqual(youfish._parse_extractor_args([]), {})

    def test_single_client(self):
        self.assertEqual(
            youfish._parse_extractor_args(["--extractor-args", "youtube:player_client=tv_embedded"]),
            {"youtube": {"player_client": ["tv_embedded"]}})

    def test_multi_client_and_fetch_pot(self):
        self.assertEqual(
            youfish._parse_extractor_args(
                ["--extractor-args", "youtube:player_client=tv,mweb;fetch_pot=always"]),
            {"youtube": {"player_client": ["tv", "mweb"], "fetch_pot": ["always"]}})

    def test_bare_key_and_missing_value_dont_crash(self):
        self.assertEqual(youfish._parse_extractor_args(["--extractor-args", "youtube:flag"]),
                         {"youtube": {"flag": []}})
        self.assertEqual(youfish._parse_extractor_args(["--extractor-args"]), {})   # value absent


class ZipappVersion(unittest.TestCase):
    """_zipapp_version reads yt_dlp/version.py's __version__ WITHOUT importing the zipapp (importing
    would pin the whole process to one yt_dlp version)."""
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zipapp-")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_reads_version_from_zip(self):
        import zipfile
        p = os.path.join(self._tmp, "yt-dlp.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("yt_dlp/__init__.py", "")
            z.writestr("yt_dlp/version.py", "__version__ = '2026.08.19'\n")
        self.assertEqual(youfish._zipapp_version(p), "2026.08.19")

    def test_non_zip_returns_blank(self):
        p = os.path.join(self._tmp, "junk")
        with open(p, "wb") as f:
            f.write(b"not a zip")
        self.assertEqual(youfish._zipapp_version(p), "")


class FastResolveStatusAndGate(unittest.TestCase):
    def setUp(self):
        self._imp, self._pyok = youfish._import_yt_dlp, youfish._FAST_RESOLVE_PY_OK
        self._zrp = youfish._ytdlp_zipapp_read_path
        self._tmp = tempfile.mkdtemp(prefix="frs-")

    def tearDown(self):
        youfish._import_yt_dlp, youfish._FAST_RESOLVE_PY_OK = self._imp, self._pyok
        youfish._ytdlp_zipapp_read_path = self._zrp
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_status_not_installed(self):
        youfish._ytdlp_zipapp_read_path = lambda: os.path.join(self._tmp, "absent.zip")
        st = youfish.fast_resolve_status()
        self.assertEqual((st["installed"], st["version"]), (False, ""))

    def test_status_installed_reports_version(self):
        import zipfile
        p = os.path.join(self._tmp, "yt-dlp.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("yt_dlp/__init__.py", "")
            z.writestr("yt_dlp/version.py", "__version__ = '2026.08.19'\n")
        youfish._ytdlp_zipapp_read_path = lambda: p
        st = youfish.fast_resolve_status()
        self.assertEqual((st["installed"], st["version"]), (True, "2026.08.19"))

    def test_ready_false_when_python_too_old_never_touches_zip(self):
        # The REAL _import_yt_dlp with the gate forced off: returns None before ever looking
        # at the zipapp (path mock would explode if consulted), and burns no import cache.
        youfish._FAST_RESOLVE_PY_OK = False
        youfish._ytdlp_zipapp_read_path = lambda: (_ for _ in ()).throw(AssertionError("touched zip"))
        self.assertIsNone(youfish._import_yt_dlp())
        self.assertFalse(youfish._fast_resolve_ready())

    def test_ready_false_when_import_unavailable(self):
        youfish._import_yt_dlp = lambda: None
        self.assertFalse(youfish._fast_resolve_ready())

    def test_ready_true_when_importable(self):
        youfish._import_yt_dlp = lambda: object()
        self.assertTrue(youfish._fast_resolve_ready())

    def test_status_reports_device_python_gate(self):
        # The UI's honest why-not text keys off these; python_ok mirrors the module gate,
        # which itself reflects THIS interpreter (yt-dlp needs >= 3.10).
        youfish._ytdlp_zipapp_read_path = lambda: os.path.join(self._tmp, "absent.zip")
        st = youfish.fast_resolve_status()
        self.assertEqual(st["python_ok"], youfish._FAST_RESOLVE_PY_OK)
        self.assertEqual(st["python_ok"], sys.version_info >= (3, 10))
        self.assertEqual(st["python_version"], "%d.%d" % sys.version_info[:2])

    def test_fast_resolve_is_not_a_setting(self):
        # Deliberate: in-process yt-dlp is plumbing, not a preference — no defaults key
        # (a stale stored "fast_resolve" from older builds is ignored, never consulted).
        self.assertNotIn("fast_resolve", youfish._SETTINGS_DEFAULTS)


class ZipappAutofetch(unittest.TestCase):
    """_autofetch_zipapp: launch-time self-heal — fetch the missing copy exactly once per
    process, only on a capable python, only when the consented-to binary is already there.
    Reads the READ path, so a shared FinTube copy counts as present (never refetched)."""

    def setUp(self):
        self._saved = dict(zrp=youfish._ytdlp_zipapp_read_path, yp=youfish._ytdlp_path,
                           inst=youfish.install_ytdlp_zipapp, done=youfish._ZIPAPP_AUTOFETCH_DONE,
                           pyok=youfish._FAST_RESOLVE_PY_OK)
        youfish._ZIPAPP_AUTOFETCH_DONE = False
        youfish._FAST_RESOLVE_PY_OK = True
        self.calls = []
        youfish.install_ytdlp_zipapp = lambda: self.calls.append(1)
        self._tmp = tempfile.mkdtemp(prefix="zaf-")

    def tearDown(self):
        (youfish._ytdlp_zipapp_read_path, youfish._ytdlp_path, youfish.install_ytdlp_zipapp) = (
            self._saved["zrp"], self._saved["yp"], self._saved["inst"])
        youfish._ZIPAPP_AUTOFETCH_DONE = self._saved["done"]
        youfish._FAST_RESOLVE_PY_OK = self._saved["pyok"]
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_fetches_once_when_missing_and_binary_present(self):
        youfish._ytdlp_zipapp_read_path = lambda: os.path.join(self._tmp, "absent.zip")
        youfish._ytdlp_path = lambda: os.path.join(self._tmp, "yt-dlp")
        youfish._autofetch_zipapp()
        youfish._autofetch_zipapp()      # second prewarm nudge in the same process
        self.assertEqual(len(self.calls), 1)

    def test_installer_refuses_when_python_too_old(self):
        # Engine-side belt for the QML gate: a too-old python must never even download the
        # zipapp (rides can race the async status load). Emits an honest done(False) event.
        import types as _t
        prev = sys.modules.get("pyotherside")
        events = []
        sys.modules["pyotherside"] = _t.SimpleNamespace(
            send=lambda *a: events.append(a))
        try:
            youfish._FAST_RESOLVE_PY_OK = False
            res = self._saved["inst"]()          # the REAL installer (setUp mocks the name)
            self.assertEqual(res, {"ok": False})
            self.assertTrue(events and events[0][0] == "ytdlp_zipapp_done"
                            and events[0][1] is False)
        finally:
            if prev is not None:
                sys.modules["pyotherside"] = prev
            else:
                sys.modules.pop("pyotherside", None)

    def test_skips_when_python_too_old(self):
        youfish._FAST_RESOLVE_PY_OK = False
        youfish._ytdlp_zipapp_read_path = lambda: os.path.join(self._tmp, "absent.zip")
        youfish._ytdlp_path = lambda: os.path.join(self._tmp, "yt-dlp")
        youfish._autofetch_zipapp()
        self.assertEqual(self.calls, [])

    def test_skips_when_already_present_or_no_binary(self):
        p = os.path.join(self._tmp, "have.zip")
        open(p, "wb").close()
        youfish._ytdlp_zipapp_read_path = lambda: p
        youfish._ytdlp_path = lambda: os.path.join(self._tmp, "yt-dlp")
        youfish._autofetch_zipapp()      # zipapp already there (own or FinTube's)
        youfish._ZIPAPP_AUTOFETCH_DONE = False
        youfish._ytdlp_zipapp_read_path = lambda: os.path.join(self._tmp, "absent.zip")
        youfish._ytdlp_path = lambda: ""
        youfish._autofetch_zipapp()      # no binary yet (fresh install pre-consent)
        self.assertEqual(self.calls, [])


class _FakeJar:
    def clear(self):
        self.cleared = True

    def load(self, path):
        self.loaded = path


class _FakeYDL:
    """Stand-in YoutubeDL: records params + extract calls, returns canned info (or raises)."""
    instances = []
    raise_on_extract = False
    formats = []

    def __init__(self, params):
        self.params = dict(params)
        self.cookiejar = _FakeJar()
        self.extract_calls = []
        _FakeYDL.instances.append(self)

    def extract_info(self, url, download=False):
        self.extract_calls.append((url, download, dict(self.params.get("extractor_args") or {})))
        if _FakeYDL.raise_on_extract:
            raise RuntimeError("boom")
        return {"title": "T", "formats": list(_FakeYDL.formats), "duration": 100,
                "_internal": "strip-me"}

    def sanitize_info(self, info):
        info = dict(info)
        info.pop("_internal", None)          # the real sanitize_info strips yt-dlp internals
        return info


class FastResolveRouting(unittest.TestCase):
    """resolve() runs the token-free hot dump IN-PROCESS whenever the zipapp is importable, and
    falls back to the binary on any in-process failure or for the token (fetch_pot) path."""
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args", "_yt_extractor_args",
                     "_proxied", "get_settings", "_import_yt_dlp", "_pot_active"):
            self._saved[name] = getattr(youfish, name)
        self._run = youfish.subprocess.run
        self._tls = youfish._inproc_tls

        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._pot_active = lambda: False          # no probe / token path in the common case
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False,
                                        }
        youfish._import_yt_dlp = lambda: types.SimpleNamespace(YoutubeDL=_FakeYDL)
        youfish._inproc_tls = youfish.threading.local()   # fresh warm-instance store per test
        youfish.invalidate_resolve_cache()

        _FakeYDL.instances = []
        _FakeYDL.raise_on_extract = False
        _FakeYDL.formats = [vf("137", 1080, "avc1"), af("251", 160, "opus", lang_pref=10)]

        self.binary_calls = []

        def fake_run(cmd, **kw):
            self.binary_calls.append(cmd)
            data = {"title": "BIN", "duration": 100,
                    "formats": [vf("137", 1080, "avc1"), af("251", 160, "opus", lang_pref=10)]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(youfish, name, fn)
        youfish.subprocess.run = self._run
        youfish._inproc_tls = self._tls

    def test_token_free_dump_runs_in_process(self):
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: (
            ["--extractor-args", "youtube:player_client=mweb;fetch_pot=always"] if want_pot else
            ["--extractor-args", "youtube:player_client=tv_embedded"])
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "T")            # from the in-process fake, not "BIN"
        self.assertEqual(res["info"]["audio_itag"], "251")
        self.assertEqual(self.binary_calls, [])                # binary NEVER spawned
        self.assertEqual(len(_FakeYDL.instances), 1)           # one warm instance built
        ydl = _FakeYDL.instances[0]
        self.assertEqual(ydl.params.get("source_address"), "0.0.0.0")          # == -4 (force IPv4)
        self.assertEqual(ydl.extract_calls[0][2], {"youtube": {"player_client": ["tv_embedded"]}})

    def test_warm_instance_reused_across_resolves(self):
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=tv_embedded"]
        youfish.resolve("vidA")
        youfish.resolve("vidB")
        self.assertEqual(len(_FakeYDL.instances), 1)           # same warm YoutubeDL, not rebuilt
        self.assertEqual(len(_FakeYDL.instances[0].extract_calls), 2)

    def test_inproc_failure_falls_back_to_binary(self):
        _FakeYDL.raise_on_extract = True
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=tv_embedded"]
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")          # binary served it
        self.assertTrue(self.binary_calls)                     # fallback actually ran

    def test_token_path_never_runs_in_process(self):
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=mweb;fetch_pot=always"]
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")
        self.assertEqual(_FakeYDL.instances, [])               # in-process path skipped entirely

    def test_no_zipapp_uses_binary(self):
        youfish._import_yt_dlp = lambda: None   # copy absent / not importable → binary path
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")
        self.assertEqual(_FakeYDL.instances, [])


class AnonymousPrimary(unittest.TestCase):
    """The token-free PRIMARY dump resolves WITHOUT cookies (anonymous) — YouTube gates authenticated
    token-free requests but not anonymous ones. Only the fallback re-runs WITH cookies (restricted
    content). Exercised on the binary path (zipapp pinned absent)."""
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args", "_yt_extractor_args",
                     "_proxied", "get_settings", "_cookies_args", "_pot_active", "_import_yt_dlp"):
            self._saved[name] = getattr(youfish, name)
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._pot_active = lambda: False
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False}
        youfish._import_yt_dlp = lambda: None   # zipapp pinned absent → binary path

        import contextlib

        @contextlib.contextmanager
        def _ck():
            yield ["--cookies", "CKFILE"]
        youfish._cookies_args = _ck
        # primary (want_pot=False) → tv_embedded; fallback (want_pot=True) → the wider retry set
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=" + (client_override or "tv_embedded")]
        youfish.invalidate_resolve_cache()
        self.calls = []

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run

    def _full(self):
        return {"title": "T", "duration": 100,
                "formats": [vf("137", 1080, "avc1"), af("251", 160, "opus", lang_pref=10)]}

    def test_public_primary_is_anonymous(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self._full()), stderr="")
        youfish.subprocess.run = run
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(self.calls), 1)                  # public → one dump, no fallback
        self.assertNotIn("--cookies", self.calls[0])          # PRIMARY carries no cookies

    def test_restricted_falls_back_with_cookies(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            if "tv_embedded" in " ".join(cmd):                # the anonymous primary FAILS (restricted)
                return types.SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm")
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self._full()), stderr="")
        youfish.subprocess.run = run
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertNotIn("--cookies", self.calls[0])          # primary anonymous
        self.assertIn("--cookies", self.calls[1])             # fallback re-runs WITH cookies


class ReresolveAnonFirst(unittest.TestCase):
    """_reresolve mirrors resolve(): an anonymous token-free dump first (same client + auth
    posture as the primary that produced the playing URLs), the cookie'd + PO-token dump only
    when the anonymous pass didn't yield the wanted itag."""

    def setUp(self):
        self.calls = []
        self._saved = dict(path=youfish._ytdlp_path, run=youfish.subprocess.run,
                           pot=youfish._pot_active, ens=youfish._ensure_pot_server,
                           ck=youfish._write_cookies_temp, gs=youfish.get_settings,
                           imp=youfish._import_yt_dlp)
        youfish._ytdlp_path = lambda: "/bin/yt-dlp"
        youfish._pot_active = lambda: True
        youfish._ensure_pot_server = lambda **kw: True
        youfish._write_cookies_temp = lambda: ""   # signed out → _cookies_args yields []
        youfish.get_settings = lambda: {}
        youfish._import_yt_dlp = lambda: None   # zipapp pinned absent → binary path
        youfish._url_cache.clear()
        youfish._reresolve_spawns[:] = []

    def tearDown(self):
        youfish._ytdlp_path = self._saved["path"]
        youfish.subprocess.run = self._saved["run"]
        youfish._pot_active = self._saved["pot"]
        youfish._ensure_pot_server = self._saved["ens"]
        youfish._write_cookies_temp = self._saved["ck"]
        youfish.get_settings = self._saved["gs"]
        youfish._import_yt_dlp = self._saved["imp"]
        youfish._url_cache.clear()
        youfish._reresolve_spawns[:] = []

    @staticmethod
    def _dump_json(itags):
        return json.dumps({"formats": [{"format_id": i, "url": "https://g/" + i} for i in itags]})

    def test_anon_hit_spawns_once_no_token(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=self._dump_json(["303"]), stderr="")
        youfish.subprocess.run = run
        got = youfish._reresolve("vid", "303", "https://g/old")
        self.assertEqual(got, "https://g/303")
        self.assertEqual(len(self.calls), 1)                     # anon pass sufficed
        joined = " ".join(self.calls[0])
        self.assertNotIn("--cookies", joined)                    # cookie-free
        self.assertNotIn("fetch_pot=always", joined)             # token-free

    def test_missing_itag_falls_back_to_token_dump(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            first = len(self.calls) == 1
            return types.SimpleNamespace(
                returncode=0,
                stdout=self._dump_json(["136"] if first else ["136", "303"]), stderr="")
        youfish.subprocess.run = run
        got = youfish._reresolve("vid", "303", "https://g/old")
        self.assertEqual(got, "https://g/303")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("fetch_pot=always", " ".join(self.calls[0]))   # anon first
        self.assertIn("fetch_pot=always", " ".join(self.calls[1]))      # token safety net second
        self.assertEqual(len(youfish._reresolve_spawns), 2)             # both spawns rate-counted

    def test_fallback_respects_rate_limit(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=self._dump_json(["136"]), stderr="")
        youfish.subprocess.run = run
        now = time.time()
        youfish._reresolve_spawns[:] = [now] * (youfish._RERESOLVE_BURST - 1)   # one slot left
        got = youfish._reresolve("vid", "303", "https://g/old")
        self.assertIsNone(got)                                   # itag never found
        self.assertEqual(len(self.calls), 1)                     # the anon spawn took the last slot;
                                                                 # the token fallback was NOT spawned


class _FakeHttpResp:
    """Minimal urllib-response stand-in: .status/.getcode(), .headers (dict), .read(n), .close()."""
    def __init__(self, code, data, headers=None):
        self.status = code
        self._data = data
        self._off = 0
        self.headers = dict(headers or {})

    def getcode(self):
        return self.status

    def read(self, n):
        b = self._data[self._off:self._off + n]
        self._off += len(b)
        return b

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class DirectFetchStreamer(unittest.TestCase):
    """_DirectFetch: the in-process stand-in for the yt-dlp streaming child. Chunked Range
    delivery (the burst-window behaviour), clean-EOF vs error semantics (both read as b"", like
    a child pipe closing), truncation self-heal, the kill() duck surface, and _spawn's
    direct-vs-binary chooser + byte-0 fallback flip."""

    DATA = bytes(range(25)) * 1                     # 25 known bytes

    def setUp(self):
        self._urlopen = youfish.urllib.request.urlopen
        self._chunk = youfish._DIRECT_CHUNK
        self._ipv4 = youfish._force_ipv4
        youfish._force_ipv4 = lambda: None          # leave the test process's resolver alone
        youfish._DIRECT_CHUNK = 10
        self.ranges = []                            # every Range header the fetcher sent

    def tearDown(self):
        youfish.urllib.request.urlopen = self._urlopen
        youfish._DIRECT_CHUNK = self._chunk
        youfish._force_ipv4 = self._ipv4

    def _serve(self, data, truncate_first=0):
        """Fake urlopen honouring bounded ranges over `data`; optionally truncate the first
        response after N bytes (server closed early) to exercise the reopen path."""
        state = {"first": True}

        def fake(req, timeout=None):
            rng = req.headers.get("Range", "")
            m = __import__("re").match(r"bytes=(\d+)-(\d+)", rng)
            a, b = int(m.group(1)), int(m.group(2))
            self.ranges.append((a, b))
            if a >= len(data):
                raise urllib.error.HTTPError(req.full_url, 416, "range", {}, None)
            body = data[a:b + 1]
            if state["first"] and truncate_first:
                state["first"] = False
                body = body[:truncate_first]
            return _FakeHttpResp(206, body,
                                 {"Content-Range": "bytes %d-%d/%d" % (a, min(b, len(data) - 1),
                                                                       len(data))})
        youfish.urllib.request.urlopen = fake

    def test_probe_asks_for_the_same_range_as_the_first_fetch(self):
        self._serve(self.DATA)
        for url in ("https://r.googlevideo.com/vp?clen=25", "https://r.googlevideo.com/vp?clen=7",
                    "https://r.googlevideo.com/vp"):
            del self.ranges[:]
            self.assertEqual(youfish._probe_status(url, "UA"), 206)
            youfish._DirectFetch(url, "UA", 0, youfish._clen(url)).read(1)
            self.assertEqual(self.ranges[0], self.ranges[1], url)
        self.assertEqual(self.ranges[0], (0, 9))

    @staticmethod
    def _drain(f, n=7):
        out = b""
        while True:
            b = f.read(n)
            if not b:
                return out
            out += b

    def test_chunked_ranges_full_delivery_clean_eof(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA)
        self.assertEqual(self.ranges, [(0, 9), (10, 19), (20, 24)])   # bounded chunks, clamped end
        self.assertEqual(f.poll(), 0)                                 # clean EOF, not an error

    def test_resume_at_offset(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 12, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA[12:])
        self.assertEqual(self.ranges[0][0], 12)                       # first chunk starts AT the offset

    def test_unknown_total_learned_and_416_ends_clean(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, None)
        self.assertEqual(self._drain(f), self.DATA)                   # total learned from Content-Range
        self.assertEqual(f.poll(), 0)

    def test_error_at_byte0_reads_as_pipe_death(self):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
        youfish.urllib.request.urlopen = fake
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, 100)
        self.assertEqual(f.read(7), b"")                              # like the child dying at byte 0
        self.assertEqual(f.poll(), 1)                                 # error, not clean EOF

    def _refuse_first(self, n, data=None):
        """Fake googlevideo that answers 403 to the first n requests, then serves (a fresh PO-token
        URL's warm-up), or 403s everything once data has been served (data=bytes: mid-stream)."""
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            a, b = map(int, req.headers["Range"][6:].split("-"))
            self.ranges.append((a, b))
            if calls["n"] <= n or (data is not None and a > 0):
                raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
            body = (data or self.DATA)[a:b + 1]
            return _FakeHttpResp(206, body, {"Content-Range": "bytes %d-%d/%d"
                                             % (a, a + len(body) - 1, len(data or self.DATA))})
        youfish.urllib.request.urlopen = fake
        self._step, self._warm = youfish._POT_WARMUP_STEP, youfish._POT_WARMUP
        youfish._POT_WARMUP_STEP = 0
        self.addCleanup(setattr, youfish, "_POT_WARMUP_STEP", self._step)
        self.addCleanup(setattr, youfish, "_POT_WARMUP", self._warm)
        return calls

    @staticmethod
    def _gv(expire_in, pot=True):
        return ("https://r.googlevideo.com/vp?expire=%d&itag=251%s"
                % (time.time() + expire_in, "&pot=Mlx" if pot else ""))

    def test_young_token_url_rides_out_its_warmup(self):
        calls = self._refuse_first(3)                 # refused for ~1.5 s on a phone, then served
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA)
        self.assertEqual(calls["n"], 6)               # 3 refusals + the 3 real chunks
        self.assertEqual(self.ranges[:4], [(0, 9)] * 4)   # the same first request, retried

    def test_token_url_refused_past_the_warmup_dies(self):
        self._refuse_first(10 ** 6)
        youfish._POT_WARMUP = 0.05
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        self.assertEqual(f.read(7), b"")
        self.assertEqual((f.poll(), f.err), (1, "HTTP 403"))   # into the re-resolve as before

    def test_only_young_token_urls_wait(self):
        for url in (self._gv(21599, pot=False),       # token-free: a 403 is a real refusal
                    self._gv(3600)):                  # issued hours ago: expired or gated, not warming
            calls = self._refuse_first(1)
            f = youfish._DirectFetch(url, "UA", 0, len(self.DATA))
            self.assertEqual(f.read(7), b"", url)
            self.assertEqual(calls["n"], 1, url)

    def test_refusal_after_the_first_bytes_never_waits(self):
        calls = self._refuse_first(0, data=self.DATA)  # chunk 1 served, chunk 2 refused
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA[:10])
        self.assertEqual((f.poll(), calls["n"]), (1, 2))

    def test_kill_during_warmup_stops_it(self):
        calls = self._refuse_first(10 ** 6)
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        youfish._POT_WARMUP_STEP = 0.01
        t = threading.Timer(0.05, f.kill)
        t.start()
        self.assertEqual(f.read(7), b"")
        t.join()
        self.assertEqual(f.poll(), 0)                  # killed, not an error
        self.assertLess(calls["n"], 100)

    def test_midchunk_truncation_self_heals(self):
        self._serve(self.DATA, truncate_first=4)                      # server closes after 4 bytes
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA)                   # no byte lost, no byte doubled
        self.assertIn((4, 13), self.ranges)                           # reopened FROM the truncation point

    def test_kill_finalises_and_unblocks(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, len(self.DATA))
        self.assertTrue(f.read(7))
        f.kill()
        self.assertEqual(f.read(7), b"")
        self.assertEqual(f.poll(), 0)
        f.wait(timeout=5)                                             # duck surface for _reap_proc

    def test_spawn_prefers_direct_and_honours_flip(self):
        self._serve(self.DATA)
        s = types.SimpleNamespace(url="https://r.googlevideo.com/vp", ua="UA", total=25,
                                  itag="136", use_binary=False)
        self.assertIsInstance(youfish._spawn(s, 0), youfish._DirectFetch)
        argvs = []
        saved_popen, saved_path = youfish.subprocess.Popen, youfish._ytdlp_path
        youfish.subprocess.Popen = lambda argv, **kw: argvs.append(argv) or types.SimpleNamespace(
            stdout=None, kill=lambda: None, poll=lambda: 0, wait=lambda **k: 0)
        youfish._ytdlp_path = lambda: "/bin/yt-dlp"
        try:
            s.use_binary = True                                       # the byte-0 flip happened
            youfish._spawn(s, 5)
        finally:
            youfish.subprocess.Popen, youfish._ytdlp_path = saved_popen, saved_path
        self.assertEqual(len(argvs), 1)                               # binary child took over
        self.assertIn("--http-chunk-size", argvs[0])
        self.assertIn("Range: bytes=5-", " ".join(argvs[0]))          # resume offset forwarded


class PotPluginProbe(unittest.TestCase):
    """_pot_plugin_probe: one offline verbose run tells whether the installed yt-dlp resolves
    the bgutil plugin dir (the 2026-09-02 'Plugin directories: none' incident detector), plus
    yt-dlp's own JS-runtime view."""

    HEADER_OK = ("[debug] Command-line config: [...]\n"
                 "[debug] yt-dlp version stable@2026.08.19\n"
                 "[debug] JS runtimes: deno 2026.1\n"
                 "[debug] Plugin directories: /repo/plugin/yt_dlp_plugins\n"
                 "[debug] Loaded 1744 extractors\n")
    HEADER_NONE = ("[debug] yt-dlp version stable@2026.08.19\n"
                   "[debug] JS runtimes: none\n"
                   "[debug] Plugin directories: none\n")
    HEADER_OLD = "[debug] yt-dlp version stable@2023.01.01\n"

    def setUp(self):
        self._saved = dict(path=youfish._ytdlp_path, run=youfish.subprocess.run,
                           inst=youfish._pot_installed, pd=youfish._pot_plugin_dir,
                           rd=youfish._pot_repo_dir)
        youfish._ytdlp_path = lambda: "/bin/yt-dlp"
        youfish._pot_installed = lambda: True
        youfish._pot_plugin_dir = lambda: "/repo"
        youfish._pot_repo_dir = lambda: "/repo"
        self.argv = None

    def tearDown(self):
        youfish._ytdlp_path = self._saved["path"]
        youfish.subprocess.run = self._saved["run"]
        youfish._pot_installed = self._saved["inst"]
        youfish._pot_plugin_dir = self._saved["pd"]
        youfish._pot_repo_dir = self._saved["rd"]

    def _with_header(self, header):
        def run(cmd, **kw):
            self.argv = cmd
            return types.SimpleNamespace(returncode=1, stdout="", stderr=header)
        youfish.subprocess.run = run
        return youfish._pot_plugin_probe()

    def test_resolved_dir_reads_loaded(self):
        p = self._with_header(self.HEADER_OK)
        self.assertTrue(p["checked"])
        self.assertIs(p["loaded"], True)
        self.assertEqual(p["js_runtimes"], "deno 2026.1")
        self.assertIn("--no-plugin-dirs", self.argv)     # only OUR dir is probed
        self.assertIn("--simulate", self.argv)           # offline — fails before any network
        self.assertIn("-v", self.argv)                   # the header only prints verbose

    def test_none_reads_not_loaded(self):
        p = self._with_header(self.HEADER_NONE)
        self.assertIs(p["loaded"], False)                # the incident shape
        self.assertEqual(p["js_runtimes"], "none")

    def test_missing_line_is_undetermined_not_alarm(self):
        p = self._with_header(self.HEADER_OLD)
        self.assertIsNone(p["loaded"])

    def test_not_installed_skips(self):
        youfish._pot_installed = lambda: False
        self.assertFalse(youfish._pot_plugin_probe()["checked"])


class PotStatusDenoManaged(unittest.TestCase):
    """pot_status flags whether the Deno in use is the APP-MANAGED copy — the one with no other
    updater, which the UI offers an 'Update Deno' button for."""

    def setUp(self):
        self._saved = dict(dp=youfish._deno_path, md=youfish._managed_deno,
                           port=youfish._pot_ready_on_port, inst=youfish._pot_installed,
                           gs=youfish.get_settings)
        youfish._pot_ready_on_port = lambda timeout=0.25: False
        youfish._pot_installed = lambda: False
        youfish.get_settings = lambda: {}

    def tearDown(self):
        youfish._deno_path = self._saved["dp"]
        youfish._managed_deno = self._saved["md"]
        youfish._pot_ready_on_port = self._saved["port"]
        youfish._pot_installed = self._saved["inst"]
        youfish.get_settings = self._saved["gs"]

    def test_managed_and_system_deno(self):
        youfish._managed_deno = lambda: "/data/bin/deno"
        youfish._deno_path = lambda: "/data/bin/deno"
        self.assertTrue(youfish.pot_status()["deno_managed"])
        youfish._deno_path = lambda: "/usr/bin/deno"
        self.assertFalse(youfish.pot_status()["deno_managed"])
        youfish._deno_path = lambda: None
        self.assertFalse(youfish.pot_status()["deno_managed"])


class ChannelAwareDownloads(unittest.TestCase):
    """Direct downloads (binary install, zipapp, SHA2-256SUMS) must follow the user's update
    channel — a nightly BINARY beside a stable ZIPAPP means the in-process fast path silently
    misses the breakage fix the user switched to nightly for."""

    def setUp(self):
        self._gs = youfish.get_settings
        self._open = youfish._https_open

    def tearDown(self):
        youfish.get_settings = self._gs
        youfish._https_open = self._open

    def test_release_base_follows_channel(self):
        youfish.get_settings = lambda: {"ytdlp_channel": "nightly"}
        self.assertIn("yt-dlp-nightly-builds", youfish._ytdlp_release_base())
        youfish.get_settings = lambda: {}
        self.assertIn("/yt-dlp/yt-dlp/", youfish._ytdlp_release_base())
        youfish.get_settings = lambda: {"ytdlp_channel": "weird"}   # unknown -> stable, never KeyError
        self.assertIn("/yt-dlp/yt-dlp/", youfish._ytdlp_release_base())

    def test_expected_sha_reads_channel_sums(self):
        seen = []

        class _Sums:
            def read(self):
                return b"abc123 *yt-dlp\ndef456  yt-dlp_linux_aarch64\n"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        youfish._https_open = lambda url, ctx, timeout=30: seen.append(url) or _Sums()
        youfish.get_settings = lambda: {"ytdlp_channel": "nightly"}
        self.assertEqual(youfish._expected_sha256(None, "yt-dlp"), "abc123")
        self.assertIn("yt-dlp-nightly-builds", seen[0])
        self.assertTrue(seen[0].endswith("SHA2-256SUMS"))


class PotBindLocalhost(unittest.TestCase):
    """_pot_bind_localhost against the exact bgutil 1.3.2 source shape: BOTH hardcoded bind
    hosts move to 127.0.0.1, and the hardcoded success-log address strings are corrected too
    (upstream prints "[::]:<port>" regardless of the actual bind — a recurring log red
    herring). Genuine error-path text is left untouched."""

    SRC = (
        'httpServer\n'
        '    .listen(\n'
        '        {\n'
        '            host: "::",\n'
        '            port: PORT_NUMBER,\n'
        '        },\n'
        '        (err) => {\n'
        '            if (err) {\n'
        '                console.error(\n'
        '                    `Could not listen on [::]:${PORT_NUMBER}, falling back to 0.0.0.0 '
        '(Caused by ${strerror(err)})`,\n'
        '                );\n'
        '            } else {\n'
        '                console.log(\n'
        '                    `Started POT server (v${VERSION}) on on address [::]:${PORT_NUMBER}`,\n'
        '                );\n'
        '            }\n'
        '        },\n'
        '    )\n'
        '    .on("error", () => {\n'
        '        httpServer.listen(\n'
        '            {\n'
        '                host: "0.0.0.0",\n'
        '                port: PORT_NUMBER,\n'
        '            },\n'
        '            (err) => {\n'
        '                console.log(\n'
        '                    `Started POT server (v${VERSION}) on address 0.0.0.0:${PORT_NUMBER}`,\n'
        '                );\n'
        '            },\n'
        '        );\n'
        '    });\n'
    )

    def _patch(self, src):
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        os.makedirs(os.path.join(td, "src"))
        p = os.path.join(td, "src", "main.ts")
        with open(p, "w") as f:
            f.write(src)
        saved = youfish._pot_server_dir
        youfish._pot_server_dir = lambda: td
        try:
            youfish._pot_bind_localhost()
        finally:
            youfish._pot_server_dir = saved
        with open(p) as f:
            return f.read()

    def test_binds_and_success_logs_localhost(self):
        out = self._patch(self.SRC)
        self.assertNotIn('host: "::"', out)
        self.assertNotIn('host: "0.0.0.0"', out)
        self.assertEqual(out.count('host: "127.0.0.1"'), 2)
        self.assertEqual(out.count('address 127.0.0.1:${PORT_NUMBER}'), 2)
        self.assertNotIn('address [::]:', out)
        self.assertNotIn('address 0.0.0.0:', out)
        self.assertIn('falling back to 0.0.0.0', out)            # error text untouched
        self.assertIn('Could not listen on [::]:', out)

    def test_idempotent_and_unknown_shape_noop(self):
        out1 = self._patch(self.SRC)
        out2 = self._patch(out1)
        self.assertEqual(out1, out2)                             # second run changes nothing
        odd = "serve({ hostname: cfg.host })"
        self.assertEqual(self._patch(odd), odd)                  # unknown shape → untouched



class DownloadAudioMeta(unittest.TestCase):
    """download() in the music app: ALWAYS audio (kind normalised, single .m4a, no ffmpeg / no
    merge args), and the track's artist/cover metadata lands in downloads.json for the
    Downloads list + offline playback."""
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_write_cookies_temp", "_ensure_pot_server",
                     "_yt_extractor_args", "_pot_ytdlp_args", "_downloads_dir",
                     "_downloads_path", "_set_pdeathsig"):
            self._saved[name] = getattr(youfish, name)
        self._popen = youfish.subprocess.Popen
        self._prev_po = sys.modules.get("pyotherside")

        self._tmp = tempfile.mkdtemp(prefix="dlmeta-")
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._write_cookies_temp = lambda: ""
        youfish._ensure_pot_server = lambda **kw: True
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        youfish._pot_ytdlp_args = lambda: []
        youfish._downloads_dir = lambda: self._tmp
        youfish._downloads_path = lambda: os.path.join(self._tmp, "downloads.json")
        youfish._set_pdeathsig = lambda: None

        self.events = []
        self.done = threading.Event()
        po = types.ModuleType("pyotherside")
        def send(*args):
            self.events.append(args)
            if args and args[0] == "download_done":
                self.done.set()
        po.send = send
        sys.modules["pyotherside"] = po

        self.cmds = []
        tmp = self._tmp
        cmds = self.cmds
        class FakeProc:
            def __init__(self, cmd, **kw):
                cmds.append(cmd)
                # yt-dlp would write the file; fake it so the success path registers.
                open(os.path.join(tmp, "Song [vid1] audio.m4a"), "w").write("x")
                self.stdout = iter(["[download]  50.0% of 3MiB\n",
                                    "[download] 100.0% of 3MiB\n"])
                self.returncode = 0
            def wait(self):
                return 0
        youfish.subprocess.Popen = FakeProc

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(youfish, name, fn)
        youfish.subprocess.Popen = self._popen
        if self._prev_po is not None:
            sys.modules["pyotherside"] = self._prev_po
        else:
            sys.modules.pop("pyotherside", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_signed_in_download_names_no_client(self):
        youfish._yt_extractor_args = self._saved["_yt_extractor_args"]     # the real one
        ck = os.path.join(self._tmp, "c.txt")
        youfish._write_cookies_temp = lambda: open(ck, "w").close() or ck
        saved = (youfish._pot_active, youfish.get_settings)
        youfish._pot_active = lambda: True
        youfish.get_settings = lambda: {"sponsorblock": False}
        try:
            youfish.download("vid1", "Song")
            self.assertTrue(self.done.wait(5), "download thread never finished")
        finally:
            youfish._pot_active, youfish.get_settings = saved
        cmd = self.cmds[0]
        self.assertIn("--cookies", cmd)
        self.assertFalse([a for a in cmd if "player_client=" in a], cmd)   # visionos would be skipped

    def test_kind_normalised_meta_stored_no_merge_args(self):
        res = youfish.download("vid1", "Song", "video",       # asks for video → still audio
                               {"subtitle": "Artist", "thumb": "http://t/1.jpg",
                                "artistId": "UCabc", "junk": "dropped"})
        self.assertTrue(res.get("ok"))
        self.assertTrue(self.done.wait(5), "download thread never finished")
        cmd = self.cmds[0]
        self.assertIn("140", cmd[cmd.index("-f") + 1])         # the single-file m4a format
        self.assertNotIn("--merge-output-format", cmd)         # no ffmpeg merging, ever
        lst = youfish.list_downloads()
        self.assertEqual(len(lst), 1)
        e = lst[0]
        self.assertEqual(e["kind"], "audio")                   # normalised
        self.assertEqual(e["subtitle"], "Artist")
        self.assertEqual(e["thumb"], "http://t/1.jpg")
        self.assertEqual(e["artistId"], "UCabc")
        self.assertNotIn("junk", e)                            # only the known meta keys
        prog = [ev for ev in self.events if ev[0] == "download_progress"]
        self.assertTrue(prog and prog[0][2] == "audio")        # progress reported as audio too

    def test_delete_download_removes_file_and_entry(self):
        youfish.download("vid1", "Song", "audio", {"subtitle": "A"})
        self.assertTrue(self.done.wait(5))
        path = youfish.list_downloads()[0]["path"]
        self.assertTrue(os.path.exists(path))
        out = youfish.delete_download("vid1", "audio")
        self.assertEqual(out["downloads"], [])
        self.assertFalse(os.path.exists(path))


class DownloadQueueBatch(unittest.TestCase):
    """download_many(): the playlist 'Download all' batch. Skips already-downloaded + duplicate +
    id-less items, reports the queued id list, and every job runs serially through the one shared
    worker (never a burst of parallel yt-dlp processes)."""
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_write_cookies_temp", "_ensure_pot_server",
                     "_yt_extractor_args", "_pot_ytdlp_args", "_downloads_dir",
                     "_downloads_path", "_set_pdeathsig"):
            self._saved[name] = getattr(youfish, name)
        self._popen = youfish.subprocess.Popen
        self._prev_po = sys.modules.get("pyotherside")
        self._gap = youfish._DL_GAP
        youfish._DL_GAP = (0, 0)                       # no real sleeping in tests

        self._tmp = tempfile.mkdtemp(prefix="dlq-")
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._write_cookies_temp = lambda: ""
        youfish._ensure_pot_server = lambda **kw: True
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        youfish._pot_ytdlp_args = lambda: []
        youfish._downloads_dir = lambda: self._tmp
        youfish._downloads_path = lambda: os.path.join(self._tmp, "downloads.json")
        youfish._set_pdeathsig = lambda: None

        self.done_ids = []
        self.cond = threading.Condition()
        po = types.ModuleType("pyotherside")
        def send(*args):
            if args and args[0] == "download_done":
                with self.cond:
                    self.done_ids.append((args[1], args[3]))   # (videoId, ok)
                    self.cond.notify_all()
        po.send = send
        sys.modules["pyotherside"] = po

        class FakeProc:
            def __init__(self, cmd, **kw):
                o = cmd[cmd.index("-o") + 1]           # base + ".%(ext)s"
                open(o.replace("%(ext)s", "m4a"), "w").close()   # the file yt-dlp would write
                self.stdout = iter(["[download] 100.0% of 1MiB\n"])
                self.returncode = 0
            def wait(self):
                return 0
        youfish.subprocess.Popen = FakeProc

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(youfish, name, fn)
        youfish.subprocess.Popen = self._popen
        youfish._DL_GAP = self._gap
        if self._prev_po is not None:
            sys.modules["pyotherside"] = self._prev_po
        else:
            sys.modules.pop("pyotherside", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _wait_for(self, n, timeout=5):
        end = time.time() + timeout
        with self.cond:
            while len(self.done_ids) < n and time.time() < end:
                self.cond.wait(max(0, end - time.time()))
            return len(self.done_ids)

    def test_batch_queues_new_skips_dupes_and_downloaded(self):
        items = [{"videoId": "a", "title": "A", "subtitle": "AA"},
                 {"videoId": "b", "title": "B"},
                 {"videoId": "a", "title": "A dup"},   # duplicate id within the batch → skipped
                 {"title": "no id"}]                    # id-less → skipped
        res = youfish.download_many(items)
        self.assertEqual(res["queued"], ["a", "b"])
        self.assertEqual(res["skipped"], 2)
        self.assertEqual(self._wait_for(2), 2)         # both ran serially through the worker
        self.assertEqual({vid for vid, ok in self.done_ids if ok}, {"a", "b"})
        self.assertEqual({d["id"] for d in youfish.list_downloads()}, {"a", "b"})
        # a second batch skips the now-already-downloaded 'a'
        res2 = youfish.download_many([{"videoId": "a", "title": "A"},
                                      {"videoId": "c", "title": "C"}])
        self.assertEqual(res2["queued"], ["c"])
        self.assertEqual(res2["skipped"], 1)
        self.assertEqual(self._wait_for(3), 3)         # drain 'c' before teardown restores mocks

    def test_a_track_already_on_its_way_is_not_queued_again(self):
        # A repeat run found the finished file and failed trying to "resume" it.
        with youfish._dl_pending_lock:
            youfish._dl_pending.add("p")
        try:
            before = youfish._dl_queue.qsize()
            self.assertEqual(youfish.download_many([{"videoId": "p", "title": "P"}])["queued"], [])
            youfish.download("p", "P")
            self.assertEqual(youfish._dl_queue.qsize(), before)
        finally:
            with youfish._dl_pending_lock:
                youfish._dl_pending.discard("p")

    def test_list_downloads_skips_damaged_entries(self):
        good = os.path.join(self._tmp, "a.m4a")
        open(good, "w").close()
        with open(youfish._downloads_path(), "w") as f:
            json.dump([{"id": "a", "kind": "audio", "path": good}, 1, "x", None], f)
        self.assertEqual([d["id"] for d in youfish.list_downloads()], ["a"])

    def test_safe_name_fits_the_filename_byte_limit(self):
        name = youfish._safe_name("\u3042" * 200)
        self.assertLessEqual(len(name.encode("utf-8")), 150)
        self.assertTrue(name)


class YtmIdentity(unittest.TestCase):
    """The self-healing InnerTube identity (ytm.py): scrape the live client version/key from the
    ytcfg blob, fall back to the shipped defaults when the cache is cold. Guards the scrape regex —
    the one bit of parsing that could silently stop matching if YouTube reshapes music.youtube.com."""

    class _Resp:
        headers = {"Content-Encoding": ""}
        def __init__(self, page):
            self._page = page
        def read(self):
            return self._page.encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _scrape(self, page):
        import ytm
        orig = ytm.urllib.request.urlopen
        ytm.urllib.request.urlopen = lambda *a, **k: YtmIdentity._Resp(page)
        try:
            return ytm._fetch_ytm_identity()
        finally:
            ytm.urllib.request.urlopen = orig

    def test_load_falls_back_to_defaults_when_uncached(self):
        import ytm
        orig = ytm._ytm_config_path
        ytm._ytm_config_path = lambda: "/nonexistent/dir/ytm_config.json"
        try:
            cfg = ytm._ytm_cfg_load()
        finally:
            ytm._ytm_config_path = orig
        self.assertEqual(cfg["key"], ytm._DEFAULT_INNERTUBE_KEY)
        self.assertEqual(cfg["version"], ytm._DEFAULT_CLIENT_VERSION)

    def test_scrape_extracts_key_and_version(self):
        key, ver = self._scrape(
            '<script>ytcfg.set({"INNERTUBE_API_KEY":"AIzaTESTKEY123",'
            '"INNERTUBE_CLIENT_VERSION":"1.20260815.01.00","X":"y"});</script>')
        self.assertEqual(key, "AIzaTESTKEY123")
        self.assertEqual(ver, "1.20260815.01.00")

    def test_scrape_rejects_bogus_version(self):
        _key, ver = self._scrape('<script>{"INNERTUBE_CLIENT_VERSION":"garbage"}</script>')
        self.assertIsNone(ver)   # the sanity check drops a value that isn't a 1.YYYYMMDD.xx.xx


class YtmAccountSelector(unittest.TestCase):
    """Account/channel selection (ytm.py): enumerate logins + brand-account channels from the
    InnerTube account switcher, persist a choice, and apply it as X-Goog-AuthUser + X-Goog-PageId.
    Fixes the multi-account bug where every call was pinned to login 0's default channel."""

    def setUp(self):
        import ytm
        self._saved = {n: getattr(ytm, n) for n in
                       ("_innertube", "_auth_mode", "_load_cookies", "_save_cookies",
                        "_home_cache_path")}

    def tearDown(self):
        import ytm
        for n, v in self._saved.items():
            setattr(ytm, n, v)

    @staticmethod
    def _switcher_for(authuser):
        # account/accounts_list returns only the channel(s) for the probed authuser. Login 0 has a
        # personal channel (active) + a brand account; login 1 is a bare account; 2+ = logged out.
        if authuser == 0:
            return {"contents": [{"accountItemSectionRenderer": {"contents": [
                {"accountItem": {"accountName": {"simpleText": "Personal Me"},
                                 "channelHandle": {"simpleText": "@me"}, "isSelected": True,
                                 "serviceEndpoint": {"selectActiveIdentityEndpoint": {
                                     "supportedTokens": [
                                         {"accountStateToken": {"obfuscatedGaiaId": "gaia0"}}]}}}},
                {"accountItem": {"accountName": {"simpleText": "Brand A"},
                                 "channelHandle": {"simpleText": "@branda"},
                                 "serviceEndpoint": {"selectActiveIdentityEndpoint": {
                                     "supportedTokens": [
                                         {"pageIdToken": {"pageId": "UCbrandA"}},
                                         {"accountStateToken": {"obfuscatedGaiaId": "gaia0b"}}]}}}}]}}]}
        if authuser == 1:
            return {"contents": [{"accountItemSectionRenderer": {"contents": [
                {"accountItem": {"accountName": {"simpleText": "Second Login"},
                                 "serviceEndpoint": {"selectActiveIdentityEndpoint": {
                                     "supportedTokens": [
                                         {"accountStateToken": {"obfuscatedGaiaId": "gaia1"}}]}}}}]}}]}
        return {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}

    def test_list_accounts_probes_logins_and_brand_channels(self):
        import ytm
        ytm._auth_mode = lambda: "cookie"
        ytm._innertube = lambda ep, body, **k: self._switcher_for(k.get("authuser", 0))
        ytm._load_cookies = lambda: {"authuser": "0", "page_id": ""}   # default identity
        res = ytm.list_accounts()
        self.assertTrue(res["ok"])
        a = res["accounts"]
        self.assertEqual(len(a), 3)          # probed authuser 0 (2) + 1 (1), then 2 = logged out
        self.assertEqual((a[0]["authuser"], a[0]["page_id"], a[0]["name"]), ("0", "", "Personal Me"))
        self.assertTrue(a[0]["selected"])                     # isSelected AND matches default
        self.assertEqual((a[1]["authuser"], a[1]["page_id"]), ("0", "UCbrandA"))
        self.assertEqual(a[1]["handle"], "@branda")
        self.assertFalse(a[1]["selected"])
        self.assertEqual((a[2]["authuser"], a[2]["name"]), ("1", "Second Login"))

    def test_list_accounts_requires_sign_in(self):
        import ytm
        ytm._auth_mode = lambda: "none"
        res = ytm.list_accounts()
        self.assertFalse(res["ok"])
        self.assertEqual(res["accounts"], [])

    def test_list_accounts_logged_out(self):
        import ytm
        ytm._auth_mode = lambda: "cookie"
        ytm._innertube = lambda ep, body, **k: {
            "responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}
        res = ytm.list_accounts()
        self.assertFalse(res["ok"])
        self.assertEqual(res["accounts"], [])

    def test_selected_follows_stored_choice_not_browser(self):
        # The browser's active channel (isSelected) must NOT mark an account selected — only OUR
        # stored choice does. Here the personal channel is browser-active but we've chosen the brand.
        import ytm
        ytm._auth_mode = lambda: "cookie"

        def sw(authuser):
            if authuser == 0:
                return {"contents": [{"accountItemSectionRenderer": {"contents": [
                    {"accountItem": {"accountName": {"simpleText": "Personal"}, "isSelected": True,
                        "serviceEndpoint": {"selectActiveIdentityEndpoint": {"supportedTokens": [
                            {"accountStateToken": {"obfuscatedGaiaId": "g0"}}]}}}},
                    {"accountItem": {"accountName": {"simpleText": "Brand"},
                        "serviceEndpoint": {"selectActiveIdentityEndpoint": {"supportedTokens": [
                            {"pageIdToken": {"pageId": "UCb"}}]}}}}]}}]}
            return {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}

        ytm._innertube = lambda ep, body, **k: sw(k.get("authuser", 0))
        ytm._load_cookies = lambda: {"authuser": "0", "page_id": "UCb"}   # chose the brand channel
        res = ytm.list_accounts()
        sel = {x["name"]: x["selected"] for x in res["accounts"]}
        self.assertFalse(sel["Personal"])   # browser-active, but not our choice
        self.assertTrue(sel["Brand"])       # our stored choice

    def test_select_persists_identity_and_clears_home_cache(self):
        import ytm, os, tempfile
        store = {"sapisid": "abc"}
        ytm._load_cookies = lambda: dict(store)
        ytm._save_cookies = lambda c: (store.clear(), store.update(c))
        tf = tempfile.NamedTemporaryFile(prefix="home_cache.", suffix=".json", delete=False)
        tf.write(b'{"shelves":[]}'); tf.close()
        ytm._home_cache_path = lambda: tf.name
        try:
            res = ytm.select_account("1", "UCxyz", "ds1", "My Brand")
            self.assertTrue(res["ok"])
            self.assertFalse(os.path.exists(tf.name))          # personalized home dropped
            self.assertEqual(store["authuser"], "1")
            self.assertEqual(store["page_id"], "UCxyz")
            self.assertEqual(ytm.selected_account(),
                             {"authuser": "1", "page_id": "UCxyz", "name": "My Brand"})
        finally:
            if os.path.exists(tf.name):
                os.remove(tf.name)

    def test_select_requires_sign_in(self):
        import ytm
        store = {}                                             # no sapisid → not signed in
        ytm._load_cookies = lambda: dict(store)
        ytm._save_cookies = lambda c: (store.clear(), store.update(c))
        res = ytm.select_account("1", "UCxyz", "", "x")
        self.assertFalse(res["ok"])


class PotEnsureBudget(unittest.TestCase):
    """_ensure_pot_server(wait=) must give up quickly when another thread owns an in-flight
    boot (holds the lock) — the resolve hot path passes a short grace instead of joining a
    slow boot (field log 2026-09-08: 40s waiting on a server that never came up)."""

    def test_budget_respected_while_boot_in_flight(self):
        saved = (youfish._pot_active, youfish._pot_ready_on_port)
        youfish._pot_active = lambda: True
        youfish._pot_ready_on_port = lambda timeout=0.25: False
        self.assertTrue(youfish._pot_lock.acquire(timeout=1))   # simulate prewarm mid-boot
        try:
            t0 = time.time()
            self.assertFalse(youfish._ensure_pot_server(wait=0.3))
            self.assertLess(time.time() - t0, 2.0)   # gave up within the grace, not 25s
        finally:
            youfish._pot_lock.release()
            youfish._pot_active, youfish._pot_ready_on_port = saved


# --- SponsorBlock segment fetch + offline cache --------------------------------------------- #

class _SBResp:
    """Context-manager urlopen stand-in whose .read() returns the whole body (no-arg, like a real
    HTTP response), matching how sponsor_segments consumes it."""
    def __init__(self, data):
        self._data = data
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class SponsorBlockSegments(unittest.TestCase):
    """sponsor_segments(): parse + sort the API's segments, treat 404 as 'nothing submitted', and
    fall back to segments cached on a downloaded copy on any other failure (offline skip)."""

    def setUp(self):
        self._urlopen = youfish.urllib.request.urlopen
        self._ipv4 = youfish._force_ipv4
        self._list = youfish.list_downloads
        youfish._force_ipv4 = lambda: None                # don't touch the test process's resolver

    def tearDown(self):
        youfish.urllib.request.urlopen = self._urlopen
        youfish._force_ipv4 = self._ipv4
        youfish.list_downloads = self._list

    def test_parses_and_sorts_api_segments(self):
        payload = json.dumps([
            {"category": "sponsor", "segment": [30.0, 45.5]},
            {"category": "intro",   "segment": [0.0, 5.0]},
            {"category": "zero",    "segment": [10.0, 10.0]},   # zero-length → dropped
            {"category": "broken",  "segment": [1.0]},          # malformed → dropped
        ]).encode()
        youfish.urllib.request.urlopen = lambda req, timeout=None: _SBResp(payload)
        res = youfish.sponsor_segments("vid123")
        self.assertTrue(res["ok"])
        self.assertEqual([s["category"] for s in res["segments"]], ["intro", "sponsor"])  # by start
        self.assertEqual(res["segments"][1]["start"], 30.0)
        self.assertEqual(res["segments"][1]["end"], 45.5)

    def test_404_is_empty_not_cache(self):
        def boom(req, timeout=None):
            raise youfish.urllib.error.HTTPError(req.full_url, 404, "none", {}, None)
        youfish.urllib.request.urlopen = boom
        youfish.list_downloads = lambda: [                # a cache exists, but 404 must ignore it
            {"id": "vid123", "sb_segments": [{"start": 1, "end": 2, "category": "sponsor"}]}]
        res = youfish.sponsor_segments("vid123")
        self.assertTrue(res["ok"])
        self.assertEqual(res["segments"], [])             # 404 = definitively nothing submitted

    def test_network_error_falls_back_to_cache(self):
        def boom(req, timeout=None):
            raise youfish.urllib.error.URLError("offline")
        youfish.urllib.request.urlopen = boom
        youfish.list_downloads = lambda: [
            {"id": "other",  "sb_segments": [{"start": 0, "end": 1, "category": "intro"}]},
            {"id": "vid123", "sb_segments": [{"start": 5, "end": 9, "category": "sponsor"}]}]
        res = youfish.sponsor_segments("vid123")
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("cached"))
        self.assertEqual(res["segments"][0]["category"], "sponsor")

    def test_cache_miss_when_not_downloaded(self):
        youfish.list_downloads = lambda: []
        self.assertEqual(youfish._cached_sb_segments("vid123"), {"ok": False, "segments": []})


# --- Loudness normalization: /player loudnessDb extraction --------------------------------- #

class PlayerLoudness(unittest.TestCase):
    """ytm.player_loudness(): pull playerConfig.audioConfig.loudnessDb out of an InnerTube /player
    response for playtime volume normalization. Absent field / a bad request → None (the caller
    then leaves the gain at unity), a present value → the float."""

    def setUp(self):
        import ytm
        self.ytm = ytm
        self._inner = ytm._innertube

    def tearDown(self):
        self.ytm._innertube = self._inner

    def test_reads_loudness_db(self):
        self.ytm._innertube = lambda ep, body, **k: {
            "playerConfig": {"audioConfig": {"loudnessDb": 7.5, "perceptualLoudnessDb": -12.3}}}
        res = self.ytm.player_loudness("vid123")
        self.assertTrue(res["ok"])
        self.assertEqual(res["loudness_db"], 7.5)

    def test_absent_field_is_none(self):
        self.ytm._innertube = lambda ep, body, **k: {"playabilityStatus": {"status": "OK"}}
        res = self.ytm.player_loudness("vid123")
        self.assertTrue(res["ok"])
        self.assertIsNone(res["loudness_db"])

    def test_request_failure_reports_not_ok(self):
        def boom(ep, body, **k):
            raise RuntimeError("bot-walled")
        self.ytm._innertube = boom
        res = self.ytm.player_loudness("vid123")
        self.assertFalse(res["ok"])
        self.assertIsNone(res["loudness_db"])

    def test_empty_video_id_short_circuits(self):
        called = {"n": 0}
        def counting(ep, body, **k):
            called["n"] += 1
            return {}
        self.ytm._innertube = counting
        res = self.ytm.player_loudness("")
        self.assertTrue(res["ok"])
        self.assertIsNone(res["loudness_db"])
        self.assertEqual(called["n"], 0)            # no network call for an empty id


# --- InnerTube fast path (innertube.py + the youfish wiring) ---------------------------------- #
# Canned responses mirror the shapes captured live from YouTube (2026-10): lockupViewModel channel
# grids, shortsLockupViewModel Shorts, pageHeaderViewModel headers, VISIONOS player responses.

CID = "UCBJycsmduvYEL83R_U4JriQ"
_REAL_TRANSPORT = innertube._transport


def _lockup(vid, title, dur="11:47", views="4.8M views", ago="19 hours ago", live=False,
            members=False):
    badge = {"thumbnailBadgeViewModel": {
        "text": "LIVE" if live else dur,
        "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE" if live else
                      "THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT"}}
    row = {"metadataParts": [{"text": {"content": views}}] +
                            ([{"text": {"content": ago}}] if ago else [])}
    if members:
        row["badges"] = [{"badgeViewModel": {"badgeText": "Members only",
                                             "badgeStyle": "BADGE_MEMBERS_ONLY"}}]
    return {"richItemRenderer": {"content": {"lockupViewModel": {
        "contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"overlays": [
            {"thumbnailBottomOverlayViewModel": {"badges": [badge]}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [row]}}}}}}}}


def _cont_item(token):
    return {"continuationItemRenderer": {"continuationEndpoint": {
        "continuationCommand": {"token": token}}}}


def _browse(items, tab="Videos", cid=CID, name="Marques Brownlee"):
    content = {"richGridRenderer": {"contents": items}} if tab else {"sectionListRenderer": {}}
    return {
        "metadata": {"channelMetadataRenderer": {
            "title": name, "externalId": cid,
            "channelUrl": "https://www.youtube.com/channel/" + cid,
            "avatar": {"thumbnails": [{"url": "https://yt3.example/av=s900-c-k", "width": 900,
                                       "height": 900}]}}},
        "header": {"pageHeaderRenderer": {"content": {"pageHeaderViewModel": {
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": "@mkbhd"}}]},
                {"metadataParts": [{"text": {"content": "21.4M subscribers"}},
                                   {"text": {"content": "1.8K videos"}}]}]}}}}}},
        "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [
            {"tabRenderer": {"title": "Home", "selected": not tab}},
            {"tabRenderer": {"title": tab or "Videos", "selected": bool(tab), "content": content}}]}},
    }


def _shorts_item(vid, title):
    return {"richItemRenderer": {"content": {"shortsLockupViewModel": {
        "entityId": "shorts-shelf-item-" + vid,
        "onTap": {"innertubeCommand": {"reelWatchEndpoint": {"videoId": vid}}},
        "overlayMetadata": {"primaryText": {"content": title},
                            "secondaryText": {"content": "7.9M views"}}}}}}


def _player(vid="dQw4w9WgXcQ", status="OK", reason=None, live=False, dubbed=False):
    def fmt(itag, mime, **kw):
        d = {"itag": itag, "mimeType": mime, "bitrate": 1000 * (itag % 300),
             "url": "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=%d&expire=%d"
                    % (itag, int(time.time()) + 20000),
             "contentLength": "1000"}
        d.update(kw)
        return d
    if dubbed:
        audio = [fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM",
                     audioTrack={"displayName": "English original", "id": "en.4",
                                 "audioIsDefault": True}),
                 fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM",
                     audioTrack={"displayName": "Japanese", "id": "ja.3",
                                 "audioIsDefault": False})]
    else:
        audio = [fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM"),
                 fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM",
                     isDrc=True),
                 fmt(140, 'audio/mp4; codecs="mp4a.40.2"', audioQuality="AUDIO_QUALITY_MEDIUM")]
    video = [fmt(137, 'video/mp4; codecs="avc1.640028"', height=1080, width=1920, fps=30,
                 qualityLabel="1080p"),
             fmt(136, 'video/mp4; codecs="avc1.4d401f"', height=720, width=1280, fps=30,
                 qualityLabel="720p"),
             fmt(247, 'video/webm; codecs="vp9"', height=720, width=1280, fps=30,
                 qualityLabel="720p"),
             fmt(398, 'video/mp4; codecs="av01.0.05M.08"', height=720, width=1280, fps=30,
                 qualityLabel="720p"),
             {"itag": 299, "mimeType": 'video/mp4; codecs="avc1.64002a"', "height": 1080,
              "signatureCipher": "s=abc&url=https%3A%2F%2Fx"}]          # ciphered → dropped
    return {
        "playabilityStatus": {"status": status, "reason": reason},
        "videoDetails": {"videoId": vid, "title": "Never Gonna", "author": "Rick",
                         "channelId": CID, "lengthSeconds": "213", "viewCount": "1700000000",
                         "isLive": live,
                         "shortDescription": "0:00 Intro\n1:00 Verse\n2:30 Chorus\n"},
        "streamingData": {} if status != "OK" else {"adaptiveFormats": video + audio},
        "captions": {"playerCaptionsTracklistRenderer": {
            "captionTracks": [
                {"baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=en", "languageCode": "en",
                 "name": {"runs": [{"text": "English"}]}},
                {"baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=en&kind=asr",
                 "languageCode": "en", "kind": "asr",
                 "name": {"runs": [{"text": "English (auto-generated)"}]}}],
            "translationLanguages": [
                {"languageCode": "de", "languageName": {"runs": [{"text": "German"}]}},
                {"languageCode": "en", "languageName": {"runs": [{"text": "English"}]}}]}},
    }


class _FakeTransport:
    """Stand-in for innertube._transport: answers by endpoint (+ browse params / continuation)."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, body=None, headers=None, timeout=10):
        ep = url.split("/youtubei/v1/")[1].split("?")[0]
        req = json.loads(body.decode()) if body else {}
        self.calls.append((ep, req, headers or {}))
        key = ep
        if ep == "browse":
            key = "browse:" + (req.get("continuation") or
                               "%s/%s" % (req.get("browseId"), req.get("params")))
        ans = self.routes.get(key)
        if ans is None:
            return 404, b"{}"
        if isinstance(ans, Exception):
            raise ans
        return 200, json.dumps(ans).encode()


class InnertubeParsing(unittest.TestCase):
    def setUp(self):
        self._t = innertube._transport
        innertube._visitor.update({"data": "", "ts": 0.0})
        innertube._cid_cache.clear()

    def tearDown(self):
        innertube._transport = self._t

    def _route(self, routes):
        innertube._transport = _FakeTransport(routes)
        return innertube._transport

    def test_text_and_count_parsers(self):
        self.assertEqual(innertube.text({"runs": [{"text": "a"}, {"text": "b"}]}), "ab")
        self.assertEqual(innertube.text({"simpleText": "x"}), "x")
        self.assertEqual(innertube.text({"content": "y"}), "y")
        self.assertEqual(innertube.parse_count("4.8M views"), 4800000)
        self.assertEqual(innertube.parse_count("1,234 views"), 1234)
        self.assertEqual(innertube.parse_count("21.4M subscribers"), 21400000)
        self.assertEqual(innertube.parse_count("1.8K videos"), 1800)
        self.assertEqual(innertube.parse_count("No views"), 0)
        self.assertEqual(innertube.parse_duration("11:47"), 707)
        self.assertEqual(innertube.parse_duration("1:02:03"), 3723)
        self.assertEqual(innertube.parse_duration("LIVE"), 0)
        self.assertEqual(innertube.parse_ago("19 hours ago"), 19 * 3600)
        self.assertEqual(innertube.parse_ago("Streamed 2 days ago"), 2 * 86400)
        self.assertIsNone(innertube.parse_ago("Scheduled for 10/20/26"))

    def test_channel_tab_lockups_header_and_continuation(self):
        params = innertube.urllib.parse.unquote(innertube.TAB_PARAMS["videos"])
        t = self._route({"browse:%s/%s" % (CID, params): _browse([
            _lockup("aaaaaaaaaa1", "New one"),
            _lockup("aaaaaaaaaa2", "On air", live=True, views="1.2K watching", ago=None),
            _lockup("aaaaaaaaaa3", "Members", members=True),
            {"richItemRenderer": {"content": {"videoRenderer": {   # legacy renderer still parses
                "videoId": "aaaaaaaaaa4", "title": {"runs": [{"text": "Legacy"}]},
                "lengthText": {"simpleText": "1:02:03"}, "viewCountText": {"simpleText": "12 views"},
                "publishedTimeText": {"simpleText": "3 weeks ago"}}}}},
            _cont_item("TOKEN1")])})
        res = innertube.channel_tab(CID, "videos")
        items = {i["id"]: i for i in res["items"]}
        self.assertEqual(list(items), ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3", "aaaaaaaaaa4"])
        self.assertEqual(items["aaaaaaaaaa1"]["duration"], 707)
        self.assertEqual(items["aaaaaaaaaa1"]["views"], 4800000)
        self.assertEqual(items["aaaaaaaaaa1"]["ago"], 19 * 3600)
        self.assertTrue(items["aaaaaaaaaa2"]["live"])
        self.assertTrue(items["aaaaaaaaaa3"]["members"])
        self.assertEqual(items["aaaaaaaaaa4"]["duration"], 3723)
        self.assertEqual(res["continuation"], "TOKEN1")
        ch = res["channel"]
        self.assertEqual((ch["id"], ch["name"], ch["subscribers"], ch["video_count"]),
                         (CID, "Marques Brownlee", 21400000, 1800))
        self.assertTrue(ch["avatars"])
        self.assertEqual(t.calls[0][2]["X-YouTube-Client-Name"], "1")   # browse = WEB client

    def test_missing_tab_raises(self):
        params = innertube.urllib.parse.unquote(innertube.TAB_PARAMS["videos"])
        self._route({"browse:%s/%s" % (CID, params): _browse([], tab=None)})
        with self.assertRaises(innertube.InnertubeError):
            innertube.channel_tab(CID, "videos")

    def test_shorts_and_continuation(self):
        params = innertube.urllib.parse.unquote(innertube.TAB_PARAMS["shorts"])
        self._route({
            "browse:%s/%s" % (CID, params): _browse([_shorts_item("sssssssss01", "A short")],
                                                    tab="Shorts"),
            "browse:TOKEN1": {"onResponseReceivedActions": [{"appendContinuationItemsAction": {
                "continuationItems": [_lockup("bbbbbbbbbb1", "Older"), _cont_item("TOKEN2")]}}]}})
        sh = innertube.channel_tab(CID, "shorts")["items"]
        self.assertEqual([(i["id"], i["short"], i["title"]) for i in sh],
                         [("sssssssss01", True, "A short")])
        c = innertube.continuation("TOKEN1")
        self.assertEqual([i["id"] for i in c["items"]], ["bbbbbbbbbb1"])
        self.assertEqual(c["continuation"], "TOKEN2")

    def test_resolve_channel(self):
        t = self._route({"navigation/resolve_url": {"endpoint": {"browseEndpoint": {"browseId": CID}}}})
        self.assertEqual(innertube.resolve_channel(CID), CID)                       # no request
        self.assertEqual(innertube.resolve_channel("https://www.youtube.com/channel/%s/videos" % CID), CID)
        self.assertEqual(len(t.calls), 0)
        self.assertEqual(innertube.resolve_channel("https://www.youtube.com/@mkbhd/videos"), CID)
        self.assertEqual(innertube.resolve_channel("@mkbhd"), CID)                  # cached now
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.calls[0][1]["url"], "https://www.youtube.com/@mkbhd")

    def test_player_info_shapes_formats_like_ytdlp(self):
        t = self._route({"visitor_id": {"responseContext": {"visitorData": "VD1"}},
                         "player": _player()})
        info = innertube.player_info("dQw4w9WgXcQ")
        fmts = {f["format_id"]: f for f in info["formats"]}
        self.assertIn("251", fmts)
        self.assertIn("251-drc", fmts)                         # DRC variant named like yt-dlp
        self.assertNotIn("299", fmts)                          # ciphered: yt-dlp's job, not ours
        v = fmts["137"]
        self.assertEqual((v["ext"], v["vcodec"], v["acodec"], v["height"], v["fps"]),
                         ("mp4", "avc1.640028", "none", 1080, 30))
        a = fmts["140"]
        self.assertEqual((a["ext"], a["vcodec"], a["acodec"]), ("m4a", "none", "mp4a.40.2"))
        self.assertEqual(a["format_note"], "medium")
        self.assertTrue(v["http_headers"]["User-Agent"])
        self.assertEqual(info["channel_url"], "https://www.youtube.com/channel/" + CID)
        self.assertEqual([c["start_time"] for c in info["chapters"]], [0, 60, 150])
        self.assertEqual(info["live_status"], "not_live")
        self.assertEqual(list(info["subtitles"]), ["en"])
        self.assertIn("fmt=json3", info["subtitles"]["en"][0]["url"])
        self.assertIn("de", info["automatic_captions"])
        self.assertIn("tlang=de", info["automatic_captions"]["de"][0]["url"])
        self.assertNotIn("tlang", info["automatic_captions"]["en"][0]["url"])   # the real ASR track
        # The visitor id is minted once and rides on the player call.
        self.assertEqual([c[0] for c in t.calls], ["visitor_id", "player"])
        self.assertEqual(t.calls[1][2]["X-Goog-Visitor-Id"], "VD1")
        # ...in the header only: the player body mirrors yt-dlp's (hl + UTC, no gl, html5 pref).
        req = t.calls[1][1]
        self.assertEqual(req["context"]["client"]["clientName"], "VISIONOS")
        self.assertNotIn("gl", req["context"]["client"])
        self.assertNotIn("visitorData", req["context"]["client"])
        self.assertEqual(req["context"]["client"]["timeZone"], "UTC")
        self.assertEqual(req["playbackContext"]["contentPlaybackContext"]["html5Preference"],
                         "HTML5_PREF_WANTS")
        self.assertTrue(req["contentCheckOk"] and req["racyCheckOk"])
        innertube.player_info("dQw4w9WgXcQ")
        self.assertEqual([c[0] for c in t.calls], ["visitor_id", "player", "player"])

    def test_player_info_dubbed_tracks(self):
        self._route({"visitor_id": {"responseContext": {"visitorData": "VD1"}},
                     "player": _player(dubbed=True)})
        fmts = {f["format_id"]: f for f in innertube.player_info("dQw4w9WgXcQ")["formats"]}
        en, ja = fmts["251-en.4"], fmts["251-ja.3"]
        self.assertEqual((en["language"], en["language_preference"]), ("en", 10))
        self.assertEqual((ja["language"], ja["language_preference"]), ("ja", -1))
        self.assertEqual(youfish._pick_audio(list(fmts.values()))["format_id"], "251-en.4")

    def test_bot_wall_is_flagged_and_burns_the_visitor(self):
        self._route({"visitor_id": {"responseContext": {"visitorData": "VD1"}},
                     "player": _player(status="LOGIN_REQUIRED",
                                       reason="Sign in to confirm you're not a bot")})
        with self.assertRaises(innertube.Unplayable) as cm:
            innertube.player_info("dQw4w9WgXcQ")
        self.assertTrue(cm.exception.bot)
        self.assertEqual(innertube._visitor["data"], "")       # re-minted on the next call

    def test_chapters_rules(self):
        f = innertube.chapters_from_description
        self.assertEqual(f("0:00 a\n0:05 b\n1:00 c"), [])           # < 10s apart → not chapters
        self.assertEqual(f("1:00 a\n2:00 b\n3:00 c"), [])           # must start at 0:00
        self.assertEqual(f("0:00 a\n1:00 b"), [])                   # need at least three
        self.assertEqual([c["title"] for c in f("Intro - 0:00\nMiddle - 2:00\nEnd - 1:00:00")],
                         ["Intro", "Middle", "End"])
        # an emoji + VS16 + "(" before the digits (freeCodeCamp's "⌨️ (0:00) Introduction")
        self.assertEqual([c["start_time"] for c in f("⌨️ (0:00) Intro\n⌨️ (1:45) Setup\n"
                                                      "⌨️ (6:40) Hello")], [0, 105, 400])

    def test_configure_from_ytdlp_follows_jsless_default(self):
        class Policy:
            def __init__(self, required):
                self.required = required
        base = types.ModuleType("yt_dlp.extractor.youtube._base")
        base.INNERTUBE_CLIENTS = {
            "web": {"INNERTUBE_CONTEXT": {"client": {"clientName": "WEB",
                                                     "clientVersion": "2.20990101.00.00"}}},
            "needs_js": {"REQUIRE_JS_PLAYER": True},
            "needs_pot": {"REQUIRE_JS_PLAYER": False, "INNERTUBE_CONTEXT_CLIENT_NAME": 5,
                          "GVS_PO_TOKEN_POLICY": {"https": Policy(True)},
                          "INNERTUBE_CONTEXT": {"client": {"clientName": "X", "clientVersion": "1",
                                                           "userAgent": "UA-X"}}},
            "newclient": {"REQUIRE_JS_PLAYER": False, "INNERTUBE_CONTEXT_CLIENT_NAME": 77,
                          "GVS_PO_TOKEN_POLICY": {"https": Policy(False)},
                          "INNERTUBE_CONTEXT": {"client": {"clientName": "NEWCLIENT",
                                                           "clientVersion": "9.9",
                                                           "userAgent": "UA-NEW", "hl": "en"}}},
        }
        video = types.ModuleType("yt_dlp.extractor.youtube._video")
        video.YoutubeIE = type("YoutubeIE", (), {
            "_DEFAULT_JSLESS_CLIENTS": ("needs_js", "needs_pot", "newclient")})
        names = ["yt_dlp", "yt_dlp.extractor", "yt_dlp.extractor.youtube",
                 "yt_dlp.extractor.youtube._base", "yt_dlp.extractor.youtube._video"]
        saved_mods = {n: sys.modules.get(n) for n in names}
        saved_player, saved_web = dict(innertube._PLAYER), dict(innertube._WEB)
        pkg = types.ModuleType("yt_dlp.extractor.youtube")
        pkg._base, pkg._video = base, video
        for n, m in zip(names, [types.ModuleType("yt_dlp"), types.ModuleType("yt_dlp.extractor"),
                                pkg, base, video]):
            sys.modules[n] = m
        try:
            innertube._visitor.update({"data": "OLD", "ts": time.time()})
            self.assertEqual(innertube.configure_from_ytdlp(None), "newclient")
            self.assertEqual(innertube.player_client(), ("newclient", "NEWCLIENT", "UA-NEW"))
            self.assertEqual(innertube._WEB["clientVersion"], "2.20990101.00.00")
            self.assertEqual(innertube._visitor["data"], "")     # new client → new visitor id
        finally:
            for n, m in saved_mods.items():
                if m is None:
                    sys.modules.pop(n, None)
                else:
                    sys.modules[n] = m
            innertube._PLAYER.clear(); innertube._PLAYER.update(saved_player)
            innertube._WEB.clear(); innertube._WEB.update(saved_web)


class _FakeIT:
    """A stand-in innertube module for the youfish wiring tests."""
    InnertubeError = innertube.InnertubeError

    def __init__(self):
        self.player = None           # dict or Exception
        self.calls = []

    def player_client(self):
        return ("visionos", "VISIONOS", "UA-V")

    def player_info(self, vid):
        self.calls.append(("player", vid))
        if isinstance(self.player, Exception):
            raise self.player
        return json.loads(json.dumps(self.player))

    def visitor_data(self):
        return "V"


class InnertubeEngineWiring(unittest.TestCase):
    """The fast path wired into the audio-only resolve + 403 self-heal (all offline)."""

    def setUp(self):
        self._saved = {n: getattr(youfish, n) for n in (
            "_it", "_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args", "_yt_extractor_args",
            "_proxied", "get_settings", "_import_yt_dlp", "_data_dir", "_pot_active",
            "_ytdlp_formats", "_url_cache", "_probe_status")}
        self._run = youfish.subprocess.run
        self._tmp = tempfile.mkdtemp(prefix="it-")
        self.fake = _FakeIT()
        youfish._it = lambda: self.fake
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {}
        youfish._import_yt_dlp = lambda: None
        youfish._data_dir = lambda: self._tmp
        youfish._url_cache = {}
        youfish._fast_player_state.update({"bot_fails": 0, "off_until": 0.0, "strikes": []})
        self.probed = []

        def probe(url, ua, timeout=3):
            self.probed.append(url)
            return 206
        youfish._probe_status = probe
        youfish.invalidate_resolve_cache()
        self.spawns = []

        def no_spawn(cmd, **kw):
            self.spawns.append(cmd)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="spawned in a fast-path test")
        youfish.subprocess.run = no_spawn

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run
        youfish._fast_player_state.update({"bot_fails": 0, "off_until": 0.0, "strikes": []})
        youfish.invalidate_resolve_cache()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _fast_data(self, loudness=None, **kw):
        pr = _player(**kw)
        if loudness:
            pr["playerConfig"] = {"audioConfig": loudness}
        innertube._transport = _FakeTransport({"visitor_id": {"responseContext": {"visitorData": "V"}},
                                               "player": pr})
        try:
            innertube._visitor.update({"data": "", "ts": 0.0})
            return innertube.player_info("dQw4w9WgXcQ")
        finally:
            innertube._transport = _REAL_TRANSPORT

    def _ytdlp_run(self, cmd, **kw):
        self.spawns.append(cmd)
        data = {"title": "Y", "duration": 10,
                "formats": [af("251", 160, "opus", url="https://yt/251")]}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

    def test_resolve_uses_fast_path_without_ytdlp(self):
        self.fake.player = self._fast_data()
        res = youfish.resolve("dQw4w9WgXcQ")
        self.assertTrue(res.get("ok"), res)
        info = res["info"]
        self.assertEqual(info["audio_itag"], "251")              # opus, non-DRC wins the tie
        self.assertEqual(info["title"], "Never Gonna")
        self.assertTrue(info["audio_urls"])
        self.assertEqual(self.spawns, [])                        # no yt-dlp at all
        self.assertEqual(len(self.probed), 1)                    # ONLY the audio URL was probed
        self.assertIn("itag=251", self.probed[0])

    def test_resolve_without_ytdlp_binary_still_works_on_fast_path(self):
        youfish._ytdlp_path = lambda: ""
        self.fake.player = self._fast_data()
        self.assertTrue(youfish.resolve("dQw4w9WgXcQ").get("ok"))

    def test_loudness_carried_from_perceptual_and_target(self):
        self.fake.player = self._fast_data(loudness={"perceptualLoudnessDb": -8.29,
                                                     "loudnessTargetLkfs": -14})
        info = youfish.resolve("dQw4w9WgXcQ")["info"]
        self.assertAlmostEqual(info["loudness_db"], 5.71, places=2)

    def test_loudness_prefers_explicit_db_and_is_none_when_absent(self):
        self.assertEqual(youfish._loudness_db({"loudness": {"db": 0.99, "perceptual_db": -9,
                                                            "target_lkfs": -14}}), 0.99)
        self.assertIsNone(youfish._loudness_db({"loudness": {"db": None, "perceptual_db": None,
                                                             "target_lkfs": None}}))
        self.assertIsNone(youfish._loudness_db({}))

    def test_ytdlp_resolve_has_no_loudness(self):
        self.fake.player = innertube.Unplayable("nope")
        youfish.subprocess.run = self._ytdlp_run
        info = youfish.resolve("dQw4w9WgXcQ")["info"]
        self.assertIsNone(info["loudness_db"])

    def test_refused_fast_urls_fall_back_and_trip_the_breaker(self):
        self.fake.player = self._fast_data()
        youfish._probe_status = lambda url, ua, timeout=3: 403
        youfish.subprocess.run = self._ytdlp_run
        res = youfish.resolve("dQw4w9WgXcQ")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "Y")               # yt-dlp's answer, not the fast one
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(len(youfish._fast_player_state["strikes"]), 1)
        youfish.invalidate_resolve_cache()
        youfish.resolve("dQw4w9WgXcQ")                            # second refusal opens the breaker
        self.assertGreater(youfish._fast_player_state["off_until"], time.time())
        n = len([c for c in self.fake.calls if c[0] == "player"])
        youfish.invalidate_resolve_cache()
        youfish.resolve("dQw4w9WgXcQ")
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "player"]), n)   # not asked

    def test_strikes_expire_outside_the_window(self):
        youfish._fast_player_state["strikes"] = [time.time() - youfish._FAST_URL_WINDOW - 5]
        youfish._fast_strike("x")
        self.assertEqual(len(youfish._fast_player_state["strikes"]), 1)
        self.assertEqual(youfish._fast_player_state["off_until"], 0.0)

    def test_network_failure_in_probe_is_no_verdict(self):
        self.fake.player = self._fast_data()
        youfish._probe_status = lambda url, ua, timeout=3: None
        self.assertIsNotNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        self.assertEqual(youfish._fast_player_state["strikes"], [])

    def test_resolve_falls_back_when_fast_path_fails(self):
        self.fake.player = innertube.Unplayable("Sign in to confirm your age")
        youfish.subprocess.run = self._ytdlp_run
        res = youfish.resolve("dQw4w9WgXcQ")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "Y")
        self.assertEqual(len(self.spawns), 1)

    def test_live_and_user_client_skip_fast_path(self):
        self.fake.player = self._fast_data(live=True)
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))     # live -> yt-dlp's path
        self.fake.player = self._fast_data()
        youfish.get_settings = lambda: {"player_client": "tv"}
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))     # explicit client honoured
        self.assertEqual([c for c in self.fake.calls if c[0] == "player"], [("player", "dQw4w9WgXcQ")])

    def test_no_audio_format_falls_back(self):
        d = self._fast_data()
        d["formats"] = [f for f in d["formats"] if f.get("vcodec") not in (None, "none")]
        self.fake.player = d
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))

    def test_bot_wall_trips_the_circuit_breaker(self):
        self.fake.player = innertube.Unplayable("Sign in to confirm you're not a bot", bot=True)
        for _ in range(youfish._FAST_PLAYER_BOT_LIMIT):
            self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        n = len(self.fake.calls)
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        self.assertEqual(len(self.fake.calls), n)                        # breaker open: no request

    def test_reresolve_asks_the_fast_client_first(self):
        self.fake.player = self._fast_data()
        youfish._ytdlp_formats = lambda vid, anon=False: self.fail("yt-dlp re-resolve not needed")
        fresh = youfish._reresolve("dQw4w9WgXcQ", "251",
                                   "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&old=1")
        self.assertIn("itag=251", fresh)

    def test_reresolve_refused_fast_url_goes_to_ytdlp(self):
        self.fake.player = self._fast_data()
        youfish._probe_status = lambda url, ua, timeout=3: 403
        youfish._ytdlp_formats = lambda vid, anon=False: {"251": "https://new/251"}
        fresh = youfish._reresolve("dQw4w9WgXcQ", "251",
                                   "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&old=1")
        self.assertEqual(fresh, "https://new/251")
        self.assertEqual(len(youfish._fast_player_state["strikes"]), 1)

    def test_reresolve_after_a_refusal_skips_the_fast_client(self):
        self.fake.player = self._fast_data()
        old = "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&old=1"
        youfish._url_cache = {"dQw4w9WgXcQ": {"ts": time.time(), "fmts": {
            "251": "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&cached=1"}}}
        youfish._ytdlp_formats = lambda vid, anon=False: {"251": "https://new/251"}
        fresh = youfish._reresolve("dQw4w9WgXcQ", "251", old, refused=True)
        self.assertEqual(fresh, "https://new/251")
        self.assertEqual([c for c in self.fake.calls if c[0] == "player"], [])

    def test_reresolve_finds_a_fast_path_dub_track_under_ytdlp_naming(self):
        # The fast path calls a dub "251-es.3"; yt-dlp numbers it "251-8" and keys it "251@es".
        youfish._ytdlp_formats = lambda vid, anon=False: {"251-8": "https://new/es",
                                                          "251@es": "https://new/es"}
        old = "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&old=1"
        self.assertEqual(youfish._reresolve("dQw4w9WgXcQ", "251-es.3", old, refused=True),
                         "https://new/es")
        self.assertEqual(youfish._track_alias("251-drc-en-US.4"), "251-drc@en-US")
        self.assertIsNone(youfish._track_alias("251-8"))

    def test_drop_resolved_forgets_only_that_video(self):
        youfish._resolve_cache.update({
            "aaaaaaaaaaa\x1fvisionos\x1f0": {"payload": {}, "good_until": time.time() + 99, "ts": 0},
            "aaaaaaaaaaa\x1fvisionos\x1f1": {"payload": {}, "good_until": time.time() + 99, "ts": 0},
            "bbbbbbbbbbb\x1fvisionos\x1f0": {"payload": {}, "good_until": time.time() + 99, "ts": 0}})
        youfish._drop_resolved("aaaaaaaaaaa")
        self.assertEqual(list(youfish._resolve_cache), ["bbbbbbbbbbb\x1fvisionos\x1f0"])

    def test_reresolve_foreign_client_goes_to_ytdlp(self):
        youfish._ytdlp_formats = lambda vid, anon=False: {"251": "https://new/251"}
        fresh = youfish._reresolve("dQw4w9WgXcQ", "251", "https://rr1.googlevideo.com/x?c=TVHTML5&itag=251")
        self.assertEqual(fresh, "https://new/251")
        self.assertEqual(self.fake.calls, [])

    def test_token_free_client_follows_innertube_not_tv_embedded(self):
        self.assertEqual(youfish._token_free_client(), "visionos")
        youfish._it = lambda: None
        self.assertEqual(youfish._token_free_client(), "visionos")
        youfish._pot_active = lambda: True
        self.assertEqual(youfish._default_client(), "visionos")


class YtmPlaylistContinuation(unittest.TestCase):
    """ytm.get_playlist follows the shelf's continuation token up to `limit`."""

    @staticmethod
    def _row(vid):
        return {"musicResponsiveListItemRenderer": {
            "playlistItemData": {"videoId": vid},
            "flexColumns": [{"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                {"text": "Song " + vid}]}}}]}}

    @classmethod
    def _tail(cls, items, token):
        if token:
            items.append({"continuationItemRenderer": {"continuationEndpoint": {
                "continuationCommand": {"token": token}}}})
        return items

    def _first(self, n, token):
        items = self._tail([self._row("a%010d" % i) for i in range(n)], token)
        return {"contents": {"twoColumnBrowseResultsRenderer": {"secondaryContents": {
            "sectionListRenderer": {
                "contents": [{"musicPlaylistShelfRenderer": {"contents": items}}],
                # the section list's own token (the "related" area) must NOT be followed
                "continuations": [{"nextContinuationData": {"continuation": "RELATED"}}]}}}}}

    def _page(self, prefix, n, token):
        items = self._tail([self._row("%s%010d" % (prefix, i)) for i in range(n)], token)
        return {"onResponseReceivedActions": [{"appendContinuationItemsAction": {
            "continuationItems": items}}]}

    def setUp(self):
        import ytm
        self.ytm = ytm
        self._orig = ytm._innertube
        self.calls = []
        pages = {"BID": self._first(3, "T1"), "T1": self._page("b", 3, "T2"),
                 "T2": self._page("c", 2, None)}

        def fake(endpoint, body, **kw):
            key = body.get("continuation") or body.get("browseId")
            self.calls.append(key)
            if key not in pages:
                raise RuntimeError("unexpected " + key)
            return pages[key]
        self.pages = pages
        ytm._innertube = fake

    def tearDown(self):
        self.ytm._innertube = self._orig

    def test_follows_continuations_to_the_end(self):
        res = self.ytm.get_playlist("BID", limit=100)
        self.assertEqual(len(res["tracks"]), 8)
        self.assertEqual(self.calls, ["BID", "T1", "T2"])        # never the RELATED token

    def test_stops_at_limit(self):
        res = self.ytm.get_playlist("BID", limit=5)
        self.assertEqual(len(res["tracks"]), 5)
        self.assertEqual(self.calls, ["BID", "T1"])

    def test_continuation_error_keeps_what_we_have(self):
        base = self.ytm._innertube

        def flaky(endpoint, body, **kw):
            if body.get("continuation") == "T2":
                raise RuntimeError("boom")
            return base(endpoint, body, **kw)
        self.ytm._innertube = flaky
        res = self.ytm.get_playlist("BID", limit=100)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["tracks"]), 6)

    def test_token_loop_is_cut(self):
        self.pages["BID"] = self._first(2, "T9")
        self.pages["T9"] = self._page("d", 2, "T9")               # points at itself
        res = self.ytm.get_playlist("BID", limit=10000)
        self.assertEqual(len(res["tracks"]), 4)                   # 2 + 2, then a page adding nothing


class YtmSweepFixes(unittest.TestCase):
    """Search's top-result card, instrumental lyrics, podcast episodes and logout (bug sweep)."""

    def setUp(self):
        import ytm
        self.ytm = ytm
        self._orig = ytm._innertube
        self._tmp = tempfile.mkdtemp(prefix="ytmsw-")
        self._dd = ytm._data_dir
        ytm._data_dir = lambda: self._tmp

    def tearDown(self):
        self.ytm._innertube = self._orig
        self.ytm._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_search_leads_with_the_top_result_card(self):
        card = {"musicCardShelfRenderer": {
            "title": {"runs": [{"text": "Radiohead", "navigationEndpoint": {"browseEndpoint": {
                "browseId": "UCr_iyUANcn9OX_yy9piYoLw",
                "browseEndpointContextSupportedConfigs": {"browseEndpointContextMusicConfig": {
                    "pageType": "MUSIC_PAGE_TYPE_ARTIST"}}}}}]},
            "subtitle": {"runs": [{"text": "Artist"}]},
            "contents": [YtmPlaylistContinuation._row("songsong001")]}}
        self.ytm._innertube = lambda endpoint, body, **kw: {"contents": [card]}
        items = self.ytm.search("radiohead")["items"]
        self.assertEqual([(i["kind"], i.get("browseId") or i.get("videoId")) for i in items],
                         [("artist", "UCr_iyUANcn9OX_yy9piYoLw"), ("song", "songsong001")])

    def test_instrumental_is_an_answer(self):
        r = self.ytm._lyrics_result({"instrumental": True})
        self.assertTrue(r["ok"])
        self.assertTrue(r["instrumental"])
        self.assertFalse(self.ytm._lyrics_result({})["ok"])

    def test_podcast_page_lists_its_episodes(self):
        ep = {"musicMultiRowListItemRenderer": {
            "onTap": {"watchEndpoint": {"videoId": "episode0001"}},
            "title": {"runs": [{"text": "Episode 1"}]},
            "subtitle": {"runs": [{"text": "46K views"}, {"text": " \u2022 "}, {"text": "20h ago"}]},
            "playbackProgress": {"musicPlaybackProgressRenderer": {
                "durationText": {"runs": [{"text": " \u2022 "}, {"text": "30 min"}]}}}}}
        self.ytm._innertube = lambda endpoint, body, **kw: {"contents": [{"musicShelfRenderer": {
            "contents": [ep]}}]}
        tracks = self.ytm.get_playlist("MPSPPLsomething")["tracks"]
        self.assertEqual([(t["videoId"], t["title"], t["subtitle"]) for t in tracks],
                         [("episode0001", "Episode 1", "46K views \u2022 20h ago \u2022 30 min")])

    def test_logout_drops_the_personalised_home_cache(self):
        for p in (self.ytm._home_cache_path(), self.ytm._cookies_path()):
            with open(p, "w") as f:
                f.write("{}")
        self.ytm.logout()
        self.assertFalse(os.path.exists(self.ytm._home_cache_path()))
        self.assertFalse(os.path.exists(self.ytm._cookies_path()))

    def test_a_token_refresh_racing_a_logout_writes_nothing_back(self):
        self.ytm._save_tokens({"refresh_token": "R", "access_token": "old"})
        self.ytm.logout()
        self.ytm._save_tokens({"refresh_token": "R", "access_token": "new"}, only_if_current=True)
        self.assertFalse(os.path.exists(self.ytm._tokens_path()))


class YtmPooledTransport(unittest.TestCase):
    """ytm._innertube on innertube.request (the keep-alive pool): same answers, same HTTPError on a
    refusal, and Set-Cookie rotations still reach the store."""

    def setUp(self):
        import ytm
        import http.client
        self.ytm = ytm
        self.saved = dict((n, getattr(ytm, n)) for n in (
            "_ytm_config", "_context", "_auth_mode", "_load_cookies", "_selected_identity",
            "_visitor_id_cached", "_persist_rotations", "_warm_ytm_config", "_log"))
        self.logged = []
        ytm._log = lambda m: self.logged.append(m)
        self.saved_req = innertube.request
        ytm._ytm_config = lambda: {"key": "K"}
        ytm._context = lambda: {"client": {}}
        ytm._auth_mode = lambda: "cookie"
        ytm._load_cookies = lambda: {"cookie": "SID=a", "sapisid": "s"}
        ytm._selected_identity = lambda: {"authuser": "0", "page_id": ""}
        ytm._visitor_id_cached = lambda: "V"
        ytm._warm_ytm_config = lambda: None
        self.rotated = []
        ytm._persist_rotations = lambda u: self.rotated.append(u)
        self.calls = []
        self.reply = (200, b'{"ok": 1}', http.client.HTTPMessage())

        def fake(method, url, body=None, headers=None, timeout=10):
            self.calls.append((method, url, json.loads(body.decode()), headers))
            return self.reply
        innertube.request = fake

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(self.ytm, n, v)
        innertube.request = self.saved_req

    def test_answer_comes_from_the_pool(self):
        self.assertEqual(self.ytm._innertube("browse", {"browseId": "FEmusic_home"}), {"ok": 1})
        method, url, payload, headers = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/youtubei/v1/browse?key=K", url)
        self.assertEqual(payload["browseId"], "FEmusic_home")
        self.assertTrue(headers["Authorization"].startswith("SAPISIDHASH"))
        self.assertEqual(self.rotated, [])

    def test_refusal_raises_the_same_http_error(self):
        import http.client
        import urllib.error
        h = http.client.HTTPMessage()
        h["Content-Encoding"] = "gzip"            # the pool already gunzipped the body
        self.reply = (401, b'{"error": "nope"}', h)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.ytm._innertube("browse", {})
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(str(cm.exception), "HTTP Error 401: Unauthorized")
        # The handler logs the error body (as before): plain text, not a failed gunzip.
        self.assertIn('{"error": "nope"}', " ".join(self.logged))

    def test_rotated_cookies_are_absorbed(self):
        import http.client
        h = http.client.HTTPMessage()
        h["Set-Cookie"] = "SIDCC=new1; Domain=.youtube.com; Path=/; Secure"
        h["Set-Cookie"] = "__Secure-3PSIDCC=new2; Domain=.youtube.com; Path=/; Secure"
        self.reply = (200, b"{}", h)
        self.ytm._innertube("next", {})
        self.assertEqual(self.rotated, [{"SIDCC": "new1", "__Secure-3PSIDCC": "new2"}])

    def test_falls_back_to_urllib_without_the_pool(self):
        self.ytm_pool = self.ytm._pool
        self.ytm._pool = lambda: None
        orig = self.ytm.urllib.request.urlopen
        self.ytm.urllib.request.urlopen = lambda *a, **k: YtmIdentity._Resp('{"via": "urllib"}')
        try:
            self.assertEqual(self.ytm._innertube("browse", {}), {"via": "urllib"})
        finally:
            self.ytm.urllib.request.urlopen = orig
            self.ytm._pool = self.ytm_pool
        self.assertEqual(self.calls, [])


class YtmAtomicStores(unittest.TestCase):
    def setUp(self):
        import ytm
        self.ytm = ytm
        self._tmp = tempfile.mkdtemp(prefix="ytm-")
        self._dd = ytm._data_dir
        ytm._data_dir = lambda: self._tmp

    def tearDown(self):
        self.ytm._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _load(p):
        with open(p) as f:
            return json.load(f)

    def test_atomic_write_is_private_and_leaves_no_temp(self):
        p = os.path.join(self._tmp, "x.json")
        self.ytm._atomic_write_json(p, {"a": 1})
        self.assertEqual(self._load(p), {"a": 1})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(self._tmp), ["x.json"])

    def test_failed_write_keeps_the_old_file(self):
        p = os.path.join(self._tmp, "x.json")
        self.ytm._atomic_write_json(p, {"a": 1})
        with self.assertRaises(TypeError):
            self.ytm._atomic_write_json(p, {"a": object()})
        self.assertEqual(self._load(p), {"a": 1})
        self.assertEqual(os.listdir(self._tmp), ["x.json"])

    def test_play_history_dedups_and_survives_concurrent_writers(self):
        ts = [threading.Thread(target=self.ytm.record_play, args=("v%010d" % i, "T")) for i in range(30)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.ytm.record_play("v0000000003", "again")
        ids = [h["videoId"] for h in self.ytm.play_history(1000)]
        self.assertEqual(len(ids), 30)                            # no lost updates
        self.assertEqual(ids[0], "v0000000003")                   # re-play moved to the front
        self.assertEqual([f for f in os.listdir(self._tmp) if f.startswith(".tmp-")], [])

    def test_lyrics_cache_roundtrip_is_private(self):
        orig = self.ytm.get_lyrics
        self.ytm.get_lyrics = lambda *a, **k: {"ok": True, "synced": [{"t": 1, "line": "x"}],
                                               "plain": "x"}
        try:
            self.ytm.cache_lyrics("abc", "t", "a")
        finally:
            self.ytm.get_lyrics = orig
        self.assertTrue(self.ytm.cached_lyrics("abc")["ok"])
        self.assertEqual(os.stat(os.path.join(self._tmp, "lyrics", "abc.json")).st_mode & 0o777, 0o600)


class YtdlpVersionCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ver-")
        self.bin = os.path.join(self._tmp, "yt-dlp")
        with open(self.bin, "w") as f:
            f.write("x")
        self._saved = (youfish._ytdlp_path, youfish._data_dir, youfish.subprocess.run)
        youfish._ytdlp_path = lambda: self.bin
        youfish._data_dir = lambda: self._tmp
        self.runs = 0

        def run(cmd, **kw):
            self.runs += 1
            return types.SimpleNamespace(returncode=0, stdout="2026.08.19\n", stderr="")
        youfish.subprocess.run = run

    def tearDown(self):
        youfish._ytdlp_path, youfish._data_dir, youfish.subprocess.run = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_version_remembered_per_binary(self):
        self.assertEqual(youfish.ytdlp_version(), "2026.08.19")
        self.assertEqual(youfish.ytdlp_version(), "2026.08.19")
        self.assertEqual(self.runs, 1)                        # second launch: no spawn
        youfish.ytdlp_version(fresh=True)
        self.assertEqual(self.runs, 2)                        # install proves the binary runs
        with open(self.bin, "w") as f:
            f.write("updated binary")                         # an update changes the stat
        youfish.ytdlp_version()
        self.assertEqual(self.runs, 3)


class SignedInClientArgs(unittest.TestCase):
    """yt-dlp drops clients that don't support cookies (visionos among them) and fails with none
    left, so a signed-in run must not name the automatic token-free client."""

    def setUp(self):
        self._saved = {n: getattr(youfish, n) for n in (
            "get_settings", "_pot_active", "_write_cookies_temp", "_ytdlp_path",
            "_ensure_pot_server", "_pot_ytdlp_args", "_set_pdeathsig")}
        self._run = youfish.subprocess.run
        self.settings = {}
        youfish.get_settings = lambda: self.settings
        youfish._pot_active = lambda: True
        self._tmp = tempfile.mkdtemp(prefix="authed-")

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_signed_in_run_leaves_the_client_to_ytdlp(self):
        self.assertIn("player_client=visionos", youfish._yt_extractor_args(want_pot=True)[1])
        args = youfish._yt_extractor_args(want_pot=True, cookies=True)
        self.assertEqual(args, ["--extractor-args", "youtube:fetch_pot=always"])

    def test_user_client_and_override_still_win_when_signed_in(self):
        self.assertIn("player_client=mweb",
                      youfish._yt_extractor_args(client_override="mweb", cookies=True)[1])
        self.settings = {"player_client": "tv"}
        self.assertIn("player_client=tv", youfish._yt_extractor_args(cookies=True)[1])

    def test_cookied_reresolve_dump_names_no_client(self):
        ck = os.path.join(self._tmp, "c.txt")
        youfish._write_cookies_temp = lambda: open(ck, "w").close() or ck
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._set_pdeathsig = lambda: None
        cmds = []

        def run(cmd, **kw):
            cmds.append(cmd)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        youfish.subprocess.run = run
        youfish._ytdlp_formats("dQw4w9WgXcQ")
        self.assertIn("--cookies", cmds[0])
        self.assertFalse([a for a in cmds[0] if "player_client=" in a], cmds[0])

class Arm32Userland(unittest.TestCase):
    """32-bit ARM: yt-dlp is the stable one-directory armv7l zip, unpacked behind a bin/yt-dlp
    symlink and updated by downloading again; Deno and the PO-token provider are refused."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.saved = dict((n, getattr(youfish, n)) for n in (
            "_data_dir", "_ARM32", "_YTDLP_ASSET", "ytdlp_version", "_ytdlp_latest_tag",
            "_install_ytdlp_sync", "get_settings"))
        youfish._data_dir = lambda: self._tmp
        youfish._ARM32 = True
        youfish._YTDLP_ASSET = "yt-dlp_linux_armv7l.zip"
        os.makedirs(os.path.join(self._tmp, "bin"))
        self.sent = []
        self.prev_po = sys.modules.get("pyotherside")
        sys.modules["pyotherside"] = types.SimpleNamespace(send=lambda *a: self.sent.append(a))

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(youfish, n, v)
        if self.prev_po is not None:
            sys.modules["pyotherside"] = self.prev_po
        else:
            sys.modules.pop("pyotherside", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _zip(self, version, extra=None):
        import zipfile
        path = os.path.join(self._tmp, "y.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("yt-dlp_linux_armv7l", "#!/bin/sh\necho %s\n" % version)
            z.writestr("_internal/base_library.zip", version)
            for name in (extra or []):
                z.writestr(name, "x")
        return path

    def test_unpack_points_bin_ytdlp_at_the_tree(self):
        youfish._unpack_ytdlp_dir(self._zip("2026.01.01"))
        exe = youfish._managed_ytdlp()
        self.assertTrue(os.path.islink(exe))
        self.assertTrue(os.access(exe, os.X_OK))
        self.assertEqual(os.readlink(exe), os.path.join("yt-dlp-armv7l", "yt-dlp_linux_armv7l"))
        out = youfish.subprocess.run([exe], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "2026.01.01")

    def test_reinstall_swaps_and_parks_the_old_tree(self):
        youfish._unpack_ytdlp_dir(self._zip("old"))
        youfish._unpack_ytdlp_dir(self._zip("new"))
        bindir = os.path.join(self._tmp, "bin")
        with open(os.path.join(bindir, "yt-dlp-armv7l", "_internal", "base_library.zip")) as f:
            self.assertEqual(f.read(), "new")
        with open(os.path.join(bindir, "yt-dlp-armv7l.old", "_internal", "base_library.zip")) as f:
            self.assertEqual(f.read(), "old")
        self.assertFalse(os.path.exists(os.path.join(bindir, "yt-dlp-armv7l.new")))

    def test_unsafe_archive_is_refused_and_leaves_the_install(self):
        youfish._unpack_ytdlp_dir(self._zip("good"))
        with self.assertRaises(ValueError):
            youfish._unpack_ytdlp_dir(self._zip("evil", extra=["../../escape"]))
        self.assertFalse(os.path.exists(os.path.join(self._tmp, "escape")))
        out = youfish.subprocess.run([youfish._managed_ytdlp()], capture_output=True,
                                     text=True).stdout.strip()
        self.assertEqual(out, "good")

    def test_channel_is_pinned_to_stable(self):
        youfish.get_settings = lambda: {"ytdlp_channel": "nightly"}
        self.assertEqual(youfish._ytdlp_channel(), "stable")
        youfish._ARM32 = False
        self.assertEqual(youfish._ytdlp_channel(), "nightly")

    def test_update_downloads_only_when_behind(self):
        calls = []
        youfish._install_ytdlp_sync = lambda progress=None: (calls.append(1) or
                                                             (True, "Installed yt-dlp 2", "2"))
        youfish.ytdlp_version = lambda fresh=False: "1"
        youfish._ytdlp_latest_tag = lambda: "1"
        self.assertTrue(youfish._ytdlp_update_by_download()["ok"])
        self.assertEqual(calls, [])                       # up to date: nothing fetched
        youfish._ytdlp_latest_tag = lambda: ""
        res = youfish._ytdlp_update_by_download()
        self.assertFalse(res["ok"])                       # can't tell: don't pull 40 MB blind
        self.assertEqual(calls, [])
        youfish._ytdlp_latest_tag = lambda: "2"
        res = youfish._ytdlp_update_by_download()
        self.assertEqual((res["ok"], res["version"], calls), (True, "2", [1]))

    def test_deno_and_provider_are_refused(self):
        self.assertFalse(youfish.install_deno()["ok"])
        self.assertFalse(youfish.install_pot_provider()["ok"])
        self.assertEqual([e[0] for e in self.sent], ["deno_install_done", "pot_install_done"])
        self.assertTrue(all(e[1] is False for e in self.sent))



# --- Release-safety net: features that had no offline test (see TESTING.md) ------------------ #

class _TmpDataDir(unittest.TestCase):
    """Base: every engine and ytm store lands in a throwaway data dir."""
    def setUp(self):
        import ytm
        self.ytm = ytm
        self._tmp = tempfile.mkdtemp(prefix="stores-")
        self._dd, self._ydd = youfish._data_dir, ytm._data_dir
        youfish._data_dir = ytm._data_dir = lambda: self._tmp

    def tearDown(self):
        youfish._data_dir, self.ytm._data_dir = self._dd, self._ydd
        shutil.rmtree(self._tmp, ignore_errors=True)


def _qml_defaults(path):
    """(key, literal) pairs for every `s.key || <literal>` and `s.key === undefined) ? <literal>`
    fallback in a QML file."""
    qml = open(path).read()
    pairs = re.findall(r"=\s*s\.(\w+)\s*\|\|\s*(\"[^\"]*\"|[\d.]+)", qml)
    pairs += re.findall(r"\(s\.(\w+)\s*===\s*undefined\)\s*\?\s*(\"[^\"]*\"|[\d.]+|true|false)", qml)
    return pairs


class SettingsStore(_TmpDataDir):
    QML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "qml", "Backend.qml")

    def test_defaults_then_persist(self):
        self.assertIs(youfish.get_settings()["autoplay"], True)
        youfish.set_setting("autoplay", False)
        self.assertIs(youfish.get_settings()["autoplay"], False)
        self.assertEqual(oct(os.stat(youfish._settings_path()).st_mode & 0o777), "0o600")

    def test_output_settings_drop_cached_resolves_others_dont(self):
        calls = []
        saved = youfish.invalidate_resolve_cache
        youfish.invalidate_resolve_cache = lambda: calls.append(1)
        try:
            for key in youfish._RESOLVE_OUTPUT_KEYS:
                youfish.set_setting(key, "x")
            self.assertEqual(len(calls), len(youfish._RESOLVE_OUTPUT_KEYS))
            for key in ("normalize_volume", "autoplay", "sponsorblock", "eq_enabled"):
                youfish.set_setting(key, True)
            self.assertEqual(len(calls), len(youfish._RESOLVE_OUTPUT_KEYS))
        finally:
            youfish.invalidate_resolve_cache = saved

    def test_corrupt_settings_file_falls_back_to_defaults(self):
        with open(youfish._settings_path(), "w") as f:
            f.write("{not json")
        self.assertEqual(youfish.get_settings()["norm_max_boost_db"], 12.0)

    def test_qml_fallbacks_match_engine_defaults(self):
        # Backend.qml repeats some defaults; a drift means the UI shows one value while the engine
        # uses another.
        found = _qml_defaults(self.QML)
        self.assertTrue(found)
        for key, lit in found:
            if key not in youfish._SETTINGS_DEFAULTS:
                continue
            want = youfish._SETTINGS_DEFAULTS[key]
            if want in ("", 0, None) and want is not False:   # `|| ""` just guards a missing key
                continue
            self.assertEqual(json.loads(lit), want, "Backend.qml fallback for %s" % key)

    def test_qml_sponsorblock_actions_match_engine_default(self):
        qml = open(self.QML).read()
        m = re.search(r"property var\s+sbActions:\s*\((\{[^)]*\})\)", qml)
        self.assertTrue(m)
        self.assertEqual(json.loads(m.group(1)), youfish._SETTINGS_DEFAULTS["sponsorblock_actions"])


class SponsorBlockCategoriesMatchUI(unittest.TestCase):
    """Every category the engine fetches has a row on the SponsorBlock page, and vice versa: a
    fetched category without a row could never be switched on."""
    def test_same_set(self):
        page = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "qml", "pages",
                                 "SponsorBlockPage.qml")).read()
        ui = re.findall(r'key:\s*"(\w+)"', page)
        self.assertEqual(sorted(ui), sorted(json.loads(youfish._SB_CATEGORIES)))

class ResolveKeyInputs(_TmpDataDir):
    """Invariant: every input that changes resolve's answer is part of the cache key."""
    def test_client_token_and_login_change_the_key(self):
        saved = {n: getattr(youfish, n) for n in ("_default_client", "_pot_active", "_signed_in")}
        try:
            youfish._default_client = lambda: ""
            youfish._pot_active = lambda: False
            youfish._signed_in = lambda: False
            keys = {youfish._resolve_key("vid")}
            youfish._default_client = lambda: "tv"
            keys.add(youfish._resolve_key("vid"))
            youfish._pot_active = lambda: True
            keys.add(youfish._resolve_key("vid"))
            youfish._signed_in = lambda: True
            keys.add(youfish._resolve_key("vid"))
            self.assertEqual(len(keys), 4)
            youfish.set_setting("autoplay", False)                 # UI taste: same key
            self.assertIn(youfish._resolve_key("vid"), keys)
        finally:
            for n, v in saved.items():
                setattr(youfish, n, v)


class DownloadFolder(_TmpDataDir):
    def test_pick_reset_and_refuse(self):
        target = os.path.join(self._tmp, "Music")
        r = youfish.set_download_dir("file://" + target)
        self.assertTrue(r["ok"])
        self.assertTrue(r["custom"])
        self.assertEqual(r["effective"], target)
        blocker = os.path.join(self._tmp, "file")
        open(blocker, "w").close()
        r = youfish.set_download_dir(os.path.join(blocker, "sub"))   # can't create under a file
        self.assertFalse(r["ok"])
        self.assertEqual(youfish.download_location()["configured"], target)   # previous kept
        self.assertFalse(youfish.set_download_dir("")["custom"])


class AtomicJsonWrite(_TmpDataDir):
    def test_failed_write_keeps_the_old_file(self):
        p = os.path.join(self._tmp, "x.json")
        youfish._atomic_write_json(p, {"a": 1})
        with self.assertRaises(Exception):
            youfish._atomic_write_json(p, {"bad": object()})     # not serialisable
        with open(p) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertEqual(sorted(os.listdir(self._tmp)), ["x.json"])   # no temp file left behind


class YtmLocalStores(_TmpDataDir):
    def test_disliked_set_follows_ratings(self):
        self.ytm._update_disliked("a", "DISLIKE")
        self.ytm._update_disliked("b", "DISLIKE")
        self.assertEqual(self.ytm.disliked_ids(), ["a", "b"])
        self.ytm._update_disliked("a", "LIKE")
        self.ytm._update_disliked("b", "INDIFFERENT")
        self.assertEqual(self.ytm.disliked_ids(), [])

    def test_rating_needs_sign_in_and_keeps_the_store(self):
        saved = self.ytm.is_logged_in
        self.ytm.is_logged_in = lambda: False
        try:
            self.assertFalse(self.ytm.rate_song("a", "DISLIKE")["ok"])
            self.assertEqual(self.ytm.disliked_ids(), [])
        finally:
            self.ytm.is_logged_in = saved

    def test_play_history_newest_first_capped_and_cleared(self):
        for i in range(5):
            self.ytm.record_play("v%d" % i, title="T%d" % i)
        self.ytm.record_play("v1")                               # a replay moves to the top
        self.assertEqual([h["videoId"] for h in self.ytm.play_history(3)], ["v1", "v4", "v3"])
        self.ytm.clear_play_history()
        self.assertEqual(self.ytm.play_history(), [])

    def test_cached_home_survives_a_missing_or_corrupt_file(self):
        self.assertEqual(self.ytm.cached_home(), {"shelves": []})
        with open(self.ytm._home_cache_path(), "w") as f:
            f.write("[1, 2")
        self.assertEqual(self.ytm.cached_home(), {"shelves": []})


class LyricsParsing(unittest.TestCase):
    def setUp(self):
        import ytm
        self.ytm = ytm

    def test_lrc_times_fractions_and_order(self):
        lrc = "[ar:Someone]\n[00:05.5]half\n[00:01.25]first\n[01:02.345]long\nno tag\n[00:03]plain"
        self.assertEqual(self.ytm._parse_lrc(lrc), [
            {"t": 1250, "line": "first"}, {"t": 3000, "line": "plain"},
            {"t": 5500, "line": "half"}, {"t": 62345, "line": "long"}])

    def test_lrc_repeated_line_with_several_tags(self):
        self.assertEqual([x["t"] for x in self.ytm._parse_lrc("[00:10.00][00:20.00]chorus")],
                         [10000, 20000])

    def test_empty_lrc(self):
        self.assertEqual(self.ytm._parse_lrc(""), [])
        self.assertEqual(self.ytm._parse_lrc(None), [])

    def test_best_match_prefers_synced_then_closest_duration(self):
        hits = [{"id": 1, "plainLyrics": "x", "duration": 200},
                {"id": 2, "syncedLyrics": "[00:01]x", "duration": 250},
                {"id": 3, "syncedLyrics": "[00:01]x", "duration": 201}]
        self.assertEqual(self.ytm._best_lyrics_match(hits, 200)["id"], 3)
        self.assertEqual(self.ytm._best_lyrics_match([{"id": 4}, {"id": 5, "plainLyrics": "y"}],
                                                     0)["id"], 5)
        self.assertIsNone(self.ytm._best_lyrics_match([], 100))


class ProxyAllowList(unittest.TestCase):
    """The proxy only refetches https Google media hosts (no open proxy, no SSRF)."""
    def test_allowed(self):
        for u in ("https://rr1---sn-x.googlevideo.com/videoplayback?a=1",
                  "https://i.ytimg.com/vi/x/hq.jpg", "https://music.youtube.com/x"):
            self.assertTrue(youfish._proxy_url_ok(u), u)

    def test_refused(self):
        for u in ("http://rr1.googlevideo.com/x", "https://127.0.0.1/x", "file:///etc/passwd",
                  "https://evil.com/googlevideo.com", "https://googlevideo.com.evil.com/x",
                  "https://evilgooglevideo.com/x", ""):
            self.assertFalse(youfish._proxy_url_ok(u), u)


class MediaProxyEndToEnd(_TmpDataDir):
    """A real request through the localhost proxy: playback's own path minus the network. The
    fake googlevideo serves known bytes; the proxy must hand back exactly the asked range."""
    DATA = bytes(range(256)) * 40          # 10240 bytes

    def setUp(self):
        super().setUp()
        self._saved = {n: getattr(youfish, n) for n in ("_force_ipv4", "_ytdlp_path", "_DIRECT_CHUNK")}
        self._urlopen = youfish.urllib.request.urlopen
        youfish._force_ipv4 = lambda: None
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._DIRECT_CHUNK = 4096
        data = self.DATA

        def fake(req, timeout=None):
            a, b = map(int, re.match(r"bytes=(\d+)-(\d+)", req.headers["Range"]).groups())
            if a >= len(data):
                raise urllib.error.HTTPError(req.full_url, 416, "range", {}, None)
            return _FakeHttpResp(206, data[a:b + 1], {"Content-Range": "bytes %d-%d/%d" % (
                a, min(b, len(data) - 1), len(data))})
        youfish.urllib.request.urlopen = fake
        youfish._ensure_proxy()
        self._sdir = youfish._STREAM_DIR
        youfish._STREAM_DIR = self._tmp

    def tearDown(self):
        youfish.release_playback("pv", [])
        youfish.urllib.request.urlopen = self._urlopen
        youfish._STREAM_DIR = self._sdir
        for n, v in self._saved.items():
            setattr(youfish, n, v)
        super().tearDown()

    def _get(self, url, rng=None):
        import http.client
        u = urllib.parse.urlsplit(url)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.request("GET", u.path + "?" + u.query, headers={"Range": rng} if rng else {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def test_range_requests_get_the_exact_bytes(self):
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
        url = youfish._proxied(src, "pv", "251", "UA/1")
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        st, h, body = self._get(url, "bytes=0-")
        self.assertEqual(st, 206)
        self.assertEqual(body, self.DATA)
        st, h, body = self._get(url, "bytes=5000-")          # a seek
        self.assertEqual((st, h["Content-Range"]), (206, "bytes 5000-10239/10240"))
        self.assertEqual(body, self.DATA[5000:])

    def test_seek_past_the_end_is_a_clean_416(self):
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d" % len(self.DATA)
        st, h, _ = self._get(youfish._proxied(src, "pv", "251"), "bytes=10240-")
        self.assertEqual((st, h["Content-Range"]), (416, "bytes */10240"))

    def test_non_google_target_is_refused(self):
        st, _, _ = self._get(youfish._proxied("https://example.com/x?clen=10", "pv", "251"))
        self.assertEqual(st, 403)

    def test_plays_without_yt_dlp_installed(self):
        """A fast-path resolve needs no yt-dlp, and neither does the direct fetch. The proxy used
        to refuse such a stream (503 "no stream capacity"), so nothing played on a phone where
        yt-dlp wasn't installed or couldn't run (the first 32-bit report)."""
        youfish._ytdlp_path = lambda: None
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
        st, _, body = self._get(youfish._proxied(src, "pv", "251", "UA/1"), "bytes=0-")
        self.assertEqual((st, body), (206, self.DATA))

    def test_byte0_refusal_without_yt_dlp_retries_direct(self):
        """A stream refused at byte 0 flips to the yt-dlp child, unless there's no yt-dlp: then
        the direct fetch tries again (it used to crash the reader and fail the stream)."""
        youfish._ytdlp_path = lambda: None
        serve, calls = youfish.urllib.request.urlopen, []

        def once_refused(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
            return serve(req, timeout)
        youfish.urllib.request.urlopen = once_refused
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
        st, _, body = self._get(youfish._proxied(src, "pv", "251", "UA/1"), "bytes=0-")
        self.assertEqual((st, body), (206, self.DATA))
        self.assertGreater(len(calls), 1)

    def test_refusal_says_why(self):
        free = youfish._free_bytes
        youfish._free_bytes = lambda path: 120 << 20
        try:
            src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
            u = urllib.parse.urlsplit(youfish._proxied(src, "pv", "251"))
            import http.client
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
            c.request("GET", u.path + "?" + u.query)
            r = c.getresponse()
            r.read()
            c.close()
        finally:
            youfish._free_bytes = free
        self.assertEqual(r.status, 503)
        self.assertIn("not enough free storage", r.reason)
        self.assertIn("120 MB free", r.reason)



# --- Gap closing: every engine function the coverage report listed as untested ---------------- #

def _swap(tc, obj, **attrs):
    """Set attributes for one test; cleanup (after tearDown) puts every original back."""
    for name, value in attrs.items():
        tc.addCleanup(setattr, obj, name, getattr(obj, name))
        setattr(obj, name, value)


def _fake_pyotherside(tc):
    """Install a recording pyotherside for one test; returns the list of sent events."""
    sent = []
    prev = sys.modules.get("pyotherside")
    sys.modules["pyotherside"] = types.SimpleNamespace(send=lambda *a: sent.append(a))

    def restore():
        if prev is not None:
            sys.modules["pyotherside"] = prev
        else:
            sys.modules.pop("pyotherside", None)
    tc.addCleanup(restore)
    return sent


def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _script(dirpath, name, body):
    """A tiny executable shell script standing in for a real binary."""
    path = os.path.join(dirpath, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


class _ModProxy:
    """A module stand-in for one test: the given overrides, everything else from the real one."""
    def __init__(self, mod, **over):
        self.__dict__["_mod"] = mod
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._mod, name)


class _Blob(_FakeHttpResp):
    """_FakeHttpResp that also reads to the end, reports its final URL and can die mid-body."""
    def __init__(self, data, headers=None, url="https://example.invalid/x", fail_after=None):
        _FakeHttpResp.__init__(self, 200, data, headers)
        self.url, self.fail_after, self.closed = url, fail_after, False

    def read(self, n=-1):
        if self.fail_after is not None and self._off >= self.fail_after:
            raise OSError("connection reset mid-download")
        if n is None or n < 0:
            n = len(self._data) - self._off
        return _FakeHttpResp.read(self, n)

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True


class YtdlpInstallVerified(_TmpDataDir):
    """install_ytdlp installs only a binary that matches the release's SHA2-256SUMS; a tampered,
    interrupted or non-running download leaves the current install alone."""
    BIN = b"\x7fELF fake yt-dlp build " * 4000          # ~100 KB: several chunks, real progress

    def setUp(self):
        super().setUp()
        import hashlib
        self.sha = hashlib.sha256(self.BIN).hexdigest()
        self.sums = "%s  yt-dlp\n%s  yt-dlp_linux_aarch64\n" % ("0" * 64, self.sha)
        self.body, self.fail_after, self.ver = self.BIN, None, "2026.10.01"
        self.opened, self.version_calls = [], []
        _swap(self, youfish, _force_ipv4=lambda: None, _ARM32=False,
              _YTDLP_ASSET="yt-dlp_linux_aarch64", _https_open=self._open,
              ytdlp_version=self._version)

    def _open(self, url, ctx, timeout=60):
        self.opened.append(url)
        if url.endswith("SHA2-256SUMS"):
            return _Blob(self.sums.encode())
        return _Blob(self.body, {"Content-Length": str(len(self.body))},
                     fail_after=self.fail_after)

    def _version(self, fresh=False):
        self.version_calls.append(fresh)
        return self.ver

    def _existing(self, data=b"old build"):
        os.makedirs(os.path.dirname(youfish._managed_ytdlp()), exist_ok=True)
        with open(youfish._managed_ytdlp(), "wb") as f:
            f.write(data)

    def _installed(self):
        with open(youfish._managed_ytdlp(), "rb") as f:
            return f.read()

    def test_verified_download_is_installed_and_executable(self):
        pct = []
        ok, msg, ver = youfish._install_ytdlp_sync(pct.append)
        self.assertEqual((ok, msg, ver), (True, "Installed yt-dlp 2026.10.01", "2026.10.01"))
        self.assertEqual(self._installed(), self.BIN)
        self.assertEqual(os.stat(youfish._managed_ytdlp()).st_mode & 0o777, 0o755)
        self.assertFalse(os.path.exists(youfish._managed_ytdlp() + ".part"))
        self.assertEqual(self.version_calls, [True])      # proved it runs, not a cached answer
        self.assertEqual(pct[-1], 100.0)
        self.assertEqual(pct, sorted(pct))
        base = youfish._YTDLP_RELEASE_BASES["stable"]
        self.assertEqual(self.opened, [base + "SHA2-256SUMS", base + "yt-dlp_linux_aarch64"])

    def test_checksum_mismatch_discards_and_keeps_the_old_binary(self):
        self._existing()
        self.body = self.BIN + b"tampered"
        ok, msg, ver = youfish._install_ytdlp_sync()
        self.assertEqual((ok, msg, ver),
                         (False, "Checksum mismatch — download discarded, nothing installed", ""))
        self.assertEqual(self._installed(), b"old build")
        self.assertFalse(os.path.exists(youfish._managed_ytdlp() + ".part"))
        self.assertEqual(self.version_calls, [])          # the bad file never ran

    def test_sums_line_with_star_and_uppercase_still_verifies(self):
        self.sums = "%s *yt-dlp_linux_aarch64\n" % self.sha.upper()
        ok, msg, _ = youfish._install_ytdlp_sync()
        self.assertTrue(ok)
        self.assertNotIn("not verified", msg)

    def test_missing_checksum_is_installed_but_flagged(self):
        self.sums = "%s  yt-dlp\n" % self.sha                # only the zipapp is listed
        ok, msg, _ = youfish._install_ytdlp_sync()
        self.assertTrue(ok)
        self.assertTrue(msg.endswith("(checksum unavailable, not verified)"), msg)

    def test_binary_that_wont_run_is_reported(self):
        self.ver = ""
        ok, msg, ver = youfish._install_ytdlp_sync()
        self.assertFalse(ok)
        self.assertIn("won't run here", msg)

    def test_interrupted_download_cleans_up_and_keeps_the_old_binary(self):
        self._existing()
        self.fail_after = 65536
        ok, msg, ver = youfish._install_ytdlp_sync()
        self.assertEqual((ok, ver), (False, ""))
        self.assertIn("connection reset", msg)
        self.assertEqual(self._installed(), b"old build")
        self.assertFalse(os.path.exists(youfish._managed_ytdlp() + ".part"))

    def test_nightly_channel_downloads_from_the_nightly_repo(self):
        _swap(self, youfish, get_settings=lambda: {"ytdlp_channel": "nightly"})
        self.assertTrue(youfish._install_ytdlp_sync()[0])
        base = youfish._YTDLP_RELEASE_BASES["nightly"]
        self.assertTrue(all(u.startswith(base) for u in self.opened), self.opened)

    def test_install_ytdlp_reports_progress_and_result(self):
        sent = _fake_pyotherside(self)

        def fake_sync(progress=None):
            progress(50.0)
            return True, "Installed yt-dlp v", "v"
        _swap(self, youfish, _install_ytdlp_sync=fake_sync)
        self.assertEqual(youfish.install_ytdlp(), {"ok": True})
        self.assertTrue(_wait_for(lambda: any(e[0] == "ytdlp_install_done" for e in sent)))
        self.assertEqual(sent, [("ytdlp_install_progress", 50.0),
                                ("ytdlp_install_done", True, "Installed yt-dlp v", "v")])


class HttpsOnlyDownloads(unittest.TestCase):
    """Binary downloads refuse plain HTTP, including a redirect that downgrades to it, and the
    checksum lookup matches the exact asset name."""

    def setUp(self):
        self.reqs = []
        self.resp = _Blob(b"", url="https://objects.githubusercontent.com/x")

        def urlopen(req, timeout=None, context=None):
            self.reqs.append((req, timeout))
            return self.resp
        _swap(self, youfish.urllib.request, urlopen=urlopen)

    def test_plain_http_is_refused_before_any_request(self):
        with self.assertRaises(ValueError):
            youfish._https_open("http://github.com/x", None)
        self.assertEqual(self.reqs, [])

    def test_redirect_to_http_is_refused_and_closed(self):
        self.resp = _Blob(b"", url="http://evil.example/yt-dlp")
        with self.assertRaises(ValueError):
            youfish._https_open("https://github.com/x", None)
        self.assertTrue(self.resp.closed)

    def test_https_end_to_end_is_returned(self):
        r = youfish._https_open("HTTPS://github.com/x", None, timeout=7)
        self.assertIs(r, self.resp)
        req, timeout = self.reqs[0]
        self.assertEqual((req.get_header("User-agent"), timeout), (youfish._BROWSER_UA, 7))

    def test_expected_sha256_matches_the_exact_asset(self):
        sums = ("aa" * 32 + "  yt-dlp_linux_aarch64\n" + "BB" * 32 + "  yt-dlp\n"
                + "cc" * 32 + "  yt-dlp.tar.gz\n")
        _swap(self, youfish, _https_open=lambda url, ctx, timeout=60: _Blob(sums.encode()),
              _YTDLP_ASSET="yt-dlp_linux_aarch64", get_settings=lambda: {})
        self.assertEqual(youfish._expected_sha256(None), "aa" * 32)
        self.assertEqual(youfish._expected_sha256(None, "yt-dlp"), "bb" * 32)
        self.assertIsNone(youfish._expected_sha256(None, "yt-dlp_macos"))

    def test_latest_release_tags_come_from_the_github_api(self):
        _swap(self, youfish, _force_ipv4=lambda: None)
        self.resp = _Blob(b'{"tag_name": " 2026.10.01 "}')
        self.assertEqual(youfish._ytdlp_latest_tag(), "2026.10.01")
        self.assertEqual(self.reqs[-1][0].full_url,
                         "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest")
        self.resp = _Blob(b'{"tag_name": "1.4.0"}')
        self.assertEqual(youfish._pot_latest_tag(), "1.4.0")
        self.assertIn("bgutil-ytdlp-pot-provider", self.reqs[-1][0].full_url)

        def down(*a, **k):
            raise OSError("offline")
        _swap(self, youfish.urllib.request, urlopen=down)
        self.assertEqual((youfish._ytdlp_latest_tag(), youfish._pot_latest_tag()), ("", ""))


class PotServerSandbox(_TmpDataDir):
    """The Deno token server runs default-deny: no writes, no subprocesses, no native code, and
    reads confined to its own tree. FFI is granted to the one canvas addon only when needed."""

    def setUp(self):
        super().setUp()
        self.settings = {}
        _swap(self, youfish, _deno_path=lambda: "/opt/deno", get_settings=lambda: self.settings)
        self.srv = youfish._pot_server_dir()

    def test_server_dir_lives_in_the_app_data_dir(self):
        self.assertEqual(self.srv, os.path.join(self._tmp, "potprovider",
                                                "bgutil-ytdlp-pot-provider", "server"))

    def test_default_flags_deny_write_run_and_ffi(self):
        flags = youfish._pot_server_flags()
        self.assertEqual(flags[:2], ["/opt/deno", "run"])
        self.assertEqual(flags[-1], os.path.join(self.srv, "src", "main.ts"))
        for f in ("--deny-write", "--deny-run", "--deny-ffi", "--allow-read=" + self.srv):
            self.assertIn(f, flags)
        for f in flags:
            self.assertFalse(f in ("-A", "--allow-all", "--allow-read", "--allow-ffi", "--allow-sys")
                             or f.startswith(("--allow-write", "--allow-run", "--allow-sys=")),
                             "over-broad Deno permission: " + f)

    def test_ffi_is_granted_only_to_the_built_canvas_addon(self):
        self.settings = {"pot_needs_ffi": True}
        self.assertIn("--deny-ffi", youfish._pot_server_flags())     # needed but never built
        self.assertEqual(youfish._canvas_node_path(), "")
        addon = os.path.join(self.srv, "node_modules", "canvas", "build", "Release", "canvas.node")
        os.makedirs(os.path.dirname(addon))
        open(addon, "w").close()
        self.assertEqual(youfish._canvas_node_path(), addon)
        flags = youfish._pot_server_flags()
        self.assertIn("--allow-ffi=" + addon, flags)
        self.assertNotIn("--deny-ffi", flags)
        self.settings = {}                                            # built, but not needed
        self.assertIn("--deny-ffi", youfish._pot_server_flags())

    def test_webgpu_shim_is_prepended_once(self):
        main = os.path.join(self.srv, "src", "main.ts")
        youfish._pot_disable_webgpu()                                 # no checkout yet: no-op
        self.assertFalse(os.path.exists(main))
        os.makedirs(os.path.dirname(main))
        with open(main, "w") as f:
            f.write("console.log('server');\n")
        youfish._pot_disable_webgpu()
        youfish._pot_disable_webgpu()
        src = open(main).read()
        self.assertTrue(src.startswith("/* youfish:no-webgpu */"))
        self.assertEqual(src.count("youfish:no-webgpu"), 1)
        self.assertIn("requestAdapter", src)
        self.assertTrue(src.endswith("console.log('server');\n"))


class PotSidecarControl(_TmpDataDir):
    """Turning the token provider on/off, stopping the sidecar, its log, and the update action."""

    class _Proc:
        def __init__(self, alive=True, wait_raises=False):
            self.alive, self.wait_raises, self.calls = alive, wait_raises, []

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.calls.append("terminate")

        def wait(self, timeout=None):
            self.calls.append("wait")
            if self.wait_raises:
                raise youfish.subprocess.TimeoutExpired("deno", timeout)

        def kill(self):
            self.calls.append("kill")

    def test_stop_terminates_then_kills_a_stuck_server(self):
        p = self._Proc()
        _swap(self, youfish, _pot_proc=p)
        youfish.stop_pot_server()
        self.assertEqual((p.calls, youfish._pot_proc), (["terminate", "wait"], None))
        p = self._Proc(wait_raises=True)
        youfish._pot_proc = p
        youfish.stop_pot_server()
        self.assertEqual(p.calls, ["terminate", "wait", "kill"])
        p = self._Proc(alive=False)
        youfish._pot_proc = p
        youfish.stop_pot_server()                                   # already exited
        self.assertEqual(p.calls, [])
        youfish.stop_pot_server()                                   # nothing tracked: no-op

    def test_enable_starts_and_disable_stops_the_server(self):
        calls = []
        _swap(self, youfish, _ensure_pot_server=lambda wait=25.0: calls.append("start"),
              stop_pot_server=lambda: calls.append("stop"), pot_status=lambda: {"st": 1})
        self.assertEqual(youfish.set_pot_enabled(True), {"st": 1})
        self.assertTrue(youfish.get_settings()["pot_provider"])
        youfish.set_pot_enabled(False)
        self.assertFalse(youfish.get_settings()["pot_provider"])
        self.assertEqual(calls, ["start", "stop"])

    def test_update_needs_the_latest_tag(self):
        sent = _fake_pyotherside(self)
        installs = []
        _swap(self, youfish, _pot_latest_tag=lambda: "",
              install_pot_provider=lambda tag=None, persist_tag=False:
              installs.append((tag, persist_tag)) or {"ok": True})
        self.assertEqual(youfish.update_pot_provider(), {"ok": False})
        self.assertEqual(sent[0][:2], ("pot_install_done", False))
        self.assertEqual(installs, [])
        youfish._pot_latest_tag = lambda: "1.4.0"
        self.assertEqual(youfish.update_pot_provider(), {"ok": True})
        self.assertEqual(installs, [("1.4.0", True)])

    def test_log_tail_and_rotation(self):
        log = os.path.join(youfish._pot_dir(), "server.log")
        self.assertEqual(youfish._pot_server_log_tail(), "")
        os.makedirs(os.path.dirname(log))
        with open(log, "w") as f:
            f.write("one\n\n two\nthree\n   \nfour\n")
        self.assertEqual(youfish._pot_server_log_tail(2), "three\nfour")
        self.assertEqual(youfish._pot_server_log_tail(0), "four")         # at least one line
        _swap(self, youfish, _pot_log_rotated=False)
        youfish._pot_rotate_log()
        self.assertFalse(os.path.exists(log))
        self.assertIn("four", open(log + ".prev").read())
        with open(log, "w") as f:
            f.write("this launch\n")
        youfish._pot_rotate_log()                                    # once per process
        self.assertEqual(open(log).read(), "this launch\n")

    def test_pot_of_never_leaks_the_token(self):
        self.assertEqual(youfish._pot_of(""), "no-url")
        self.assertEqual(youfish._pot_of("https://rr1.googlevideo.com/v?itag=251"), "no-pot")
        self.assertEqual(youfish._pot_of("https://rr1.googlevideo.com/v?pot=SECRETTOKEN123"),
                         "len=14 pfx=SECRETTO")

    def test_deno_version_parses_the_first_line(self):
        deno = _script(self._tmp, "deno", 'echo "deno 2.1.4 (stable, release, aarch64)"; '
                                          'echo "v8 13.0"')
        _swap(self, youfish, _deno_path=lambda: deno)
        self.assertEqual(youfish.deno_version(), "2.1.4")
        _script(self._tmp, "deno", 'echo "something else entirely"')
        self.assertEqual(youfish.deno_version(), "something else entirely")
        _script(self._tmp, "deno", "exit 0")
        self.assertEqual(youfish.deno_version(), "")
        youfish._deno_path = lambda: None
        self.assertEqual(youfish.deno_version(), "")

    def test_git_path_prefers_path_then_known_spots(self):
        _swap(self, youfish.shutil, which=lambda name: "/somewhere/git")
        self.assertEqual(youfish._git_path(), "/somewhere/git")
        youfish.shutil.which = lambda name: None
        found = youfish._git_path()
        self.assertTrue(found is None or (found.endswith("/git") and os.access(found, os.X_OK)))

    @unittest.skipUnless(sys.platform.startswith("linux"), "prctl is Linux-only")
    def test_pdeathsig_is_armed_on_the_calling_thread(self):
        import ctypes
        import signal
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        got = ctypes.c_int(0)

        def run():
            youfish._set_pdeathsig()
            libc.prctl(2, ctypes.byref(got))                          # PR_GET_PDEATHSIG
            libc.prctl(1, 0)                                          # disarm again
        t = threading.Thread(target=run)
        t.start()
        t.join()
        self.assertEqual(got.value, signal.SIGKILL)


class PotHealthChecks(unittest.TestCase):
    """The sidecar liveness checks against a real localhost socket: a port that's open, a server
    that answers /ping (even with an error status), and one that's gone."""

    def setUp(self):
        import http.server
        test = self
        self.status, self.body = 200, b'{"version": "1.3.2"}'

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                test.paths.append(self.path)
                self.send_response(test.status)
                self.send_header("Content-Length", str(len(test.body)))
                self.end_headers()
                self.wfile.write(test.body)

            def log_message(self, *a):
                pass
        self.paths = []
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        prev = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        self.addCleanup(lambda: os.environ.__setitem__("no_proxy", prev) if prev is not None
                        else os.environ.pop("no_proxy", None))
        _swap(self, youfish, _POT_PORT=self.srv.server_address[1])

    def test_listening_and_answering(self):
        self.assertTrue(youfish._pot_ready_on_port())
        self.assertEqual(youfish._pot_http_ping(), {"ok": True, "version": "1.3.2"})
        self.assertEqual(self.paths, ["/ping"])

    def test_an_error_status_still_means_alive(self):
        self.status, self.body = 500, b"boom"
        self.assertEqual(youfish._pot_http_ping(), {"ok": True, "version": ""})
        self.status, self.body = 200, b"not json"
        self.assertEqual(youfish._pot_http_ping(), {"ok": True, "version": ""})

    def test_a_closed_port_is_down(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        youfish._POT_PORT = s.getsockname()[1]
        s.close()
        self.assertFalse(youfish._pot_ready_on_port())
        self.assertEqual(youfish._pot_http_ping(timeout=0.5), {"ok": False, "version": ""})


class PotDiagnosticsReport(_TmpDataDir):
    """The 'Run diagnostics' verdict: a crashed server, an unloaded plugin and a failed restart
    each show up as NOT working, with the reason in the report."""

    def setUp(self):
        super().setUp()
        self.settings = {"pot_provider": True}
        self.port_up, self.restarts = True, []
        _swap(self, youfish, get_settings=lambda: self.settings, _pot_installed=lambda: True,
              _pot_ready_on_port=lambda timeout=0.25: self.port_up,
              _pot_http_ping=lambda timeout=1.5: {"ok": True, "version": "1.3.2"},
              _deno_path=lambda: "/opt/deno", deno_version=lambda: "2.1.4",
              _git_path=lambda: "/usr/bin/git", _ytdlp_path=lambda: "/opt/yt-dlp",
              ytdlp_version=lambda fresh=False: "2026.10.01", _pot_effective_tag=lambda: "1.3.2",
              _pot_plugin_probe=lambda timeout=30: {
                  "checked": True, "loaded": True, "detail": "Plugin directories: x",
                  "js_runtimes": "deno", "bgutil_lines": ""},
              _ensure_pot_server=lambda wait=25.0: self.restarts.append(1) or False,
              _pot_proc=None, _pot_last_error="")

    def test_healthy(self):
        d = youfish.pot_diagnostics()
        self.assertIn("verdict            : working", d["report"])
        self.assertIn("answering HTTP     : yes (server v1.3.2)", d["report"])
        self.assertEqual((d["running"], d["responding"], d["plugin_loaded"]), (True, True, True))
        self.assertEqual(self.restarts, [])

    def test_crashed_server_and_unloaded_plugin(self):
        youfish._pot_proc = types.SimpleNamespace(poll=lambda: -11)
        youfish._pot_plugin_probe = lambda timeout=30: {
            "checked": True, "loaded": False, "detail": "Plugin directories: none",
            "js_runtimes": "", "bgutil_lines": ""}
        d = youfish.pot_diagnostics()
        self.assertIn("STARTED, then EXITED (code -11)", d["report"])
        self.assertIn("plugin in yt-dlp   : NOT LOADED", d["report"])
        self.assertIn("verdict            : NOT working", d["report"])
        self.assertEqual(d["prev_exit"], -11)

    def test_dead_port_triggers_one_restart_attempt(self):
        self.port_up = False
        youfish._pot_last_error = "deno exited at once"
        d = youfish.pot_diagnostics()
        self.assertEqual(self.restarts, [1])
        self.assertIn("restart attempt    : FAILED — deno exited at once", d["report"])
        self.assertIn("verdict            : NOT working", d["report"])
        self.assertFalse(d["responding"])

    def test_not_set_up(self):
        self.settings = {}
        youfish._pot_installed = lambda: False
        youfish._deno_path = lambda: None
        d = youfish.pot_diagnostics()
        self.assertIn("Deno   : NOT FOUND", d["report"])
        self.assertIn("verdict            : not set up", d["report"])
        self.assertIsNone(d["plugin_loaded"])                        # the probe never ran
        self.settings = {}
        youfish._pot_installed = lambda: True
        self.assertIn("installed but switched off", youfish.pot_diagnostics()["report"])


class ProbeUrlOk(unittest.TestCase):
    """The pre-flight probe escalates to the token client only on a definite 403, never on a
    maybe (timeouts and other errors pass), and asks with the player's own UA."""

    def setUp(self):
        self.reqs, self.outcome = [], 206
        _swap(self, youfish, _force_ipv4=lambda: None)

        def urlopen(req, timeout=None):
            self.reqs.append(req)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            if self.outcome >= 400:
                raise urllib.error.HTTPError(req.full_url, self.outcome, "x", {}, None)
            return _FakeHttpResp(self.outcome, b"ab")
        _swap(self, youfish.urllib.request, urlopen=urlopen)

    def test_outcomes(self):
        import urllib.error as ue
        url = "https://rr1.googlevideo.com/videoplayback?itag=251"
        self.assertTrue(youfish._probe_url_ok(url, "UA/1"))
        self.assertEqual(self.reqs[0].get_header("Range"), "bytes=0-1")
        self.assertEqual(self.reqs[0].get_header("User-agent"), "UA/1")
        self.outcome = 403
        self.assertFalse(youfish._probe_url_ok(url, None))
        self.assertEqual(self.reqs[-1].get_header("User-agent"), youfish._BROWSER_UA)
        for other in (404, 500, ue.URLError("dns"), TimeoutError("slow")):
            self.outcome = other
            self.assertTrue(youfish._probe_url_ok(url, "UA/1"), other)
        n = len(self.reqs)
        self.assertTrue(youfish._probe_url_ok("", "UA/1"))
        self.assertEqual(len(self.reqs), n)                          # no URL: nothing fetched


class StreamReaping(unittest.TestCase):
    """Freeing a stream slot reaps the least-recently-active idle stream, never one a connection
    holds, and queues its child for an off-lock wait()."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="reap-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        _swap(self, youfish, _STREAM_DIR=self._tmp, _streams={}, _reap_pending=[])

    def _stream(self, vid, refs, last_active, proc=None):
        s = youfish._Stream(vid, "251", "u", "ua", 100)
        s.refs, s.last_active, s.proc = refs, last_active, proc
        youfish._streams[(vid, "251")] = s
        return s

    def test_oldest_idle_stream_goes_first(self):
        killed = []
        proc = types.SimpleNamespace(kill=lambda: killed.append(1))
        with youfish._streams_lock:            # the contract; also keeps the real reaper out
            busy = self._stream("busy", 1, 10)
            old = self._stream("old", 0, 100, proc)
            new = self._stream("new", 0, 200)
            youfish._reap_one_idle_locked()
            self.assertEqual(sorted(youfish._streams), [("busy", "251"), ("new", "251")])
            self.assertEqual((old.state, os.path.exists(old.path)), ("DEAD", False))
            self.assertEqual((killed, youfish._reap_pending), ([1], [proc]))
            self.assertEqual((busy.state, new.state), ("RUN", "RUN"))
            youfish._reap_one_idle_locked()
            youfish._reap_one_idle_locked()                          # only the busy one left
            self.assertEqual(list(youfish._streams), [("busy", "251")])
            os.close(busy.fd)

    def test_reap_proc_tolerates_none_and_a_stuck_child(self):
        youfish._reap_proc(None)
        waits = []

        def wait(timeout=None):
            waits.append(timeout)
            raise youfish.subprocess.TimeoutExpired("yt-dlp", timeout)
        youfish._reap_proc(types.SimpleNamespace(wait=wait))
        self.assertEqual(waits, [5])


class InnertubeModuleLoad(unittest.TestCase):
    """_it() loads innertube once (IPv4 pinned first) and reports a missing module as None;
    the launch prewarm mints the visitor id through it and survives a failure."""

    def setUp(self):
        self.pinned = []
        _swap(self, youfish, _INNERTUBE={"mod": None, "tried": False},
              _force_ipv4=lambda: self.pinned.append(1))

    def test_loads_once_with_ipv4_pinned(self):
        self.assertIs(_REAL_IT(), innertube)
        self.assertIs(_REAL_IT(), innertube)
        self.assertEqual(self.pinned, [1])

    def test_missing_module_means_none(self):
        prev = sys.modules["innertube"]
        sys.modules["innertube"] = None                              # import now raises
        try:
            self.assertIsNone(_REAL_IT())
        finally:
            sys.modules["innertube"] = prev
        self.assertIsNone(_REAL_IT())                                # not retried
        self.assertTrue(youfish._INNERTUBE["tried"])

    def test_prewarm_mints_the_visitor_id(self):
        minted = []
        it = types.SimpleNamespace(visitor_data=lambda: minted.append(1))
        _swap(self, youfish, _it=lambda: it)
        youfish._innertube_prewarm()
        self.assertEqual(minted, [1])

        def boom():
            raise OSError("offline")
        it.visitor_data = boom
        youfish._innertube_prewarm()                                 # swallowed
        youfish._it = lambda: None
        youfish._innertube_prewarm()

    def test_url_shape_is_identity_free(self):
        exp = int(time.time()) + 100
        u = ("https://rr1.googlevideo.com/videoplayback?c=VISIONOS&ip=1.2.3.4&n=abc&expire=%d"
             "&sparams=expire,ip" % exp)
        shape = youfish._url_shape(u)
        self.assertRegex(shape, r"^c=VISIONOS ip=v4 n=y pot=- exp=\+(99|100)s sparams=expire,ip$")
        self.assertNotIn("1.2.3.4", shape)
        self.assertIn("ip=v6", youfish._url_shape("https://x/v?ip=2001:db8::1&pot=T"))
        self.assertIn("pot=y", youfish._url_shape("https://x/v?ip=2001:db8::1&pot=T"))
        self.assertEqual(youfish._url_shape(object()), "?")


class PrefetchResolve(unittest.TestCase):
    """Speculative prefetch: one job per key, at most two at once (the rest dropped, not queued),
    nothing for a cached video, and a throwaway thread never starts the PO-token sidecar."""

    def setUp(self):
        self.go = threading.Event()
        self.resolved, self.prewarmed = [], []
        self.cache = {}

        def resolve(video_id, key, speculative=False):
            self.resolved.append((video_id, speculative))
            self.go.wait(5)
        _swap(self, youfish, _resolve_key=lambda v: "k:" + v,
              _resolve_cache_get=lambda k: self.cache.get(k), _pot_active=lambda: False,
              _resolve_and_cache=resolve, _prefetch_pending=set(),
              _prefetch_sema=threading.BoundedSemaphore(2),
              prewarm=lambda: self.prewarmed.append(1))
        self.addCleanup(self.go.set)

    def _idle(self):
        return _wait_for(lambda: not youfish._prefetch_pending)

    def test_empty_and_cached_are_no_ops(self):
        self.assertEqual(youfish.prefetch_resolve(""), {"ok": True, "queued": False})
        self.cache["k:abc"] = {"ok": True}
        self.assertEqual(youfish.prefetch_resolve("abc"),
                         {"ok": True, "queued": False, "cached": True})
        self.assertEqual(self.resolved, [])

    def test_same_key_is_deduplicated_while_in_flight(self):
        self.assertEqual(youfish.prefetch_resolve("abc", speculative=True),
                         {"ok": True, "queued": True})
        self.assertTrue(_wait_for(lambda: self.resolved))
        self.assertEqual(youfish.prefetch_resolve("abc"),
                         {"ok": True, "queued": False, "inflight": True})
        self.go.set()
        self.assertTrue(self._idle())
        self.assertEqual(self.resolved, [("abc", True)])
        youfish.prefetch_resolve("abc")                              # done: may run again
        self.assertTrue(self._idle())
        self.assertEqual(len(self.resolved), 2)

    def test_over_the_cap_is_dropped(self):
        for v in ("a", "b"):
            youfish.prefetch_resolve(v)
        self.assertTrue(_wait_for(lambda: len(self.resolved) == 2))
        self.assertEqual(youfish.prefetch_resolve("c")["queued"], True)
        self.assertTrue(_wait_for(lambda: "k:c" not in youfish._prefetch_pending))
        self.assertEqual(sorted(v for v, _ in self.resolved), ["a", "b"])     # c never ran
        self.go.set()
        self.assertTrue(self._idle())
        for _ in range(2):                                           # both slots came back
            self.assertTrue(youfish._prefetch_sema.acquire(blocking=False))

    def test_cold_sidecar_is_left_to_prewarm(self):
        _swap(self, youfish, _pot_active=lambda: True, _pot_ready_on_port=lambda timeout=0.25: False)
        youfish.prefetch_resolve("abc")
        self.assertTrue(self._idle())
        self.assertEqual((self.resolved, self.prewarmed), ([], [1]))

    def test_thread_start_failure_does_not_wedge_the_key(self):
        def no_thread(*a, **k):
            raise RuntimeError("can't start new thread")
        _swap(self, youfish, threading=_ModProxy(threading, Thread=no_thread))
        self.assertEqual(youfish.prefetch_resolve("abc"),
                         {"ok": False, "queued": False, "error": "spawn failed"})
        self.assertEqual(youfish._prefetch_pending, set())


class YtdlpSelfUpdate(unittest.TestCase):
    """Update runs the binary's own updater on the user's channel and reports what it said; a
    missing binary, a timeout and the 32-bit build (which can't self-update) are handled."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="upd-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.bin = _script(self._tmp, "yt-dlp", 'echo "args: $*"; exit 0')
        self.settings = {}
        _swap(self, youfish, _ytdlp_path=lambda: self.bin, ytdlp_version=lambda fresh=False: "V",
              get_settings=lambda: self.settings, _ARM32=False)

    def test_updates_on_the_chosen_channel(self):
        self.settings = {"ytdlp_channel": "nightly"}
        r = youfish.ytdlp_update()
        self.assertEqual((r["ok"], r["version"], r["channel"]), (True, "V", "nightly"))
        self.assertEqual(r["output"], "args: -4 --update-to nightly@latest")

    def test_failure_and_silence_are_reported(self):
        _script(self._tmp, "yt-dlp", 'echo "ERROR: pip installs cannot self-update" >&2; exit 1')
        r = youfish.ytdlp_update()
        self.assertFalse(r["ok"])
        self.assertIn("cannot self-update", r["output"])
        _script(self._tmp, "yt-dlp", "exit 0")
        self.assertEqual(youfish.ytdlp_update()["output"], "yt-dlp reported nothing")
        _script(self._tmp, "yt-dlp", "i=0; while [ $i -lt 300 ]; do echo line-$i; i=$((i+1)); done")
        self.assertEqual(len(youfish.ytdlp_update()["output"]), 400)

    def test_missing_binary_and_timeout(self):
        youfish._ytdlp_path = lambda: None
        self.assertEqual(youfish.ytdlp_update(),
                         {"ok": False, "error": "yt-dlp not found", "version": ""})
        youfish._ytdlp_path = lambda: self.bin

        def slow(*a, **k):
            raise youfish.subprocess.TimeoutExpired(a[0], k.get("timeout"))
        _swap(self, youfish, subprocess=_ModProxy(youfish.subprocess, run=slow))
        self.assertEqual(youfish.ytdlp_update(),
                         {"ok": False, "error": "update timed out", "version": "V"})

    def test_arm32_managed_build_redownloads(self):
        _swap(self, youfish, _ARM32=True, _managed_ytdlp=lambda: self.bin,
              _ytdlp_update_by_download=lambda: {"ok": True, "via": "download"})
        self.assertEqual(youfish.ytdlp_update(), {"ok": True, "via": "download"})


class LaunchWarmups(unittest.TestCase):
    """prewarm at launch: arms the delayed yt-dlp import once, mints the InnerTube visitor id,
    rotates the sidecar log, and starts the sidecar only when the provider is active."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="warm-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.calls = []
        rec = lambda name: (lambda *a, **k: self.calls.append(name))
        self.active = False
        _swap(self, youfish, _ytdlp_warm_armed=False, _YTDLP_WARM_DELAY=0.01,
              _delayed_ytdlp_warm=rec("import"), _innertube_prewarm=rec("visitor"),
              _autofetch_zipapp=rec("zipapp"), _pot_rotate_log=rec("rotate"),
              _pot_active=lambda: self.active, _ensure_pot_server=rec("sidecar"))

    def test_prewarm(self):
        youfish.prewarm()
        self.assertTrue(_wait_for(lambda: {"import", "visitor"} <= set(self.calls)))
        self.assertIn("zipapp", self.calls)
        self.assertIn("rotate", self.calls)
        self.assertNotIn("sidecar", self.calls)
        self.calls[:] = []
        self.active = True
        youfish.prewarm()
        self.assertTrue(_wait_for(lambda: "sidecar" in self.calls))
        time.sleep(0.05)
        self.assertNotIn("import", self.calls)                       # armed once per process

    def test_delayed_warm_imports_and_probes_only_when_debugging(self):
        youfish._delayed_ytdlp_warm = LaunchWarmups._real_delayed
        _swap(self, youfish, _import_yt_dlp=lambda: self.calls.append("import"),
              _spawn_tax_probe=lambda: self.calls.append("probe"), _DEBUG=False)
        youfish._delayed_ytdlp_warm()
        youfish._DEBUG = True
        youfish._delayed_ytdlp_warm()
        self.assertEqual(self.calls, ["import", "import", "probe"])

    def test_spawn_tax_probe_logs_only_under_debug(self):
        logged = []
        ytdlp = _script(self._tmp, "yt-dlp", "echo 2026.10.01")
        _swap(self, youfish, _tlog=logged.append, _ytdlp_path=lambda: ytdlp, _DEBUG=False)
        youfish._spawn_tax_probe()
        self.assertEqual(logged, [])
        youfish._DEBUG = True
        youfish._spawn_tax_probe()
        self.assertEqual(len(logged), 1)
        self.assertRegex(logged[0], r"^spawn_tax \d+\.\d\ds .* yt-dlp 2026\.10\.01$")
        youfish._ytdlp_path = lambda: None
        youfish._spawn_tax_probe()
        self.assertEqual(logged[-1], "spawn_tax: yt-dlp not found")
        youfish._ytdlp_path = lambda: os.path.join(self._tmp, "gone")
        youfish._spawn_tax_probe()
        self.assertTrue(logged[-1].startswith("spawn_tax: probe failed"))

    def test_inproc_logger_surfaces_errors_only_when_debugging(self):
        logged = []
        _swap(self, youfish, _tlog=logged.append, _DEBUG=False)
        lg = youfish._InprocLogger()
        for f in (lg.debug, lg.info, lg.warning, lg.error):
            f("msg")
        youfish._DEBUG = True
        for f in (lg.debug, lg.info, lg.warning, lg.error):
            f("boom")
        self.assertEqual(logged, ["inproc yt-dlp error: boom"])

    def test_deno_probe_counts_only_deno_spawns_on_an_armed_thread(self):
        import subprocess
        _swap(self, subprocess, Popen=subprocess.Popen)
        _swap(self, youfish, _orig_popen=None)
        self.addCleanup(lambda: youfish._inproc_tls.__dict__.pop("deno", None))
        deno = _script(self._tmp, "deno", "exit 0")
        other = _script(self._tmp, "node", "exit 0")
        youfish._inproc_install_deno_probe()
        wrapper = subprocess.Popen
        youfish._inproc_install_deno_probe()                         # idempotent
        self.assertIs(subprocess.Popen, wrapper)
        subprocess.run([deno])                                       # not armed: untouched
        acc = [0, 0.0]
        youfish._inproc_tls.deno = acc
        subprocess.run([deno])
        subprocess.run([other])
        p = subprocess.Popen([deno])
        p.wait()
        p.wait()                                                     # counted once per process
        self.assertEqual(acc[0], 2)
        self.assertGreaterEqual(acc[1], 0.0)


LaunchWarmups._real_delayed = youfish._delayed_ytdlp_warm


class DownloadsStorePath(_TmpDataDir):
    """The downloads list lives in the app's own data dir."""

    def test_path(self):
        self.assertEqual(youfish._downloads_path(), os.path.join(self._tmp, "downloads.json"))


class InnertubePool(unittest.TestCase):
    """innertube's keep-alive pool: reuse per host, idle expiry, a per-host cap, one retry on a
    stale reused socket (never on a fresh one), gunzip, and plain urllib behind an env proxy."""

    class _Resp:
        def __init__(self, status=200, data=b"{}", will_close=False, enc=""):
            import http.client
            self.status, self._data, self.will_close = status, data, will_close
            self.msg = http.client.HTTPMessage()
            if enc:
                self.msg["Content-Encoding"] = enc

        def read(self):
            return self._data

        def getheader(self, name):
            return self.msg.get(name)

    def setUp(self):
        test = self
        self.conns, self.replies = [], []

        class Conn:
            def __init__(self, host, timeout=None):
                self.host, self.timeout, self.reqs, self.closed = host, timeout, [], False
                self.sock = types.SimpleNamespace(settimeout=lambda t: self.reqs.append(("tmo", t)))
                test.conns.append(self)

            def request(self, method, path, body=None, headers=None):
                self.reqs.append((method, path, body, headers))
                r = test.replies.pop(0)
                if isinstance(r, Exception):
                    self._next = None
                    raise r
                self._next = r

            def getresponse(self):
                return self._next

            def close(self):
                self.closed = True
        self.Conn = Conn
        _swap(self, innertube, _pool={},
              http=types.SimpleNamespace(client=types.SimpleNamespace(HTTPSConnection=Conn)))
        _swap(self, innertube.urllib.request, getproxies=lambda: {})

    def test_connection_is_reused_per_host(self):
        self.replies = [self._Resp(data=b"a"), self._Resp(data=b"b"), self._Resp(data=b"c")]
        st, data, hdrs = innertube.request("POST", "https://www.youtube.com/youtubei/v1/next?x=1",
                                           b"{}", {"X": "1"}, timeout=4)
        self.assertEqual((st, data), (200, b"a"))
        self.assertEqual(self.conns[0].reqs[0],
                         ("POST", "/youtubei/v1/next?x=1", b"{}",
                          {"X": "1", "Accept-Encoding": "gzip"}))
        self.assertEqual(innertube._transport("GET", "https://www.youtube.com/a", timeout=9),
                         (200, b"b"))
        self.assertEqual(len(self.conns), 1)
        self.assertIn(("tmo", 9), self.conns[0].reqs)                 # reused: new timeout applied
        innertube.request("GET", "https://music.youtube.com/")
        self.assertEqual([c.host for c in self.conns], ["www.youtube.com", "music.youtube.com"])

    def test_server_close_is_not_pooled(self):
        self.replies = [self._Resp(will_close=True), self._Resp()]
        innertube.request("GET", "https://www.youtube.com/")
        self.assertTrue(self.conns[0].closed)
        innertube.request("GET", "https://www.youtube.com/")
        self.assertEqual(len(self.conns), 2)

    def test_idle_connections_expire(self):
        old = self.Conn("www.youtube.com")
        innertube._pool["www.youtube.com"] = [(old, time.time() - innertube._POOL_IDLE_MAX - 1)]
        conn, reused = innertube._get_conn("www.youtube.com", 5)
        self.assertFalse(reused)
        self.assertTrue(old.closed)
        self.assertIsNot(conn, old)

    def test_per_host_cap(self):
        extra = [self.Conn("h") for _ in range(innertube._POOL_PER_HOST + 1)]
        for c in extra:
            innertube._put_conn("h", c)
        self.assertEqual(len(innertube._pool["h"]), innertube._POOL_PER_HOST)
        self.assertEqual([c.closed for c in extra], [False] * innertube._POOL_PER_HOST + [True])

    def test_stale_reused_socket_is_retried_once_on_a_fresh_one(self):
        self.replies = [self._Resp(data=b"first"), ConnectionResetError("stale"),
                        self._Resp(data=b"fresh")]
        innertube.request("GET", "https://www.youtube.com/")
        self.assertEqual(innertube.request("GET", "https://www.youtube.com/")[1], b"fresh")
        self.assertEqual(len(self.conns), 2)
        self.assertTrue(self.conns[0].closed)

    def test_fresh_connection_failure_is_raised(self):
        self.replies = [ConnectionRefusedError("down")]
        with self.assertRaises(ConnectionRefusedError):
            innertube.request("GET", "https://www.youtube.com/")
        self.assertEqual(len(self.conns), 1)
        self.replies = [self._Resp(), OSError("stale"), OSError("still down")]
        innertube.request("GET", "https://www.youtube.com/")
        with self.assertRaises(OSError):
            innertube.request("GET", "https://www.youtube.com/")
        self.assertEqual(len(self.conns), 3)                          # one retry, not a loop

    def test_gzip_is_undone(self):
        import gzip
        self.replies = [self._Resp(data=gzip.compress(b'{"a": 1}'), enc="gzip")]
        self.assertEqual(innertube.request("GET", "https://www.youtube.com/")[1], b'{"a": 1}')

    def test_env_proxy_goes_through_urllib(self):
        import gzip
        import http.client
        import io
        seen = []
        h = http.client.HTTPMessage()
        h["Content-Encoding"] = "gzip"

        def urlopen(req, timeout=None):
            seen.append(req)
            if req.full_url.endswith("/err"):
                raise urllib.error.HTTPError(req.full_url, 429, "slow", h,
                                             io.BytesIO(gzip.compress(b"slow down")))
            r = _Blob(gzip.compress(b"proxied"))
            r.headers = h
            return r
        _swap(self, innertube.urllib.request, getproxies=lambda: {"https": "http://proxy:3128"},
              urlopen=urlopen)
        st, data, hdrs = innertube.request("GET", "https://www.youtube.com/x", headers={"A": "b"})
        self.assertEqual((st, data), (200, b"proxied"))
        self.assertEqual(seen[0].get_header("Accept-encoding"), "gzip")
        self.assertEqual(innertube.request("GET", "https://www.youtube.com/err")[:2],
                         (429, b"slow down"))
        self.assertEqual(self.conns, [])                              # the pool was never used


class InnertubeReelItem(unittest.TestCase):
    """Old-style Shorts (reelItemRenderer) parse to a short item; a bad id is skipped."""

    def test_reel(self):
        it = innertube._from_reel({"videoId": "abcdefghijk", "headline": {"simpleText": "Clip"},
                                   "viewCountText": {"simpleText": "1.2K views"}})
        self.assertEqual((it["id"], it["title"], it["views"], it["short"]),
                         ("abcdefghijk", "Clip", 1200, True))
        self.assertIsNone(innertube._from_reel({"videoId": "short"}))
        self.assertIsNone(innertube._from_reel({}))


# --- ytm.py: login, cookies, session ----------------------------------------------------------- #

def _cookie_db(path, rows, wal_open=False):
    """A Firefox/Gecko moz_cookies database. wal_open=True leaves the rows in an unflushed WAL (the
    browser is still running) and returns the open connection."""
    import sqlite3
    con = sqlite3.connect(path)
    if wal_open:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT, value TEXT, "
                "host TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)")
    con.commit()
    con.executemany("INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure) "
                    "VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    if wal_open:
        return con
    con.close()
    return None


_SIGNED_IN_ROWS = [
    ("SID", "sid1", ".youtube.com", "/", 1900000000, 1),
    ("__Secure-3PAPISID", "sap3", ".youtube.com", "/", 1900000000, 1),
    ("SAPISID", "sapA", ".google.com", "/", 1900000000, 1),
    ("YSC", "ysc", ".youtube.com", "/", 0, 1),                   # session cookie: not an auth one
    ("NID", "nid", "accounts.google.com", "", 1900000000, 0),
    ("tracker", "t", ".example.com", "/", 1900000000, 0),       # not Google: never read
]


class YtmCookieJar(_TmpDataDir):
    """Reading the Sailfish browser's cookie jar (including a login still in its WAL), the
    Netscape text for yt-dlp, the 0600 store, and folding Google's cookie rotations back in."""

    def test_reads_only_google_rows_and_cleans_up(self):
        db = os.path.join(self._tmp, "cookies.sqlite")
        _cookie_db(db, _SIGNED_IN_ROWS)
        made = []
        real = self.ytm.tempfile.mkdtemp
        _swap(self, self.ytm.tempfile, mkdtemp=lambda **k: made.append(real(**k)) or made[-1])
        rows = self.ytm._read_cookie_jar(db)
        self.assertEqual(sorted(r[0] for r in rows), ["NID", "SAPISID", "SID", "YSC",
                                                       "__Secure-3PAPISID"])
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))                   # the private copy is gone

    def test_a_fresh_login_still_in_the_wal_is_read(self):
        db = os.path.join(self._tmp, "cookies.sqlite")
        con = _cookie_db(db, _SIGNED_IN_ROWS, wal_open=True)
        try:
            self.assertTrue(os.path.exists(db + "-wal"))
            self.assertEqual(len(self.ytm._read_cookie_jar(db)), 5)
        finally:
            con.close()

    def test_unreadable_jar_is_empty(self):
        self.assertEqual(self.ytm._read_cookie_jar(os.path.join(self._tmp, "missing")), [])
        bad = os.path.join(self._tmp, "bad.sqlite")
        with open(bad, "w") as f:
            f.write("not a database")
        self.assertEqual(self.ytm._read_cookie_jar(bad), [])

    def test_netscape_text(self):
        txt = self.ytm._netscape_from_rows([
            ("SID", "v1", ".youtube.com", "/", 1900000000, 1),
            ("NID", "v2", "accounts.google.com", "", None, 0),
            ("X", "v3", "", "/", 0, 0)])                               # no host: skipped
        self.assertEqual(txt, "# Netscape HTTP Cookie File\n\n"
                              ".youtube.com\tTRUE\t/\tTRUE\t1900000000\tSID\tv1\n"
                              "accounts.google.com\tFALSE\t/\tFALSE\t0\tNID\tv2\n")

    def test_store_is_private_and_atomic(self):
        self.ytm._save_cookies({"cookie": "SID=a", "sapisid": "s"})
        path = self.ytm._cookies_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.ytm._save_cookies({"bad": object()})                   # can't serialise
        self.assertEqual(self.ytm._load_cookies(), {"cookie": "SID=a", "sapisid": "s"})
        self.assertEqual(sorted(os.listdir(self._tmp)), ["ytm_cookies.json"])

    def test_refresh_netscape_rewrites_only_rotated_values(self):
        txt = ("# Netscape HTTP Cookie File\n\n.youtube.com\tTRUE\t/\tTRUE\t0\tSIDCC\told\n"
               ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tkeep\n# SIDCC\tcomment\n")
        out = self.ytm._refresh_netscape(txt, {"SIDCC": "new"})
        self.assertIn("\tSIDCC\tnew\n", out)
        self.assertIn("\tSID\tkeep\n", out)
        self.assertIn("# SIDCC\tcomment", out)
        self.assertEqual(self.ytm._refresh_netscape(txt, {}), txt)
        self.assertEqual(self.ytm._refresh_netscape("", {"SIDCC": "x"}), "")

    def test_rotations_update_the_header_the_yt_dlp_text_and_the_sapisid(self):
        ytdlp = ".youtube.com\tTRUE\t/\tTRUE\t0\tSIDCC\told\n"
        self.ytm._save_cookies({"cookie": "SID=a; SIDCC=old", "sapisid": "s0", "ytdlp": ytdlp})
        self.ytm._persist_rotations({"SIDCC": "new", "__Secure-3PAPISID": "s1", "TRACK": "x"})
        c = self.ytm._load_cookies()
        self.assertEqual(c["cookie"], "SID=a; SIDCC=new; __Secure-3PAPISID=s1")
        self.assertEqual(c["sapisid"], "s1")
        self.assertIn("\tSIDCC\tnew", c["ytdlp"])
        self.assertAlmostEqual(c["rotated_at"], time.time(), delta=5)

    def test_unchanged_or_signed_out_writes_nothing(self):
        saves = []
        real = self.ytm._save_cookies
        _swap(self, self.ytm, _save_cookies=lambda c: saves.append(c) or real(c))
        self.ytm._persist_rotations({"SIDCC": "x"})                  # nothing stored: signed out
        real({"cookie": "SID=a; SIDCC=same", "sapisid": "s"})
        self.ytm._persist_rotations({"SIDCC": "same", "UNTRACKED": "y"})
        self.assertEqual(saves, [])


class YtmBrowserImport(_TmpDataDir):
    """'Import login from browser': finds the jar, refuses a signed-out one, stores only the auth
    cookies in the header, and says so when Google already treats the session as signed out."""

    def setUp(self):
        super().setUp()
        self.empty = os.path.join(self._tmp, "empty.sqlite")
        self.jar = os.path.join(self._tmp, "jar.sqlite")
        _cookie_db(self.empty, [])
        _swap(self, self.ytm, _BROWSER_COOKIE_PATHS=[os.path.join(self._tmp, "missing.sqlite"),
                                                     self.empty, self.jar])
        self.verdict = {"ok": True, "account": "Vee", "checked": True, "present": True}
        _swap(self, self.ytm, verify_session=lambda: self.verdict)

    def test_signed_in_jar_is_imported(self):
        _cookie_db(self.jar, _SIGNED_IN_ROWS)
        r = self.ytm.import_browser_login()
        self.assertEqual(r, {"ok": True, "count": 3, "live": True, "account": "Vee"})
        c = self.ytm._load_cookies()
        self.assertEqual(c["cookie"], "__Secure-3PAPISID=sap3; SAPISID=sapA; SID=sid1")
        self.assertEqual((c["sapisid"], c["source"]), ("sap3", self.jar))
        self.assertIn("\tYSC\tysc", c["ytdlp"])                      # yt-dlp gets every row
        self.assertNotIn("tracker", c["ytdlp"])
        self.assertEqual(os.stat(self.ytm._cookies_path()).st_mode & 0o777, 0o600)

    def test_stale_session_is_imported_with_a_warning(self):
        _cookie_db(self.jar, _SIGNED_IN_ROWS)
        self.verdict = {"ok": False, "account": "", "checked": True, "present": True}
        r = self.ytm.import_browser_login()
        self.assertEqual((r["ok"], r["live"]), (True, False))
        self.assertIn("still sees you as signed out", r["warning"])
        self.verdict = {"ok": False, "account": "", "checked": False, "present": True}
        self.assertTrue(self.ytm.import_browser_login()["live"])    # offline: stay optimistic

    def test_no_jar_or_signed_out_jar_is_refused(self):
        r = self.ytm.import_browser_login()
        self.assertEqual((r["ok"], r["count"]), (False, 0))
        self.assertIn("No browser cookies found", r["error"])
        _cookie_db(self.jar, [("SID", "sid1", ".youtube.com", "/", 0, 1)])
        r = self.ytm.import_browser_login()
        self.assertFalse(r["ok"])
        self.assertIn("not a signed-in Google session", r["error"])
        self.assertFalse(os.path.exists(self.ytm._cookies_path()))


class YtmSessionState(_TmpDataDir):
    """Which login is in use, and verify_session's three answers: signed in (with the name),
    conclusively signed out, and inconclusive (offline), which must not read as signed out."""

    def setUp(self):
        super().setUp()
        self.reply = {}
        self.asked = []

        def innertube_call(endpoint, body, **kw):
            self.asked.append(endpoint)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        _swap(self, self.ytm, _innertube=innertube_call)

    def test_auth_mode_and_account_status(self):
        self.assertEqual(self.ytm.account_status(), {"logged_in": False, "method": "none"})
        self.ytm._save_tokens({"refresh_token": "r"})
        self.assertEqual(self.ytm.account_status(), {"logged_in": True, "method": "oauth"})
        self.ytm._save_cookies({"sapisid": "s", "cookie": "SAPISID=s"})
        self.assertEqual(self.ytm.account_status()["method"], "cookie")    # cookies win

    def test_signed_out_is_not_asked(self):
        self.assertEqual(self.ytm.verify_session(),
                         {"ok": False, "account": "", "checked": True, "present": False})
        self.assertEqual(self.asked, [])

    def test_verdicts(self):
        self.ytm._save_cookies({"sapisid": "s", "cookie": "SAPISID=s"})
        self.reply = {"actions": [{"openPopupAction": {"popup": {"multiPageMenuRenderer": {
            "header": {"activeAccountHeaderRenderer": {
                "accountName": {"runs": [{"text": "Vee "}, {"text": "W"}]}}}}}}}]}
        self.assertEqual(self.ytm.verify_session(),
                         {"ok": True, "account": "Vee W", "checked": True, "present": True})
        self.assertEqual(self.asked, ["account/account_menu"])
        self.reply = {"x": [{"activeAccountHeaderRenderer": {"accountName": {}}}]}
        self.assertEqual(self.ytm.verify_session()["account"], "your account")
        self.reply = {"actions": [{"signInEndpoint": {}}]}           # the anonymous menu
        self.assertEqual(self.ytm.verify_session(),
                         {"ok": False, "account": "", "checked": True, "present": True})
        self.reply = OSError("offline")
        self.assertEqual(self.ytm.verify_session(),
                         {"ok": False, "account": "", "checked": False, "present": True})


class YtmOAuth(_TmpDataDir):
    """OAuth device login and token refresh: every terminal state of the polling loop reaches
    QML, and a refresh that races a logout never writes the old session back."""

    def setUp(self):
        super().setUp()
        self.posts, self.replies = [], []

        def post(url, fields, timeout=30):
            self.posts.append((url, dict(fields)))
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            if callable(r):
                return r()
            return r
        _swap(self, self.ytm, _post_form=post)

    def test_access_token_cached_refreshed_or_none(self):
        self.assertIsNone(self.ytm._access_token())                  # signed out: never asks
        self.ytm._save_tokens({"refresh_token": "R", "access_token": "A",
                               "expires_at": time.time() + 600})
        self.assertEqual(self.ytm._access_token(), "A")
        self.assertEqual(self.posts, [])
        self.ytm._save_tokens({"refresh_token": "R", "access_token": "A",
                               "expires_at": time.time() + 30})      # inside the 60s margin
        self.replies = [{"access_token": "B", "expires_in": 100}]
        self.assertEqual(self.ytm._access_token(), "B")
        self.assertEqual(self.posts[0][0], self.ytm._OAUTH_TOKEN_URL)
        self.assertEqual((self.posts[0][1]["grant_type"], self.posts[0][1]["refresh_token"]),
                         ("refresh_token", "R"))
        t = self.ytm._load_tokens()
        self.assertEqual(t["access_token"], "B")
        self.assertAlmostEqual(t["expires_at"], time.time() + 100, delta=5)

    def test_refresh_failures_return_none(self):
        self.ytm._save_tokens({"refresh_token": "R"})
        self.replies = [OSError("offline"), {"error": "invalid_grant"}]
        self.assertIsNone(self.ytm._access_token())
        self.assertIsNone(self.ytm._access_token())

    def test_refresh_racing_a_logout_is_not_written_back(self):
        self.ytm._save_tokens({"refresh_token": "R"})

        def logout_meanwhile():
            self.ytm.logout()
            return {"access_token": "B"}
        self.replies = [logout_meanwhile]
        self.assertEqual(self.ytm._access_token(), "B")
        self.assertEqual(self.ytm._load_tokens(), {})

    def _login(self, replies):
        """Run login_begin to its terminal event on a virtual clock; returns (events, sleeps)."""
        sent = _fake_pyotherside(self)
        clock = [1000.0]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            clock[0] += s
        _swap(self, self.ytm, time=_ModProxy(time, time=lambda: clock[0], sleep=sleep))
        self.replies = replies
        self.ytm.login_begin()
        self.assertTrue(_wait_for(lambda: any(e[0] == "ytm_login_done" for e in sent)))
        return sent, sleeps

    @staticmethod
    def _pending(err):
        import io
        return urllib.error.HTTPError("u", 428, "x", {}, io.BytesIO(json.dumps({"error": err}).encode()))

    def _device(self, **kw):
        d = {"user_code": "ABCD-EFGH", "verification_url": "https://google.com/device",
             "device_code": "DEV", "interval": 2, "expires_in": 60}
        d.update(kw)
        return d

    def test_device_login_succeeds_after_pending_and_slow_down(self):
        sent, sleeps = self._login([self._device(), self._pending("authorization_pending"),
                                    {"error": "slow_down"},
                                    {"access_token": "A", "refresh_token": "R", "expires_in": 3600}])
        self.assertEqual(sent, [("ytm_login_code", "ABCD-EFGH", "https://google.com/device"),
                                ("ytm_login_done", True, "")])
        self.assertEqual(sleeps, [2, 2, 7])
        self.assertEqual(self.posts[1][1]["device_code"], "DEV")
        self.assertEqual(self.ytm._load_tokens()["refresh_token"], "R")
        self.assertEqual(self.ytm._auth_mode(), "oauth")

    def test_device_login_failures(self):
        cases = [
            ([OSError("offline")], "Couldn't start login: offline"),
            ([{"user_code": "X"}], "Login service gave no device code."),
            ([self._device(), self._pending("access_denied")], "Login denied."),
            ([self._device(), {"error": "expired_token"}], "Login expired."),
            ([self._device(), {"error": "invalid_client"}], "invalid_client"),
            ([self._device(), OSError("dropped")], "dropped"),
        ]
        for replies, msg in cases:
            sent, _ = self._login(replies)
            self.assertEqual(sent[-1], ("ytm_login_done", False, msg), replies)
        self.assertEqual(self.ytm._load_tokens(), {})

    def test_device_login_times_out(self):
        sent, sleeps = self._login([self._device(interval=20, expires_in=50)]
                                   + [self._pending("authorization_pending")
                                      for _ in range(5)])
        self.assertEqual(sent[-1], ("ytm_login_done", False, "Login timed out."))
        self.assertEqual(sleeps, [20, 20, 20])


class YtmHttpHelpers(unittest.TestCase):
    """The two plain HTTP helpers: a form POST for OAuth and a gzip-aware JSON GET."""

    def setUp(self):
        import ytm
        self.ytm, self.reqs = ytm, []
        self.resp = None
        _swap(self, ytm, _force_ipv4=lambda: None)

        def urlopen(req, timeout=None):
            self.reqs.append((req, timeout))
            return self.resp
        _swap(self, ytm.urllib.request, urlopen=urlopen)

    def test_post_form(self):
        self.resp = YtmIdentity._Resp('{"device_code": "D"}')
        self.assertEqual(self.ytm._post_form("https://oauth2.googleapis.com/device/code",
                                             {"client_id": "a b", "scope": "s"}), {"device_code": "D"})
        req, timeout = self.reqs[0]
        self.assertEqual((req.get_method(), req.data, timeout), ("POST", b"client_id=a+b&scope=s", 30))
        self.assertEqual(req.get_header("Content-type"), "application/x-www-form-urlencoded")

    def test_get_json_gunzips(self):
        import gzip
        r = _Blob(gzip.compress(b'{"syncedLyrics": "x"}'), {"Content-Encoding": "gzip"})
        self.resp = r
        self.assertEqual(self.ytm._http_get_json("https://lrclib.net/api/get?x=1"),
                         {"syncedLyrics": "x"})
        req, timeout = self.reqs[0]
        self.assertEqual((req.get_header("Accept-encoding"), timeout), ("gzip", 15))
        self.assertIn("FinTune", req.get_header("User-agent"))


class YtmVisitorAndConfig(_TmpDataDir):
    """The visitor id and the scraped client identity are cached, fetched once at a time, and a
    failed fetch neither caches garbage nor retries on every request."""

    def setUp(self):
        super().setUp()
        ytm = self.ytm
        saved_visitor = dict(ytm._visitor_cache)
        self.addCleanup(lambda: (ytm._visitor_cache.clear(), ytm._visitor_cache.update(saved_visitor)))
        ytm._visitor_cache.update({"id": "", "at": 0})
        _swap(self, ytm, _force_ipv4=lambda: None, _ytm_cfg_cache=None, _ytm_cfg_warming=False,
              _ytm_cfg_last_try=0.0, _visitor_warming=False)
        self.pages, self.fetches = [], 0

        def urlopen(req, timeout=None):
            self.fetches += 1
            page = self.pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return YtmIdentity._Resp(page)
        _swap(self, ytm.urllib.request, urlopen=urlopen)

    def test_visitor_id_is_scraped_and_cached(self):
        self.assertEqual(self.ytm._visitor_id_cached(), "")
        self.pages = [OSError("offline"), '<script>{"VISITOR_DATA":"CgtWSVM%3D"}</script>']
        self.assertEqual(self.ytm._visitor_id(), "")                   # failure: nothing cached
        self.assertEqual(self.ytm._visitor_id(), "CgtWSVM%3D")
        self.assertEqual(self.ytm._visitor_id(), "CgtWSVM%3D")
        self.assertEqual(self.ytm._visitor_id_cached(), "CgtWSVM%3D")
        self.assertEqual(self.fetches, 2)
        self.ytm._visitor_cache["at"] = time.time() - 3601            # an hour later: stale
        self.assertEqual(self.ytm._visitor_id_cached(), "")
        self.pages = ['{"visitorData":"Second"}']
        self.assertEqual(self.ytm._visitor_id(), "Second")

    def test_visitor_warm_is_single_flight(self):
        gate, calls = threading.Event(), []
        _swap(self, self.ytm, _visitor_id=lambda: calls.append(1) or gate.wait(5))
        self.addCleanup(gate.set)
        self.ytm._warm_visitor_id()
        self.ytm._warm_visitor_id()
        self.assertTrue(_wait_for(lambda: calls))
        gate.set()
        self.assertTrue(_wait_for(lambda: not self.ytm._visitor_warming))
        self.assertEqual(calls, [1])

    def test_identity_defaults_then_live(self):
        fetched = []
        _swap(self, self.ytm, _fetch_ytm_identity=lambda timeout=15: fetched.append(1) or
              ("AIzaNEW", "1.20261020.01.00"))
        self.assertEqual(self.ytm._ytm_config_path(), os.path.join(self._tmp, "ytm_config.json"))
        ident = self.ytm.innertube_identity()                         # cold: defaults + a refresh
        self.assertEqual(ident, {"version": self.ytm._DEFAULT_CLIENT_VERSION, "source": "default"})
        self.assertTrue(_wait_for(lambda: not self.ytm._ytm_cfg_warming and fetched))
        self.assertEqual(self.ytm.innertube_identity(),
                         {"version": "1.20261020.01.00", "source": "live"})
        self.assertEqual(self.ytm._context()["client"]["clientVersion"], "1.20261020.01.00")
        with open(self.ytm._ytm_config_path()) as f:
            self.assertEqual(json.load(f)["key"], "AIzaNEW")
        self.assertEqual(fetched, [1])                                # fresh now: no more scrapes

    def test_failed_scrape_keeps_defaults_and_waits_before_retrying(self):
        fetched = []
        _swap(self, self.ytm, _fetch_ytm_identity=lambda timeout=15: fetched.append(1) or
              (None, None))
        self.ytm._ytm_config()
        self.assertTrue(_wait_for(lambda: not self.ytm._ytm_cfg_warming and fetched))
        self.ytm._ytm_config()
        self.ytm._ytm_config()
        time.sleep(0.05)
        self.assertEqual(fetched, [1])                                # the 10-minute retry gate
        self.assertFalse(os.path.exists(self.ytm._ytm_config_path()))
        self.assertEqual(self.ytm._ytm_config()["version"], self.ytm._DEFAULT_CLIENT_VERSION)


class YtmLibraryEdits(_TmpDataDir):
    """Adding to a playlist and listing library playlists: signed-out refusals, the VL prefix,
    a refused edit reported as one, and only real playlist cards listed."""

    def setUp(self):
        super().setUp()
        self.calls, self.reply = [], {}
        self.ytm._save_cookies({"sapisid": "s", "cookie": "SAPISID=s"})

        def innertube_call(endpoint, body, **kw):
            self.calls.append((endpoint, body))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        _swap(self, self.ytm, _innertube=innertube_call)

    def test_add_to_playlist(self):
        self.assertEqual(self.ytm.add_to_playlist("VLPL123", "vid12345678"), {"ok": True})
        self.assertEqual(self.calls, [("browse/edit_playlist", {
            "playlistId": "PL123",
            "actions": [{"action": "ACTION_ADD_VIDEO", "addedVideoId": "vid12345678"}]})])
        self.reply = {"status": "STATUS_FAILED"}
        self.assertEqual(self.ytm.add_to_playlist("PL123", "v"),
                         {"ok": False, "error": "Couldn't add (playlist may not be editable)"})
        self.reply = OSError("HTTP Error 401")
        self.assertEqual(self.ytm.add_to_playlist("PL123", "v"),
                         {"ok": False, "error": "HTTP Error 401"})
        self.assertFalse(self.ytm.add_to_playlist("", "v")["ok"])
        self.ytm.logout()
        self.assertEqual(self.ytm.add_to_playlist("PL123", "v"),
                         {"ok": False, "error": "Not signed in"})

    def test_library_playlists(self):
        def card(title, bid=None):
            nav = {"browseEndpoint": {"browseId": bid}} if bid else {}
            return {"musicTwoRowItemRenderer": {"title": {"runs": [{"text": title}]},
                                                "subtitle": {"runs": [{"text": "Playlist"}]},
                                                "navigationEndpoint": nav}}
        self.reply = {"contents": {"grid": {"items": [card("Liked Music", "VLLM"),
                                                      card("New playlist"),
                                                      card("Road trip", "VLPL9")]}}}
        r = self.ytm.get_library_playlists()
        self.assertEqual(self.calls[0], ("browse", {"browseId": "FEmusic_liked_playlists"}))
        self.assertEqual([(p["title"], p["browseId"]) for p in r["playlists"]],
                         [("Liked Music", "VLLM"), ("Road trip", "VLPL9")])
        self.reply = OSError("offline")
        self.assertEqual(self.ytm.get_library_playlists(),
                         {"logged_in": True, "playlists": [], "error": "offline"})
        self.ytm.logout()
        self.assertEqual(self.ytm.get_library_playlists(), {"logged_in": False, "playlists": []})

    def test_clear_cached_lyrics(self):
        path = self.ytm._lyrics_cache_path("vid12345678")
        with open(path, "w") as f:
            f.write("{}")
        self.ytm.clear_cached_lyrics("vid12345678")
        self.assertFalse(os.path.exists(path))
        self.ytm.clear_cached_lyrics("vid12345678")                   # already gone: fine
        outside = os.path.join(self._tmp, "settings.json")
        with open(outside, "w") as f:
            f.write("{}")
        self.ytm.clear_cached_lyrics("../settings")                   # sanitised: stays inside
        self.assertTrue(os.path.exists(outside))


class YtmDebugShape(unittest.TestCase):
    """The debug response dumps are values-free and silent unless debugging."""

    def setUp(self):
        import ytm
        self.ytm, self.logged = ytm, []
        _swap(self, ytm, _log=self.logged.append, _DEBUG=False)

    def test_skeleton_drops_values(self):
        data = {"a": {"name": "Vee", "n": 3, "items": [{"id": "secret"}, {"id": "x"}], "e": []}}
        self.assertEqual(self.ytm._skeleton(data),
                         {"a": {"name": "str", "n": "int", "items": [{"id": "str"}], "e": []}})
        self.assertEqual(self.ytm._skeleton({"a": {"b": 1}}, maxdepth=0), {"a": "…"})

    def test_dump_shape_only_when_debugging(self):
        data = {"contents": {"musicShelfRenderer": {"x": 1}}, "continuationContents": {}}
        self.ytm._dump_shape("home", data)
        self.assertEqual(self.logged, [])
        self.ytm._DEBUG = True
        self.ytm._dump_shape("home", data)
        self.assertEqual(self.logged[0], "home top-keys: ['contents', 'continuationContents']")
        self.assertEqual(self.logged[1],
                         "home renderer-keys: ['continuationContents', 'musicShelfRenderer']")
        self.assertTrue(self.logged[2].startswith("home raw-head: {"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
