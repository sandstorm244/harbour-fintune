# CLAUDE.md

Guidance for AI agents and new contributors working in this repo. The README is the
user-facing intro; this file is the working map — where things live, the invariants that
shape the code, and the traps that bite you if you edit blind. Almost every non-obvious
line already has a comment explaining *why*; when in doubt, read the comment at the cited
location before changing it. FinTune is the audio sibling of **FinTube**
(`harbour-fintube`, a YouTube video client); the two apps are kept structurally converged
so fixes port between them, and this file calls out where FinTube's CLAUDE.md would
mislead you if copied verbatim.

## What this is

FinTune is a native **YouTube Music** client for **Sailfish OS** (version 1.2.2, GPLv3).
Playback is **audio-only** — m4a/opus streams, no video decode path in normal use, no
ffmpeg anywhere in the app. Four layers that talk in a strict line:

| Layer   | Where                                        | Role |
|---------|----------------------------------------------|------|
| UI      | `qml/pages/*.qml`, `qml/*.qml`               | Silica views. Deliberately dumb — no real logic. |
| Bridge  | `qml/Backend.qml`                            | Async PyOtherSide facade. Nothing returns a value. |
| Metadata| `python/ytm.py` (1753 lines)                 | InnerTube WEB_REMIX client: search, home, artist/playlist pages, radio, lyrics, library, login/accounts. Called directly from QML. |
| Engine  | `python/youfish.py` (3493 lines)             | yt-dlp, audio format selection, media proxy, PO-token sidecar, settings/downloads stores. |
| Player  | `src/videoplayer.cpp`, `src/hwvideosink.cpp` | C++ GStreamer `VideoPlayer` (a QML type), reused here as a hidden `audioOnly` audio engine. |

**The ytm.py/youfish.py split is inverted from FinTube — read this before assuming
anything from FinTube's CLAUDE.md carries over.** In FinTube, `youfish.py` (6384 lines) is
the giant that does everything and `ytm.py` (759 lines) is a slim cookie-login sibling
whose whole job is importing the browser's YouTube session (`is_logged_in()` is a bare
presence check, no OAuth). In FinTune the roles swap sizes *and* jobs: `python/ytm.py`
(1753 lines) is the app's own InnerTube metadata engine targeting `music.youtube.com`
WEB_REMIX (`ytm.py:42-48`) — not `youtube.com` WEB like FinTube's (FinTube
`python/ytm.py:80-85`) — and it owns search/home/artist/radio/lyrics/library/history/
playlists/cookie-and-OAuth login, called directly from QML (`ytm.search`, `ytm.get_home`,
`ytm.get_radio`, `ytm.get_lyrics`, …). The InnerTube browse/metadata layer is
FinTune-original, but the **cookie-login half is the one part genuinely shared with
FinTube**: it started here, was ported into FinTube's slim `ytm.py` by popular request, and
improvements have flowed back since (the *Port better login handler from FinTube* commit),
so the two login copies are kept converged. `python/youfish.py` (3493 lines) is narrower
than FinTube's copy: it's an **audio-only cut** — the localhost media proxy, stream
registry, yt-dlp/Deno management, and PO-token sidecar are largely byte-identical to
FinTube's (confirmed by diff), but there is no video-candidate ladder, no ffmpeg, and no
video downloads at all. The only coupling between the two files is one-directional:
`youfish.py` imports `ytm` purely to read `ytm.netscape_cookies()` when handing yt-dlp a
cookies file.

**Naming quirk carried over from FinTube:** the product/QML type is `FinTune`, but the
engine is still branded **"youfish"** internally — `YOUFISH_DEBUG`, `YOUFISH_HWDEC`, the
`[youfish]` log prefix, and even a stray `harbour-youfish` example command in a comment are
copied verbatim from FinTube (`youfish.py:306-308`), not FinTune-specific.

## Build, run, test

```sh
sh build.sh          # shadow build via sfdk → ../harbour-fintune.build/RPMS/
                     # override target: TARGET=SailfishOS-5.1.0.11-aarch64 sh build.sh
sh clean.sh          # remove build scatter from the source tree
python3 python/test_youfish.py   # offline engine tests — pure stdlib, externals mocked
```

