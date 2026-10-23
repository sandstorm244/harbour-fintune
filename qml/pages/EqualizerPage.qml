import QtQuick 2.0
import Sailfish.Silica 1.0
import "../logic.js" as Logic

// 10-band equalizer. Bands map to equalizer-10bands' fixed centre frequencies. Adjusting a band
// previews live on the audio engine; the value is persisted (and re-applied across tracks) on
// release. Presets set all ten at once.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock / resume bar over the equalizer

    readonly property var freqLabels: [qsTr("%1 Hz").arg(29), qsTr("%1 Hz").arg(59), qsTr("%1 Hz").arg(119), qsTr("%1 Hz").arg(237), qsTr("%1 Hz").arg(474),
                                       qsTr("%1 Hz").arg(947), qsTr("%1 kHz").arg("1.9"), qsTr("%1 kHz").arg("3.8"), qsTr("%1 kHz").arg("7.5"), qsTr("%1 kHz").arg(15)]
    // Preset names, in the order of Logic.EQ_PRESETS (the curves). "Flat" is index 0.
    readonly property var presetNames: [qsTr("Flat"), qsTr("Bass boost"), qsTr("Treble boost"), qsTr("Rock"),
                                        qsTr("Pop"), qsTr("Vocal"), qsTr("Electronic")]

    function commitBand(i, v) {
        var b = app.backend.eqBands.slice()
        b[i] = v
        app.backend.setEqBands(b)
    }
    function applyPreset(idx) {
        var plan = Logic.presetPlan(idx, app.backend.eqEnabled)
        if (!plan) return
        app.backend.setEqBands(plan.bands)
        if (plan.enable)
            app.backend.setEqEnabled(true)          // picking a curve implies you want it on
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: qsTr("Equalizer") }

            TextSwitch {
                text: qsTr("Equalizer")
                description: qsTr("Apply the band gains below to playback.")
                automaticCheck: false
                checked: app.backend.eqEnabled
                onClicked: app.backend.setEqEnabled(!app.backend.eqEnabled)
            }

            ComboBox {
                width: parent.width
                label: qsTr("Preset")
                // Apply on the item's own click (not currentIndex, which fires on load and would
                // reset the saved bands to Flat every time the page opens).
                menu: ContextMenu {
                    Repeater {
                        model: page.presetNames
                        MenuItem {
                            text: modelData
                            onClicked: page.applyPreset(index)
                        }
                    }
                }
            }

            Item { width: 1; height: Theme.paddingMedium }

            Repeater {
                model: 10
                delegate: Slider {
                    width: col.width
                    // Musical range; the engine clamps to -24..+12 regardless.
                    minimumValue: -12
                    maximumValue: 12
                    stepSize: 1
                    enabled: app.backend.eqEnabled
                    value: app.backend.eqBands[index] || 0
                    label: page.freqLabels[index]
                    valueText: qsTr("%1 dB").arg((value > 0 ? "+" : "") + Math.round(value))
                    // Live preview while dragging; persist + re-establish the binding on release.
                    onValueChanged: if (app.player) app.player.setEqBand(index, value)
                    onReleased: {
                        page.commitBand(index, value)
                        value = Qt.binding(function() { return app.backend.eqBands[index] || 0 })
                    }
                }
            }
        }

        VerticalScrollDecorator { }
    }
}
