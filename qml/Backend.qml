import QtQuick 2.0
import io.thp.pyotherside 1.5

// Non-visual wrapper around the Python module. All calls are async and run off
// the UI thread (PyOtherSide worker), which is what we want for yt-dlp subprocess
// calls that take a few seconds.
Item {
    id: backend

    // True once we've confirmed a working yt-dlp is present.
    property bool ready: false
    property bool pyReady: false         // youfish (engine) imported and callable
    property bool ytmReady: false        // ytm (YouTube Music metadata) imported and callable
    property string ytdlpVersion: ""
    property bool updating: false
    property bool installing: false      // downloading yt-dlp into the app data dir
    property real installPct: -1
    property bool ffmpegReady: false     // ffmpeg present (bundled/system) → HD merged downloads
    property string ffmpegVersion: ""
    property bool ffmpegInstalling: false
    property real ffmpegPct: -1
    property string ffmpegStatusMsg: ""
    property bool denoInstalling: false  // downloading Deno (the PO provider's runtime) into bin/
    property real denoPct: -1
    property string denoStatusMsg: ""
    property string playerClient: ""     // yt-dlp youtube player_client ("" = auto)
    property string ytdlpChannel: "stable" // yt-dlp update channel: "stable" | "nightly"
    property string innertubeVersion: ""   // live WEB_REMIX client version (self-healing identity)
    property bool innertubeLive: false     // true once auto-detected from the site (else default)
    property bool homeBackdrop: true     // blurred now-playing art behind the home carousels
    property bool eqEnabled: false       // 10-band equalizer on/off
    property var  eqBands: [0,0,0,0,0,0,0,0,0,0]  // per-band gain (dB), applied by the C++ player
    property real boostGain: 1.0         // volume boost (linear, 1.0 = none) above system max
    property bool autoplay: true         // keep playing related songs (radio) when the queue ends
    property bool skipDisliked: false    // auto-skip disliked songs during autoplay
    property var  dislikedIds: []        // videoIds you've disliked (for skip-disliked)
    property var downloads: []           // completed downloads [{id,title,kind,path}]

    // PO-token provider (bgutil) — opt-in, user-installed Deno sidecar (see SettingsPage).
    property bool potInstalled: false
    property bool potEnabled: false      // installed AND switched on → used by yt-dlp
    property bool potDeno: false         // a Deno runtime is present (required to install/run)
    property bool potRunning: false      // the token server is currently listening
    property bool potInstalling: false   // clone + deno install in progress
    property string potStatusMsg: ""     // latest setup progress / result line
    property string potTag: ""           // pinned provider version
    property bool potResponding: false   // server actually ANSWERS HTTP → "working" (vs just port-open)
    property string potServerVersion: "" // version the running provider reports (from its /ping)
    property string potLastError: ""     // why the provider last failed to start / respond (diagnostics)
    property string potDenoPath: ""      // where Deno was found ("" = not found)

    // Download folder — where downloaded tracks are written. downloadDir is the configured value
    // ("" = the app's own folder); downloadDirEffective is the absolute path actually in use.
    property string downloadDir: ""
    property string downloadDirEffective: ""

    // --- YouTube Music (ytm.py) account/login state ---
    property bool ytmLoggedIn: false     // signed in → personalized home + library
    property bool ytmLoginActive: false  // a device-login flow is in progress
    property string ytmLoginCode: ""     // the code the user types at google.com/device
    property string ytmLoginUrl: ""      // where to type it
    property string ytmLoginMsg: ""      // latest login status / error line
    // Authoritative session state from verifySession: "" = never signed in, "live" = Google accepts
    // it, "expired" = credentials stored but rejected (needs re-import). Drives the visual indicator.
    property string ytmSessionState: ""
    property string ytmAccount: ""       // signed-in account name (from the last successful verify)

    signal resolved(var info)
    signal resolveError(string message)
    signal updateFinished(bool ok, string message)
    signal downloadProgress(string videoId, string kind, real percent)
    signal downloadFinished(string videoId, string kind, bool ok, string message)

    // --- YouTube Music results (from ytm.py) ---
    signal musicResults(var items)
    signal musicError(string message)
    signal musicHomeLoaded(var shelves, bool loggedIn)
    signal ytmLoginFinished(bool ok, string message)

    function resolve(videoId) {
        // audio_only=true → single yt-dlp pass (skip the HD-video-pair retry); much faster start.
        py.call("youfish.resolve", [videoId, true], function(res) {
            if (res && res.ok) backend.resolved(res.info)
            else backend.resolveError(res ? res.error : "resolve failed")
        })
    }

    // Resolve into a caller callback instead of the shared `resolved` signal — used to prefetch
    // the NEXT track while the current one plays, without disturbing current playback.
    function prefetchResolve(videoId, callback) {
        py.call("youfish.resolve", [videoId, true], function(res) {
            callback(res && res.ok ? res.info : null)
        })
    }

    // Song radio (autoplay continuation) for a videoId → caller callback.
    function musicRadio(videoId, callback) {
        py.call("ytm.get_radio", [videoId, 25], function(res) { callback(res || {}) })
    }

    // Lyrics (LRCLIB) for a track → caller callback ({ok, synced:[{t,line}], plain, ...}).
    // Checks the offline cache first (saved alongside a download), so a downloaded track shows
    // synced lyrics with no network; falls back to a live fetch otherwise.
    function musicLyrics(videoId, title, artist, durationSec, callback) {
        py.call("ytm.cached_lyrics", [videoId || ""], function(hit) {
            if (hit && hit.ok) { callback(hit); return }
            py.call("ytm.get_lyrics", [title || "", artist || "", durationSec || 0],
                    function(res) { callback(res || {}) })
        })
    }
    // Fetch + store a track's lyrics for offline use (fired when its audio download completes).
    function musicCacheLyrics(videoId, title, artist, durationSec) {
        if (!videoId) return
        py.call("ytm.cache_lyrics",
                [videoId, title || "", artist || "", durationSec || 0], function() {})
    }

    // --- YouTube Music: search / home / account (ytm.py) ---

    // Combined music search — songs + artists + albums + playlists (works logged out).
    // Emits musicResults / musicError.
    function musicSearch(query) {
        if (!query) return
        py.call("ytm.search", [query, 40], function(res) {
            if (res && res.items) backend.musicResults(res.items)
            else backend.musicError(res && res.error ? res.error : "search failed")
        })
    }

    // Home shelves — personalized when signed in, generic otherwise. Emits musicHomeLoaded.
    function musicHome() {
        py.call("ytm.get_home", [], function(res) {
            if (!res) { backend.musicError("couldn't load home"); return }
            backend.ytmLoggedIn = !!res.logged_in
            backend.musicHomeLoaded(res.shelves || [], !!res.logged_in)
        })
    }

    // The last-cached home shelves (instant, from disk) → caller callback, for an immediate
    // render on launch while musicHome() refreshes in the background.
    function musicHomeCached(callback) {
        py.call("ytm.cached_home", [], function(res) { callback(res || { shelves: [] }) })
    }

    // Like / Dislike / clear a song's rating (requires sign-in). rating: LIKE|DISLIKE|INDIFFERENT.
    function musicRate(videoId, rating) {
        if (!videoId) return
        py.call("ytm.rate_song", [videoId, rating], function() { backend.loadDisliked() })
    }

    function ytmAccountStatus() {
        py.call("ytm.account_status", [], function(res) {
            if (res) backend.ytmLoggedIn = !!res.logged_in
        })
    }

    // Authoritative sign-in check: confirms Google still ACCEPTS the stored session (an idle session
    // gets silently rejected while account_status still reports "signed in"). Only overrides
    // ytmLoggedIn on a CONCLUSIVE result (res.checked) so a transient network error doesn't flip it.
    // Callback gets {ok, account, checked}. Reports the account name on success.
    function verifySession(callback) {
        py.call("ytm.verify_session", [], function(res) {
            if (res && res.checked) {
                backend.ytmLoggedIn = !!res.ok
                backend.ytmSessionState = res.present ? (res.ok ? "live" : "expired") : ""
                backend.ytmAccount = res.ok ? (res.account || "") : ""
            }
            // res.checked === false is INCONCLUSIVE (offline) — leave the last known state alone.
            if (callback) callback(res || {})
        })
    }

    // --- Play history (recently-played tracks) ---
    function musicRecordPlay(track) {
        if (!track || !track.videoId) return
        py.call("ytm.record_play", [track.videoId, track.title || "", track.subtitle || "",
                                     track.thumb || "", track.artistId || ""], function() {})
    }
    function musicHistory(callback) {
        py.call("ytm.play_history", [200], function(list) { callback(list || []) })
    }
    function musicClearHistory(callback) {
        py.call("ytm.clear_play_history", [], function(res) { if (callback) callback(res || {}) })
    }

    // Tracks of a playlist / album / mix page → the caller's callback (page-scoped). `params`
    // (optional) targets an artist "Show all songs" endpoint.
    function musicPlaylist(browseId, params, callback) {
        py.call("ytm.get_playlist", [browseId, 200, params || ""],
                function(res) { callback(res || {}) })
    }

    // Full artist page (top songs + Albums/Singles/Videos carousels) → the caller's callback.
    function musicArtist(browseId, callback) {
        py.call("ytm.get_artist", [browseId], function(res) { callback(res || {}) })
    }

    // Your library playlists (requires sign-in) → the caller's callback.
    function musicLibrary(callback) {
        py.call("ytm.get_library_playlists", [], function(res) { callback(res || {}) })
    }

    // Add a track to one of your playlists (auth) → the caller's callback ({ok, error?}).
    function musicAddToPlaylist(playlistId, videoId, callback) {
        py.call("ytm.add_to_playlist", [playlistId || "", videoId || ""],
                function(res) { callback(res || {}) })
    }

    // Begin OAuth device login. The code + URL arrive via the ytm_login_code event
    // (see onReceived); ytm_login_done reports the outcome.
    function ytmLoginBegin() {
        if (backend.ytmLoginActive) return
        backend.ytmLoginActive = true
        backend.ytmLoginCode = ""
        backend.ytmLoginUrl = ""
        backend.ytmLoginMsg = "Requesting a code…"
        py.call("ytm.login_begin", [], function() {})
    }

    function ytmLogout() {
        py.call("ytm.logout", [], function() {
            backend.ytmLoggedIn = false
            backend.ytmLoginCode = ""
            backend.ytmLoginUrl = ""
            backend.ytmLoginMsg = ""
            backend.ytmSessionState = ""
            backend.ytmAccount = ""
        })
    }

    // Import the signed-in session straight from the Sailfish browser's cookie jar — no
    // copy-paste (FinTune is unsandboxed, so it can read the browser's cookies). Requires the
    // user to be signed in to music.youtube.com in the Sailfish browser.
    function ytmImportBrowserLogin() {
        backend.ytmLoginMsg = "Importing from browser…"
        py.call("ytm.import_browser_login", [], function(res) {
            if (res && res.ok && res.live === false) {
                // Cookies imported, but Google rejects the session — the browser session is stale.
                backend.ytmLoggedIn = false
                backend.ytmSessionState = "expired"
                backend.ytmAccount = ""
                backend.ytmLoginMsg = res.warning || "Imported, but the session looks signed out."
                backend.ytmLoginFinished(false, backend.ytmLoginMsg)
            } else if (res && res.ok) {
                backend.ytmLoggedIn = true
                backend.ytmSessionState = "live"
                backend.ytmAccount = res.account || ""
                backend.ytmLoginMsg = res.account
                    ? ("Signed in as " + res.account + ".")
                    : ("Imported " + (res.count || 0) + " cookies.")
                backend.ytmLoginFinished(true, "")
            } else {
                backend.ytmLoginMsg = (res && res.error) ? res.error : "Import failed."
                backend.ytmLoginFinished(false, backend.ytmLoginMsg)
            }
        })
    }

    // Re-run the yt-dlp presence/version check (called on launch and on demand).
    function recheck() {
        py.call("youfish.ytdlp_version", [], function(v) {
            backend.ytdlpVersion = v || ""
            backend.ready = (v && v.length > 0)
        })
    }

    // --- Settings (persisted by Python) ---
    function loadSettings() {
        py.call("youfish.get_settings", [], function(s) {
            if (!s) return
            backend.playerClient = s.player_client || ""
            backend.ytdlpChannel = s.ytdlp_channel || "stable"
            backend.homeBackdrop = (s.home_backdrop === undefined) ? true : !!s.home_backdrop
            backend.eqEnabled = !!s.eq_enabled
            if (s.eq_bands && s.eq_bands.length === 10)
                backend.eqBands = s.eq_bands
            backend.boostGain = s.boost_gain || 1.0
            backend.autoplay = (s.autoplay === undefined) ? true : !!s.autoplay
            backend.skipDisliked = !!s.skip_disliked
        })
    }

    function setAutoplay(on) {
        py.call("youfish.set_setting", ["autoplay", !!on], function(s) {
            if (s) backend.autoplay = (s.autoplay === undefined) ? true : !!s.autoplay
        })
    }
    function setSkipDisliked(on) {
        py.call("youfish.set_setting", ["skip_disliked", !!on], function(s) {
            if (s) backend.skipDisliked = !!s.skip_disliked
        })
    }
    // Disliked videoIds (updated by rate_song); loaded at startup and after each rating.
    function loadDisliked() {
        py.call("ytm.disliked_ids", [], function(list) { backend.dislikedIds = list || [] })
    }

    // Equalizer + volume boost: persist + mirror. The C++ player is driven from these in main.qml.
    function setEqEnabled(on) {
        py.call("youfish.set_setting", ["eq_enabled", !!on], function(s) {
            if (s) backend.eqEnabled = !!s.eq_enabled
        })
    }
    function setEqBands(bands) {
        py.call("youfish.set_setting", ["eq_bands", bands], function(s) {
            if (s && s.eq_bands && s.eq_bands.length === 10)
                backend.eqBands = s.eq_bands
        })
    }
    function setBoostGain(gain) {
        py.call("youfish.set_setting", ["boost_gain", gain], function(s) {
            if (s) backend.boostGain = s.boost_gain || 1.0
        })
    }

    // Blurred now-playing art behind the home carousels (UI taste setting).
    function setHomeBackdrop(on) {
        py.call("youfish.set_setting", ["home_backdrop", !!on], function(s) {
            if (s) backend.homeBackdrop = !!s.home_backdrop
        })
    }

    // Generic string setting (player_client). Mirrors the saved value back onto the property.
    function setSetting(key, value) {
        py.call("youfish.set_setting", [key, value], function(s) {
            if (!s) return
            backend.playerClient = s.player_client || ""
            backend.ytdlpChannel = s.ytdlp_channel || "stable"
        })
    }

    // --- Downloads (background, progress via pyotherside events) ---
    // Download a music track for offline play (audio), carrying its artist/cover so the Downloads
    // list and player keep them. track = {videoId,title,subtitle,thumb,artistId}.
    function downloadTrack(track) {
        if (!track || !track.videoId) return
        py.call("youfish.download",
                [track.videoId, track.title || "", "audio",
                 { subtitle: track.subtitle || "", thumb: track.thumb || "",
                   artistId: track.artistId || "" }],
                function() {})
    }
    function loadDownloads() {
        py.call("youfish.list_downloads", [], function(list) { backend.downloads = list || [] })
    }
    function deleteDownload(videoId, kind) {
        py.call("youfish.delete_download", [videoId, kind], function(res) {
            if (res && res.downloads) backend.downloads = res.downloads
        })
        py.call("ytm.clear_cached_lyrics", [videoId], function() {})   // drop its offline lyrics too
    }
    // Where downloads are written: load the current folder, or set/reset it (folder picker in
    // Settings). setDownloadDir validates writability in Python and reports {ok, error?}.
    function loadDownloadLocation() {
        py.call("youfish.download_location", [], function(r) {
            if (!r) return
            backend.downloadDir = r.configured || ""
            backend.downloadDirEffective = r.effective || ""
        })
    }
    function setDownloadDir(path, callback) {
        py.call("youfish.set_download_dir", [path || ""], function(r) {
            if (r) {
                backend.downloadDir = r.configured || ""
                backend.downloadDirEffective = r.effective || ""
            }
            if (callback) callback(r || {})
        })
    }

    // Download yt-dlp into the app data dir (the sandbox-reachable location). Progress +
    // completion arrive as pyotherside events (see onReceived), reusing updateFinished.
    function installYtdlp() {
        if (backend.installing) return
        backend.installing = true
        backend.installPct = 0
        py.call("youfish.install_ytdlp", [], function() {})
    }

    // Self-update yt-dlp via its own `-U`. Can take a while (downloads the binary).
    function updateYtdlp() {
        if (backend.updating) return
        backend.updating = true
        py.call("youfish.ytdlp_update", [], function(res) {
            backend.updating = false
            if (res && res.version) {
                backend.ytdlpVersion = res.version
                backend.ready = res.version.length > 0
            }
            backend.updateFinished(!!(res && res.ok),
                res ? (res.output || res.error || "") : "update failed")
        })
    }

    // ffmpeg — optional, enables HD merged downloads. Managed like yt-dlp (bundled binary).
    function recheckFfmpeg() {
        py.call("youfish.ffmpeg_version", [], function(v) {
            backend.ffmpegVersion = v || ""
            backend.ffmpegReady = (v && v.length > 0)
        })
    }
    function installFfmpeg() {
        if (backend.ffmpegInstalling) return
        backend.ffmpegInstalling = true
        backend.ffmpegPct = 0
        backend.ffmpegStatusMsg = ""
        py.call("youfish.install_ffmpeg", [], function() {})
    }
    // Download Deno (the PO provider's runtime) into our own bin/ — so the provider needs no
    // manual runtime install. ~40 MB one-time fetch; progress/result arrive as pyotherside events.
    function installDeno() {
        if (backend.denoInstalling) return
        backend.denoInstalling = true
        backend.denoPct = 0
        backend.denoStatusMsg = ""
        py.call("youfish.install_deno", [], function() {})
    }

    // --- PO-token provider (bgutil): opt-in setup + on/off, all driven from Python ---
    function loadPotStatus() {
        py.call("youfish.pot_status", [], function(s) {
            if (!s) return
            backend.potInstalled = !!s.installed
            backend.potEnabled = !!s.enabled
            backend.potDeno = !!s.deno
            backend.potDenoPath = s.deno_path || ""
            backend.potRunning = !!s.running
            backend.potResponding = !!s.responding
            backend.potServerVersion = s.server_version || ""
            backend.potLastError = s.last_error || ""
            backend.potTag = s.tag || ""
        })
    }
    function startPotProvider() {
        // Nudge the PO-token sidecar up if it isn't already listening. prewarm() is idempotent
        // (a no-op when the server is up) and starts it on a persistent background thread.
        py.call("youfish.prewarm", [], function() {})
    }
    // Full copy-pasteable health report for the provider + its deps → caller callback. The report
    // action actively (re)starts the server, so refresh the status props afterward — otherwise the
    // top status line stays stale ("server not started") while the report already says "working".
    function potDiagnostics(callback) {
        py.call("youfish.pot_diagnostics", [], function(res) {
            backend.loadPotStatus()
            if (callback) callback(res || {})
        })
    }
    function installPotProvider() {
        if (backend.potInstalling) return
        backend.potInstalling = true
        backend.potStatusMsg = "Starting…"
        py.call("youfish.install_pot_provider", [], function() {})
    }
    // Resolve the latest provider release from GitHub + (re)install it. User-initiated, so the
    // sidecar only moves in step with the yt-dlp plugin on an explicit tap — never silently.
    function updatePotProvider() {
        if (backend.potInstalling) return
        backend.potInstalling = true
        backend.potStatusMsg = "Checking for the latest provider…"
        py.call("youfish.update_pot_provider", [], function() {})
    }
    function setPotEnabled(on) {
        py.call("youfish.set_pot_enabled", [!!on], function(s) {
            if (!s) return
            backend.potInstalled = !!s.installed
            backend.potEnabled = !!s.enabled
            backend.potRunning = !!s.running
            backend.potResponding = !!s.responding
            backend.potLastError = s.last_error || ""
        })
    }

    // Live InnerTube client identity (self-healing) — surfaced in Settings so the auto-detect is
    // verifiable. Reading it also nudges a background refresh when the cached version is stale.
    function loadInnertubeIdentity() {
        py.call("ytm.innertube_identity", [], function(r) {
            if (!r) return
            backend.innertubeVersion = r.version || ""
            backend.innertubeLive = (r.source === "live")
        })
    }

    // Session keep-alive: while signed in and the app is running (foreground OR background audio
    // playback — SFOS keeps the process alive during playback), periodically make one authed call.
    // verifySession hits account_menu, whose success path folds Google's cookie rotations back into
    // the store (_absorb_rotations) — so this both KEEPS the session fresh off our own traffic and
    // refreshes the indicator. Can't help while the app is fully closed/suspended (nothing runs then).
    Timer {
        interval: 15 * 60 * 1000     // 15 min — well inside the rotating-token lifetime
        repeat: true
        running: backend.ytmReady && backend.ytmLoggedIn
        onTriggered: backend.verifySession()
    }

    Python {
        id: py
        Component.onCompleted: {
            addImportPath(Qt.resolvedUrl("../python").toString().replace("file://", ""))
            importModule("youfish", function() {
                backend.pyReady = true
                backend.recheck()
                backend.recheckFfmpeg()
                backend.loadSettings()
                backend.loadDownloads()
                backend.loadDownloadLocation()
                backend.loadPotStatus()
                py.call("youfish.prewarm", [], function() {})  // POT server up before first play
            })
            // The YouTube Music metadata layer (separate module, same worker).
            importModule("ytm", function() {
                backend.ytmReady = true
                backend.ytmAccountStatus()          // fast, presence-based (may be optimistically stale)
                backend.verifySession()             // then correct it against what Google actually accepts
                backend.loadDisliked()
                backend.loadInnertubeIdentity()
            })
        }
        // Background-download progress/completion events from the Python thread.
        onReceived: {
            if (data[0] === "download_progress")
                backend.downloadProgress(data[1], data[2], data[3])
            else if (data[0] === "download_done") {
                backend.downloadFinished(data[1], data[2], data[3], data[4])
                backend.loadDownloads()
            }
            else if (data[0] === "ytdlp_install_progress")
                backend.installPct = data[1]
            else if (data[0] === "ytdlp_install_done") {
                backend.installing = false
                backend.installPct = -1
                if (data[3] && data[3].length > 0) {
                    backend.ytdlpVersion = data[3]
                    backend.ready = true
                }
                backend.updateFinished(!!data[1], data[2])
            }
            else if (data[0] === "ffmpeg_install_progress")
                backend.ffmpegPct = data[1]
            else if (data[0] === "ffmpeg_install_done") {
                backend.ffmpegInstalling = false
                backend.ffmpegPct = -1
                backend.ffmpegStatusMsg = data[2]
                if (data[3] && data[3].length > 0) {
                    backend.ffmpegVersion = data[3]
                    backend.ffmpegReady = true
                }
            }
            else if (data[0] === "deno_install_progress")
                backend.denoPct = data[1]
            else if (data[0] === "deno_install_done") {
                backend.denoInstalling = false
                backend.denoPct = -1
                backend.denoStatusMsg = data[2]
                backend.loadPotStatus()   // refresh potDeno — the provider setup unlocks once found
            }
            else if (data[0] === "pot_install_progress")
                backend.potStatusMsg = data[1]
            else if (data[0] === "pot_install_done") {
                backend.potInstalling = false
                backend.potStatusMsg = data[2]
                backend.loadPotStatus()
            }
            // YouTube Music OAuth device login.
            else if (data[0] === "ytm_login_code") {
                backend.ytmLoginCode = data[1]
                backend.ytmLoginUrl = data[2]
                backend.ytmLoginMsg = "Enter this code to sign in."
            }
            else if (data[0] === "ytm_login_done") {
                backend.ytmLoginActive = false
                backend.ytmLoginCode = ""
                if (data[1]) {
                    backend.ytmLoggedIn = true
                    backend.ytmLoginMsg = "Signed in."
                } else {
                    backend.ytmLoginMsg = data[2] || "Login failed."
                }
                backend.ytmLoginFinished(!!data[1], data[2] || "")
            }
        }
        onError: console.log("python error: " + traceback)
    }
}
