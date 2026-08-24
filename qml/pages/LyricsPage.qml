import QtQuick 2.0
import Sailfish.Silica 1.0

// Lyrics for the current track (LRCLIB). Synced lyrics scroll and highlight in time with playback
// (tap a line to seek there); plain lyrics show as scrollable text. Re-fetches when the track
// changes so it keeps up with the queue.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var    synced: []        // [{t, line}]
    property string plain: ""
    property bool   loading: true
    property bool   instrumental: false
    property string errorText: ""
    property int    currentIndex: -1
    property string loadedFor: ""     // videoId the current lyrics belong to

    function load() {
        if (!app.npId) { page.loading = false; page.errorText = "Nothing playing."; return }
        page.loading = true
        page.errorText = ""
        page.synced = []
        page.plain = ""
        page.instrumental = false
        page.currentIndex = -1
        page.loadedFor = app.npId
        var want = app.npId
        app.backend.musicLyrics(app.npId, app.npTitle, app.npArtist,
                                Math.floor((app.player.duration || 0) / 1000),
                                function(res) {
            if (want !== app.npId)         // track changed mid-fetch → this result is stale
                return
            page.loading = false
            if (res && res.ok) {
                page.synced = res.synced || []
                page.plain = res.plain || ""
                page.instrumental = !!res.instrumental
                page.updateCurrent()
            } else {
                page.errorText = (res && res.error) ? res.error : "No lyrics found."
            }
        })
    }

    // Track the playing line for synced lyrics; auto-scroll to keep it centred.
    function updateCurrent() {
        if (page.synced.length === 0)
            return
        var pos = app.player.position
        var idx = -1
        for (var i = 0; i < page.synced.length; i++) {
            if (page.synced[i].t <= pos) idx = i
            else break
        }
        if (idx !== page.currentIndex) {
            page.currentIndex = idx
            if (idx >= 0)
                lyricsList.positionViewAtIndex(idx, ListView.Center)
        }
    }

    Component.onCompleted: page.load()
    Connections {
        target: app
        onNpIdChanged: if (app.npId !== page.loadedFor) page.load()
    }
    Connections {
        target: app.player
        onPositionChanged: page.updateCurrent()
    }

    // --- Synced view ---
    SilicaListView {
        id: lyricsList
        anchors.fill: parent
        visible: page.synced.length > 0
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0
        header: PageHeader { title: "Lyrics" }
        model: page.synced

        delegate: BackgroundItem {
            width: lyricsList.width
            height: lyricLabel.paintedHeight + Theme.paddingMedium
            onClicked: app.player.seek(modelData.t)     // jump to this line
            Label {
                id: lyricLabel
                anchors {
                    left: parent.left; right: parent.right
                    leftMargin: Theme.horizontalPageMargin
                    rightMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                text: modelData.line.length > 0 ? modelData.line : "♪"
                wrapMode: Text.Wrap
                horizontalAlignment: Text.AlignHCenter
                font.pixelSize: index === page.currentIndex ? Theme.fontSizeLarge
                                                            : Theme.fontSizeMedium
                color: index === page.currentIndex ? Theme.highlightColor
                       : (index < page.currentIndex ? Theme.secondaryColor
                                                    : Theme.primaryColor)
                opacity: index === page.currentIndex ? 1.0 : 0.7
                Behavior on font.pixelSize { NumberAnimation { duration: 150 } }
            }
        }
        VerticalScrollDecorator { }
    }

    // --- Plain / status view ---
    SilicaFlickable {
        id: plainFlick
        anchors.fill: parent
        visible: page.synced.length === 0
        contentHeight: plainCol.height + Theme.paddingLarge

        Column {
            id: plainCol
            width: parent.width
            spacing: Theme.paddingLarge

            PageHeader { title: "Lyrics" }

            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                size: BusyIndicatorSize.Large
                running: page.loading
                visible: page.loading
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: !page.loading && page.plain.length > 0
                text: page.plain
                wrapMode: Text.Wrap
                color: Theme.primaryColor
                font.pixelSize: Theme.fontSizeMedium
            }
        }

        ViewPlaceholder {
            enabled: !page.loading && page.plain.length === 0
            text: page.instrumental ? "Instrumental"
                  : (page.errorText.length > 0 ? page.errorText : "No lyrics")
            hintText: page.instrumental ? "" : "Lyrics come from LRCLIB — not every track has them."
        }

        VerticalScrollDecorator { }
    }
}
