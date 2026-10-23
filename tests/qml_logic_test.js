// Tests for qml/logic.js, the pure decisions the app makes (queue, radio, ladder, ...). Run: node tests/qml_logic_test.js
// The lib is loaded the way QML sees it (".pragma library" stripped, no Node globals), and is first
// checked for syntax that Qt 5.6's ES5 engine on the phone would reject but Node accepts.
"use strict";
var fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");

var LIB = path.join(__dirname, "..", "qml", "logic.js");

function es5Problems(src) {
    var code = src.replace(/\/\*[\s\S]*?\*\//g, "")           // block comments
                  .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')        // string contents
                  .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
                  .replace(/\/\/.*$/gm, "");                    // line comments
    var rules = [[/=>/, "arrow function"], [/\blet\s/, "let"], [/\bconst\s/, "const"],
                 [/`/, "template string"], [/\bclass\s/, "class"], [/\.\.\./, "spread"],
                 [/\bof\s/, "for...of"]];
    return rules.filter(function (r) { return r[0].test(code); })
                .map(function (r) { return r[1]; });
}

function load(file) {
    var src = fs.readFileSync(file, "utf8");
    var bad = es5Problems(src);
    if (bad.length)
        throw new Error(path.basename(file) + " uses syntax Qt 5.6 can't run: " + bad.join(", "));
    var ctx = {};
    vm.createContext(ctx);
    vm.runInContext(src.replace(/^\.pragma library.*$/m, ""), ctx, { filename: file });
    return ctx;
}

var L = load(LIB);
var tests = [], failed = 0;
function test(name, fn) { tests.push([name, fn]); }
var eq = assert.deepStrictEqual;
// Objects built inside the vm context have a different Object prototype; compare plain copies.
function plain(v) { return v === undefined ? v : JSON.parse(JSON.stringify(v)); }

var DIS = ["d1", "d2"];
function ids(q) { return plain(q).map(function (t) { return t.videoId; }); }
function Q() { return Array.prototype.slice.call(arguments).map(function (v) { return { videoId: v }; }); }
// A fixed "random" source: always picks the lowest index, so a shuffle is deterministic.
function zero() { return 0; }

// --- next track -------------------------------------------------------------------------------
test("nextIndex: plain next, end of queue", function () {
    eq(L.nextIndex(Q("a", "b"), 0, false, DIS), 1);
    eq(L.nextIndex(Q("a", "b"), 1, false, DIS), -1);
});
test("nextIndex: hops disliked tracks only when skipDisliked is on", function () {
    var q = Q("a", "d1", "d2", "b");
    eq(L.nextIndex(q, 0, true, DIS), 3);
    eq(L.nextIndex(q, 0, false, DIS), 1);
});
test("nextIndex: everything ahead disliked → -1 (wrap or radio)", function () {
    eq(L.nextIndex(Q("a", "d1", "d2"), 0, true, DIS), -1);
});

// --- radio ------------------------------------------------------------------------------------
test("mergeRadio: drops queued, repeated, id-less and disliked tracks", function () {
    var q = L.mergeRadio(Q("a"), Q("a", "b", "b", "d1", "c").concat([{ title: "no id" }]), true, DIS);
    eq(ids(q), ["a", "b", "c"]);
});
test("mergeRadio: keeps disliked when skipDisliked is off, and never changes the input", function () {
    var queue = Q("a");
    eq(ids(L.mergeRadio(queue, Q("d1"), false, DIS)), ["a", "d1"]);
    eq(ids(queue), ["a"]);
});
test("mergeRadio: no answer → queue unchanged", function () {
    eq(ids(L.mergeRadio(Q("a"), undefined, true, DIS)), ["a"]);
});

// --- shuffle ----------------------------------------------------------------------------------
test("shuffleTail keeps history and the current track in place", function () {
    var q = L.shuffleTail(Q("h", "cur", "x", "y", "z"), 1, zero);
    eq(ids(q).slice(0, 2), ["h", "cur"]);
    eq(ids(q).slice(2).sort(), ["x", "y", "z"]);
    eq(ids(q), ["h", "cur", "y", "z", "x"]);
});
test("shuffleTail from -1 shuffles the whole queue (repeat-all wrap)", function () {
    eq(ids(L.shuffleTail(Q("a", "b", "c"), -1, zero)).sort(), ["a", "b", "c"]);
    eq(ids(L.shuffleTail(Q("a", "b", "c"), -1, zero)), ["b", "c", "a"]);
});
test("shuffled keeps every track", function () {
    eq(ids(L.shuffled(Q("a", "b", "c", "d"), Math.random)).sort(), ["a", "b", "c", "d"]);
    eq(plain(L.shuffled(undefined)), []);
});
test("unshuffle restores the snapshot order of what's still ahead", function () {
    var snap = Q("a", "b", "c", "d", "e");
    // Shuffled, then played a and d: queue is a, d (current), e, b, c, plus radio r.
    var now = Q("a", "d", "e", "b", "c", "r");
    eq(ids(L.unshuffle(now, 1, snap)), ["a", "d", "b", "c", "e", "r"]);
});
test("unshuffle with an empty snapshot keeps the order", function () {
    eq(ids(L.unshuffle(Q("a", "b", "c"), 0, [])), ["a", "b", "c"]);
});

// --- queue edits ------------------------------------------------------------------------------
test("clampIndex", function () {
    eq(L.clampIndex(5, 3), 2); eq(L.clampIndex(-1, 3), 0); eq(L.clampIndex(undefined, 3), 0);
});
test("removeAt before, after and at the current track", function () {
    eq(plain(L.removeAt(Q("a", "b", "c"), 0, 1)), { queue: Q("b", "c"), index: 0 });
    eq(plain(L.removeAt(Q("a", "b", "c"), 2, 1)), { queue: Q("a", "b"), index: 1 });
    eq(L.removeAt(Q("a", "b", "c"), 1, 1), null);
    eq(L.removeAt(Q("a"), 4, 0), null);
});
test("insertNext puts the track right after the current one", function () {
    eq(ids(L.insertNext(Q("a", "b", "c"), 0, { videoId: "n" })), ["a", "n", "b", "c"]);
});

// --- playback ladder --------------------------------------------------------------------------
var info = { audio_urls: ["u1", "u2"], muxed_url: "m" };
test("ladderStep walks audio, muxed, one re-resolve, then skip", function () {
    eq(plain(L.ladderStep(info, 0, false, false, true)), { action: "audio", url: "u1" });
    eq(plain(L.ladderStep(info, 1, false, false, true)), { action: "audio", url: "u2" });
    eq(plain(L.ladderStep(info, 2, false, false, true)), { action: "muxed", url: "m" });
    eq(plain(L.ladderStep(info, 2, true, false, true)), { action: "reresolve" });
    eq(plain(L.ladderStep(info, 2, true, true, true)), { action: "skip" });
});
test("ladderStep: a local file has no muxed or re-resolve rung worth trying twice", function () {
    eq(L.ladderStep({ audio_urls: ["file:///x"] }, 1, false, true, true).action, "skip");
});
test("ladderStep: an older answer with one audio_url", function () {
    eq(plain(L.ladderStep({ audio_url: "a" }, 0, false, false, true)), { action: "audio", url: "a" });
    eq(L.ladderStep({}, 0, false, false, false).action, "skip");
});
test("MAX_FAIL_STREAK", function () { eq(L.MAX_FAIL_STREAK, 4); });

// --- volume normalization ---------------------------------------------------------------------
function near(a, b) { assert.ok(Math.abs(a - b) < 1e-9, a + " != " + b); }
test("gainForLoudness: loud tracks turned down, quiet ones up to the cap", function () {
    near(L.gainForLoudness(0, 12), 1);
    near(L.gainForLoudness(6, 12), Math.pow(10, -6 / 20));
    near(L.gainForLoudness(-6, 12), Math.pow(10, 6 / 20));
    near(L.gainForLoudness(-20, 0), 1);                 // max boost 0 dB: never louder
    near(L.gainForLoudness(-20, 3), Math.pow(10, 3 / 20));
    near(L.gainForLoudness(40, 12), 0.1);               // floor at -20 dB
});

// --- SponsorBlock -----------------------------------------------------------------------------
var segs = [{ start: 10, end: 20, category: "sponsor" }, { start: 30, end: 40, category: "intro" }];
test("sbAction", function () {
    eq(L.sbAction(true, { sponsor: "skip" }, "sponsor"), "skip");
    eq(L.sbAction(false, { sponsor: "skip" }, "sponsor"), "off");
    eq(L.sbAction(true, {}, "sponsor"), "off");
});
test("sponsorDecision: skip, manual, outside", function () {
    eq(L.sponsorDecision(segs, 12, true, { sponsor: "skip" }).skip.end, 20);
    eq(L.sponsorDecision(segs, 31, true, { intro: "manual" }).manual.category, "intro");
    eq(plain(L.sponsorDecision(segs, 25, true, { sponsor: "skip" })), { skip: null, manual: null });
});
test("sponsorDecision: the last 0.4 s doesn't count (no seek loop)", function () {
    eq(L.sponsorDecision(segs, 19.7, true, { sponsor: "skip" }).skip, null);
});
test("sponsorDecision: master switch off does nothing", function () {
    eq(plain(L.sponsorDecision(segs, 12, false, { sponsor: "skip" })), { skip: null, manual: null });
});
test("sponsorDecision: an overlapping skip wins over manual", function () {
    var both = [{ start: 0, end: 50, category: "intro" }, { start: 5, end: 15, category: "sponsor" }];
    var d = L.sponsorDecision(both, 8, true, { intro: "manual", sponsor: "skip" });
    eq(d.skip.category, "sponsor"); eq(d.manual, null);
});

// --- helpers ----------------------------------------------------------------------------------
test("fileUri percent-encodes each path segment", function () {
    eq(L.fileUri("/home/u/Music/A b [x].m4a"), "file:///home/u/Music/A%20b%20%5Bx%5D.m4a");
    eq(L.fileUri(""), "");
});
test("artUrl upscales Google and YouTube art", function () {
    eq(L.artUrl("https://lh3.googleusercontent.com/x=w60-h60-l90", 0), "https://lh3.googleusercontent.com/x=w600-h600-l90");
    eq(L.artUrl("https://i.ytimg.com/vi/x/mqdefault.jpg", 300), "https://i.ytimg.com/vi/x/hqdefault.jpg");
    eq(L.artUrl("", 300), "");
});
test("browseTarget routes songs, artists, channels and lists", function () {
    eq(L.browseTarget({ videoId: "v", browseId: "UCx" }), "song");
    eq(L.browseTarget({ kind: "artist", browseId: "MPx" }), "artist");
    eq(L.browseTarget({ browseId: "UCabc" }), "artist");
    eq(L.browseTarget({ browseId: "MPREb_1" }), "playlist");
    eq(L.browseTarget({}), ""); eq(L.browseTarget(null), "");
});
test("lyricLineAt", function () {
    var lines = [{ t: 1000 }, { t: 5000 }];
    eq(L.lyricLineAt(lines, 500), -1); eq(L.lyricLineAt(lines, 1000), 0);
    eq(L.lyricLineAt(lines, 9000), 1); eq(L.lyricLineAt([], 9000), -1);
});

// --- repeat, end of track, failures -----------------------------------------------------------
test("nextRepeatMode cycles off → all → one → off", function () {
    eq([0, 1, 2].map(L.nextRepeatMode), [1, 2, 0]);
});
test("endAction: repeat one replays, otherwise next", function () {
    eq(L.endAction(L.REPEAT_ONE), "replay");
    eq(L.endAction(L.REPEAT_ALL), "next"); eq(L.endAction(L.REPEAT_OFF), "next");
});
test("afterQueueEnd: wrap on repeat all, radio otherwise", function () {
    eq(L.afterQueueEnd(L.REPEAT_ALL, 3), "wrap");
    eq(L.afterQueueEnd(L.REPEAT_ALL, 0), "radio");
    eq(L.afterQueueEnd(L.REPEAT_OFF, 3), "radio");
});
test("failAction stops after MAX_FAIL_STREAK failures in a row", function () {
    eq(L.failAction(1), "next"); eq(L.failAction(L.MAX_FAIL_STREAK), "next");
    eq(L.failAction(L.MAX_FAIL_STREAK + 1), "stop");
});

// --- radio requests ---------------------------------------------------------------------------
test("radioStale: unchanged queue keeps the answer", function () {
    var q = Q("a", "b");
    eq(L.radioStale(L.radioSnapshot(q, ""), Q("a", "b"), "x"), false);
});
test("radioStale: a longer, shorter or different queue drops it", function () {
    var snap = L.radioSnapshot(Q("a", "b"), "");
    eq(L.radioStale(snap, Q("a", "b", "c"), ""), true);
    eq(L.radioStale(snap, Q("a"), ""), true);
    eq(L.radioStale(snap, Q("a", "z"), ""), true);
});
test("radioStale: the continuation also needs the same track playing", function () {
    var snap = L.radioSnapshot(Q("a", "b"), "b");
    eq(L.radioStale(snap, Q("a", "b"), "b"), false);
    eq(L.radioStale(snap, Q("a", "b"), "a"), true);
});
test("radioStale: an empty queue snapshot", function () {
    eq(L.radioStale(L.radioSnapshot([], "x"), [], "x"), false);
});
test("radioTailSeed: only when the next track is the last one", function () {
    var q = Q("a", "b", "c");
    eq(L.radioTailSeed(q, 1, false, true, 0, ""), "c");
    eq(L.radioTailSeed(q, 0, false, true, 0, ""), "");     // two tracks still ahead
    eq(L.radioTailSeed(q, 2, false, true, 0, ""), "");     // already on the last one
});
test("radioTailSeed: not while loading, without autoplay, with repeat, or twice", function () {
    var q = Q("a", "b");
    eq(L.radioTailSeed(q, 0, true, true, 0, ""), "");
    eq(L.radioTailSeed(q, 0, false, false, 0, ""), "");
    eq(L.radioTailSeed(q, 0, false, true, L.REPEAT_ALL, ""), "");
    eq(L.radioTailSeed(q, 0, false, true, 0, "b"), "");
    eq(L.radioTailSeed(q, -1, false, true, 0, ""), "");
});
test("continuationAction", function () {
    eq(L.continuationAction(true, "a", "a", true), "wait");     // tail request for this track is out
    eq(L.continuationAction(true, "a", "b", true), "stop");     // another request is out
    eq(L.continuationAction(false, "", "", true), "stop");
    eq(L.continuationAction(false, "a", "", false), "stop");    // autoplay off
    eq(L.continuationAction(false, "a", "", true), "fetch");
});

// --- downloads --------------------------------------------------------------------------------
var DL = [{ id: "a", kind: "audio", path: "/m/a.m4a", loudness_db: -3, sb_segments: [{ start: 1 }] },
          { id: "b", kind: "audio", path: "" },
          { id: "c", kind: "video", path: "/v/c.mp4", loudness_db: 0 }];
test("localPath: audio downloads with a file only", function () {
    eq(L.localPath(DL, "a"), "/m/a.m4a");
    eq(L.localPath(DL, "b"), ""); eq(L.localPath(DL, "c"), ""); eq(L.localPath(DL, ""), "");
    eq(L.localPath(undefined, "a"), "");
});
test("localLoudnessDb and localSponsorSegments", function () {
    eq(L.localLoudnessDb(DL, "a"), -3); eq(L.localLoudnessDb(DL, "c"), 0);
    eq(L.localLoudnessDb(DL, "b"), undefined); eq(L.localLoudnessDb(DL, ""), undefined);
    eq(plain(L.localSponsorSegments(DL, "a")), [{ start: 1 }]);
    eq(plain(L.localSponsorSegments(DL, "b")), []);
});
test("prefetchId skips downloaded tracks and missing entries", function () {
    eq(L.prefetchId(Q("x", "a", "y"), 1, DL), "");
    eq(L.prefetchId(Q("x", "a", "y"), 2, DL), "y");
    eq(L.prefetchId(Q("x"), -1, DL), "");
});
test("audioDownloads", function () {
    eq(plain(L.audioDownloads(DL)).map(function (d) { return d.id; }), ["a", "b"]);
});
test("dlStarted / dlProgress / dlFinished never change the old map", function () {
    var a0 = {};
    var a1 = L.dlStarted(a0, { videoId: "v", title: "", subtitle: "S" }, "track");
    eq(plain(a1), { v: { title: "track", subtitle: "S", pct: 0 } });
    eq(plain(a0), {});
    var a2 = L.dlProgress(a1, "v", 40, "Downloading");
    eq(plain(a2.v), { title: "track", subtitle: "S", pct: 40 });
    eq(plain(L.dlProgress({}, "w", 5, "Downloading").w), { title: "Downloading", subtitle: "", pct: 5 });
    var fin = L.dlFinished(a2, "v");
    eq(plain(fin.active), {}); eq(fin.was.subtitle, "S"); eq(a2.v.pct, 40);
    eq(L.dlFinished(a0, "v").was, undefined);
});
test("dlQueued marks exactly the queued ids", function () {
    var tracks = [{ videoId: "a", title: "A" }, { videoId: "b" }, { videoId: "c", title: "C" }];
    var a = L.dlQueued({ z: { title: "Z", subtitle: "", pct: 9 } }, tracks, ["b", "c", "q"], "track");
    eq(plain(a), { z: { title: "Z", subtitle: "", pct: 9 },
                   b: { title: "track", subtitle: "", pct: 0 },
                   c: { title: "C", subtitle: "", pct: 0 } });
});
test("dlQueuedToast", function () {
    eq(L.dlQueuedToast(3, 0), "queued"); eq(L.dlQueuedToast(3, 2), "queued-skipped");
    eq(L.dlQueuedToast(0, 5), "none");
});
test("dlActiveList", function () {
    eq(plain(L.dlActiveList({ v: { title: "T", subtitle: "S", pct: 7 } })), [{ id: "v", title: "T", pct: 7 }]);
});

// --- normalization source ---------------------------------------------------------------------
test("normSource: off, local, pending, resolve, fetch", function () {
    eq(plain(L.normSource(false, "v", -3, {})), { source: "off" });
    eq(plain(L.normSource(true, "", -3, {})), { source: "off" });
    eq(plain(L.normSource(true, "v", -3, null)), { source: "local", db: -3 });   // download wins
    eq(plain(L.normSource(true, "v", undefined, null)), { source: "pending" });
    eq(plain(L.normSource(true, "v", undefined, { loudness_db: 2 })), { source: "resolve", db: 2 });
    eq(plain(L.normSource(true, "v", undefined, {})), { source: "fetch" });
    eq(plain(L.normSource(true, "v", undefined, undefined)), { source: "fetch" });
});
test("normGain: unity when off or unknown", function () {
    eq(L.normGain(false, 6, 12), 1); eq(L.normGain(true, null, 12), 1);
    near(L.normGain(true, 6, 12), Math.pow(10, -6 / 20));
});

// --- ratings, engine answers, shelves ---------------------------------------------------------
test("rateToggle: tap sets, same tap clears", function () {
    eq(plain(L.rateToggle("", "LIKE")), { rating: "LIKE", send: "LIKE", toast: "liked" });
    eq(plain(L.rateToggle("LIKE", "LIKE")), { rating: "", send: "INDIFFERENT", toast: "like-removed" });
    eq(plain(L.rateToggle("LIKE", "DISLIKE")), { rating: "DISLIKE", send: "DISLIKE", toast: "disliked" });
    eq(plain(L.rateToggle("DISLIKE", "DISLIKE")), { rating: "", send: "INDIFFERENT", toast: "dislike-removed" });
});
test("searchOutcome: an error wins unless items came back", function () {
    eq(plain(L.searchOutcome({ items: [], error: "boom" })), { error: "boom" });
    eq(plain(L.searchOutcome({ items: [{ a: 1 }], error: "partial" })), { items: [{ a: 1 }] });
    eq(plain(L.searchOutcome({ items: [] })), { items: [] });
    eq(plain(L.searchOutcome(null)), { error: "" });
});
test("sessionFromVerify: inconclusive leaves the state alone", function () {
    eq(L.sessionFromVerify({ checked: false, ok: false }), null);
    eq(L.sessionFromVerify(null), null);
    eq(plain(L.sessionFromVerify({ checked: true, ok: true, present: true, account: "Me" })),
       { loggedIn: true, state: "live", account: "Me" });
    eq(plain(L.sessionFromVerify({ checked: true, ok: false, present: true, account: "Me" })),
       { loggedIn: false, state: "expired", account: "" });
    eq(plain(L.sessionFromVerify({ checked: true, ok: false, present: false })),
       { loggedIn: false, state: "", account: "" });
});
test("importOutcome", function () {
    eq(L.importOutcome({ ok: true, live: false }), "stale");
    eq(L.importOutcome({ ok: true, live: true }), "ok");
    eq(L.importOutcome({ ok: true }), "ok");                  // no live check → trust it
    eq(L.importOutcome({ ok: false }), "failed"); eq(L.importOutcome(null), "failed");
});
test("cachedShelves fills an empty page only", function () {
    eq(plain(L.cachedShelves([], [1])), [1]);
    eq(L.cachedShelves([2], [1]), null);
    eq(L.cachedShelves([], []), null); eq(L.cachedShelves([], undefined), null);
});
test("freshShelves keeps what's showing when the answer is empty", function () {
    eq(plain(L.freshShelves([1], [2])), [2]);
    eq(plain(L.freshShelves([1], [])), [1]);
});

// --- equalizer, SponsorBlock settings ---------------------------------------------------------
test("EQ_PRESETS: Flat first, ten bands each, one per name on the Equalizer page", function () {
    eq(plain(L.EQ_PRESETS[0]), [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
    L.EQ_PRESETS.forEach(function (p) { eq(p.length, 10); });
    var page = fs.readFileSync(path.join(__dirname, "..", "qml", "pages", "EqualizerPage.qml"), "utf8");
    var names = page.match(/presetNames:\s*\[([\s\S]*?)\]/)[1].match(/qsTr\(/g);
    eq(names.length, L.EQ_PRESETS.length);
});
test("presetPlan: a curve turns the equalizer on, Flat doesn't", function () {
    eq(plain(L.presetPlan(1, false)), { bands: [6, 5, 4, 2, 0, 0, 0, 0, 0, 0], enable: true });
    eq(L.presetPlan(1, true).enable, false);
    eq(L.presetPlan(0, false).enable, false);
    eq(L.presetPlan(7, false), null); eq(L.presetPlan(-1, false), null);
    var b = L.presetPlan(2, false).bands; b[0] = 99;
    eq(L.EQ_PRESETS[2][0], 0);                                   // a copy, not the table
});
test("sbActionIndex / sbIndexAction round trip", function () {
    ["off", "skip", "manual"].forEach(function (a) { eq(L.sbIndexAction(L.sbActionIndex(a)), a); });
    eq(L.sbActionIndex(undefined), 0); eq(L.sbIndexAction(5), "off");
});

// --- the ES5 guard itself ---------------------------------------------------------------------
test("es5 guard catches ES6 and ignores comments and strings", function () {
    eq(es5Problems("var f = (a) => a"), ["arrow function"]);
    eq(es5Problems("// let x = 1\nvar s = 'const x'"), []);
});

tests.forEach(function (t) {
    try { t[1](); }
    catch (e) { failed++; console.log("FAIL: " + t[0] + "\n  " + String(e.message).split("\n").join("\n  ")); }
});
console.log((tests.length - failed) + "/" + tests.length + " QML logic tests passed");
process.exit(failed ? 1 : 0);
