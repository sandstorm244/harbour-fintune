# Testing FinTune

Two layers catch different bugs:

- **Automated tests** run in seconds on a PC and in CI. They cover the engine (`python/`), its
  parsers against real recorded YouTube answers, and the app's queue and player decisions
  (`qml/logic.js`).
- **The device checklist** below covers what only the phone can show: the C++ player, real
  YouTube Music, the Silica UI, and the yt-dlp/Deno helpers.

A feature belongs in both. When you add or change one, add a test for its logic and a line to
the checklist.

## Automated tests

```sh
sh test.sh                          # everything; prints ALL TESTS PASSED or what failed
python3 python/test_youfish.py      # engine only, verbose
python3 tests/test_parsers.py       # InnerTube parsers vs recorded YouTube answers
python3 tests/test_ytm_parsers.py   # YouTube Music parsers + lyrics, same idea
python3 tests/coverage_report.py    # which engine code no test runs
python3 tests/live_ytdlp.py         # real yt-dlp against live YouTube (--pot: with PO token)
node tests/qml_logic_test.js        # queue / player decisions only
```

- **Engine suite** (`python/test_youfish.py`): pure standard library and fully offline. yt-dlp,
  the network and YouTube are mocked, and the localhost media proxy runs for real against a fake
  googlevideo.
- **QML logic suite** (`tests/qml_logic_test.js`): next track with the disliked hop, radio merge,
  shuffle and unshuffle, queue edits, the audio ladder, normalization gain, SponsorBlock, routing
  and synced lyrics. It needs Node.js (Fedora: `sudo dnf install nodejs`). It also rejects syntax
  the phone's Qt 5.6 JavaScript can't run (`let`, `=>`, template strings and so on), which Node
  would accept.
- **Parser suites** (`tests/test_parsers.py`, `tests/test_ytm_parsers.py`): the parsers for
  YouTube's answers, run against real answers recorded from YouTube (`tests/fixtures/`, your IP
  scrubbed out). They check what the app needs (ids, titles, durations, a next page), not exact
  values.
- **Coverage check** (`tests/coverage_report.py --check`): fails when an engine function has no
  test. `tests/coverage_baseline.txt` lists the few that can't run offline, each with the reason;
  that list may only shrink. Run the script without `--check` to see how much of each module the
  tests run.
- **Live yt-dlp check** (`tests/live_ytdlp.py`): the one test that uses the real yt-dlp and live
  YouTube. It downloads yt-dlp (binary and in-process copy, checks they match) and runs the
  engine's yt-dlp paths with InnerTube switched off: resolve and the first audio bytes through the
  proxy, and an offline download, on both forms of yt-dlp. `--pot` also installs Deno and the
  PO-token provider and checks the token path. YouTube's "confirm you're not a bot" and rate
  limits count as **skipped (inconclusive)**, never as failures. Not part of `test.sh`: it needs
  the network and takes about a minute. GitHub runs it every Monday and on demand (Actions → live
  yt-dlp → Run workflow); its IPs often get the bot wall, so a run with skips proves less than one
  from home.
- **CI**: every offline suite runs on every push and pull request. A release build runs them
  first and doesn't publish if one fails.

**When YouTube changes something.** If diagnostics show the "InnerTube fast path … fell back" count
growing, or users report empty pages, run `python3 tests/test_parsers.py --live` (and
`tests/test_ytm_parsers.py --live`). The failing check names the parser that no longer understands
YouTube's answer. Fix it, then re-record with `--record` so the offline tests follow the new layout.

**Keep decisions testable.** If the app decides something (which track is next, what to skip,
what to remember), put the decision in `qml/logic.js` as a plain function, or in the engine if
it's stored. QML only applies the result.

## Release procedure

1. `sh test.sh` passes, and so does `python3 tests/live_ytdlp.py --pot` from your own
   connection (it checks the newest yt-dlp still works with the app).
2. Set `Version:` in `rpm/harbour-fintune.spec` (the RPM takes its version from there, not
   from the tag), then tag a **beta**: `git tag 1.4.0-beta1 && git push origin 1.4.0-beta1`.
   A tag with a dash is published as a pre-release, so only people who pick it get it.
3. Install the beta RPM on the phone. Run **Smoke** plus every section the release touched (see
   the table below). If in doubt, run them all; the full list takes about 30 minutes.
