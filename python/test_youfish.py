#!/usr/bin/env python3
"""Offline regression tests for FinTune's shared engine (youfish.py).

Runs with NO device, network, yt-dlp, or PO-token server — resolve()'s externals are all mocked,
so this exercises the pure format-selection + resolve wiring in isolation. Run:  python3 test_youfish.py

These exist because this layer kept regressing: the `by_itag` NameError that crashed EVERY music
resolve, and the dub-instead-of-source audio pick. Both are covered below so they can't
come back silently. youfish.py imports `pyotherside` only inside functions, so `import youfish` is safe
here.
"""

import contextlib
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import youfish  # noqa: E402


# --- tiny format builders (mirror yt-dlp's --dump-single-json format dicts) ------------------- #

def vf(fid, height, vcodec, fps=30, url="v"):
    """A video-only format."""
    return {"format_id": fid, "height": height, "vcodec": vcodec, "acodec": "none",
            "fps": fps, "url": url, "http_headers": {"User-Agent": "UA"}}


def af(fid, abr, acodec, url="a", note=None, lang_pref=None):
    """An audio-only format."""
    f = {"format_id": fid, "abr": abr, "acodec": acodec, "vcodec": "none", "url": url,
         "http_headers": {"User-Agent": "UA"}}
    if note is not None:
        f["format_note"] = note
    if lang_pref is not None:
        f["language_preference"] = lang_pref
    return f


def muxed(fid="18", url="m"):
    return {"format_id": fid, "height": 360, "vcodec": "avc1", "acodec": "mp4a.40.2",
            "url": url, "protocol": "https", "http_headers": {"User-Agent": "UA"}}


# --- pure helpers ----------------------------------------------------------------------------- #

class CodecFamily(unittest.TestCase):
    def test_video(self):
        self.assertEqual(youfish._codec_family("avc1.4d401f"), "h264")
        self.assertEqual(youfish._codec_family("vp09.00.10"), "vp9")
        self.assertEqual(youfish._codec_family("av01.0.08M"), "")   # AV1 undecodable here
        self.assertEqual(youfish._codec_family(None), "")

    def test_audio(self):
        self.assertEqual(youfish._audio_family("opus"), "opus")
        self.assertEqual(youfish._audio_family("mp4a.40.2"), "aac")
        self.assertEqual(youfish._audio_family("none"), "")
        self.assertEqual(youfish._audio_family(None), "")


class AudioOrigPref(unittest.TestCase):
    def test_language_preference_field_wins(self):
        self.assertEqual(youfish._audio_orig_pref({"language_preference": 10}), 10)
        self.assertEqual(youfish._audio_orig_pref({"language_preference": -1}), -1)

    def test_note_fallback(self):
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English original (default)"}), 10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English descriptive"}), -10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "Portuguese"}), 0)
        self.assertEqual(youfish._audio_orig_pref({}), 0)


class VideoCandidates(unittest.TestCase):
    def setUp(self):
        self._gs = youfish.get_settings
        youfish.get_settings = lambda: {"hw_decode": False}

    def tearDown(self):
        youfish.get_settings = self._gs

    def test_excludes_av1_and_over_max_height(self):
        fmts = [vf("137", 1080, "avc1"), vf("399", 1080, "av01"), vf("271", 1440, "vp09")]
        got = [f["format_id"] for f in youfish._video_candidates(fmts)]
        self.assertEqual(got, ["137"])   # av01 + 1440p dropped

    def test_sw_prefers_h264_hw_prefers_vp9(self):
        fmts = [vf("137", 1080, "avc1"), vf("248", 1080, "vp09")]
        self.assertEqual(youfish._video_candidates(fmts)[0]["format_id"], "137")
        youfish.get_settings = lambda: {"hw_decode": True}
        self.assertEqual(youfish._video_candidates(fmts)[0]["format_id"], "248")

    def test_sorted_by_height_desc(self):
        fmts = [vf("135", 480, "avc1"), vf("137", 1080, "avc1"), vf("136", 720, "avc1")]
        got = [f["height"] for f in youfish._video_candidates(fmts)]
        self.assertEqual(got, [1080, 720, 480])


