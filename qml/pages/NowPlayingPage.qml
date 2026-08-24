import QtQuick 2.0
import Sailfish.Silica 1.0
import QtGraphicalEffects 1.0

// Now Playing — a view of the app's global audio engine (app.player). Navigating away
// doesn't stop playback; this page just reflects and controls it.
//
// Layout: header pinned to the top, transport pinned to the bottom, and the artwork + title
// centred in the space between — so the controls sit low (thumb-reachable) and there's no
// dead band under them.
Page {
    id: page
    objectName: "nowPlaying"          // lets app.openNowPlaying() avoid stacking duplicates
    allowedOrientations: Orientation.All

    function fmtTime(ms) {
        if (!ms || ms < 0) ms = 0
        var s = Math.floor(ms / 1000)
        var m = Math.floor(s / 60)
        var r = s % 60
        return m + ":" + (r < 10 ? "0" + r : r)
    }

    // --- Blurred album-art backdrop ---
    // The cover art, blown up and blurred, fills the page behind everything; a scrim over it
    // keeps the title / times / controls readable on any artwork. The crisp cover in the middle
    // then reads as sitting "in front of" its own colour wash. Falls back to the plain page
    // background when there's no art yet.
    Image {
        id: bgArt
        anchors.fill: parent
        fillMode: Image.PreserveAspectCrop
        clip: true
        asynchronous: true
        visible: false                       // source for the blur only
        source: app.artUrl(app.npThumb, 600)
    }
    FastBlur {
        anchors.fill: parent
        source: bgArt
        radius: 72
        cached: true                         // static per track — render once
        visible: bgArt.status === Image.Ready
    }
    Rectangle {
        anchors.fill: parent
        visible: bgArt.status === Image.Ready
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.rgba(Theme.overlayBackgroundColor, 0.5) }
            GradientStop { position: 1.0; color: Theme.rgba(Theme.overlayBackgroundColor, 0.8) }
        }
    }

    SilicaFlickable {
        id: flick
        anchors.fill: parent
        contentHeight: height          // no scrolling — everything is laid out to fit

        PullDownMenu {
            MenuItem {
                text: app.isDownloaded(app.npId) ? "Remove download" : "Download"
                enabled: app.npId.length > 0
                onClicked: app.isDownloaded(app.npId)
                           ? app.backend.deleteDownload(app.npId, "audio")
                           : app.downloadTrack({ videoId: app.npId, title: app.npTitle,
                                 subtitle: app.npArtist, thumb: app.npThumb,
                                 artistId: app.npArtistId })
            }
            MenuItem {
                text: "Add to playlist"
                enabled: app.npId.length > 0
                onClicked: app.addToPlaylist({ videoId: app.npId, title: app.npTitle,
                    subtitle: app.npArtist, thumb: app.npThumb, artistId: app.npArtistId })
            }
            MenuItem {
                text: "Lyrics"
                enabled: app.npId.length > 0
                onClicked: pageStack.push(Qt.resolvedUrl("LyricsPage.qml"))
            }
            MenuItem {
                text: "Up next"
                onClicked: pageStack.push(Qt.resolvedUrl("QueuePage.qml"))
            }
        }

        PageHeader { id: header; title: "Now Playing" }

        // --- Bottom cluster: error, seek bar, times, transport (pinned to the bottom) ---
        Column {
            id: controls
            anchors {
                left: parent.left; right: parent.right
                bottom: parent.bottom
                bottomMargin: Theme.paddingLarge
            }
            spacing: Theme.paddingMedium

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: app.npError.length > 0
                text: app.npError
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.errorColor
            }

            // Like / dislike — one on each side, above the progress bar. Accent when active, dim
            // otherwise; disabled signed out (rating needs your account); tapping the active one
            // clears it. Dislike reuses the like glyph rotated 180° (a rotated thumbs-up reads as
            // thumbs-down) since this theme ships icon-m-like but not icon-m-dislike.
            Item {
                width: parent.width
                height: Theme.itemSizeSmall

                IconButton {
                    anchors {
                        left: parent.left; leftMargin: Theme.horizontalPageMargin
                        verticalCenter: parent.verticalCenter
                    }
                    icon.source: "image://theme/icon-m-like?"
                                 + (app.npRating === "LIKE" ? Theme.highlightColor
                                                            : Theme.secondaryColor)
                    enabled: app.npId.length > 0 && app.backend.ytmLoggedIn
                    onClicked: app.rateCurrent("LIKE")
                }
                IconButton {
                    anchors {
                        right: parent.right; rightMargin: Theme.horizontalPageMargin
                        verticalCenter: parent.verticalCenter
                    }
                    rotation: 180                 // flip the thumbs-up into a thumbs-down
                    icon.source: "image://theme/icon-m-like?"
                                 + (app.npRating === "DISLIKE" ? Theme.highlightColor
                                                              : Theme.secondaryColor)
                    enabled: app.npId.length > 0 && app.backend.ytmLoggedIn
                    onClicked: app.rateCurrent("DISLIKE")
                }
            }

            Slider {
                id: seekSlider
                width: parent.width
                minimumValue: 0
                maximumValue: app.player.duration > 0 ? app.player.duration : 1
                enabled: app.player.duration > 0
                value: app.player.position
                // Silica sets `value` imperatively while dragging (breaking the binding); seek
                // on release and re-establish the binding so the handle tracks playback again.
                onReleased: {
                    app.player.seek(value)
                    value = Qt.binding(function() { return app.player.position })
                }
                valueText: page.fmtTime(value)
            }

            Item {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                height: posLabel.height
                Label {
                    id: posLabel
                    anchors.left: parent.left
                    text: page.fmtTime(app.player.position)
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                }
                Label {
                    anchors.right: parent.right
                    text: app.player.duration > 0 ? page.fmtTime(app.player.duration) : "–:--"
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                }
            }

            // --- Transport: repeat · prev · play/pause · next ---
            // The play/pause button must sit dead-centre, so the repeat toggle on the left is
            // balanced by an equal-width spacer on the right (5 symmetric slots → play is middle).
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.paddingLarge

                // Repeat toggle: off → repeat-all → repeat-one. Dim when off, accent when on;
                // a "1" badge marks repeat-one (single icon, so no dependency on a themed
                // repeat-one glyph that may not exist).
                IconButton {
                    id: repeatBtn
                    anchors.verticalCenter: parent.verticalCenter
                    icon.source: "image://theme/icon-m-repeat?"
                                 + (app.repeatMode > 0 ? Theme.highlightColor
                                                       : Theme.secondaryColor)
                    onClicked: app.repeatMode = (app.repeatMode + 1) % 3
                    Label {
                        anchors {
                            right: parent.right; rightMargin: Theme.paddingSmall
                            bottom: parent.bottom; bottomMargin: Theme.paddingSmall
                        }
                        visible: app.repeatMode === 2
                        text: "1"
                        font.pixelSize: Theme.fontSizeTiny
                        font.bold: true
                        color: Theme.highlightColor
                    }
                }

                IconButton {
                    anchors.verticalCenter: parent.verticalCenter
                    icon.source: "image://theme/icon-m-previous"
                    enabled: app.hasPrev
                    onClicked: app.playPrev()
                }
                IconButton {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Theme.iconSizeLarge; height: width
                    icon.source: app.player.playing ? "image://theme/icon-l-pause"
                                                     : "image://theme/icon-l-play"
                    enabled: !app.npResolving
                    onClicked: app.togglePlay()
                }
                IconButton {
                    anchors.verticalCenter: parent.verticalCenter
                    icon.source: "image://theme/icon-m-next"
                    // Always available while something's playing — at the queue's end it starts
                    // the song radio (autoplay continuation).
                    enabled: app.npActive
                    onClicked: app.playNext()
                }
                // Invisible counterweight to the repeat button, keeping play/pause centred.
                Item {
                    anchors.verticalCenter: parent.verticalCenter
                    width: repeatBtn.width; height: repeatBtn.height
                }
            }
        }

        // --- Middle: album art + title/artist, centred between header and controls ---
        Item {
            id: middle
            anchors {
                top: header.bottom
                bottom: controls.top
                left: parent.left; right: parent.right
            }

            Column {
                anchors.centerIn: parent
                width: parent.width
                spacing: Theme.paddingLarge

                // Square cover, sized to the available middle band (fills more of the screen
                // than a fixed cap did) but never wider than the page.
                property real artSize: Math.max(Theme.itemSizeExtraLarge,
                    Math.min(page.width - 2 * Theme.horizontalPageMargin, middle.height * 0.66))

                Item {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: parent.artSize; height: parent.artSize

                    Rectangle {
                        anchors.fill: parent
                        color: Theme.rgba(Theme.highlightBackgroundColor, 0.25)
                        radius: Theme.paddingSmall
                    }
                    Image {
                        id: art
                        anchors.fill: parent
                        fillMode: Image.PreserveAspectCrop
                        clip: true
                        asynchronous: true
                        // Big, crisp cover for the player (list thumbs are tiny); falls back to
                        // the small one while the large version loads.
                        source: app.artUrl(app.npThumb, 600)
                        Image {
                            anchors.fill: parent
                            z: -1
                            fillMode: Image.PreserveAspectCrop
                            clip: true
                            asynchronous: true
                            source: app.npThumb        // instant low-res placeholder underneath
                            visible: art.status !== Image.Ready
                        }
                    }
                    BusyIndicator {
                        anchors.centerIn: parent
                        size: BusyIndicatorSize.Large
                        running: app.npResolving
                    }
                }

                Column {
                    width: parent.width
                    spacing: Theme.paddingSmall
                    Label {
                        x: Theme.horizontalPageMargin
                        width: parent.width - 2 * Theme.horizontalPageMargin
                        text: app.npTitle
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        truncationMode: TruncationMode.Fade
                        font.pixelSize: Theme.fontSizeLarge
                        color: Theme.highlightColor
                    }
                    // Artist — tap to open their channel (when we have its browseId).
                    Item {
                        x: Theme.horizontalPageMargin
                        width: parent.width - 2 * Theme.horizontalPageMargin
                        height: artistLabel.height
                        Label {
                            id: artistLabel
                            width: parent.width
                            text: app.npArtist
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.Wrap
                            maximumLineCount: 1
                            truncationMode: TruncationMode.Fade
                            font.pixelSize: Theme.fontSizeSmall
                            color: app.npArtistId.length === 0 ? Theme.secondaryColor
                                   : (artistArea.pressed ? Theme.highlightColor
                                                         : Theme.primaryColor)
                        }
                        MouseArea {
                            id: artistArea
                            anchors.fill: parent
                            enabled: app.npArtistId.length > 0
                            onClicked: app.openArtist(app.npArtistId, app.npArtist)
                        }
                    }
                }
            }
        }
    }
}
