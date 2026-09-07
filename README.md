# FinTune

A native **YouTube Music client for Sailfish OS**. Silica/QML UI, the YouTube Music
(InnerTube) API for browsing, and an audio-only cut of **FinTube**'s engine for
playback — resolved through a **user-managed `yt-dlp` binary**, played by a shared
C++ GStreamer player.

## Features

- **Browse** — music search (songs / artists / albums / playlists), personalized
  home, library, artist pages. Works signed out; sign-in unlocks the personal
  surfaces.
- **Sign-in** — imports the session from the **Sailfish Browser's own cookie jar**
  (no embedded webview — Google blocks those), then keeps itself fresh via cookie
  write-back off the app's own traffic.
- **Playback** — audio-only pipeline with ~0.2 s track starts: streams are fetched
  in-process and served through a localhost proxy, and a **property-based opus/AAC
  fallback ladder** (source language over dubs) walks YouTube's per-video codec
  gating, with a self-healing re-resolve behind it.
- **Fast resolve** — runs yt-dlp in-process from an importable copy, no per-track
  binary spawn. Not a setting: automatic wherever the OS Python can run it
  (SFOS 5.1+), with the copy installed and updated alongside the binary; older
  devices just use the self-contained binary, which stays the fallback everywhere.
- **Queue & radio** — album/playlist queues, up-next view, play-next /
  add-to-queue, next-track prefetch (skips are instant), radio autoplay when the
  queue runs dry, repeat modes.
- **Synced lyrics** (LRCLIB) — scrolling, tap-to-seek, cached with downloads so
  they work offline.
- **Downloads** — offline m4a tracks that keep their artist + cover art;
  offline-first playback. No ffmpeg involved, ever — nothing needs merging.
- **Audio** — 10-band EQ with presets, volume boost + soft limiter.
- **Extras** — MPRIS / lock-screen controls, docked mini-player, like/dislike +
  skip-disliked, play history, blurred album-art backdrop.
- **PO-token provider** *(opt-in)* — the same sandboxed Deno `bgutil` sidecar as
  FinTube, pre-warmed at launch.
- **Shared install** — with FinTube on the same device, FinTune reuses its yt-dlp,
  fast-resolve copy and Deno. Nothing downloads twice; its own copies win if present.

## Architecture

| Layer | Where | What |
|---|---|---|
| UI | `qml/` (Silica) | `HomePage`, `SearchPage`, `NowPlayingPage`, `LyricsPage`, `LibraryPage`, … + the docked mini-player |
| Bridge | `qml/Backend.qml` | PyOtherSide — Python calls run off the UI thread |
| Metadata | `python/ytm.py` | YouTube Music **InnerTube** API over `urllib` (search / home / library / radio / lyrics); FinTune's own layer |
| Engine | `python/youfish.py` + `src/` | FinTube's engine, audio-only cut — resolve + audio ladder, media proxy, provider lifecycle. Kept structurally converged so fixes port between the apps |

## Staying current (no app rebuilds)

- **Browsing heals itself** — the InnerTube client version + API key are
  auto-detected from music.youtube.com and cached; the shipped values are only a
  cold-start fallback.
- **Playback rides external helpers** — yt-dlp (one-tap Update, stable or nightly
  channel, fast-resolve copy refreshed in lockstep), Deno, and the PO-token
  provider all install and update from the Providers page after a confirmation
  tap. Nothing to preinstall; a YouTube-side breakage is a tap, not a release.

## Build

With the Sailfish SDK (`sfdk`) configured. Shadow build (recommended — RPM lands in
a sibling `harbour-fintune.build/`):

```sh
sh build.sh          # override target: TARGET=SailfishOS-5.1.0.11-aarch64 sh build.sh
```

or in-source (`sh clean.sh` tidies up):

```sh
sfdk -c target=SailfishOS-5.1.0.11-aarch64.default build
```

Install: `rpm -U --force harbour-fintune-<ver>.aarch64.rpm`

## Tests

Offline unit tests for the audio engine — externals mocked, no device, network, or
yt-dlp needed:

```sh
python3 python/test_youfish.py
```

## License

GNU General Public License v3

## Notice

This application is vibecoded. If you don't like that, feel free to not install it.
