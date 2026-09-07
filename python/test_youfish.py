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

import json
import os
import shutil
import threading
import time
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import youfish  # noqa: E402


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
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: []
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
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: (
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
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: \
            ["--extractor-args", "youtube:player_client=tv_embedded"]
        youfish.resolve("vidA")
        youfish.resolve("vidB")
        self.assertEqual(len(_FakeYDL.instances), 1)           # same warm YoutubeDL, not rebuilt
        self.assertEqual(len(_FakeYDL.instances[0].extract_calls), 2)

    def test_inproc_failure_falls_back_to_binary(self):
        _FakeYDL.raise_on_extract = True
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: \
            ["--extractor-args", "youtube:player_client=tv_embedded"]
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")          # binary served it
        self.assertTrue(self.binary_calls)                     # fallback actually ran

    def test_token_path_never_runs_in_process(self):
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: \
            ["--extractor-args", "youtube:player_client=mweb;fetch_pot=always"]
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")
        self.assertEqual(_FakeYDL.instances, [])               # in-process path skipped entirely

    def test_no_zipapp_uses_binary(self):
        youfish._import_yt_dlp = lambda: None   # copy absent / not importable → binary path
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: []
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
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: \
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
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False: []
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