4. Fix, then tag `-beta2` and repeat as needed. When clean, tag the release (`1.4.0`).

| Files changed | Run these sections |
|---|---|
| anything | Smoke |
| `python/youfish.py` resolve, proxy, `innertube.py` | Playback, Providers |
| `qml/harbour-fintune.qml`, `qml/logic.js` | Playback, Queue and radio, SponsorBlock, Volume |
| `NowPlayingPage.qml`, `QueuePage.qml`, `LyricsPage.qml` | Now Playing, Queue and radio, Lyrics |
| `src/*.cpp` | Playback, Volume, Background and system |
| `ytm.py` | Browse, Library, Account, Lyrics |
| downloads code | Downloads |
| `rpm/`, `.pro`, `.desktop`, translations | Install and launch, Smoke |

## Device checklist

"Auto:" names the automated tests behind a line. There, the device step only checks that the
result reaches the screen or the speaker. Lines without it are only checked here.

### Smoke (every release, about 5 minutes)

- [ ] Fresh install over the previous version; Home shows shelves with no error.
- [ ] Tap a song: sound within a few seconds; seek works.
- [ ] Next and Previous work; the song after the last one comes from radio.
- [ ] Search finds a song, an artist and an album; each opens or plays.
- [ ] Providers page: yt-dlp shows a version, "Run diagnostics" finishes.

### Install and launch

- [ ] Upgrade keeps login, history, downloads, settings and the yt-dlp install.
- [ ] Phone in another language: the UI is translated and untranslated strings show in English.
- [ ] Home paints cached shelves right away, then refreshes. (Auto: `cachedShelves`, `freshShelves`.)

### Playback

- [ ] Songs play through the audio ladder; a broken stream moves to the next rung by itself.
      (Auto: `ladderStep`, `AudioCandidates`, `ResolveSmoke`.)
- [ ] A song that won't play at all is skipped; after several in a row playback stops with a
      message. (Auto: `ladderStep`, `failAction`.)
- [ ] Pause for 15+ minutes, then play: it continues (a fresh resolve if the URL went stale).
- [ ] Listen for 5+ minutes with no stall. (Auto: `DirectFetchStreamer`, `MediaProxyEndToEnd`.)
- [ ] The next song starts right away (it was prefetched). (Auto: `nextIndex`, `prefetchId`.)
- [ ] A downloaded song plays with no network (flight mode). (Auto: `localPath`.)

### Queue and radio

- [ ] Album or playlist: tap a track, it plays from there and advances through the list.
- [ ] Shuffle on: the rest of the queue is mixed and the current song keeps playing. Shuffle off:
      the original order comes back for what's still ahead. (Auto: `shuffleTail`, `unshuffle`.)
- [ ] Repeat all: after the last track the queue starts over. Repeat one: the song replays.
      (Auto: `nextRepeatMode`, `endAction`, `afterQueueEnd`.)
- [ ] A single song: "Up next" fills with its radio. At the end of an album the radio continues
      without a gap, and no song appears twice. Starting an album while a radio request is
      out doesn't append that radio to the album. (Auto: `mergeRadio`, `radioTailSeed`,
      `radioStale`, `continuationAction`.)
- [ ] Autoplay off: playback stops at the end of the queue. (Auto: `continuationAction`.)
- [ ] Play next, Add to queue, Remove from queue (before and after the current song), and jump
      to a queue entry from Up next. (Auto: `insertNext`, `removeAt`.)
- [ ] Skip disliked on: a disliked song in the queue or radio is skipped. (Auto: `nextIndex`,
      `mergeRadio`, `YtmLocalStores`.)

### Now Playing

- [ ] Art, title, artist, position and duration are right; the backdrop setting changes the
      background.
- [ ] Seek slider; play/pause; next/previous; shuffle and repeat buttons reflect their state.
- [ ] Like and dislike, signed in; tapping again clears it. Signed out the buttons are disabled.
      (Auto: `rateToggle`.)
- [ ] Artist name opens the artist page.
- [ ] Pull-down: Download, Add to playlist, Lyrics, Up next.

### Lyrics

- [ ] Synced lyrics highlight the current line and scroll; tapping a line seeks there.
      (Auto: `lyricLineAt`, `LyricsParsing`.)
- [ ] A song with only plain lyrics shows them; an instrumental says so. (Auto: `YtmSweepFixes`.)
- [ ] A downloaded song shows lyrics offline. (Auto: `YtmAtomicStores`.)
- [ ] Skipping songs with the page open shows the new song's lyrics, never the old one's.