- `build.sh` defaults to `SailfishOS-5.0.0.62-aarch64.default` (`build.sh:14`), same 5.0
  floor as FinTube, but FinTune's copy **actively enforces** it: after the sfdk build it
  stamps a marker, finds fresh RPMs, and hard-fails if `rpm -qpR` shows any `GLIBC_2.34+`
  require (`build.sh:19-48`) — a 5.1-built RPM silently locks out 5.0 devices otherwise.
  FinTube's `build.sh` has no such guard.
- The **engine test suite** (`python/test_youfish.py`, 1455 lines, 22 `TestCase` classes,
  90 tests) runs in ~0.3s, fully offline (verified: `python3 python/test_youfish.py` →
  `Ran 90 tests in 0.317s OK`). Runs in CI on every push/PR
  (`.github/workflows/tests.yml`, byte-identical to FinTube's). **Run it after any change
  to `youfish.py` or `ytm.py`.**
- It is FinTube's own suite cut to the audio-only contract — several provider-machinery
  classes (`ChannelAwareDownloads`, `ExtractorArgParse`, `PotStatusDenoManaged`,
  `PotPluginProbe`, `ZipappVersion`, `DirectFetchStreamer`, `AnonymousPrimary`, `PotTag`,
  `PotEnsureBudget`) are byte-identical to FinTube's copies — direct evidence the shared
  parts of the engine are kept in lockstep.
- **`ytm.py` is only tested at its login/identity/account-switching edges** (`YtmIdentity`,
  `YtmAccountSelector`, `test_youfish.py:1261-1434`). Verified by grep: `search`,
  `get_home`, `get_radio`, `get_lyrics`, `get_artist`, `get_playlist`,
  `get_library_playlists`, `add_to_playlist`, `play_history`, `disliked_ids`, `rate_song`,
  `login_begin`, `verify_session`, `account_status`, `import_browser_login` — the entire
  content-fetching surface that is the actual product — have **zero** references anywhere
  in the test file. Verify parsing/search/home/radio/lyrics changes on-device
  (`YOUFISH_DEBUG=1`); CI cannot catch a regression there.
- The **release build** (`.github/workflows/build.yml`) only fires on tags matching `1.*`
  and cross-builds aarch64 via `coderus/github-sfos-build@sfos5` pinned to release
  `5.0.0.43`, publishing the RPM as a GitHub release — byte-identical to FinTube's workflow.
- The C++ player and the real yt-dlp/Deno/PO-token processes are **not** covered by any
  automated test — verify those on-device.

## The invariants (do not break these)

1. **The localhost media proxy is not optional.** Same reason as FinTube: GStreamer's
   libsoup stack gets an unfixable 403 from googlevideo, urllib with identical headers
   gets 206. `_MediaProxyHandler.do_GET` (`youfish.py:975`) is verified byte-identical to
   FinTube's copy. This is also why the app must run **unsandboxed**
   (`[X-Sailjail] Sandboxing=Disabled`, `harbour-fintune.desktop:16`) and therefore
   **cannot ship on Jolla Harbour** — distribute via Chum/OpenRepos.

2. **Format selection is property-based, never itags — and there is no video ladder at
   all.** `_audio_candidates` (`youfish.py:188-221`) is textually identical to FinTube's
   `_audio_candidates` (FinTube `youfish.py:237-270`), including every comment: opus
   preferred over bitrate (push-seek reasons), source-language over dub, non-DRC before
   DRC. Unlike FinTube, there is **no** `_video_candidates`, `_codec_family`, or AV1 drop
   at all — grep for `video_candidates|videoExt|_codec_family|av01` in `youfish.py`
   returns zero hits. If `_audio_candidates` returns nothing, the only rung left is the
   `_MUXED_ITAGS = ("95","94","93","18")` last resort (`youfish.py:125`).

3. **yt-dlp has two forms that must stay in channel lockstep — plus FinTube-shared-install
   reuse.** Frozen binary is the universal default/fallback; the importable zipapp is the
   automatic in-process fast path, gated on `_FAST_RESOLVE_PY_OK = sys.version_info >=
   (3, 10)` (`youfish.py:60`). New in FinTune: the binary lookup order is FinTune's own
   managed copy → **FinTube's shared managed copy** (read-only, `_CANDIDATE_PATHS`/
   `_FINTUBE_DATA_DIR`, `youfish.py:127-134`) → system PATH, so a device with both apps
   doesn't download yt-dlp twice. The fast-resolve zipapp read path re-checks whichever
   binary is *currently active* on every call (not cached), so switching FinTube's channel
   or installing FinTune's own copy takes effect immediately (`youfish.py:1499-1512`,
   `:1693-1704`). FinTune's own Install/Update always write its own copy, never FinTube's.

