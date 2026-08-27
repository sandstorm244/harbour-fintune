# harbour-fintune — starting skeleton.
# Verify dependency/package names and plugin versions on the first on-device build.

TARGET = harbour-fintune

CONFIG += sailfishapp link_pkgconfig
# Hardware-decode path (droideglsink -> EGLImage -> GL_TEXTURE_EXTERNAL_OES): egl for the
# EGLImage/display calls, nemo-gstreamer-interfaces for nemo_gst_egl_image_memory_create_image.
# QT += opengl pulls in both the legacy QGLShaderProgram the renderer uses and the GLESv2
# link (Qt is built against GLES2 here), matching MicroTube's known-good setup. All only
# exercised at runtime when YOUFISH_HWDEC=1.
# network: QNetworkDiskCache backs the QML image loader's on-disk thumbnail cache (main.cpp).
QT += opengl network
PKGCONFIG += gstreamer-1.0 gstreamer-app-1.0 gstreamer-video-1.0 \
             egl nemo-gstreamer-interfaces-1.0

# Adding PKGCONFIG dropped the sailfishapp libs from the link line, so
# SailfishApp::main() went unresolved — re-add them (and the booster link flags).
LIBS += -lsailfishapp -lmdeclarativecache5
QMAKE_LFLAGS += -pie -rdynamic

# Keep qmake's compile intermediates out of the source root: all .o / moc_* / qrc_* land under
# .build/ instead of scattering across the project dir. The final binary + Makefile still sit at
# the root, where %qmake5_install expects them, so packaging is unaffected.
OBJECTS_DIR = .build/obj
MOC_DIR     = .build/moc
RCC_DIR     = .build/rcc

SOURCES += src/harbour-fintune.cpp \
           src/videoplayer.cpp \
           src/hwvideosink.cpp

HEADERS += src/videoplayer.h \
           src/hwvideosink.h

DISTFILES += \
    qml/harbour-fintune.qml \
    qml/Backend.qml \
    qml/MprisControls.qml \
    qml/pages/HomePage.qml \
    qml/pages/SearchPage.qml \
    qml/pages/NowPlayingPage.qml \
    qml/pages/PlaylistPage.qml \
    qml/pages/ArtistPage.qml \
    qml/pages/LibraryPage.qml \
    qml/pages/QueuePage.qml \
    qml/pages/DownloadsPage.qml \
    qml/pages/MorePage.qml \
    qml/pages/LyricsPage.qml \
    qml/pages/EqualizerPage.qml \
    qml/pages/PlaylistPickerPage.qml \
    qml/pages/HistoryPage.qml \
    qml/pages/SettingsPage.qml \
    qml/cover/CoverPage.qml \
    python/youfish.py \
    python/ytm.py \
    harbour-fintune.desktop \
    harbour.fintune.service \
    rpm/harbour-fintune.spec

# Ship the Python backend alongside the QML. sailfishapp.prf already installs the
# qml/ tree and the .desktop file; this adds our python/ module.
python.files = python
python.path = /usr/share/$${TARGET}
INSTALLS += python

# Launcher icon (rasterised from S3.svg) at the four Sailfish densities.
icon86.files  = icons/86x86/$${TARGET}.png
icon86.path   = /usr/share/icons/hicolor/86x86/apps
icon108.files = icons/108x108/$${TARGET}.png
icon108.path  = /usr/share/icons/hicolor/108x108/apps
icon128.files = icons/128x128/$${TARGET}.png
icon128.path  = /usr/share/icons/hicolor/128x128/apps
icon172.files = icons/172x172/$${TARGET}.png
icon172.path  = /usr/share/icons/hicolor/172x172/apps
INSTALLS += icon86 icon108 icon128 icon172

# D-Bus auto-activation service: lets the URL dispatcher cold-start a single FinTune instance
# and deliver openUrl. Without it, cold-launch activation fails and races the Exec fallback,
# which crashes the app a couple seconds in. Mirrors MicroTube's setup.
dbusService.files = harbour.fintune.service
dbusService.path  = /usr/share/dbus-1/services
INSTALLS += dbusService
