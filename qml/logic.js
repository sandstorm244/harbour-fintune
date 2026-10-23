.pragma library
// Pure decisions the app makes about the queue, radio, the audio ladder, SponsorBlock and volume,
// kept apart from Silica so tests/qml_logic_test.js can run them under node. Rules: ES5 only
// (Qt 5.6's engine), no QML types, no qsTr — return codes and let the caller map them to text.
// Functions return new arrays and never change the ones passed in. A bug here plays the wrong
// song or gets the player stuck, so every function has a test.

// After this many tracks in a row fail to play at all, playback stops instead of skipping on.
var MAX_FAIL_STREAK = 4

function _isDisliked(disliked, videoId) {
    return !!videoId && !!disliked && disliked.indexOf(videoId) !== -1
}

// The queue index Next lands on after `index`: the next entry, hopping over disliked tracks when
// skipDisliked is on. -1 when nothing playable is left (the caller wraps on repeat-all or falls
// through to radio). Next and the next-track prefetch both use it, so the warmed track is the
// one that plays.
function nextIndex(queue, index, skipDisliked, disliked) {
    var idx = index + 1
    if (skipDisliked)
        while (idx < queue.length && _isDisliked(disliked, queue[idx].videoId))
            idx += 1
    return idx < queue.length ? idx : -1
}

// Append a radio batch to the queue: drop tracks without an id, tracks already queued, repeats
// inside the batch itself, and disliked tracks when skipDisliked is on.
function mergeRadio(queue, tracks, skipDisliked, disliked) {
    var q = queue.slice()
    var have = {}
    for (var i = 0; i < q.length; i++)
        have[q[i].videoId] = true
    for (var j = 0; j < (tracks || []).length; j++) {
        var t = tracks[j]
        if (t && t.videoId && !have[t.videoId]
                && !(skipDisliked && _isDisliked(disliked, t.videoId))) {
            have[t.videoId] = true
            q.push(t)
        }
    }
    return q
}

// Fisher-Yates over the entries after `pivot` (history and the current track stay put).
// `rand` returns [0, 1); tests pass a fixed one.
function shuffleTail(queue, pivot, rand) {
    rand = rand || Math.random
    var q = queue.slice()
    for (var i = q.length - 1; i > pivot + 1; i--) {
        var j = pivot + 1 + Math.floor(rand() * (i - pivot))
        var tmp = q[i]; q[i] = q[j]; q[j] = tmp
    }
    return q
}

// A shuffled copy of a whole array (a fresh radio batch, so it isn't played in YTM's order).
function shuffled(arr, rand) {
    rand = rand || Math.random
    var a = (arr || []).slice()
    for (var i = a.length - 1; i > 0; i--) {
        var j = Math.floor(rand() * (i + 1))
        var tmp = a[i]; a[i] = a[j]; a[j] = tmp
    }
    return a
}

// Shuffle off: put the tracks still ahead back into the order of `snapshot` (the queue as it was
// when shuffle went on). History and the current track stay as played; tracks added while
// shuffled (radio) that aren't in the snapshot follow at the end.
function unshuffle(queue, index, snapshot) {
    var head = queue.slice(0, index + 1)
    var headIds = {}, tailIds = {}, placed = {}, restored = []
    var i
    for (i = 0; i < head.length; i++) headIds[head[i].videoId] = true
    for (i = index + 1; i < queue.length; i++) tailIds[queue[i].videoId] = true
    for (i = 0; i < (snapshot || []).length; i++) {
        var v = snapshot[i].videoId
        if (tailIds[v] && !headIds[v] && !placed[v]) { restored.push(snapshot[i]); placed[v] = true }
    }
    for (i = index + 1; i < queue.length; i++) {
        var e = queue[i]
        if (!placed[e.videoId]) { restored.push(e); placed[e.videoId] = true }
    }
    return head.concat(restored)
}

// Start index for playing a list: clamped into the list.
function clampIndex(start, length) {
    return Math.max(0, Math.min(start || 0, length - 1))
}

