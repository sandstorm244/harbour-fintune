import QtQuick 2.0
import org.nemomobile.mpris 1.0

// MPRIS media control — lockscreen widget + media keys. Loaded via a Loader so that if the
// org.nemomobile.mpris plugin isn't installed, this file simply fails to load rather than
// breaking the app. `np` is the app's nowPlaying object, injected by the Loader.
MprisPlayer {
    id: mpris
    property var np: null

    serviceName: "fintune"
    identity: "FinTune"

    canControl: true
    canPlay: true
    canPause: true
    canGoNext: !!(np && np.hasNext)
    canGoPrevious: !!(np && np.hasPrev)
    canSeek: false
    canQuit: false
    canRaise: false

    playbackStatus: (np && np.active)
        ? (np.playing ? Mpris.Playing : Mpris.Paused)
        : Mpris.Stopped

    metadata: {
        var m = {}
        if (np && np.title && np.title.length > 0)
            m[Mpris.metadataToString(Mpris.Title)] = np.title
        if (np && np.channel && np.channel.length > 0)
            m[Mpris.metadataToString(Mpris.Artist)] = [np.channel]
        if (np && np.thumb && np.thumb.length > 0)
            m[Mpris.metadataToString(Mpris.ArtUrl)] = np.thumb
        return m
    }

    onPlayPauseRequested: if (np) np.toggleRequested()
    onPlayRequested: if (np && !np.playing) np.toggleRequested()
    onPauseRequested: if (np && np.playing) np.toggleRequested()
    onStopRequested: if (np && np.playing) np.toggleRequested()
    onNextRequested: if (np) np.nextRequested()
    onPreviousRequested: if (np) np.prevRequested()
}
