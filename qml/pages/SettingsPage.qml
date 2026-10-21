import QtQuick 2.0
import Sailfish.Silica 1.0
import Sailfish.Pickers 1.0

// App settings — account, appearance and playback. Third-party tool management (yt-dlp, fast
// resolve, PO-token provider) lives on its own Providers page, reached from Home → More → Providers.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock / resume bar over Settings
    property string dlError: ""   // last download-folder error (e.g. not writable), shown inline

    // Populate the selected-channel label from the persisted choice (the "Channel" ValueButton).
    Component.onCompleted: if (app.backend.ytmLoggedIn) app.backend.ytmSelectedAccount()

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: qsTr("Settings") }

            // Account first: signing in unlocks the personalized home + library.
            SectionHeader { text: qsTr("Account") }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.ytmSessionState === "expired"
                      ? qsTr("Your sign-in expired — Google no longer accepts the saved session. Open "
                           + "music.youtube.com in the Sailfish browser (sign in if needed), then tap "
                           + "Re-import below.")
                      : (app.backend.ytmLoggedIn
                         ? (app.backend.ytmAccount
                            ? qsTr("Signed in as %1 — your personalized home, playlists and liked songs are available.")
                                  .arg(app.backend.ytmAccount)
                            : qsTr("Signed in — your personalized home, playlists and liked songs are available."))
                         : qsTr("Browsing and playback work signed out. For your recommendations and "
                                + "playlists: sign in to music.youtube.com in the Sailfish browser, then "
                                + "tap Import from browser below. (Google blocks sign-in inside apps, so we "
                                + "read the session from your real browser — nothing to copy.)"))
                color: app.backend.ytmSessionState === "expired"
                       ? Theme.errorColor
                       : (app.backend.ytmLoggedIn ? Theme.secondaryHighlightColor : Theme.secondaryColor)
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
                text: (app.backend.ytmLoggedIn || app.backend.ytmSessionState === "expired")
                      ? qsTr("Re-import from browser") : qsTr("Import from browser")
                onClicked: app.backend.ytmImportBrowserLogin()
            }
            // Ask Google directly whether the stored session still works — turns the silent
            // "signed in but only generic recommendations" state into a clear answer.
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.backend.ytmLoggedIn
                text: qsTr("Check sign-in")
                onClicked: {
                    app.backend.ytmLoginMsg = qsTr("Checking with Google…")
                    app.backend.verifySession(function(res) {
                        if (!res || res.checked === false)
                            app.backend.ytmLoginMsg = qsTr("Couldn't reach Google to check — try again.")
                        else if (res.ok)
                            app.backend.ytmLoginMsg = res.account ? qsTr("Signed in as %1.").arg(res.account) : qsTr("Signed in.")
                        else
                            app.backend.ytmLoginMsg = qsTr("Session expired — Google no longer accepts it. "
                                + "Open music.youtube.com in the Sailfish browser (sign in if needed), "
                                + "then Re-import.")
                    })
                }
            }
            // Pick which login / channel (incl. brand accounts) to use — only meaningful once signed
            // in. Defaults to login 0's primary channel; the picker is where multi-account users fix
            // "it uses the wrong channel / my playlists don't show".
            ValueButton {
                visible: app.backend.ytmLoggedIn
                //: YouTube channel / account identity
                label: qsTr("Channel")
                value: app.backend.ytmSelectedName || app.backend.ytmAccount || qsTr("Default")
                description: qsTr("Choose which account or channel (incl. brand accounts) to use.")
                onClicked: pageStack.push(Qt.resolvedUrl("AccountsPage.qml"))
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.backend.ytmLoggedIn
                text: qsTr("Sign out")
                onClicked: app.backend.ytmLogout()
            }

            SectionHeader { text: qsTr("Appearance") }

            TextSwitch {
                text: qsTr("Now-playing backdrop")
                description: qsTr("Show a soft, blurred wash of the current track's cover art behind "
                                  + "the home screen.")
                automaticCheck: false
                checked: app.backend.homeBackdrop
                onClicked: app.backend.setHomeBackdrop(!app.backend.homeBackdrop)
            }

            SectionHeader { text: qsTr("Playback") }

            ValueButton {
                label: qsTr("Equalizer")
                value: app.backend.eqEnabled ? qsTr("On") : qsTr("Off")
                onClicked: pageStack.push(Qt.resolvedUrl("EqualizerPage.qml"))
            }

            Slider {
                id: boostSlider
                width: parent.width
                minimumValue: 100
                maximumValue: 500
                stepSize: 10
                value: Math.round(app.backend.boostGain * 100)
                label: qsTr("Volume boost")
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
                text: qsTr("Pushes playback above the system maximum for quiet outputs (e.g. Bluetooth). "
                         + "A limiter tames the peaks so the extra loudness doesn't distort the way "
                         + "raising the system volume past 100% does. 100% = off.")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            TextSwitch {
                text: qsTr("Normalize volume")
                description: qsTr("Even out loudness between tracks using YouTube's own per-song levels, "
                                  + "so you don't reach for the volume between songs. Fetched as each "
                                  + "track starts; songs without level data play unchanged.")
                automaticCheck: false
                checked: app.backend.normalizeVolume
                onClicked: app.backend.setNormalizeVolume(!app.backend.normalizeVolume)
            }
            Slider {
                id: normBoostSlider
                visible: app.backend.normalizeVolume
                width: parent.width
                minimumValue: 0
                maximumValue: 12
                stepSize: 1
                value: Math.round(app.backend.normMaxBoostDb)
                label: qsTr("Max boost for quiet tracks")
                valueText: value <= 0 ? qsTr("Off — only turn loud tracks down") : qsTr("+%1 dB").arg(value)
                onReleased: {
                    app.backend.setNormMaxBoostDb(value)
                    value = Qt.binding(function() { return Math.round(app.backend.normMaxBoostDb) })
                }
            }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: app.backend.normalizeVolume
                wrapMode: Text.Wrap
                text: qsTr("How far a quiet song may be turned up to reach the common level. Loud songs "
                         + "are always turned down regardless. 0 dB matches YouTube (never boosts).")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            TextSwitch {
                text: qsTr("Autoplay")
                description: qsTr("When the queue ends, keep playing related songs (radio). "
                                  + "Turn off to stop at the end of the queue.")
                automaticCheck: false
                checked: app.backend.autoplay
                onClicked: app.backend.setAutoplay(!app.backend.autoplay)
            }

            TextSwitch {
                text: qsTr("Skip disliked songs")
                description: qsTr("Automatically skip songs you've disliked when autoplay picks the "
                                  + "next track.")
                automaticCheck: false
                checked: app.backend.skipDisliked
                onClicked: app.backend.setSkipDisliked(!app.backend.skipDisliked)
            }

            SectionHeader { text: qsTr("Downloads") }

            ValueButton {
                label: qsTr("Folder")
                value: app.backend.downloadDir ? app.backend.downloadDir : qsTr("App folder (default)")
                onClicked: pageStack.animatorPush(folderPickerPage)
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: page.dlError.length > 0
                      ? page.dlError
                      : (app.backend.downloadDir
                         ? qsTr("Tracks are saved here. Tap Folder to change it.")
                         : qsTr("Tracks are saved in the app's own private folder by default — pick a "
                                + "folder like Music or an SD card to find them in the file manager."))
                color: page.dlError.length > 0 ? Theme.errorColor : Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Button {
                visible: app.backend.downloadDir.length > 0
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Reset to app folder")
                onClicked: app.backend.setDownloadDir("", function(r) { page.dlError = "" })
            }
        }
    }

    // Folder picker for the download location. FinTune runs unsandboxed, so it can browse and
    // write anywhere in the home tree; Python validates the pick is writable before saving.
    Component {
        id: folderPickerPage
        FolderPickerPage {
            onSelectedPathChanged: {
                app.backend.setDownloadDir(selectedPath, function(r) {
                    page.dlError = (r && r.ok === false) ? (r.error || qsTr("Couldn't use that folder.")) : ""
                })
            }
        }
    }
}
