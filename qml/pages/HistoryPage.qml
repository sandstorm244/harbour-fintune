import QtQuick 2.0
import Sailfish.Silica 1.0

// Recently-played tracks, newest first. Tap to play; long-press for the usual actions.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool loading: true

    ListModel { id: histModel }

    function reload() {
        page.loading = true
        app.backend.musicHistory(function(list) {
            histModel.clear()
            for (var i = 0; i < list.length; i++)
                histModel.append(list[i])
            page.loading = false
        })
    }
    Component.onCompleted: reload()

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: histModel
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0

        header: PageHeader { title: "History" }

        PullDownMenu {
            MenuItem {
                text: "Clear history"
                onClicked: Remorse.popupAction(page, "Clearing history", function() {
                    app.backend.musicClearHistory(function() { page.reload() })
                })
            }
        }

        delegate: ListItem {
            id: item
            contentHeight: Theme.itemSizeLarge
            onClicked: app.playSong({ videoId: model.videoId, title: model.title,
                                      subtitle: model.subtitle, thumb: model.thumb,
                                      artistId: model.artistId })

            menu: ContextMenu {
                MenuItem {
                    text: "Play next"
                    onClicked: app.queueInsertNext({ videoId: model.videoId, title: model.title,
                        subtitle: model.subtitle, thumb: model.thumb, artistId: model.artistId })
                }
                MenuItem {
                    text: "Add to queue"
                    onClicked: app.queueAppend({ videoId: model.videoId, title: model.title,
                        subtitle: model.subtitle, thumb: model.thumb, artistId: model.artistId })
                }
                MenuItem {
                    text: "Add to playlist"
                    onClicked: app.addToPlaylist({ videoId: model.videoId, title: model.title,
                        subtitle: model.subtitle, thumb: model.thumb, artistId: model.artistId })
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
                source: model.thumb || ""
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
                    text: model.title || ""
                    truncationMode: TruncationMode.Fade
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                }
                Label {
                    width: parent.width
                    visible: text.length > 0
                    text: model.subtitle || ""
                    truncationMode: TruncationMode.Fade
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                }
            }
        }

        ViewPlaceholder {
            enabled: !page.loading && histModel.count === 0
            text: "No history yet"
            hintText: "Tracks you play show up here."
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading && histModel.count === 0
    }
}
