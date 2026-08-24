import QtQuick 2.0
import Sailfish.Silica 1.0

// Offline downloads — tracks saved for local, network-free playback. Tap to play; long-press to
// remove. In-progress downloads show at the top with a live percentage.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var items: []          // completed audio downloads
    property var activeList: []     // in-flight downloads [{id,title,pct}]

    function refresh() {
        var out = []
        var dl = app.backend.downloads
        for (var i = 0; i < dl.length; i++)
            if (dl[i].kind === "audio")
                out.push(dl[i])
        page.items = out
    }
    function refreshActive() {
        var out = []
        for (var k in app.dlActive)
            out.push({ id: k, title: app.dlActive[k].title, pct: app.dlActive[k].pct })
        page.activeList = out
    }

    Component.onCompleted: { app.backend.loadDownloads(); refresh(); refreshActive() }
    Connections {
        target: app.backend
        onDownloadsChanged: page.refresh()
    }
    Connections {
        target: app
        onDlActiveChanged: page.refreshActive()
    }

    SilicaListView {
        id: list
        anchors.fill: parent
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0

        header: Column {
            width: list.width

            PageHeader { title: "Downloads" }

            // In-progress downloads.
            Repeater {
                model: page.activeList
                delegate: ListItem {
                    contentHeight: Theme.itemSizeMedium
                    enabled: false
                    Column {
                        anchors {
                            left: parent.left; right: parent.right
                            leftMargin: Theme.horizontalPageMargin
                            rightMargin: Theme.horizontalPageMargin
                            verticalCenter: parent.verticalCenter
                        }
                        spacing: Theme.paddingSmall
                        Label {
                            width: parent.width
                            text: modelData.title
                            truncationMode: TruncationMode.Fade
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.highlightColor
                        }
                        ProgressBar {
                            width: parent.width
                            minimumValue: 0; maximumValue: 100
                            value: modelData.pct
                            valueText: Math.round(modelData.pct) + "%"
                        }
                    }
                }
            }
        }

        model: page.items

        delegate: ListItem {
            id: row
            contentHeight: Theme.itemSizeLarge
            onClicked: app.playLocalDownload(modelData)

            menu: ContextMenu {
                MenuItem {
                    text: "Remove download"
                    onClicked: app.backend.deleteDownload(modelData.id, "audio")
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
                    color: (app.npId === modelData.id)
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
            enabled: page.items.length === 0 && page.activeList.length === 0
            text: "No downloads"
            hintText: "Long-press a song and choose Download to save it for offline play."
        }

        VerticalScrollDecorator { }
    }
}