4. **`PR_SET_PDEATHSIG` is thread-scoped, not process-scoped.** The Deno PO-token sidecar
   MUST be spawned from a dedicated long-lived owner thread that parks on `proc.wait()`
   (`_ensure_pot_server`, `youfish.py:2505`) — near-verbatim copy of FinTube's
   (`youfish.py:2770`), comment-only diffs. Fork it from a short-lived caller and the
   kernel SIGKILLs it the instant that caller returns.

5. **The stream registry has a strict lock order:** `_streams_lock` → `s.cond`. `do_GET`
   takes only `s.cond`, never `_streams_lock`; no `proc.wait()` runs under a lock —
   verified byte-identical to FinTube's discipline (`youfish.py:963-1099`).

6. **All resolve inputs that affect output are in the cache key — and the key is
   deliberately smaller than FinTube's.** `_resolve_key` (`youfish.py:1313-1321`) =
   video id + effective client + pot_active + signed_in. `default_quality`, `audio_lang`,
   `hw_decode` are **gone**, not just excluded — those settings don't exist in FinTune's
   real schema (zero hits in `youfish.py`/`qml`/`rpm`; only two stale mock-only refs linger
   in `test_youfish.py:620,708`), because there's no video-quality cap, no dub picker, no
   HW-decode toggle at the Python layer. `_RESOLVE_OUTPUT_KEYS` shrank to
   `("player_client", "pot_provider")` (`youfish.py:1281`). Adding any new audio-affecting
   setting must update both `_resolve_key` and `_RESOLVE_OUTPUT_KEYS`, or stale audio
   survives a settings change; do not resurrect the deleted keys without first re-adding
   the corresponding setting.

7. **No shared-player reparenting exists — FinTube's invariant 7 does not apply here.**
   FinTube's `gplayer` lives at app scope but is *borrowed*: a VideoPage reparents it into
   its own `videoSurface`, gated by `holdsPlayer`. FinTune has nothing to reparent: the
   audio engine (`player`, a `VideoPlayer{ audioOnly: true }`) is declared once
   (`qml/harbour-fintune.qml:77-82`) and never moves — grepping all of `qml/` and `src/`
   for `holdsPlayer`/`reparent`/`videoSurface` returns zero hits. Playback control lives
   directly on `app` in `harbour-fintune.qml`, gated on plain state (`npActive`,
   `npAudioIdx`, `npTriedMuxed`, `npFailStreak`), not an ownership guard.

8. **Two PyOtherSide workers, one interpreter — narrower fast lane than FinTube's.** `py`
   imports **two** Python modules (`youfish` *and* `ytm`); `pyFast`
   (`Backend.qml:662-671`, declared **after** `py` for the same pyotherside-routing reason
   as FinTube) imports only `youfish` and fast-lanes exactly **2** call sites —
   `resolve()` (`Backend.qml:91-100`) and `prefetchResolve()` (`Backend.qml:106-111`) via
   `backend.fastLaneReady ? pyFast : py` (`Backend.qml:13,95,109,668`). Unlike FinTube
   (4 fast-lane sites including `get_position`), FinTune has **no** `get_position`/
   pasted-URL fast lane at all — there is no per-video resume-position store anywhere in
   `youfish.py` (grep for `get_position`/`positions.json` returns nothing; a song restarts
   from 0). Any new latency-critical *playback* call should follow the same ternary, but
   `ytm.*` (browse/library/radio/lyrics) calls must stay on `py` — `pyFast` never imports
   `ytm`.

