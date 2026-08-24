import QtQuick 2.0
import Sailfish.Silica 1.0
import FinTune 1.0
import "pages"
import "cover"

// FinTune — native YouTube Music for Sailfish OS.
//
// One hidden VideoPlayer is the app-wide *audio engine*: music playback survives page
// navigation, and Now Playing + the docked mini-player are just views of its state. We
// feed the resolved AUDIO track as the player's single source (audioUrl empty → the
// engine's muxed/single-source path), so nothing decodes video for a song.
ApplicationWindow {
    id: app

    property alias backend: backend
    property alias nowPlaying: nowPlaying
    property alias player: player     // the audio engine, so pages (Now Playing) can bind to it

    // --- App-wide "now playing" state (the source of truth; views bind to it) ---
    property bool   npActive: false
    property string npId: ""
    property string npTitle: ""
    property string npArtist: ""
    property string npArtistId: ""      // artist channel browseId (UC…), "" if unknown → not tappable
    property string npThumb: ""
    property string npRating: ""        // "LIKE" | "DISLIKE" | "" — set this session by the player buttons
    property bool   npResolving: false
    property string npError: ""
    property var    npInfo: null        // last resolved stream info (audio ladder + muxed)
    property int    npAudioIdx: 0        // current step in npInfo.audio_urls
    property bool   npTriedMuxed: false  // guard so we fall back to itag 18 at most once
    property bool   npReresolved: false  // one fresh re-resolve per track before we give up on it

    // Play queue — albums / playlists / mixes; a single-song tap is a 1-item queue so next/prev
    // and (later) radio autoplay apply uniformly.
    property var    playQueue: []        // [{videoId,title,subtitle,thumb}]
    property int    playQueueIndex: -1
    property bool   hasNext: app.playQueueIndex >= 0 && app.playQueueIndex + 1 < app.playQueue.length
    property bool   hasPrev: app.playQueueIndex > 0

    // Prefetch + radio: resolveCache holds pre-resolved stream info by videoId so a skip/advance
    // is instant; radioLoading guards the autoplay-continuation fetch; npFailStreak bounds
    // auto-skipping so an all-failing queue can't spin forever.
    property var    resolveCache: ({})
    property bool   radioLoading: false
    property int    npFailStreak: 0

    // Repeat mode: 0 off (queue ends → song radio), 1 repeat-all (queue wraps to the top),
    // 2 repeat-one (current track replays). Only the automatic end-of-track advance honours it;
    // tapping next/prev always moves through the queue.
    property int    repeatMode: 0

    // Downloads: in-flight progress by videoId ({title, pct}), for the Downloads view; plus a
    // small transient toast for start/finish feedback.
    property var    dlActive: ({})
    property string toastText: ""

    initialPage: Component { HomePage { } }

    cover: Component {
        CoverPage {
            active: nowPlaying.active
            title: nowPlaying.title
            channel: nowPlaying.channel
            thumb: nowPlaying.thumb
            playing: nowPlaying.playing
            onToggle: nowPlaying.toggleRequested()
        }
    }

    allowedOrientations: defaultAllowedOrientations

    // The hidden audio engine. Never shown — Now Playing renders album art instead.
    // audioOnly: build no video branch, so an audio-only song reaches PLAYING (a video sink
    // with no data would otherwise hang the pipeline).
    VideoPlayer {
        id: player
        visible: false
        width: 0; height: 0
        audioOnly: true
    }

    // Thin mirror of the app state for MPRIS + the cover (which bind to `nowPlaying`).
    QtObject {
        id: nowPlaying
        property bool active: app.npActive
        property string title: app.npTitle
        property string channel: app.npArtist
        property string thumb: app.npThumb
        property bool playing: player.playing
        property bool hasNext: app.npActive        // radio always continues → next is available
        property bool hasPrev: app.hasPrev
        signal toggleRequested()
        signal stopRequested()
        signal nextRequested()
        signal prevRequested()
    }
    Connections {
        target: nowPlaying
        onToggleRequested: app.togglePlay()
        onNextRequested: app.playNext()
        onPrevRequested: app.playPrev()
    }

    Backend { id: backend }

    // Push equalizer + volume-boost settings onto the audio engine. The C++ player keeps them
    // across pipeline rebuilds, so we only re-apply when the settings change (and once on load).
    function applyAudio() {
        player.setEqEnabled(backend.eqEnabled)
        var b = backend.eqBands
        for (var i = 0; i < 10 && i < b.length; i++)
            player.setEqBand(i, b[i])
        player.setBoost(backend.boostGain)
    }
    Connections {
        target: backend
        onEqEnabledChanged: app.applyAudio()
        onEqBandsChanged: app.applyAudio()
        onBoostGainChanged: app.applyAudio()
    }
    Component.onCompleted: app.applyAudio()

    // Optional MPRIS (lockscreen) controls, isolated so a missing plugin degrades quietly.
    Loader {
        source: Qt.resolvedUrl("MprisControls.qml")
        onLoaded: item.np = nowPlaying
        onStatusChanged: if (status === Loader.Error)
            console.log("FinTune: MPRIS unavailable (org.nemomobile.mpris not installed)")
    }

    // --- Playback control (drives the global player) ---

    // Play a single track (becomes a 1-item queue). item = {videoId, title, subtitle, thumb}.
    // Picking a lone song also seeds its radio so "Up next" fills in and playback keeps going
    // (albums/playlists are their own queue, so they don't get this — radio only kicks in once
    // they run dry).
    function playSong(item) {
        if (item && item.videoId) {
            app.playQueueList([item], 0)
            app.seedRadio(item.videoId)
        }
    }

    // Fetch a song's radio and append it to the (single-song) queue WITHOUT advancing — the seed
    // stays playing while its continuation fills in behind it. Guards against a different queue
    // having been started in the meantime (async callback race).
    function seedRadio(videoId) {
        if (!videoId || app.radioLoading || !backend.autoplay)
            return
        app.radioLoading = true
        backend.musicRadio(videoId, function(res) {
            app.radioLoading = false
            // Bail if the user has since started a different / longer queue (album, another song).
            if (app.playQueue.length !== 1 || app.playQueue[0].videoId !== videoId)
                return
            var tracks = (res && res.tracks) ? res.tracks : []
            var q = app.playQueue.slice()
            var have = { }
            have[videoId] = true                   // the seed is already the current track
            for (var j = 0; j < tracks.length; j++) {
                var t = tracks[j]
                if (t.videoId && !have[t.videoId]
                        && !(backend.skipDisliked && app.isDisliked(t.videoId))) {
                    have[t.videoId] = true
                    q.push(t)
                }
            }
            if (q.length > app.playQueue.length) {
                app.playQueue = q
                app.prefetchNext()                 // warm up the next track for a gapless hand-off
            }
        })
    }

    // Play a list of tracks from startIndex (albums, playlists, mixes). The rest stay queued
    // so it auto-advances and next/prev work.
    function playQueueList(items, startIndex) {
        if (!items || items.length === 0)
            return
        app.resolveCache = ({})        // fresh context — drop prefetches from the old queue
        app.npFailStreak = 0
        app.playQueue = items
        app.playQueueIndex = Math.max(0, Math.min(startIndex || 0, items.length - 1))
        app.startCurrentQueueItem(true)
    }

    // Load the current queue item's metadata and start it — from the prefetch cache if we have it
    // (instant), else via a live resolve. `openNp` brings Now Playing forward (true on a manual
    // tap; false on auto-advance so we don't yank the user off whatever they're browsing).
    function startCurrentQueueItem(openNp) {
        var it = app.playQueue[app.playQueueIndex]
        if (!it || !it.videoId)
            return
        app.npId = it.videoId
        app.npTitle = it.title || ""
        app.npArtist = it.subtitle || ""
        app.npArtistId = it.artistId || ""
        app.npThumb = it.thumb || ""
        app.npRating = ""                      // rating state is per-track (set by the player buttons)
        app.npActive = true
        app.npError = ""
        app.npReresolved = false               // fresh re-resolve budget for the new track
        backend.musicRecordPlay(it)            // remember it in the play history
        player.stop()
        var localPath = app.localPathFor(it.videoId)
        if (localPath) {                       // offline copy → play the file, no network at all
            app.npResolving = false
            app.applyResolved({ audio_urls: [app.fileUri(localPath)], http_ua: "" })
        } else {
            var cached = app.resolveCache[it.videoId]
            if (cached) {
                app.npResolving = false
                app.applyResolved(cached)      // prefetched → start with no wait
            } else {
                app.npResolving = true
                backend.resolve(it.videoId)    // resolved() → applyResolved()
            }
        }
        if (openNp)
            openNowPlaying()
        app.prefetchNext()                     // warm up the following track
    }

    function isDisliked(videoId) {
        return !!videoId && backend.dislikedIds.indexOf(videoId) !== -1
    }

    function playNext() {
        if (app.hasNext) {
            var idx = app.playQueueIndex + 1
            // Skip-disliked: hop over any disliked tracks ahead (bounded so it can't loop).
            if (backend.skipDisliked) {
                var guard = 0
                while (idx < app.playQueue.length
                       && app.isDisliked(app.playQueue[idx].videoId)
                       && guard < app.playQueue.length) {
                    idx += 1; guard += 1
                }
            }
            if (idx < app.playQueue.length) {
                app.playQueueIndex = idx
                app.startCurrentQueueItem(false)
                return
            }
            // everything after the current track was disliked → fall through to wrap / radio
        }
        if (app.repeatMode === 1 && app.playQueue.length > 0) {
            app.playQueueIndex = 0             // repeat-all → wrap to the top of the queue
            app.startCurrentQueueItem(false)
        } else {
            app.startRadioContinuation()       // queue dry → keep playing with song radio
        }
    }

    // Restart the current track from the beginning (repeat-one). Reuses the already-resolved
    // stream info — no re-resolve — and rebuilds the pipeline from step 0 of the audio ladder.
    function replayCurrent() {
        if (!app.npInfo) { app.startCurrentQueueItem(false); return }
        app.npAudioIdx = 0
        app.npTriedMuxed = false
        app.npError = ""
        app.playCurrentStep()
    }
    function playPrev() {
        if (app.hasPrev) {
            app.playQueueIndex -= 1
            app.startCurrentQueueItem(false)
        }
    }

    // Jump to a specific queue position (from the Up-next view).
    function playQueueJump(index) {
        if (index >= 0 && index < app.playQueue.length && index !== app.playQueueIndex) {
            app.playQueueIndex = index
            app.startCurrentQueueItem(false)
        }
    }

    // Insert a track right after the current one (Play next). Nothing playing → just play it.
    function queueInsertNext(track) {
        if (!track || !track.videoId)
            return
        if (!app.npActive) { app.playSong(track); return }
        var q = app.playQueue.slice()
        q.splice(app.playQueueIndex + 1, 0, track)
        app.playQueue = q
        app.prefetchNext()
    }

    // Append a track to the end of the queue (Add to queue).
    function queueAppend(track) {
        if (!track || !track.videoId)
            return
        if (!app.npActive) { app.playSong(track); return }
        var q = app.playQueue.slice()
        q.push(track)
        app.playQueue = q
    }

    // Remove a queued track (not the one currently playing) and keep the index pointing right.
    function queueRemove(index) {
        if (index < 0 || index >= app.playQueue.length || index === app.playQueueIndex)
            return
        var q = app.playQueue.slice()
        q.splice(index, 1)
        if (index < app.playQueueIndex)
            app.playQueueIndex -= 1
        app.playQueue = q
    }

    function togglePlay() {
        if (player.playing) player.pause()
        else player.play()
    }

    // Open the playlist picker to add a track to one of the user's playlists.
    function addToPlaylist(track) {
        if (!track || !track.videoId)
            return
        if (!backend.ytmLoggedIn) { app.showToast("Sign in to use playlists"); return }
        pageStack.push(Qt.resolvedUrl("pages/PlaylistPickerPage.qml"), { track: track })
    }

    // Like / dislike the current track (from the player buttons). Tapping the already-active one
    // clears the rating. Needs sign-in (the buttons disable themselves when signed out).
    function rateCurrent(rating) {
        if (!app.npId)
            return
        if (app.npRating === rating) {
            app.npRating = ""
            backend.musicRate(app.npId, "INDIFFERENT")
            app.showToast(rating === "LIKE" ? "Like removed" : "Dislike removed")
        } else {
            app.npRating = rating
            backend.musicRate(app.npId, rating)
            app.showToast(rating === "LIKE" ? "Liked" : "Disliked")
        }
    }

    // --- Downloads (offline audio) ---

    // Turn a filesystem path into a valid file:// URI (percent-encode each segment so spaces and
    // brackets in the download filename don't break GStreamer's uridecodebin).
    function fileUri(path) {
        if (!path)
            return ""
        var parts = path.split("/")
        for (var i = 0; i < parts.length; i++)
            parts[i] = encodeURIComponent(parts[i])
        return "file://" + parts.join("/")
    }

    // Local file for a videoId if it's been downloaded (audio), else "".
    function localPathFor(videoId) {
        if (!videoId)
            return ""
        var dl = backend.downloads
        for (var i = 0; i < dl.length; i++)
            if (dl[i].id === videoId && dl[i].kind === "audio" && dl[i].path)
                return dl[i].path
        return ""
    }
    function isDownloaded(videoId) { return app.localPathFor(videoId).length > 0 }

    // Start an offline audio download of a track (no-op if already downloaded).
    function downloadTrack(track) {
        if (!track || !track.videoId || app.isDownloaded(track.videoId))
            return
        backend.downloadTrack(track)
        var a = {}
        for (var k in app.dlActive) a[k] = app.dlActive[k]
        a[track.videoId] = { title: track.title || "track", subtitle: track.subtitle || "", pct: 0 }
        app.dlActive = a
        app.showToast("Downloading " + (track.title || "track") + "…")
    }

    // Play a downloaded entry ({id,title,subtitle,thumb,artistId,path}) — a 1-item queue; the
    // local-file branch in startCurrentQueueItem picks up the offline copy.
    function playLocalDownload(entry) {
        if (!entry || !entry.id)
            return
        app.playQueueList([{ videoId: entry.id, title: entry.title || "",
                             subtitle: entry.subtitle || "", thumb: entry.thumb || "",
                             artistId: entry.artistId || "" }], 0)
    }

    function showToast(msg) {
        app.toastText = msg
        toastTimer.restart()
    }
    Timer { id: toastTimer; interval: 2500; onTriggered: app.toastText = "" }

    // Download progress / completion → update the in-flight map + toast. The completed list is
    // refreshed by Backend's own event handler (loadDownloads on download_done).
    Connections {
        target: backend
        onDownloadProgress: {                  // (videoId, kind, percent)
            if (kind !== "audio")
                return
            var a = {}
            for (var k in app.dlActive) a[k] = app.dlActive[k]
            var t = (a[videoId] && a[videoId].title) ? a[videoId].title : "Downloading"
            var sub = (a[videoId] && a[videoId].subtitle) ? a[videoId].subtitle : ""
            a[videoId] = { title: t, subtitle: sub, pct: percent }
            app.dlActive = a
        }
        onDownloadFinished: {                   // (videoId, kind, ok, message)
            var was = app.dlActive[videoId]     // its title/artist, before we drop it from the map
            var a = {}
            for (var k in app.dlActive)
                if (k !== videoId) a[k] = app.dlActive[k]
            app.dlActive = a
            if (kind === "audio") {
                app.showToast(ok ? "Downloaded" : ("Download failed: " + message))
                if (ok)                          // stash its lyrics alongside the audio, for offline
                    app.backend.musicCacheLyrics(videoId, was ? was.title : "",
                                                 was ? was.subtitle : "", 0)
            }
        }
    }

    // Upscale a YouTube Music / Google art URL to a larger square. List thumbnails arrive small
    // (and get disk-cached); the Now Playing page asks for a big, crisp version instead.
    function artUrl(url, size) {
        if (!url)
            return ""
        var s = size || 600
        return url.replace(/=w\d+-h\d+/, "=w" + s + "-h" + s)
                  .replace(/\/(default|mqdefault|sddefault)\.jpg/, "/hqdefault.jpg")
    }

    // Open the artist (channel) page for a browseId.
    function openArtist(browseId, name) {
        if (!browseId)
            return
        pageStack.push(Qt.resolvedUrl("pages/ArtistPage.qml"),
                       { browseId: browseId, artistName: name || "" })
    }

    // Route a tapped card/result to the right place: a song plays, an artist opens the artist
    // page, anything else (album / playlist / mix) opens its tracklist.
    function openBrowse(item) {
        if (!item)
            return
        if (item.videoId)
            app.playSong(item)
        else if (item.kind === "artist" && item.browseId)
            app.openArtist(item.browseId, item.title)
        else if (item.browseId)
            pageStack.push(Qt.resolvedUrl("pages/PlaylistPage.qml"),
                { browseId: item.browseId, playlistTitle: item.title || "",
                  thumb: item.thumb || "" })
    }

    // Bring the Now Playing page forward without stacking duplicates.
    function openNowPlaying() {
        if (pageStack.currentPage && pageStack.currentPage.objectName === "nowPlaying")
            return
        pageStack.push(Qt.resolvedUrl("pages/NowPlayingPage.qml"))
    }

    Connections {
        target: backend
        onResolved: app.applyResolved(info)
        onResolveError: {
            app.npResolving = false
            app.skipFailedTrack()              // couldn't even resolve → move on
        }
    }

    // Start playing a resolved track (from a live resolve or the prefetch cache).
    function applyResolved(info) {
        app.npResolving = false
        app.npInfo = info
        app.npAudioIdx = 0
        app.npTriedMuxed = false
        app.playCurrentStep()
    }

    // Prefetch the NEXT queued track's stream info while the current one plays, so advancing is
    // instant. Cached by videoId; a stale cached URL self-heals via the proxy's re-resolve-on-403,
    // so entries never need explicit expiry within a session.
    function prefetchNext() {
        if (!app.hasNext)
            return
        var nx = app.playQueue[app.playQueueIndex + 1]
        if (!nx || !nx.videoId || app.resolveCache[nx.videoId])
            return
        var vid = nx.videoId
        backend.prefetchResolve(vid, function(info) {
            if (info)
                app.resolveCache[vid] = info
        })
    }

    // Queue ran dry → continue with the current track's song radio (YouTube Music autoplay).
    function startRadioContinuation() {
        if (app.radioLoading || !app.npId || !backend.autoplay)
            return
        app.radioLoading = true
        backend.musicRadio(app.npId, function(res) {
            app.radioLoading = false
            var tracks = (res && res.tracks) ? res.tracks : []
            var have = {}
            for (var i = 0; i < app.playQueue.length; i++)
                have[app.playQueue[i].videoId] = true
            var q = app.playQueue.slice()
            for (var j = 0; j < tracks.length; j++)
                if (tracks[j].videoId && !have[tracks[j].videoId]
                        && !(backend.skipDisliked && app.isDisliked(tracks[j].videoId)))
                    q.push(tracks[j])
            if (q.length > app.playQueue.length) {
                app.playQueue = q
                app.playQueueIndex += 1
                app.startCurrentQueueItem(false)
            }
            // else: radio gave nothing new → playback stops at the end.
        })
    }

    // A track failed to play at all (audio ladder + muxed exhausted, or resolve failed). Skip to
    // the next, but bound the streak so an all-failing queue doesn't spin forever.
    function skipFailedTrack() {
        app.npFailStreak += 1
        if (app.npFailStreak <= 4)
            app.playNext()
        else
            app.npError = "Couldn't play the next few tracks — stopped."
    }

    // Walk the audio ladder (best codec first), then the muxed progressive stream (itag 18), then
    // give up. YouTube SABR-gates a codec per-video, so if one audio URL 403s we advance to the
    // next. Adaptive audio plays with audioOnly (no video decode); the muxed step turns audioOnly
    // off so the itag-18 video pad is consumed (no demuxer stall) while we play just its audio.
    function playCurrentStep() {
        var info = app.npInfo
        if (!info)
            return
        var urls = (info.audio_urls && info.audio_urls.length > 0)
                   ? info.audio_urls
                   : ((info.audio_url && info.audio_url.length > 0) ? [info.audio_url] : [])
        player.stop()
        player.userAgent = info.http_ua
        player.audioUrl = ""
        if (app.npAudioIdx < urls.length) {
            player.audioOnly = true
            player.videoUrl = urls[app.npAudioIdx]
            player.play()
        } else if (!app.npTriedMuxed && info.muxed_url && info.muxed_url.length > 0) {
            app.npTriedMuxed = true
            player.audioOnly = false
            player.videoUrl = info.muxed_url
            player.play()
        } else if (!app.npReresolved && app.npId.length > 0) {
            // Ladder + muxed exhausted. Before giving up, get ONE fresh resolve of THIS track —
            // its URLs may have gone fully stale (resumed after a long pause, or the proxy's
            // per-URL 403 refresh couldn't keep up). Fresh URLs, restart the ladder from the top.
            // Runs while backgrounded too, so music self-heals without the user looking.
            app.npReresolved = true
            app.npAudioIdx = 0
            app.npTriedMuxed = false
            app.npResolving = true
            backend.resolve(app.npId)      // resolved() → applyResolved() → playCurrentStep()
        } else {
            app.skipFailedTrack()          // fresh resolve didn't help either → skip the track
        }
    }

    Connections {
        target: player
        // A fetch/decode error → try the next rung of the ladder. playCurrentStep() ends by
        // skipping the track once everything's exhausted, so this can't loop forever.
        onErrorOccurred: {
            app.npAudioIdx += 1
            app.npError = ""
            app.playCurrentStep()
        }
        // Track finished → repeat-one replays it; otherwise advance the queue (album/playlist),
        // wrap on repeat-all, or start radio when it runs dry.
        onEnded: {
            if (app.repeatMode === 2)
                app.replayCurrent()
            else
                app.playNext()
        }
        // A track that actually starts playing clears the fail streak + any transient error.
        onPlayingChanged: if (player.playing) {
            app.npFailStreak = 0
            app.npReresolved = false       // played OK → allow a fresh re-resolve on a later stall
            app.npError = ""
        }
    }

    // --- Docked mini-player: floats over every page, tap to open Now Playing ---
    Item {
        id: miniPlayer
        z: 1000
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: Theme.itemSizeMedium
        // Hidden while the full Now Playing page is up — it's redundant there.
        visible: app.npActive && !(pageStack.currentPage
                 && pageStack.currentPage.objectName === "nowPlaying")

        Rectangle {
            anchors.fill: parent
            color: Theme.overlayBackgroundColor
            opacity: 0.95
        }
        Rectangle {                   // thin accent line along the top edge
            anchors { left: parent.left; right: parent.right; top: parent.top }
            height: Math.max(2, Math.round(2 * Theme.pixelRatio))
            color: Theme.highlightColor
        }
        MouseArea {
            anchors.fill: parent
            onClicked: app.openNowPlaying()
        }
        Image {
            id: mpArt
            anchors {
                left: parent.left; leftMargin: Theme.horizontalPageMargin
                verticalCenter: parent.verticalCenter
            }
            width: Theme.itemSizeSmall; height: width
            fillMode: Image.PreserveAspectCrop
            clip: true
            asynchronous: true
            source: app.npThumb
            Rectangle {               // placeholder tint when there's no art yet
                anchors.fill: parent
                visible: mpArt.status !== Image.Ready
                color: Theme.rgba(Theme.highlightBackgroundColor, 0.3)
            }
        }
        Column {
            anchors {
                left: mpArt.right; leftMargin: Theme.paddingMedium
                right: mpPlay.left; rightMargin: Theme.paddingMedium
                verticalCenter: parent.verticalCenter
            }
            Label {
                width: parent.width
                text: app.npTitle
                truncationMode: TruncationMode.Fade
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.primaryColor
            }
            Label {
                width: parent.width
                text: app.npResolving ? "Loading…" : app.npArtist
                truncationMode: TruncationMode.Fade
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
            }
        }
        IconButton {
            id: mpPlay
            anchors {
                right: parent.right; rightMargin: Theme.horizontalPageMargin
                verticalCenter: parent.verticalCenter
            }
            icon.source: player.playing ? "image://theme/icon-m-pause"
                                        : "image://theme/icon-m-play"
            enabled: !app.npResolving
            onClicked: app.togglePlay()
        }
    }

    // Transient download toast — sits just above the mini-player.
    Rectangle {
        id: toast
        z: 1100
        visible: app.toastText.length > 0
        anchors {
            left: parent.left; right: parent.right
            bottom: parent.bottom
            bottomMargin: miniPlayer.visible ? miniPlayer.height : 0
        }
        height: toastLabel.paintedHeight + 2 * Theme.paddingMedium
        color: Theme.rgba(Theme.highlightDimmerColor, 0.95)
        Label {
            id: toastLabel
            anchors {
                left: parent.left; right: parent.right
                verticalCenter: parent.verticalCenter
                leftMargin: Theme.horizontalPageMargin
                rightMargin: Theme.horizontalPageMargin
            }
            text: app.toastText
            wrapMode: Text.Wrap
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.primaryColor
        }
    }
}