// Remove queue entry `index` (never the current one). Returns {queue, index} with the current
// index still pointing at the same track, or null when the removal isn't allowed.
function removeAt(queue, index, current) {
    if (index < 0 || index >= queue.length || index === current)
        return null
    var q = queue.slice()
    q.splice(index, 1)
    return { queue: q, index: index < current ? current - 1 : current }
}

// Insert `track` right after the current entry (Play next).
function insertNext(queue, current, track) {
    var q = queue.slice()
    q.splice(current + 1, 0, track)
    return q
}

// The audio URLs to try for a resolve answer, best first (older answers carry one audio_url).
function audioUrls(info) {
    if (info.audio_urls && info.audio_urls.length > 0)
        return info.audio_urls
    return (info.audio_url && info.audio_url.length > 0) ? [info.audio_url] : []
}

// The next step of the playback ladder: each audio URL in turn, then the muxed stream once, then
// one fresh resolve of the track, then skip it. Returns {action: "audio"|"muxed", url} or
// {action: "reresolve"} or {action: "skip"}.
function ladderStep(info, audioIdx, triedMuxed, reresolved, hasId) {
    var urls = audioUrls(info)
    if (audioIdx < urls.length)
        return { action: "audio", url: urls[audioIdx] }
    if (!triedMuxed && info.muxed_url && info.muxed_url.length > 0)
        return { action: "muxed", url: info.muxed_url }
    if (!reresolved && hasId)
        return { action: "reresolve" }
    return { action: "skip" }
}

// Playback gain for YouTube's loudnessDb (how far the master sits above its target): 10^(-dB/20),
// capped at the user's max boost and floored at 0.1 (-20 dB, mirrored in the C++ player).
function gainForLoudness(db, maxBoostDb) {
    var g = Math.pow(10, -db / 20.0)
    var ceil = Math.pow(10, maxBoostDb / 20.0)
    if (g > ceil) g = ceil
    if (g < 0.1) g = 0.1
    return g
}

// SponsorBlock action for a category: whatever the user set, "off" when unset or disabled.
function sbAction(enabled, actions, category) {
    if (!enabled)
        return "off"
    var a = actions || {}
    return a[category] ? a[category] : "off"
}

// What SponsorBlock does at `pos` (seconds): {skip: segment to jump past now, or null; manual:
// segment to show the skip button for, or null}. The last 0.4 s of a segment doesn't count, so a
// skip that lands on its end can't loop. A "skip" segment wins over a "manual" overlap.
function sponsorDecision(segments, pos, enabled, actions) {
    var manual = null
    for (var i = 0; i < (segments || []).length; i++) {
        var s = segments[i]
        if (pos < s.start || pos >= s.end - 0.4)
            continue
        var act = sbAction(enabled, actions, s.category)
        if (act === "skip")
            return { skip: s, manual: null }
        if (act === "manual" && manual === null)
            manual = s
    }
    return { skip: null, manual: manual }
}

// A filesystem path as a file:// URI, each segment percent-encoded so spaces and brackets in a
// download's name don't break GStreamer.
function fileUri(path) {
    if (!path)
        return ""
    var parts = path.split("/")
    for (var i = 0; i < parts.length; i++)
        parts[i] = encodeURIComponent(parts[i])
    return "file://" + parts.join("/")
}

// A YouTube Music / Google art URL upscaled to a `size` square (default 600).
function artUrl(url, size) {
    if (!url)
        return ""
    var s = size || 600
    return url.replace(/=w\d+-h\d+/, "=w" + s + "-h" + s)
              .replace(/\/(default|mqdefault|sddefault)\.jpg/, "/hqdefault.jpg")
}

// Where a tapped card goes: "song" plays it, "artist" opens the artist page (also user profiles
// and channels, whose ids start with UC), "playlist" opens a tracklist (album, playlist, mix).
// "" when there's nothing to open.
function browseTarget(item) {
    if (!item)
        return ""
    if (item.videoId)
        return "song"
    if (item.browseId && (item.kind === "artist" || item.browseId.indexOf("UC") === 0))
        return "artist"
    return item.browseId ? "playlist" : ""
}

