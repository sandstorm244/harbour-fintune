import QtQuick 2.0
import Sailfish.Silica 1.0

// Your library: saved playlists + Liked Music (requires sign-in). Tap one to open its
// tracklist. Signed out, it points you at Settings to import your login.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var    playlists: []
    property bool   loading: false
    property string errorText: ""

    function load() {
        if (!app.backend.ytmLoggedIn)
            return
        page.loading = true
        page.errorText = ""
        app.backend.musicLibrary(function(res) {
            page.loading = false
            if (res && res.logged_in)
                page.playlists = res.playlists || []
            else if (res && res.error)
                page.errorText = res.error
        })
    }

    Component.onCompleted: load()
    Connections {
        target: app.backend
        onYtmLoginFinished: if (ok) page.load()
    }

    SilicaListView {
        id: list
        anchors.fill: parent
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0

        header: Column {
            width: list.width
            PageHeader { title: "Library" }
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

        model: page.playlists

        delegate: ListItem {
            id: row
            contentHeight: Theme.itemSizeLarge
            onClicked: pageStack.push(Qt.resolvedUrl("PlaylistPage.qml"),
                { browseId: modelData.browseId, playlistTitle: modelData.title || "",
                  thumb: modelData.thumb || "" })

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
                    color: row.highlighted ? Theme.highlightColor : Theme.primaryColor
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
            enabled: !app.backend.ytmLoggedIn
            text: "Sign in to see your library"
            hintText: "Settings → Account → Import from browser"
        }

        VerticalScrollDecorator { }
    }
}
