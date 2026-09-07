#!/bin/sh
# Shadow build — keeps this source tree pristine. All build output (intermediates AND the RPM)
# goes to a sibling "<app>.build/" directory; nothing lands in the source dir.
#   RPM ends up in:  ../harbour-fintune.build/RPMS/
# Override the SDK target with:  TARGET=SailfishOS-x.y.z-aarch64 sh build.sh
set -e

# Default to the OLDEST supported SFOS release (5.0). glibc/Qt are backward compatible, so a
# 5.0-built rpm installs on every newer release — but a 5.1-built one demands GLIBC_2.34 and
# locks 5.0 devices out (glibc 2.34 merged libpthread into libc, so every threaded binary gets
# the versioned require even without using any new API; seen live on a 5.0.0.76 device).
# If sfdk says this target doesn't exist: `sfdk tools list` shows what's installed; add the
# 5.0 target via the SDK maintenance tool, or override TARGET= for a local-only build.
TARGET="${TARGET:-SailfishOS-5.0.0.62-aarch64.default}"
SRC="$(cd "$(dirname "$0")" && pwd)"
BUILD="$SRC.build"

mkdir -p "$BUILD"
STAMP="$BUILD/.build-stamp"
touch "$STAMP"                      # so the glibc check below only sees THIS run's rpms
cd "$BUILD"
echo "Shadow-building $SRC"
echo "            → $BUILD  (target=$TARGET)"
sfdk -c "target=$TARGET" build "$SRC"

# Guard: refuse to bless an rpm that requires glibc >= 2.34 (the SFOS 5.1 jump) — such an rpm
# will not install on 5.0 devices ("nothing provides libc.so.6(GLIBC_2.34)"). rpmbuild stamps
# these requires silently, so a too-new TARGET is only ever caught here or by a locked-out
# user. Uses the host's rpm if present, else the build engine's. Raise the ceiling only when
# the app's supported floor moves past SFOS 5.0.
fresh_rpms=$(find "$BUILD/RPMS" -name '*.rpm' -newer "$STAMP" 2>/dev/null || true)
for f in $fresh_rpms; do
    if command -v rpm >/dev/null 2>&1; then
        reqs=$(rpm -qpR "$f" 2>/dev/null || true)
    else
        reqs=$(sfdk engine exec rpm -qpR "$f" 2>/dev/null || true)
    fi
    if [ -z "$reqs" ]; then
        echo "WARNING: couldn't read requires of $f — verify by hand: rpm -qpR '$f'" >&2
        continue
    fi
    bad=$(printf '%s\n' "$reqs" | grep -oE 'GLIBC_(2\.(3[4-9]|[4-9][0-9])|[3-9]\.[0-9]+)' | sort -uV || true)
    if [ -n "$bad" ]; then
        echo "ERROR: $(basename "$f") requires $(echo $bad | tr '\n' ' ')— built against a too-new target." >&2
        echo "       This rpm will NOT install on SFOS 5.0. Rebuild with the 5.0 target." >&2
        exit 1
    fi
done
echo "Done. RPM(s) in: $BUILD/RPMS  (glibc floor check passed)"
