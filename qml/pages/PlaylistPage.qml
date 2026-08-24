import QtQuick 2.0
import Sailfish.Silica 1.0

// Tracklist for a playlist / album / mix / artist page. Tap a track to play from there
// (the rest stay queued so it auto-advances); "Play all" queues from the top.
Page {
    id: page
    allowedOrientations: Orientation.All

    property string browseId: ""
    property string browseParams: ""     // optional endpoint params (artist "Show all songs")
    property string playlistTitle: ""    // provisional (from the card); replaced once loaded
    property string thumb: ""            // cover art from the card (optional)
    property var    tracks: []
    property bool   loading: true
    property string errorText: ""

    Component.onCompleted: {
        app.backend.musicPlaylist(page.browseId, page.browseParams, function(res) {
            page.loading = false
            if (res && res.ok) {
                var tt = res.tracks || []
                // Album track rows carry no per-track cover (it's implied by the album header),
                // so fall back to this page's cover (the card art we were opened with, which is
                // known to render). Fills the tracklist rows AND the queue we hand to the player.
                if (page.thumb && page.thumb.length > 0)
                    for (var i = 0; i < tt.length; i++)
                        if (!tt[i].thumb || tt[i].thumb.length === 0)
                            tt[i].thumb = page.thumb
                page.tracks = tt
                if (res.title && res.title.length > 0)
                    page.playlistTitle = res.title
                if (page.tracks.length === 0)
                    page.errorText = "No tracks here."
            } else {
                page.errorText = (res && res.error) ? res.error : "Couldn't load this."
            }
        })
    }

    SilicaListView {
        id: list
        anchors.fill: parent
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0

        PullDownMenu {
            MenuItem {
                text: "Play all"
                enabled: page.tracks.length > 0
                onClicked: app.playQueueList(page.tracks, 0)
            }
        }

        header: Column {
            width: list.width
            PageHeader { title: page.playlistTitle || "Playlist" }
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
        }

        model: page.tracks

        delegate: ListItem {
            id: row
            contentHeight: Theme.itemSizeLarge
            enabled: !!(modelData.videoId && modelData.videoId.length)
            onClicked: app.playQueueList(page.tracks, index)

            menu: ContextMenu {
                MenuItem { text: "Play next"; onClicked: app.queueInsertNext(modelData) }
                MenuItem { text: "Add to queue"; onClicked: app.queueAppend(modelData) }
                MenuItem { text: "Add to playlist"; onClicked: app.addToPlaylist(modelData) }
                MenuItem {
                    text: app.isDownloaded(modelData.videoId) ? "Remove download" : "Download"
                    onClicked: app.isDownloaded(modelData.videoId)
                               ? app.backend.deleteDownload(modelData.videoId, "audio")
                               : app.downloadTrack(modelData)
                }
                MenuItem { text: "Like"
                    onClicked: app.backend.musicRate(modelData.videoId, "LIKE") }
                MenuItem { text: "Dislike"
                    onClicked: app.backend.musicRate(modelData.videoId, "DISLIKE") }
            }

            Image {
                id: art
                anchors {
                    left: parent.left; leftMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                width: Theme.itemSizeMedium; height: width
                fillMode: Image.PreserveAspectCrop
                clip: true
                asynchronous: true
                source: modelData.thumb || ""
                Rectangle {
                    anchors.fill: parent
                    visible: art.status !== Image.Ready
                    color: Theme.rgba(Theme.highlightBackgroundColor, 0.25)
                }
            }
            Column {
                anchors {
                    left: art.right; leftMargin: Theme.paddingMedium
                    right: parent.right; rightMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                Label {
                    width: parent.width
                    text: modelData.title || ""
                    truncationMode: TruncationMode.Fade
                    color: (app.npId === modelData.videoId)
                           ? Theme.highlightColor
                           : (row.highlighted ? Theme.highlightColor : Theme.primaryColor)
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

        ViewPlaceholder {
            enabled: !page.loading && page.tracks.length === 0 && page.errorText.length === 0
            text: "Empty"
        }

        VerticalScrollDecorator { }
    }
}