9. **`videoExt`/`audioExt` split-seek machinery does not exist — do not port FinTube's
   invariant 9 as-is.** FinTube's `videoExt`/`audioExt` properties, `extIsMp4()`, the
   mp4-only downloadbuffer+`KEY_UNIT`+audio-align scheme, `seekWhenReady()`, and
   `retrySplitSeek()` are **entirely absent** from `videoplayer.h`/`.cpp` (166 vs FinTube's
   191 header lines, 884 vs 1082 impl lines; grepped, zero hits). `sendSeek`
   (`videoplayer.cpp:240-280`) now branches only on `m_muxed`, with no container check at
   all. In practice `m_muxed` is *always true*: `qml/harbour-fintune.qml:540-551` sets
   `player.audioUrl = ""` permanently and always feeds tracks through `player.videoUrl` —
   so the non-muxed/dual-`uridecodebin` code path is structurally present but never
   exercised by the shipped app. **Caveat, not independently re-verified on hardware:**
   FinTune also dropped the container-aware `download` (downloadbuffer) gating — it's now
   unconditional for any network source (`videoplayer.cpp:425-443`), so an opus/webm
   adaptive-audio pick takes the same `KEY_UNIT` muxed seek an m4a pick does; FinTube's own
   code comments describe that as landing on the nearest matroska cluster rather than the
   exact scrub point for non-mp4 containers. Verify seek accuracy on an actual opus track
   before assuming parity with FinTube here.

10. **User data = small JSON stores, atomic writes — split across two files now.**
    `_atomic_write_json` (`youfish.py:3144`: 0600 temp file → `os.replace`) is unchanged.
    `youfish.py` itself now owns only `settings.json` (`:3223`) and `downloads.json`
    (`:3360`) — there is **no `positions.json`** (no resume-point store at all) and **no
    `watch_history.json`**. Play history (`play_history.json`) and dislikes
    (`disliked.json`) moved to `ytm.py:1257-1309` instead, since browsing/library state
    lives with the metadata engine now. `ytm.py`'s own cookie store
    (`ytm_cookies.json`) is atomic + 0600 via its own local `mkstemp`+`os.replace`
    (`ytm.py:572-591`) — implemented independently of `youfish.py`'s helper, the two
    modules don't share a data-store module.

11. **InnerTube client identity self-heals, but only asynchronously.** `_ytm_config()`
    (`ytm.py:188-200`, TTL const `_YTM_CFG_TTL:167`) never blocks the request path — it
    returns cached-or-default at once and kicks a deduped background thread
    (`_warm_ytm_config:225`) once the 12h TTL expires or on any 400 response.
    `_fetch_ytm_identity()` (`ytm.py:202-222`) scrapes
    `INNERTUBE_API_KEY`/`INNERTUBE_CLIENT_VERSION` off `music.youtube.com`'s ytcfg HTML and
    only commits when a version was actually scraped (sanity-checked against
    `^\d+\.\d{6}`, `:220`) — a key-only scrape must never overwrite a good cached version.
    Net effect: the *first* request after a real client-version rotation can still 400;
    only the *next* one benefits from the heal.

12. **Never pair `?key=` with an OAuth Bearer header.** `_auth_mode()` (`ytm.py:831-837`)
    picks `cookie` > `oauth` > `none`; cookie auth keeps `?key=` in the URL, OAuth auth
    must drop it entirely — sending both is a 400 `INVALID_ARGUMENT` (`_innertube` mode
    branch, `ytm.py:293-328`). FinTune's `ytm.py` is the only one of the two apps with an
    OAuth device-flow path at all (FinTune `ytm.py:52-59`, `:461-533`) — FinTube's `ytm.py`
    has cookie import only.

