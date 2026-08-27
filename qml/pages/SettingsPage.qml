import QtQuick 2.0
import Sailfish.Silica 1.0

// App settings — account, appearance and playback. Third-party tool management (yt-dlp, ffmpeg,
// PO-token provider) lives on its own Providers page, reached from Home → More → Providers.
Page {
    id: page
    allowedOrientations: Orientation.All

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: "Settings" }

            // Account first: signing in unlocks the personalized home + library.
            SectionHeader { text: "Account" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.ytmLoggedIn
                      ? "Signed in — your personalized home, playlists and liked songs are available."
                      : "Browsing and playback work signed out. For your recommendations and "
                        + "playlists: sign in to music.youtube.com in the Sailfish browser, then "
                        + "tap Import from browser below. (Google blocks sign-in inside apps, so we "
                        + "read the session from your real browser — nothing to copy.)"
                color: app.backend.ytmLoggedIn ? Theme.secondaryHighlightColor : Theme.secondaryColor
                font.pixelSize: Theme.fontSizeSmall
            }

            Label {
                visible: app.backend.ytmLoginMsg.length > 0
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.ytmLoginMsg
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.backend.ytmLoggedIn ? "Re-import from browser" : "Import from browser"
                onClicked: app.backend.ytmImportBrowserLogin()
            }
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.backend.ytmLoggedIn
                text: "Sign out"
                onClicked: app.backend.ytmLogout()
            }

            SectionHeader { text: "Appearance" }

            TextSwitch {
                text: "Now-playing backdrop"
                description: "Show a soft, blurred wash of the current track's cover art behind "
                             + "the home screen."
                automaticCheck: false
                checked: app.backend.homeBackdrop
                onClicked: app.backend.setHomeBackdrop(!app.backend.homeBackdrop)
            }

            SectionHeader { text: "Playback" }

            ValueButton {
                label: "Equalizer"
                value: app.backend.eqEnabled ? "On" : "Off"
                onClicked: pageStack.push(Qt.resolvedUrl("EqualizerPage.qml"))
            }

            Slider {
                id: boostSlider
                width: parent.width
                minimumValue: 100
                maximumValue: 500
                stepSize: 10
                value: Math.round(app.backend.boostGain * 100)
                label: "Volume boost"
                valueText: value + "%"
                // Live preview while dragging; persist + re-bind on release.
                onValueChanged: if (app.player) app.player.setBoost(value / 100)
                onReleased: {
                    app.backend.setBoostGain(value / 100)
                    value = Qt.binding(function() { return Math.round(app.backend.boostGain * 100) })
                }
            }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "Pushes playback above the system maximum for quiet outputs (e.g. Bluetooth). "
                      + "A limiter tames the peaks so the extra loudness doesn't distort the way "
                      + "raising the system volume past 100% does. 100% = off."
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            TextSwitch {
                text: "Autoplay"
                description: "When the queue ends, keep playing related songs (radio). "
                             + "Turn off to stop at the end of the queue."
                automaticCheck: false
                checked: app.backend.autoplay
                onClicked: app.backend.setAutoplay(!app.backend.autoplay)
            }

            TextSwitch {
                text: "Skip disliked songs"
                description: "Automatically skip songs you've disliked when autoplay picks the "
                             + "next track."
                automaticCheck: false
                checked: app.backend.skipDisliked
                onClicked: app.backend.setSkipDisliked(!app.backend.skipDisliked)
            }
        }
    }
}