### Volume

- [ ] Normalize on: a loud and a quiet song play at a similar level; max boost 0 dB never makes
      a song louder. A downloaded song is normalized offline. (Auto: `gainForLoudness`,
      `normSource`, `normGain`, `PlayerLoudness`.)
- [ ] Equalizer presets change the sound; a preset turns the equalizer on. (Auto: `presetPlan`.)
- [ ] Volume boost is louder without harsh clipping.

### SponsorBlock

- [ ] A music video with a non-music intro skips it.
- [ ] A category set to "Show skip button" shows the button in Now Playing; tapping skips.
- [ ] Master switch off: nothing is skipped. (Auto: `sponsorDecision`, `SponsorBlockSegments`.)
- [ ] Each category's setting sticks after leaving the page. (Auto: `sbActionIndex`,
      `SponsorBlockCategoriesMatchUI`.)

### Background and system

- [ ] Screen locked: playback continues; lockscreen and headset buttons work (MPRIS).
- [ ] Cover shows the song and play/pause works.
- [ ] The mini-player shows on every page except Now Playing and opens it on tap.
- [ ] Closing the app stops the audio.

### Browse

- [ ] Home shelves: songs play, artists and albums open. Profiles and channels open as artists.
      (Auto: `browseTarget`.)
- [ ] Search: top result card, songs, artists, albums, playlists, podcast episodes; a failed
      search shows its error. (Auto: `YtmSweepFixes`, `searchOutcome`.)
- [ ] Artist page: songs play from the tapped one; albums and singles open; "Show all songs".
- [ ] A long playlist loads every track. (Auto: `YtmPlaylistContinuation`.)

### Library and history

- [ ] Library lists your playlists (signed in) and they open.
- [ ] Add to playlist from a menu; the song shows up in that playlist on YouTube Music.
- [ ] History lists newest first and Clear empties it. (Auto: `YtmLocalStores`, `YtmAtomicStores`.)

### Downloads

- [ ] Download a song: progress shows, then it's in Downloads and plays offline. (Auto: `dlStarted`,
      `dlProgress`, `dlFinished`, `audioDownloads`.)
- [ ] Download all on an album: already-downloaded songs are skipped. (Auto: `DownloadQueueBatch`,
      `dlQueued`, `dlQueuedToast`.)
- [ ] Remove a download deletes the file. (Auto: `DownloadAudioMeta`.)
- [ ] Pick a custom download folder; reset it. An unwritable folder is refused. (Auto: `DownloadFolder`.)

### Account

- [ ] Import login from the browser; Home turns personal. (Auto: `YtmIdentity`, `CookiesArgs`.)
- [ ] Check sign-in reports the state; an expired session shows the banner on Home. Offline,
      the state doesn't change. (Auto: `sessionFromVerify`, `importOutcome`.)
- [ ] Switch account or brand channel; Home and Library follow. (Auto: `YtmAccountSelector`.)
- [ ] Sign out; songs still play.

### Providers

- [ ] yt-dlp Update on stable, then nightly. The version-skew warning stays hidden.
      (Auto: `ChannelAwareDownloads`, `YtdlpVersionCache`.)
- [ ] Fast resolve shows active on SFOS 5.1+ (Python 3.10+). (Auto: `FastResolveStatusAndGate`.)
- [ ] Install Deno and set up the PO-token provider; the status turns ready and songs still play.
- [ ] Player client choice: pick one, a song plays; back to Auto.
- [ ] Diagnostics report copies to the clipboard. An "InnerTube fast path … fell back" count
      that keeps growing means `innertube.py` needs an update.

### 32-bit phones (armv7hl)

There is no test phone. Check in the SDK's armv7hl target instead:
`sfdk engine exec sb2 -t SailfishOS-5.1.0.11-armv7hl.default python3 …`

- The release has an armv7hl RPM.
- yt-dlp installs and updates from the 32-bit build. (Auto: `Arm32Userland`.)
- Deno and the PO-token setup are hidden. (Auto: `Arm32Userland`.)
- Playback works before yt-dlp is installed, or when it can't run: delete `bin/yt-dlp` under
  the app's data folder and play something. A refused stream names its reason in the log
  ("no stream capacity: …"). (Auto: `MediaProxyEndToEnd`.)