13. **Downloads are unconditionally audio, no ffmpeg exists in this app.** `download()`
    (`youfish.py:3392`) forces `kind = "audio"` regardless of what's passed, always selects
    itag 140 (m4a), and never adds `--merge-output-format` — there is no
    `install_ffmpeg` function anywhere in `youfish.py` (grepped, absent), unlike FinTube
    where video downloads require it.

## Where things live

**Metadata engine (`python/ytm.py`)** — this file has no FinTube equivalent at this size;
by line:
- Identity/auth: `_fetch_ytm_identity:202`, `_ytm_config:188` (`_YTM_CFG_TTL:167` TTL const),
  `_warm_ytm_config:225`, `_auth_mode:831`, `_innertube:293` (mode branching),
  `verify_session:395-432`.
- Cookie handling: `_read_cookie_jar:594` (copies Gecko `cookies.sqlite` to a temp file so a
  live browser's WAL lock never blocks the read), `_absorb_rotations:711`,
  `_persist_rotations:671`, `_refresh_netscape:654`, `_save_cookies:572` (atomic 0600),
  `import_browser_login:757` (calls `verify_session` right after import to catch a stale
  imported session), `netscape_cookies` (consumed by `youfish.py`).
- OAuth device flow: client id/secret consts `:52-59`, `login_begin:461-533`.
- Accounts: `list_accounts:972-1027` (authuser 0..7 probe *is* the switch mechanism),
  `select_account:862-879` (drops `home_cache.json` on switch).
- Browsing (shape-search, not path-indexed): `_iter_find:882`, `_collect_renderers:1134`,
  `_find_header:1403` (`_HEADER_KEYS:1399`; falls back through 4+ header renderer names),
  `search:1363-1391` (flattens all matches in document order), `get_home:1189-1236`
  (continuation-paginated, capped at 12 hops, falls back to anonymous fetch on empty
  personalized result), `get_radio:1537-1567` (`next` endpoint,
  `playlistId="RDAMVM"+videoId`, `isAudioOnly=True`), `add_to_playlist:1712-1732` (strips
  leading `VL` from browseId).
- Lyrics (the one non-InnerTube integration): `get_lyrics:1621-1656` (LRCLIB, exact `/get`
  then two `/search` fallbacks), `_best_lyrics_match:1611`, `cache_lyrics`/`cached_lyrics:
  1659-1701`.
- Ratings/history: `rate_song:1344-1360` (via `_update_disliked:1334`, which writes
  `disliked.json`), `disliked_ids:1329`, `play_history_path:1257`, `disliked_path:1309`.
- Data stores (all under `~/.local/share/harbour-fintune`): `ytm_tokens.json` (OAuth, 0600),
  `ytm_cookies.json` (atomic 0600), `ytm_config.json` (identity cache, plain write),
  `home_cache.json`, `play_history.json` (LRU-capped 400), `disliked.json`,
  `lyrics/<videoId>.json`.

**Engine (`python/youfish.py`)** — by line:
- Resolve core: `resolve:2928`, `_resolve_uncached:2935`, `_resolve_and_cache:1351`
  (single-flight leader/joiner), `prefetch_resolve:1416`.
- Audio ladder: `_audio_candidates:188`, `_pick_audio:3129` (no `prefer_lang` param —
  "Music has no dub picker", unlike FinTube's `_pick_audio(formats, prefer_lang="")`),
  `_pick:3119` (muxed lookup).
- Mid-stream 403 self-heal: `_reresolve:1204`, `_reader:647` — structurally unchanged from
  FinTube (anonymous-first, cookie+token fallback, rate-limited).
- Media proxy: `_MediaProxyHandler:963`, `_DirectFetch:491`, `_ensure_proxy:1106`,
  `_proxied:1134`, `_proxy_url_ok:403`.
- yt-dlp management: `_ytdlp_path:1499`, `install_ytdlp:1607`, `ytdlp_update:1528`, zipapp
  at `install_ytdlp_zipapp:1733` / `_import_yt_dlp:1866`, shared-install reuse
  `_CANDIDATE_PATHS:127-134`, `_FINTUBE_DATA_DIR` (same block).
- PO-token sidecar: `_ensure_pot_server:2505`, `_pot_server_flags:2291` (Deno sandbox:
  `--allow-net` unrestricted, `--deny-write`, `--deny-run`, scoped `--allow-read`),
  `install_pot_provider:2817`, Deno reuse `_deno_path:2098`.
- Data stores: `_atomic_write_json:3144`, `_data_dir:3164` (`~/.local/share/harbour-fintune`
  — own, NOT sharing settings/tokens with FinTube, only binaries reused read-only),
  `_settings_path:3223` (FinTune-only keys `autoplay`/`skip_disliked` + `home_backdrop` at
  `:3187-3200`; note `eq_enabled`/`eq_bands`/`boost_gain` are **carried over unchanged from
  FinTube** — same comments/defaults, not FinTune additions),
  `_downloads_path:3360`, `download:3392`.
- Muxed last-resort ladder: `_MUXED_ITAGS:125` = `("95","94","93","18")`.

**Player (`src/`)** — `main()` in `harbour-fintune.cpp:53` is a near-verbatim rename of
FinTube's (same 64 MB `QNetworkDiskCache`, same `droidvdec`→`GST_RANK_NONE` demotion at
`:71-76`, same persistent-GL-off dance); `hwvideosink.cpp`/`.h` are **byte-identical** to
FinTube's (diff is empty), but `HwVideoSink`/`hwDecode` (`videoplayer.h:33`) are never
toggled from any FinTune QML — grepped `qml/`, zero hits for `hwDecode`. The real fork is
`VideoPlayer::setAudioOnly` (`videoplayer.cpp:53-59`) and the `audioOnly` guards through
`buildPipeline` (`:340,366,373-375,448,480`) that skip building the video branch entirely.
`sendSeek:240-280` is the (much smaller) seek logic — see invariant 9. The 10-band EQ +
volume-boost + `rglimiter` soft-limiter chain (`videoplayer.h:74-82,139-149`,
`videoplayer.cpp:186-205,513-551`) is **not new** — it's byte-for-byte the same chain
FinTube already ships.

**Bridge/app** — `Backend.qml` (672 lines, 53 mirrored properties, 9 signals: `resolved`,
`resolveError`, `updateFinished`, `downloadProgress`, `downloadFinished`, `musicResults`,
`musicError`, `musicHomeLoaded`, `ytmLoginFinished`). App entry `harbour-fintune.qml`
(696 lines) owns `player` + `nowPlaying` at app scope, the docked mini-player
(`:593-668`, gated purely on `npActive` — simpler than FinTube's dual-purpose `resumeBar`,
since FinTune's player is always background by construction), cover, and MPRIS via a
`Loader`. **No D-Bus `openUrl`** — `harbour-fintune.desktop:7` states why ("FinTube owns
youtube.com links; FinTune is launched directly"); grepped `qml/`+`src/`, zero
`DBusAdaptor`/`openUrl` hits despite the `.service` file still being shipped (see Gotchas).
`NowPlayingPage.qml` (337 lines) is the simplified analog of FinTube's `VideoPage.qml` —
no reparenting, no SponsorBlock, no seek-plan branching, just bindings + a blurred-art
backdrop. `LyricsPage.qml` (150 lines) is the one page with real per-frame logic:
synced-line highlight against `player.position`, with a staleness guard on `app.npId`
(`:19-44`). `QueuePage.qml` (106 lines, "Up next") has no FinTube analog at all —
FinTube keeps queue state but exposes no viewer. `EqualizerPage.qml` (101 lines) is
**converged, not FinTune-specific** — near-identical to FinTube's own EqualizerPage
against the same shared C++ EQ API.

## Gotchas that will bite you

- **`ytm.py` has zero dedicated test coverage.** `test_youfish.py` only fakes its
  `netscape_cookies()` surface (`test_youfish.py:361-379`) to exercise cookie handoff; no
  `test_ytm.py` exists anywhere in the tree. Every search/home/artist/radio/lyrics parsing
  change ships with no regression coverage — verify by hand.
- **`list_accounts()`'s `authuser` probe range (0..7) is also the account-selection
  wiring** (`ytm.py:972-1027`) — an account the probe never reaches can never be selected
  in the UI. This block is near-line-identical to FinTube's `list_accounts`
  (FinTube `ytm.py:547-602`; `select_account` pairs at FinTube `:346`) — it's the shared
  login handler (see *What this is*), one of the few places the two apps are literally, not
  just structurally, converged.
- **`_persist_rotations` must update the Cookie header and the yt-dlp Netscape text
  together under `_cookies_lock`** (`ytm.py:671-724`) — a concurrent InnerTube call's
  cookie rotation can otherwise be lost to last-writer-wins. FinTube's `ytm.py` has no
  equivalent rotation write-back at all and ages faster as a result.
- **`hwDecode`/`HwVideoSink` are stale cross-layer plumbing.** The C++ player still has the
  full wiring (`src/videoplayer.h:33`, `.cpp:29`), but `settings.json` has no `hw_decode`
  key to drive it anymore — don't assume the setting exists just because the C++ property
  does.
- **Passing a `kind` other than `"audio"` to `download()` is silently ignored** — it's
  hardcoded inside the function (`youfish.py:3401`).
- **`harbour.fintune.service` is vestigial.** It's still built into the `.pro`
  (`harbour-fintune.pro:80-85`) and still packaged (`rpm/harbour-fintune.spec`), and its
  `.pro` comment still claims it "lets the URL dispatcher cold-start a single FinTune
  instance and deliver openUrl" — but there is no working D-Bus `openUrl` anywhere (see
  Bridge/app above). Don't trust that comment; it's stale, carried over from FinTube where
  the wiring is real.
- **"Audio-only" does not shrink the build-dependency surface.** `SOURCES` still compiles
  `src/videoplayer.cpp` + `src/hwvideosink.cpp` (`harbour-fintune.pro:29-31`) because it
  *is* the audio engine (`VideoPlayer{ audioOnly: true }`), so
  `rpm/harbour-fintune.spec` still needs `gstreamer-video-1.0`, `egl`,
  `nemo-gstreamer-interfaces-1.0` (`:21-27`). Don't strip these thinking they're
  video-app-only cruft — the build breaks.
- **`rpm/harbour-fintune.spec` requires `git`** (runtime clone of the bgutil PO-token
  provider) and **deliberately does NOT require yt-dlp or ffmpeg** — same policy, same
  comment text as FinTube's spec. Never add either.
- **Qt 5.6 platform floor still applies, but the workarounds it used to force are gone.**
  No `Qt.callLater`/`Connections{target:null}`/`Screen.hasCutouts` guards exist anywhere in
  `qml/` (grepped, zero hits) — FinTube needed them specifically for its fullscreen video
  controls overlay, which FinTune has no equivalent of. If you add a full-bleed overlay
  here, copy the guard pattern from FinTube's `VideoPage.qml`, don't assume Qt 5.7+ is
  safe.
- **Local vs CI SDK mismatch:** `build.sh` defaults to `SailfishOS-5.0.0.62-aarch64` while
  `.github/workflows/build.yml` pins release `5.0.0.43` — same class of drift as FinTube.
  A local build is not guaranteed to reproduce the release RPM bit-for-bit.
- **`harbour-fintune.pro` link order:** `CONFIG += link_pkgconfig` drops the sailfishapp
  libs off the link line, so `LIBS += -lsailfishapp -lmdeclarativecache5` +
  `QMAKE_LFLAGS += -pie -rdynamic` (`harbour-fintune.pro:19-20`) are load-bearing; the
  final binary + Makefile must stay at the source root for `%qmake5_install`.
- **`.gitignore` is missing FinTube's trailing `.claude/settings.local.json` line** —
  a local Claude settings file can get committed here where it wouldn't in FinTube.
- **Shared install across both apps is real, not aspirational.** `youfish.py` hard-codes
  FinTube's data dir as a read-only fallback for an already-installed yt-dlp
  (`_FINTUBE_DATA_DIR`/`_CANDIDATE_PATHS`, `youfish.py:127-134`) — but FinTune's own
  Install/Update never write to FinTube's path, and its own managed copy always wins once
  present.