class AudioCandidates(unittest.TestCase):
    def test_ladder_order_matches_old_hardcoded(self):
        # The old hand-tuned tuple was 251,140,250,249,139,600,599 — bitrate order reproduces it.
        fmts = [af("250", 70, "opus"), af("140", 128, "mp4a.40.2"), af("251", 160, "opus"),
                af("599", 31, "mp4a"), af("249", 50, "opus"), af("139", 48, "mp4a.40.5"),
                af("600", 35, "opus")]
        got = [f["format_id"] for f in youfish._audio_candidates(fmts)]
        self.assertEqual(got, ["251", "140", "250", "249", "139", "600", "599"])

    def test_source_beats_same_bitrate_dub(self):
        # The R52 regression: a Portuguese dub at equal bitrate must NOT outrank the English source.
        fmts = [af("251-3", 160, "opus", lang_pref=-1),    # dub
                af("251-0", 160, "opus", lang_pref=10)]    # source
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-0")

    def test_source_beats_higher_bitrate_dub(self):
        # Language is the PRIMARY key: source wins even if a dub somehow has a higher bitrate.
        fmts = [af("251-3", 160, "opus", lang_pref=-1),    # dub, higher bitrate
                af("140-0", 128, "mp4a.40.2", lang_pref=10)]  # source, lower bitrate
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "140-0")

    def test_excludes_video_only_and_empty(self):
        self.assertEqual(youfish._audio_candidates([vf("137", 1080, "avc1")]), [])
        self.assertIsNone(youfish._pick_audio([vf("137", 1080, "avc1")]))


# --- resolve() smoke tests (externals mocked) ------------------------------------------------- #

class ResolveSmoke(unittest.TestCase):
    """Guards the by_itag crash + verifies resolve() returns usable audio for the music path."""

    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args",
                     "_yt_extractor_args", "_proxied", "get_settings", "_cookies_args"):
            self._saved[name] = getattr(youfish, name)
        self._saved["run"] = youfish.subprocess.run

        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._yt_extractor_args = lambda client_override=None: []
        youfish._proxied = lambda url, *a, **k: url          # identity, so we can read it back
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False}
        youfish._cookies_args = self._no_cookies

    def tearDown(self):
        for name, fn in self._saved.items():
            if name == "run":
                youfish.subprocess.run = fn
            else:
                setattr(youfish, name, fn)

    @staticmethod
    @contextlib.contextmanager
    def _no_cookies():
        yield []

    def _mock_ytdlp(self, formats, title="T"):
        data = {"title": title, "formats": formats, "duration": 100}
        def fake_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def test_music_resolve_returns_audio_urls(self):
        # web_embedded-style language-split itags: this is the exact shape that crashed on by_itag.
        self._mock_ytdlp([af("251-0", 160, "opus", lang_pref=10),
                          af("251-3", 160, "opus", lang_pref=-1),
                          af("140-0", 128, "mp4a.40.2", lang_pref=10),
                          muxed()])
        res = youfish.resolve("vid", True)     # audio_only=True (music path)
        self.assertTrue(res.get("ok"), res)
        info = res["info"]
        self.assertTrue(info["audio_url"])                       # single pick present
        self.assertGreaterEqual(len(info["audio_urls"]), 2)      # ladder populated (opus + aac)
        # dedup by (codec, bitrate) collapsed the language variants to one opus rung:
        self.assertEqual(info["audio_urls"].count("a"), len(info["audio_urls"]))  # all real urls
        self.assertNotIn("251-3", info.get("audio_itag", ""))    # source opus, not the dub

    def test_video_resolve_picks_source_audio(self):
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("251-3", 160, "opus", lang_pref=-1),   # dub
                          af("251-0", 160, "opus", lang_pref=10),   # source
                          muxed()])
        res = youfish.resolve("vid", False)    # video path
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["audio_itag"], "251-0")      # source, not the Portuguese dub
        self.assertEqual(res["info"]["video_itag"], "137")

    def test_resolve_error_when_ytdlp_fails(self):
        def fail_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm")
        youfish.subprocess.run = fail_run
        res = youfish.resolve("vid", True)
        self.assertFalse(res.get("ok"))
        self.assertIn("Sign in", res.get("error", ""))


class YtmIdentity(unittest.TestCase):
    """The self-healing InnerTube identity: scrape the live client version/key from the ytcfg blob,
    fall back to the shipped defaults when the cache is cold. Guards the scrape regex — the one bit
    of new parsing that could silently stop matching if YouTube reshapes music.youtube.com."""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
