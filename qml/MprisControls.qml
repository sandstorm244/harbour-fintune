import QtQuick 2.0
import Amber.Mpris 1.0

// MPRIS media control — lockscreen widget + media keys. Loaded via a Loader so that if the
// org.nemomobile.mpris plugin isn't installed, this file simply fails to load rather than
// breaking the app. `np` is the app's nowPlaying object, injected by the Loader.
MprisPlayer {
    id: mpris
    property var np: null
    property string title: ""
    property string artist: ""
    property string artUrl: ""
    property int duration: 0

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

    onPlayPauseRequested: if (np) np.toggleRequested()
    onPlayRequested: if (np && !np.playing) np.toggleRequested()
    onPauseRequested: if (np && np.playing) np.toggleRequested()
    onStopRequested: if (np && np.playing) np.toggleRequested()
    onNextRequested: if (np) np.nextRequested()
    onPreviousRequested: if (np) np.prevRequested()

    //MetaData changes
    onTitleChanged: mpris.metaData.title = mpris.title
    onArtistChanged: mpris.metaData.contributingArtist = mpris.artist
    onArtUrlChanged: mpris.metaData.artUrl = mpris.artUrl
    onDurationChanged: mpris.metaData.duration = mpris.duration

    //
    onPositionRequested: mpris.position = np.position
}
