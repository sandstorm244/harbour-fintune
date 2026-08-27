import QtQuick 2.0
import Sailfish.Silica 1.0
import QtGraphicalEffects 1.0

// FinTune home: recommendation shelves (personalized once signed in, generic otherwise),
// rendered as horizontal carousels. Tapping a song plays it through the app's audio engine.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var shelves: []
    property bool loading: true
    property string errorText: ""

    // Card metrics — art is square; ~2.5 cards peek across the width.
    property real cardW: Math.round(page.width * 0.38)

    function loadHome() {
        // Instant: render the cached shelves first (if we don't already have any), then refresh
        // from the network in the background. Cold start still shows the big spinner.
        app.backend.musicHomeCached(function(res) {
            if (res && res.shelves && res.shelves.length > 0 && page.shelves.length === 0) {
                page.shelves = res.shelves
                page.loading = false
            }
            if (page.shelves.length === 0)
                page.loading = true
            page.errorText = ""
            app.backend.musicHome()
        })
    }

    Connections {
        target: app.backend
        onMusicHomeLoaded: {
            page.loading = false
            if (shelves.length > 0)
                page.shelves = shelves   // fresh replaces cached; keep cached if fresh is empty
        }
        onMusicError: {
            page.loading = false
            page.errorText = message
        }
        // Refresh once a sign-in completes so personalized shelves replace the generic ones.
        onYtmLoginFinished: if (ok) page.loadHome()
    }

    Component.onCompleted: if (app.backend.ytmReady) page.loadHome()

    // Attach the "More" launcher as a forward (right-to-left swipe) sibling of Home, so the
    // secondary destinations no longer crowd the pull-down. The back swipe returns here.
    property bool _moreAttached: false
    onStatusChanged: {
        if (status === PageStatus.Active && !_moreAttached) {
            _moreAttached = true
            pageStack.pushAttached(Qt.resolvedUrl("MorePage.qml"), {
                heading: "More",
                entries: [
                    { title: "Library", desc: "Playlists + Liked Music", page: "LibraryPage.qml" },
                    { title: "Downloads", desc: "Offline tracks", page: "DownloadsPage.qml" },
                    { title: "History", desc: "Recently played", page: "HistoryPage.qml" },
                    { title: "Settings", desc: "Account, appearance, playback", page: "SettingsPage.qml" },
                    { title: "Providers", desc: "yt-dlp, ffmpeg, PO-token provider", page: "ProvidersPage.qml" }
                ]
            })
        }
    }
    // Cold start: the Python modules may still be importing when the page appears.
    Connections {
        target: app.backend.ytmReady ? null : app.backend
        onYtmReadyChanged: if (app.backend.ytmReady) page.loadHome()
    }

    // --- Subtle now-playing backdrop (experimental) ---
    // A heavily-blurred, faint wash of the current track's art behind the carousels. Kept much
    // fainter than the player's backdrop (this page is a scrolling list of text + cards, which
    // must stay crisp). Only present while something is playing; fixed (doesn't scroll).
    property bool backdropOn: app.npActive && app.backend.homeBackdrop

    Image {
        id: homeBg
        anchors.fill: parent
        fillMode: Image.PreserveAspectCrop
        clip: true
        asynchronous: true
        visible: false                       // source for the blur only
        source: page.backdropOn ? app.artUrl(app.npThumb, 600) : ""
    }
    FastBlur {
        anchors.fill: parent
        source: homeBg
        radius: 96                           // strong blur → abstract, non-distracting
        cached: true
        opacity: 0.35                        // faint
        visible: page.backdropOn && homeBg.status === Image.Ready
    }
    Rectangle {
        anchors.fill: parent
        visible: page.backdropOn && homeBg.status === Image.Ready
        color: Theme.rgba(Theme.overlayBackgroundColor, 0.3)   // keep cards + labels legible
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + (app.npActive ? Theme.itemSizeMedium : 0) + Theme.paddingLarge

        PullDownMenu {
            MenuItem {
                text: "Search"
                onClicked: pageStack.push(Qt.resolvedUrl("SearchPage.qml"))
            }
        }

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingLarge

            PageHeader { title: "FinTune" }

            // Signed-out prompt — recommendations need an account.
            BackgroundItem {
                width: parent.width
                height: signInCol.height + Theme.paddingLarge
                visible: !app.backend.ytmLoggedIn
                onClicked: pageStack.push(Qt.resolvedUrl("SettingsPage.qml"))
                Column {
                    id: signInCol
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    spacing: Theme.paddingSmall
                    Label {
                        width: parent.width
                        text: "Sign in for your recommendations"
                        font.pixelSize: Theme.fontSizeMedium
                        color: Theme.highlightColor
                        wrapMode: Text.Wrap
                    }
                    Label {
                        width: parent.width
                        text: "Your Quick picks, Listen again and Mixed-for-you shelves appear "
                              + "here once you're signed in. Tap to sign in."
                        font.pixelSize: Theme.fontSizeExtraSmall
                        color: Theme.secondaryColor
                        wrapMode: Text.Wrap
                    }
                }
            }

            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                size: BusyIndicatorSize.Large
                running: page.loading
                visible: page.loading
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: page.errorText.length > 0
                text: page.errorText
                color: Theme.errorColor
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: !page.loading && page.shelves.length === 0 && page.errorText.length === 0
                text: "Nothing to show yet. Pull down to Search for a song, or sign in for your "
                      + "personalized home."
                color: Theme.secondaryColor
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
            }

            // --- Shelves ---
            Repeater {
                model: page.shelves
                delegate: Column {
                    width: col.width
                    spacing: Theme.paddingSmall
                    property var shelf: modelData

                    SectionHeader { text: shelf.title || "" }

                    SilicaListView {
                        width: parent.width
                        height: page.cardW + Theme.itemSizeSmall
                        orientation: ListView.Horizontal
                        flickableDirection: Flickable.HorizontalFlick
                        clip: true
                        spacing: Theme.paddingMedium
                        model: shelf.items
                        header: Item { width: Theme.horizontalPageMargin; height: 1 }
                        footer: Item { width: Theme.horizontalPageMargin; height: 1 }

                        delegate: BackgroundItem {
                            width: page.cardW
                            height: page.cardW + Theme.itemSizeSmall
                            onClicked: app.openBrowse(modelData)
                            Column {
                                width: parent.width
                                spacing: Theme.paddingSmall
                                Image {
                                    width: page.cardW; height: page.cardW
                                    fillMode: Image.PreserveAspectCrop
                                    clip: true
                                    asynchronous: true
                                    source: modelData.thumb || ""
                                    Rectangle {
                                        anchors.fill: parent
                                        visible: parent.status !== Image.Ready
                                        color: Theme.rgba(Theme.highlightBackgroundColor, 0.25)
                                    }
                                }
                                Label {
                                    width: parent.width
                                    text: modelData.title || ""
                                    truncationMode: TruncationMode.Fade
                                    font.pixelSize: Theme.fontSizeExtraSmall
                                    color: Theme.primaryColor
                                }
                                Label {
                                    width: parent.width
                                    visible: !!(modelData.subtitle && modelData.subtitle.length)
                                    text: modelData.subtitle || ""
                                    truncationMode: TruncationMode.Fade
                                    font.pixelSize: Theme.fontSizeExtraSmall
                                    color: Theme.secondaryColor
                                }
                            }
                        }
                    }
                }
            }
        }

        VerticalScrollDecorator { }
    }
}
