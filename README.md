# FinTune

A native **YouTube Music client for Sailfish OS**. Silica/QML UI, a Python backend
over PyOtherSide, and audio playback through the same C++ GStreamer engine as its
sibling video client, **FinTube**.

FinTune browses YouTube Music (search, charts, and — signed in — your personalized
home and library) and plays tracks through the device's native media stack, using a
**user-managed `yt-dlp` binary** to resolve streams.

## Features

- **Browse** — music search (songs / artists / albums / playlists), personalized
  home, charts, library, artist pages, albums. Works logged out; sign-in unlocks
  the personalized surfaces.
- **Sign-in** — imports the session from the **Sailfish Browser's own cookie jar**
  (log into music.youtube.com in the real browser, then *Import*), authenticated via
  SAPISIDHASH, with cookie write-back to keep the session fresh off the app's own
  traffic. (No embedded webview — Google blocks those.)
- **Playback** — audio-only through the shared C++ GStreamer player, with a
  **property-based audio ladder** (opus/AAC by bitrate, source-language track
  preferred over dubs) that walks codecs on YouTube's per-video SABR gating, and a
  self-healing re-resolve fallback.
- **Queue & radio** — album/playlist queues, up-next view, play-next / add-to-queue,
  gapless prefetch, and radio autoplay when the queue runs dry.
- **Synced lyrics** (LRCLIB) — scrolling, tap-to-seek — **cached with downloads** so
  they show offline.
- **Offline** — download tracks for offline, offline-first playback.
- **Audio** — 10-band EQ (presets) + a volume boost with a soft limiter.
- **Extras** — MPRIS controls, lock-screen cover with blurred album-art backdrop,
  like/dislike, skip-disliked, add-to-playlist, play history.
- **PO-token provider** (opt-in) — the same sandboxed Deno `bgutil` sidecar FinTube
  uses, pre-warmed at launch.

## Architecture

| Layer | Where | What |
|---|---|---|
| UI | `qml/` (Silica) | `HomePage`, `SearchPage`, `NowPlayingPage`, `LyricsPage`, `LibraryPage`, `SettingsPage`, … + the docked mini-player |
| Bridge | `qml/Backend.qml` | PyOtherSide — Python calls run off the UI thread |
| Metadata | `python/ytm.py` | YouTube Music **InnerTube** API over `urllib` (search / home / library / radio / lyrics); FinTune's own layer |
| Engine | `python/youfish.py` + `src/` | **shared byte-for-byte with FinTube** — resolve, the media proxy, the PO-token sidecar, and the C++ GStreamer player (run audio-only here) |

The engine is kept converged with FinTube so a fix in one is a plain file copy to
the other. Music-specific logic lives in `ytm.py` and the QML.

## Prerequisites (on the device)

**Nothing to install by hand.** The app fetches every helper below itself — it just
asks you to confirm each download, then installs it into its own data dir. There are
no packages to hunt down and no RPM dependencies to satisfy first.

- **`yt-dlp`** — not bundled; the app checks at launch and, on your confirmation,
  installs/updates it into its own data dir (or reuses FinTube's binary if it's
  already there). Keep it current — extraction breaks often.
- **Deno 2.x** *(optional)* — for the PO-token provider (its own per-app,
  confirm-to-install step in Settings; without a token many streams 403).
- **ffmpeg** *(optional)* — for downloads; fetched on confirmation.

## Staying current (no app rebuilds)

Both the *playing* and the *browsing* halves keep themselves current without an app update:

- **Browsing heals itself.** The WEB_REMIX InnerTube client version + API key are
  auto-detected from music.youtube.com's `ytcfg` and cached (refreshed in the background,
  and re-scraped immediately after a 400). A client-version rotation — the thing that used
  to hard-code an expiry into the app — now needs nothing from you. The shipped values are
  only a cold-start fallback.
- **Playback** rides FinTube's external engine: **yt-dlp** (*Update* → `-U`), the
  **PO-token provider** (*Update to latest* pulls the newest bgutil release), and
  **ffmpeg** (*Update* re-fetches the current build) all update from Settings — or reuse
  FinTube's copies if it's installed.

## Build

With the Sailfish SDK (`sfdk`) configured. **Shadow build (recommended)** keeps this tree
pristine — every intermediate and the RPM land in a sibling `harbour-fintune.build/`:

```sh
sh build.sh                      # → ../harbour-fintune.build/RPMS/harbour-fintune-<ver>.aarch64.rpm
# override the target:  TARGET=SailfishOS-5.1.0.11-aarch64 sh build.sh
```

Or the classic **in-source build** (scatters qmake output into this dir — `sh clean.sh`
tidies it, and the `.pro` corrals the `.o`/`moc_*` into `.build/`):

```sh
sfdk -c target=SailfishOS-5.1.0.11-aarch64.default build   # → RPMS/harbour-fintune-<ver>.aarch64.rpm
```

Install on the connected device:

```sh
rpm -U --force <path-to>/harbour-fintune-<ver>.aarch64.rpm
```

## Tests

Offline unit tests for the shared resolve / format-selection engine (mocked
externals — no device, network, or yt-dlp):

```sh
python3 python/test_youfish.py
```

## License

GNU Public License Version 3

## Notice

This appplication has been vibecoded, if you dont like that feel free to not install the application.