// Index of the synced lyric line playing at `posMs`: the last line that has started, -1 before
// the first. Lines are sorted by t (ms).
function lyricLineAt(lines, posMs) {
    var idx = -1
    for (var i = 0; i < (lines || []).length; i++) {
        if (lines[i].t <= posMs) idx = i
        else break
    }
    return idx
}

// --- Repeat and end of track ----------------------------------------------------------------

// Repeat modes: 0 off, 1 all (wrap the queue), 2 one (replay the track).
var REPEAT_OFF = 0, REPEAT_ALL = 1, REPEAT_ONE = 2

// The repeat button cycles off → all → one → off.
function nextRepeatMode(mode) {
    return (mode + 1) % 3
}

// A track ended: "replay" it (repeat one) or go to the "next" one.
function endAction(repeatMode) {
    return repeatMode === REPEAT_ONE ? "replay" : "next"
}

// Next found nothing playable ahead: "wrap" to the top (repeat all, non-empty queue) or
// continue with "radio".
function afterQueueEnd(repeatMode, queueLength) {
    return (repeatMode === REPEAT_ALL && queueLength > 0) ? "wrap" : "radio"
}

// A track failed to play at all; `streak` counts it. "next" skips on, "stop" gives up after
// MAX_FAIL_STREAK failures in a row.
function failAction(streak) {
    return streak <= MAX_FAIL_STREAK ? "next" : "stop"
}

// --- Radio requests ---------------------------------------------------------------------------
// A radio answer arrives seconds later. If the queue changed meanwhile (another album started,
// a track was added or skipped), the answer belongs to a queue that no longer exists and is
// dropped. The caller records radioSnapshot() when asking and checks radioStale() on the answer.

// What the queue looked like when the radio was asked for. `playingId` is the track that must
// still be playing for the answer to count ("" = don't care).
function radioSnapshot(queue, playingId) {
    var n = queue.length
    return { length: n, lastId: n > 0 ? queue[n - 1].videoId : "", playingId: playingId || "" }
}

function radioStale(snap, queue, playingId) {
    if (queue.length !== snap.length)
        return true
    if (snap.length > 0 && queue[snap.length - 1].videoId !== snap.lastId)
        return true
    return snap.playingId !== "" && playingId !== snap.playingId
}

// The seed for fetching radio ahead of time: when the NEXT track is the last in the queue, its
// radio is fetched now so the queue already continues when it starts. "" = don't fetch: a radio
// request is already out, autoplay is off, repeat is on (radio is never reached), the next track
// isn't the last, or this seed was already asked (its radio added nothing new).
function radioTailSeed(queue, index, loading, autoplay, repeatMode, lastSeed) {
    var n = queue.length
    if (loading || !autoplay || repeatMode !== REPEAT_OFF || index < 0 || index + 2 !== n)
        return ""
    var seed = queue[n - 1].videoId
    return (seed && seed !== lastSeed) ? seed : ""
}

// The queue ran dry: "wait" for the ahead-of-time radio request already out for this very track
// (its answer advances the queue), "fetch" the playing track's radio, or "stop".
function continuationAction(loading, playingId, tailSeed, autoplay) {
    if (loading && playingId && playingId === tailSeed)
        return "wait"
    if (loading || !playingId || !autoplay)
        return "stop"
    return "fetch"
}

// --- Downloads ----------------------------------------------------------------------------------
// `downloads` is the engine's list [{id, kind, path, loudness_db?, sb_segments?, ...}].

// The local file of a downloaded song, or "".
function localPath(downloads, videoId) {
    if (!videoId)
        return ""
    for (var i = 0; i < (downloads || []).length; i++) {
        var d = downloads[i]
        if (d.id === videoId && d.kind === "audio" && d.path)
            return d.path
    }
    return ""
}

