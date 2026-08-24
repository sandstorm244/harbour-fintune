import QtQuick 2.0
import Sailfish.Silica 1.0

// Up next — the current play queue. The playing track is highlighted; tap any track to jump to
// it. Long-press for Like / Dislike / Remove.
Page {
    id: page
    allowedOrientations: Orientation.All

    SilicaListView {
        id: list
        anchors.fill: parent
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0

        header: PageHeader { title: "Up next" }
        model: app.playQueue

        delegate: ListItem {
            id: row
            contentHeight: Theme.itemSizeLarge
            highlighted: down || (index === app.playQueueIndex)
            onClicked: app.playQueueJump(index)

            menu: ContextMenu {
                MenuItem { text: "Add to playlist"; onClicked: app.addToPlaylist(modelData) }
                MenuItem {
                    text: app.isDownloaded(modelData.videoId) ? "Remove download" : "Download"
                    onClicked: app.isDownloaded(modelData.videoId)
                               ? app.backend.deleteDownload(modelData.videoId, "audio")
                               : app.downloadTrack(modelData)
                }
                MenuItem {
                    text: "Like"
                    onClicked: app.backend.musicRate(modelData.videoId, "LIKE")
                }
                MenuItem {
                    text: "Dislike"
                    onClicked: app.backend.musicRate(modelData.videoId, "DISLIKE")
                }
                MenuItem {
                    text: "Remove from queue"
                    visible: index !== app.playQueueIndex
                    onClicked: app.queueRemove(index)
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
            // "Now playing" marker on the current track.
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                x: Theme.horizontalPageMargin - Theme.paddingSmall
                width: Math.round(3 * Theme.pixelRatio)
                height: art.height
                radius: width / 2
                color: Theme.highlightColor
                visible: index === app.playQueueIndex
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
                    color: (index === app.playQueueIndex || row.highlighted)
                           ? Theme.highlightColor : Theme.primaryColor
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
            enabled: app.playQueue.length === 0
            text: "Nothing queued"
        }

        VerticalScrollDecorator { }
    }
}
