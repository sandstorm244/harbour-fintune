// Standard Sailfish launcher: boots qml/harbour-fintune.qml and registers the C++ GStreamer
// VideoPlayer as a QML type. We use the manual view setup (instead of SailfishApp::main) so we
// can give QML's image loader a persistent on-disk cache for album/playlist thumbnails.
#include <sailfishapp.h>
#include <QtQml>
#include <QGuiApplication>
#include <QQuickView>
#include <QQmlEngine>
#include <QQmlNetworkAccessManagerFactory>
#include <QNetworkAccessManager>
#include <QNetworkDiskCache>
#include <QStandardPaths>
#include <QDir>
#include <QFile>
#include <gst/gst.h>
#include "videoplayer.h"

// Redirect GStreamer's downloadbuffer (uridecodebin download=TRUE, used for seekable playback)
// off /tmp — which is tmpfs (RAM) on SailfishOS — onto persistent flash, so a long stream's
// spooled buffer can't grow until it OOMs the phone. uridecodebin exposes no temp-location, so
// we steer it via TMPDIR (what downloadbuffer derives its temp path from). g_get_tmp_dir() caches
// on first use, so this must run before anything in the process reads it.
static void redirectMediaBufferToFlash()
{
    const QString mediaBuf = QDir::homePath() + "/.cache/harbour-fintune/mediabuf";
    QDir d(mediaBuf);
    d.mkpath(".");
    // Drop stale spill from a previous (possibly crashed) run so flash use stays bounded.
    const QStringList stale = d.entryList(QDir::Files | QDir::NoDotAndDotDot);
    for (const QString &f : stale)
        d.remove(f);
    qputenv("TMPDIR", QFile::encodeName(mediaBuf));
}

// Persistent on-disk cache for QML network images, so album/playlist thumbnails aren't
// re-downloaded every time a shelf scrolls back into view or the app restarts. Player cover
// art (a larger URL) rides the same cache harmlessly; 64 MB LRU keeps the small thumbs warm.
class CachingNamFactory : public QQmlNetworkAccessManagerFactory
{
public:
    QNetworkAccessManager *create(QObject *parent) override
    {
        QNetworkAccessManager *nam = new QNetworkAccessManager(parent);
        QNetworkDiskCache *cache = new QNetworkDiskCache(nam);
        cache->setCacheDirectory(
            QStandardPaths::writableLocation(QStandardPaths::CacheLocation) + "/thumbs");
        cache->setMaximumCacheSize(64LL * 1024 * 1024);
        nam->setCache(cache);
        return nam;
    }
};

int main(int argc, char *argv[])
{
    redirectMediaBufferToFlash();
    gst_init(&argc, &argv);

    // droidvdec (hardware H.264) on this device can't load its codec libs
    // (libandroidicu / apexcodecs) and stalls without ever producing a frame.
    // Demote it so auto-plugging falls back to software avdec_h264 everywhere.
    GstPluginFeature *droidvdec =
        gst_registry_lookup_feature(gst_registry_get(), "droidvdec");
    if (droidvdec) {
        gst_plugin_feature_set_rank(droidvdec, GST_RANK_NONE);
        gst_object_unref(droidvdec);
    }

    qmlRegisterType<VideoPlayer>("FinTune", 1, 0, "VideoPlayer");

    QScopedPointer<QGuiApplication> app(SailfishApp::application(argc, argv));
    QScopedPointer<QQuickView> view(SailfishApp::createView());
    // Release the GL context + scene graph when the window is hidden (display blank) and rebuild
    // them fresh on show, so cached GL textures (glyph atlas, cover art) can't be left dangling
    // after a display-off/on cycle on this libhybris/EGL stack (garbled/green-tinted resume).
    view->setPersistentOpenGLContext(false);
    view->setPersistentSceneGraph(false);
    view->engine()->setNetworkAccessManagerFactory(new CachingNamFactory);
    view->setSource(SailfishApp::pathTo(QStringLiteral("qml/harbour-fintune.qml")));
    view->showFullScreen();
    return app->exec();
}
