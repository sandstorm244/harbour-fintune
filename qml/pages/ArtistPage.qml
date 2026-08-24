import QtQuick 2.0
import Sailfish.Silica 1.0

// A YouTube Music artist page: top songs (tap to play the set) with a "Show all", then the
// Albums / Singles & EPs / Videos carousels. Cards route through app.openBrowse (song plays,
// album/playlist opens its tracklist, a nested artist opens its own page).
Page {
    id: page
    allowedOrientations: Orientation.All

    property string browseId: ""
    property string artistName: ""
    property var    songs: []
    property var    shelves: []
    property var    songsMore: ({ browseId: "", params: "" })
    property bool   loading: true
    property string errorText: ""

    property real cardW: Math.round(page.width * 0.38)

    Component.onCompleted: {
        app.backend.musicArtist(page.browseId, function(res) {
            page.loading = false
            if (res && res.ok) {
                page.songs = res.songs || []
                page.shelves = res.shelves || []
                page.songsMore = res.songs_more || { browseId: "", params: "" }
                if (res.name && res.name.length > 0)
                    page.artistName = res.name
                if (page.songs.length === 0 && page.shelves.length === 0)
                    page.errorText = "Nothing to show for this artist."
            } else {
                page.errorText = (res && res.error) ? res.error : "Couldn't load this artist."
            }
        })
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + (app.npActive ? Theme.itemSizeMedium : 0) + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: page.artistName || "Artist" }

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
                color: Theme.secondaryColor
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
            }

            // --- Top songs ---
            SectionHeader { text: "Songs"; visible: page.songs.length > 0 }

            Repeater {
                model: page.songs
                delegate: ListItem {
                    id: srow
                    width: col.width
                    contentHeight: Theme.itemSizeLarge
                    onClicked: app.playQueueList(page.songs, index)

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
                        id: sart
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
                            visible: sart.status !== Image.Ready
                            color: Theme.rgba(Theme.highlightBackgroundColor, 0.25)
                        }
                    }
                    Column {
                        anchors {
                            left: sart.right; leftMargin: Theme.paddingMedium
                            right: parent.right; rightMargin: Theme.horizontalPageMargin
                            verticalCenter: parent.verticalCenter
                        }
                        Label {
                            width: parent.width
                            text: modelData.title || ""
                            truncationMode: TruncationMode.Fade
                            color: (app.npId === modelData.videoId)
                                   ? Theme.highlightColor
                                   : (srow.highlighted ? Theme.highlightColor : Theme.primaryColor)
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

            BackgroundItem {
                width: col.width
                height: Theme.itemSizeSmall
                visible: page.songsMore.browseId.length > 0
                onClicked: pageStack.push(Qt.resolvedUrl("PlaylistPage.qml"),
                    { browseId: page.songsMore.browseId, browseParams: page.songsMore.params,
                      playlistTitle: (page.artistName || "Artist") + " — Songs" })
                Label {
                    x: Theme.horizontalPageMargin
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Show all songs"
                    color: parent.highlighted ? Theme.highlightColor : Theme.highlightColor
                    font.pixelSize: Theme.fontSizeSmall
                }
            }

            // --- Carousels: Albums, Singles & EPs, Videos, … ---
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