// loudnessDb stored with a downloaded copy (offline normalization), or undefined.
function localLoudnessDb(downloads, videoId) {
    if (!videoId)
        return undefined
    for (var i = 0; i < (downloads || []).length; i++)
        if (downloads[i].id === videoId && typeof downloads[i].loudness_db === "number")
            return downloads[i].loudness_db
    return undefined
}

// SponsorBlock segments stored with a downloaded copy (offline skipping), or [].
function localSponsorSegments(downloads, videoId) {
    if (!videoId)
        return []
    for (var i = 0; i < (downloads || []).length; i++)
        if (downloads[i].id === videoId && downloads[i].sb_segments)
            return downloads[i].sb_segments
    return []
}

// The track to warm up next: the id at queue[index] unless it's downloaded (a local file never
// resolves). "" = nothing to prefetch.
function prefetchId(queue, index, downloads) {
    var nx = index >= 0 ? queue[index] : null
    return (nx && nx.videoId && !localPath(downloads, nx.videoId)) ? nx.videoId : ""
}

// The completed audio downloads, for the Downloads page.
function audioDownloads(downloads) {
    var out = []
    for (var i = 0; i < (downloads || []).length; i++)
        if (downloads[i].kind === "audio")
            out.push(downloads[i])
    return out
}

// The in-flight downloads map {videoId: {title, subtitle, pct}}. Each change returns a new map
// (QML only notices a reassigned property). `fallbackTitle` is the translated "track".
function _copyMap(m) {
    var a = {}
    for (var k in m) a[k] = m[k]
    return a
}

// One download started.
function dlStarted(active, track, fallbackTitle) {
    var a = _copyMap(active)
    a[track.videoId] = { title: track.title || fallbackTitle, subtitle: track.subtitle || "", pct: 0 }
    return a
}

// A batch was queued: mark exactly the ids the engine queued (it skips downloaded ones), so no
// entry waits for progress it will never get.
function dlQueued(active, tracks, queuedIds, fallbackTitle) {
    var byId = {}
    for (var i = 0; i < (tracks || []).length; i++)
        if (tracks[i].videoId) byId[tracks[i].videoId] = tracks[i]
    var a = _copyMap(active)
    for (var j = 0; j < (queuedIds || []).length; j++) {
        var t = byId[queuedIds[j]]
        if (t) a[queuedIds[j]] = { title: t.title || fallbackTitle, subtitle: t.subtitle || "", pct: 0 }
    }
    return a
}

// Toast for a queued batch: "queued", "queued-skipped" (some were already saved) or "none".
function dlQueuedToast(queuedCount, skipped) {
    if (queuedCount <= 0)
        return "none"
    return skipped > 0 ? "queued-skipped" : "queued"
}

// Progress for one download; an unknown id gets `fallbackTitle` (the translated "Downloading").
function dlProgress(active, videoId, pct, fallbackTitle) {
    var a = _copyMap(active)
    var old = a[videoId]
    a[videoId] = { title: (old && old.title) ? old.title : fallbackTitle,
                   subtitle: (old && old.subtitle) ? old.subtitle : "", pct: pct }
    return a
}

// A download finished: {active: the map without it, was: its entry (title/artist) or undefined}.
function dlFinished(active, videoId) {
    var a = {}
    for (var k in active)
        if (k !== videoId) a[k] = active[k]
    return { active: a, was: active[videoId] }
}

// The in-flight map as a list for the Downloads page.
function dlActiveList(active) {
    var out = []
    for (var k in active)
        out.push({ id: k, title: active[k].title, pct: active[k].pct })
    return out
}

// --- Volume normalization source ---------------------------------------------------------------

// Where a track's loudness comes from: {source: "off"} (normalize off or no track), "local"
// (stored with the download, `db` set), "pending" (resolve in flight: decide when it lands; the
// caller passes info === null for that), "resolve" (the resolve answer carried it, `db` set), or
// "fetch" (ask the engine).
function normSource(enabled, videoId, localDb, info) {
    if (!enabled || !videoId)
        return { source: "off" }
    if (localDb !== undefined)
        return { source: "local", db: localDb }
    if (info === null)
        return { source: "pending" }
    if (info && typeof info.loudness_db === "number")
        return { source: "resolve", db: info.loudness_db }
    return { source: "fetch" }
}

