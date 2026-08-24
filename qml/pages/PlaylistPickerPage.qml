import QtQuick 2.0
import Sailfish.Silica 1.0

// Pick one of the user's playlists to add a track to. Opened from a song's "Add to playlist"
// action (context menu or the Now Playing pulldown). Tapping a playlist adds and pops back.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var    track: null       // the song to add: {videoId, title, subtitle, thumb, ...}
    property var    playlists: []
    property bool   loading: true
    property bool   adding: false
    property string errorText: ""

    Component.onCompleted: {
        app.backend.musicLibrary(function(res) {
            page.loading = false
            if (res && res.logged_in === false) {
                page.errorText = "Sign in to use playlists."
                return
            }
            page.playlists = (res && res.playlists) ? res.playlists : []
            if (page.playlists.length === 0)
                page.errorText = "No playlists found. Create one in YouTube Music first."
        })
    }

    function addTo(pl) {
        if (page.adding || !page.track || !page.track.videoId || !pl.browseId)
            return
        page.adding = true
        app.backend.musicAddToPlaylist(pl.browseId, page.track.videoId, function(res) {
            page.adding = false
            if (res && res.ok)
                app.showToast("Added to " + (pl.title || "playlist"))
            else
                app.showToast((res && res.error) ? res.error : "Couldn't add to playlist")
            pageStack.pop()
        })
    }

    SilicaListView {
        id: list
        anchors.fill: parent
        bottomMargin: app.npActive ? Theme.itemSizeMedium : 0

        header: Column {
            width: list.width
            PageHeader {
                title: "Add to playlist"
                description: page.track ? (page.track.title || "") : ""
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
                color: Theme.secondaryColor
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
            }
        }

        model: page.playlists

        delegate: ListItem {
            id: row
            contentHeight: Theme.itemSizeLarge
            enabled: !page.adding && !!modelData.browseId
            onClicked: page.addTo(modelData)

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
            enabled: !page.loading && page.playlists.length === 0
            text: page.errorText.length > 0 ? page.errorText : "No playlists"
        }

        VerticalScrollDecorator { }
    }
}
