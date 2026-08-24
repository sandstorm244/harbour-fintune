import QtQuick 2.0
import Sailfish.Silica 1.0

// Music search — works signed out. Tap a result to play it through the audio engine.
// The SearchField is kept OUTSIDE the ListView: as a list header it lost active focus on every
// results update (the view re-lays-out its header), which dismissed the keyboard mid-typing.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var results: []
    property bool searching: false
    property string errorText: ""
    property string query: ""

    Connections {
        target: app.backend
        onMusicResults: {
            page.searching = false
            page.errorText = ""
            page.results = items
        }
        onMusicError: {
            page.searching = false
            page.errorText = message
        }
    }

    // Debounce so we don't fire a request on every keystroke.
    Timer {
        id: debounce
        interval: 450
        onTriggered: {
            var q = page.query.trim()
            if (q.length === 0) { page.results = []; page.searching = false; return }
            page.searching = true
            app.backend.musicSearch(q)
        }
    }

    SearchField {
        id: searchField
        anchors { top: parent.top; left: parent.left; right: parent.right }
        placeholderText: "Search songs, artists, albums"
        inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText
        EnterKey.iconSource: "image://theme/icon-m-enter-close"
        EnterKey.onClicked: focus = false
        onTextChanged: { page.query = text; debounce.restart() }
        Component.onCompleted: forceActiveFocus()
    }

    SilicaListView {
        id: list
        anchors {
            top: searchField.bottom; bottom: parent.bottom
            left: parent.left; right: parent.right
        }
        clip: true
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0

        header: Column {
            width: list.width
            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                size: BusyIndicatorSize.Medium
                running: page.searching
                visible: page.searching
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

        model: page.results

        delegate: ListItem {
            id: item
            contentHeight: Theme.itemSizeLarge
            property bool isSong: !!(modelData.videoId && modelData.videoId.length)
            property bool isBrowse: !!(modelData.browseId && modelData.browseId.length)
            enabled: item.isSong || item.isBrowse
            // A song plays; an artist opens the artist page; an album/playlist opens its tracklist.
            onClicked: app.openBrowse(modelData)

            // Long-press actions — songs only.
            menu: item.isSong ? songMenu : null
            Component {
                id: songMenu
                ContextMenu {
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

            // Type tag on the right for non-song results, so the combined feed stays scannable.
            Label {
                id: kindLbl
                visible: modelData.kind && modelData.kind !== "song"
                anchors {
                    right: parent.right; rightMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                text: modelData.kind === "artist" ? "Artist"
                      : modelData.kind === "album" ? "Album"
                      : modelData.kind === "playlist" ? "Playlist" : ""
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
            }

            Column {
                anchors {
                    left: art.right; leftMargin: Theme.paddingMedium
                    right: kindLbl.visible ? kindLbl.left : parent.right
                    rightMargin: Theme.paddingMedium
                    verticalCenter: parent.verticalCenter
                }
                Label {
                    width: parent.width
                    text: modelData.title || ""
                    truncationMode: TruncationMode.Fade
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
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
            enabled: !page.searching && page.results.length === 0
                     && page.query.trim().length === 0
            text: "Search YouTube Music"
            hintText: "Songs, artists, albums — no sign-in needed to play."
        }

        VerticalScrollDecorator { }
    }
}
