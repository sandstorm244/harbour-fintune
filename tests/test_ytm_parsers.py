#!/usr/bin/env python3
"""ytm.py's YouTube Music parsers (home, search, artist, album, radio) and the LRCLIB
lyrics lookup, against real answers recorded in tests/fixtures/ytm.json.gz. Same idea and flags
as tests/test_parsers.py (which covers innertube.py):

    python3 tests/test_ytm_parsers.py            # offline, against the recording
    python3 tests/test_ytm_parsers.py --live     # the same checks against the live services
    python3 tests/test_ytm_parsers.py --record   # re-record (saved only if the checks pass)

Recorded signed out, in a throwaway data dir, so no login is involved.
"""
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))
sys.path.insert(0, HERE)
import ytm  # noqa: E402
from test_parsers import FIXTURES, _scrub  # noqa: E402

MODE = "replay"
FIXTURE = os.path.join(FIXTURES, "ytm.json.gz")
QUERY = "daft punk"
LYRICS = ("Get Lucky", "Daft Punk", 248)


def _key(kind, what, body=None):
    return "%s %s %s" % (kind, what, json.dumps(body or {}, sort_keys=True))


def _run():
    """Search → first artist → their first album → the first song's radio; and a lyrics lookup.
    (Loudness comes from innertube's player answer, checked in test_parsers.py.)"""
    out = {"home": ytm.get_home(), "search": ytm.search(QUERY)}
    items = out["search"]["items"]
    artist = next(i for i in items if i.get("kind") == "artist")
    out["artist"] = ytm.get_artist(artist["browseId"])
    album = next(it for sh in out["artist"]["shelves"] for it in sh["items"] if it.get("browseId"))
    out["album"] = ytm.get_playlist(album["browseId"])
    song = next(i for i in items if i.get("kind") == "song")
    out["radio"] = ytm.get_radio(song["videoId"])
    out["lyrics"] = ytm.get_lyrics(*LYRICS)
    return out


_STATE = {}


def data():
    if "out" in _STATE:
        return _STATE["out"]
    saved = (ytm._innertube, ytm._http_get_json, ytm._data_dir)
    tmp = tempfile.mkdtemp(prefix="ytmfix-")
    ytm._data_dir = lambda: tmp                      # signed out, no caches from a real install
    calls = {}
    if MODE == "replay":
        with gzip.open(FIXTURE, "rt") as f:
            calls = json.load(f)["calls"]

        def answer(k):
            if k not in calls:
                raise AssertionError("no recording for %s — re-record (--record)" % k[:160])
            return json.loads(calls[k][0]["data"])
        ytm._innertube = lambda endpoint, body, **kw: answer(_key("POST", endpoint, body))
        ytm._http_get_json = lambda url, timeout=15: answer(_key("GET", url))
    elif MODE == "record":
        real_it, real_get = ytm._innertube, ytm._http_get_json

        def rec_it(endpoint, body, **kw):
            res = real_it(endpoint, body, **kw)
            calls.setdefault(_key("POST", endpoint, body), []).append(
                {"status": 200, "data": json.dumps(res)})
            return res

        def rec_get(url, timeout=15):
            res = real_get(url, timeout)
            calls.setdefault(_key("GET", url), []).append({"status": 200, "data": json.dumps(res)})
            return res
        ytm._innertube, ytm._http_get_json = rec_it, rec_get
    try:
        _STATE["out"] = _run()
    finally:
        ytm._innertube, ytm._http_get_json, ytm._data_dir = saved
        shutil.rmtree(tmp, ignore_errors=True)
    _STATE["calls"] = calls
    return _STATE["out"]


class YtmParsers(unittest.TestCase):
    def assertTracks(self, tracks, n):
        self.assertGreaterEqual(len(tracks), n, "too few tracks parsed")
        for t in tracks:
            self.assertRegex(t.get("videoId") or "", r"^[\w-]{11}$")
            self.assertTrue(t.get("title"), t)
        self.assertGreaterEqual(sum(1 for t in tracks if t.get("subtitle")), 0.8 * len(tracks),
                                "artist lines missing")

    def test_home_shelves(self):
        home = data()["home"]
        self.assertFalse(home["logged_in"])
        self.assertGreaterEqual(len(home["shelves"]), 3)
        for sh in home["shelves"]:
            self.assertTrue(sh.get("title"), sh)
            self.assertTrue(sh.get("items"), sh["title"])
            for it in sh["items"]:
                self.assertTrue(it.get("title"), it)
                self.assertTrue(it.get("videoId") or it.get("browseId"), it)

    def test_search_kinds(self):
        items = data()["search"]["items"]
        self.assertGreaterEqual(len(items), 10)
        kinds = {i.get("kind") for i in items}
        self.assertTrue({"song", "artist", "album"} <= kinds, kinds)
        self.assertTracks([i for i in items if i.get("kind") == "song"], 3)

    def test_artist_page(self):
        a = data()["artist"]
        self.assertTrue(a["ok"], a)
        self.assertIn("Daft Punk", a["name"])
        self.assertTracks(a["songs"], 5)
        self.assertTrue(a["shelves"])
        self.assertTrue(any(it.get("browseId") for sh in a["shelves"] for it in sh["items"]))

    def test_album_tracklist(self):
        al = data()["album"]
        self.assertTrue(al["ok"], al)
        self.assertTrue(al["title"])
        self.assertGreaterEqual(len(al["tracks"]), 5)
        for t in al["tracks"]:
            self.assertRegex(t.get("videoId") or "", r"^[\w-]{11}$")
            self.assertTrue(t.get("title"), t)

    def test_radio(self):
        r = data()["radio"]
        self.assertNotIn("error", r)
        self.assertTracks(r["tracks"], 10)
        self.assertEqual(len({t["videoId"] for t in r["tracks"]}), len(r["tracks"]))

    def test_lyrics_synced(self):
        ly = data()["lyrics"]
        self.assertTrue(ly["ok"], ly)
        self.assertGreaterEqual(len(ly["synced"]), 10)
        ts = [l["t"] for l in ly["synced"]]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(any(re.search(r"lucky", l["line"], re.I) for l in ly["synced"]))


def main(argv):
    global MODE
    if "--live" in argv:
        MODE = "live"
    elif "--record" in argv:
        MODE = "record"
    res = unittest.main(argv=[sys.argv[0]] + [a for a in argv if not a.startswith("--")],
                        exit=False, verbosity=1).result
    if MODE == "record":
        if not res.wasSuccessful() and "--force" not in argv:
            print("Checks failed: recording NOT saved (a parser needs updating first, or "
                  "--record --force to save anyway).")
            return 1
        os.makedirs(FIXTURES, exist_ok=True)
        with gzip.open(FIXTURE, "wt") as f:
            json.dump({"recorded": time.strftime("%Y-%m-%d"), "calls": _scrub(_STATE["calls"])}, f)
        print("saved %s" % FIXTURE)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