// The gain to apply for a known loudness: unity when normalize is off or there's no number.
function normGain(enabled, db, maxBoostDb) {
    return (enabled && typeof db === "number") ? gainForLoudness(db, maxBoostDb) : 1.0
}

// --- Ratings ---------------------------------------------------------------------------------

// Tapping Like or Dislike: the same rating again clears it. Returns {rating: the new state ("" =
// none), send: what to send to YouTube (LIKE / DISLIKE / INDIFFERENT), toast: "liked",
// "disliked", "like-removed" or "dislike-removed"}.
function rateToggle(current, tapped) {
    var like = tapped === "LIKE"
    if (current === tapped)
        return { rating: "", send: "INDIFFERENT", toast: like ? "like-removed" : "dislike-removed" }
    return { rating: tapped, send: tapped, toast: like ? "liked" : "disliked" }
}

// --- Engine answers ---------------------------------------------------------------------------

// A search answer: {items} or {error}. An error wins unless items came back too ({items: [],
// error} is an error); error "" = no answer at all (the caller shows a generic message).
function searchOutcome(res) {
    if (res && res.error && !(res.items && res.items.length > 0))
        return { error: res.error }
    if (res && res.items)
        return { items: res.items }
    return { error: "" }
}

// A sign-in check: the new {loggedIn, state ("live" | "expired" | ""), account}, or null when
// the check was inconclusive (offline) and the last known state must stay.
function sessionFromVerify(res) {
    if (!res || !res.checked)
        return null
    return { loggedIn: !!res.ok, state: res.present ? (res.ok ? "live" : "expired") : "",
             account: res.ok ? (res.account || "") : "" }
}

// A browser login import: "stale" (cookies imported but Google rejects the session), "ok", or
// "failed".
function importOutcome(res) {
    if (res && res.ok && res.live === false)
        return "stale"
    return (res && res.ok) ? "ok" : "failed"
}

// --- Home shelves -----------------------------------------------------------------------------

// The cached shelves to paint, or null: they only fill an empty page, never replace what's
// showing.
function cachedShelves(current, cached) {
    return (current.length === 0 && cached && cached.length > 0) ? cached : null
}

// Fresh shelves replace what's showing; an empty answer keeps it.
function freshShelves(current, fresh) {
    return (fresh && fresh.length > 0) ? fresh : current
}

// --- Equalizer and SponsorBlock settings ------------------------------------------------------

// Preset curves (dB per band, 10 bands), in the order of the Equalizer page's preset names.
// Index 0 is Flat.
var EQ_PRESETS = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 2, 4, 5, 6],
    [5, 3, -1, -2, -1, 1, 3, 4, 4, 4],
    [-1, 2, 4, 4, 2, 0, -1, -1, 0, 1],
    [-2, -1, 0, 2, 4, 4, 3, 2, 0, -1],
    [4, 3, 0, -2, -3, 0, 2, 3, 4, 5]
]

// Picking preset `idx`: {bands: a copy of its curve, enable: turn the equalizer on (any curve but
// Flat, when it's off)}, or null for an index out of range.
function presetPlan(idx, eqEnabled) {
    if (idx < 0 || idx >= EQ_PRESETS.length)
        return null
    return { bands: EQ_PRESETS[idx].slice(), enable: idx !== 0 && !eqEnabled }
}

// SponsorBlock category action ↔ menu index: 0 Off, 1 Auto-skip, 2 Show skip button.
function sbActionIndex(action) {
    return action === "skip" ? 1 : (action === "manual" ? 2 : 0)
}
function sbIndexAction(index) {
    return index === 1 ? "skip" : (index === 2 ? "manual" : "off")
}
