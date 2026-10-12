#!/bin/bash
# Build the Preview .deb. No root required: dpkg-deb builds from a staging tree.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/preview/__init__.py")"
ARCH="all"
PKG="preview_${VERSION}_${ARCH}"
BUILD="${BUILD_DIR:-$ROOT/build}"
STAGE="$BUILD/$PKG"
OUT="${OUT_DIR:-$ROOT/dist}"

echo "==> Building Preview $VERSION"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/preview" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/metainfo" \
         "$STAGE/usr/share/doc/preview"
mkdir -p "$OUT"

# --- application code -------------------------------------------------------
cp -r "$ROOT/preview" "$STAGE/usr/share/preview/preview"
# Ship source only; .pyc files are regenerated and would differ per Python.
find "$STAGE/usr/share/preview" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE/usr/share/preview" -name '*.pyc' -delete

install -m 0755 "$ROOT/bin/preview" "$STAGE/usr/bin/preview"

# --- desktop integration ----------------------------------------------------
install -m 0644 "$ROOT/data/com.nxtwave.Preview.desktop" \
    "$STAGE/usr/share/applications/com.nxtwave.Preview.desktop"
install -m 0644 "$ROOT/data/icons/com.nxtwave.Preview.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/com.nxtwave.Preview.svg"
install -m 0644 "$ROOT/data/com.nxtwave.Preview.metainfo.xml" \
    "$STAGE/usr/share/metainfo/com.nxtwave.Preview.metainfo.xml"

# --- documentation ----------------------------------------------------------
install -m 0644 "$HERE/copyright" "$STAGE/usr/share/doc/preview/copyright"
if [ -f "$ROOT/README.md" ]; then
    install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/preview/README.md"
fi
printf 'preview (%s) stable; urgency=low\n\n  * Initial release.\n\n -- Vikram Balai <vikrambalai1002@gmail.com>  %s\n' \
    "$VERSION" "$(date -R)" > "$BUILD/changelog.Debian"
gzip -9cn "$BUILD/changelog.Debian" > "$STAGE/usr/share/doc/preview/changelog.Debian.gz"
chmod 0644 "$STAGE/usr/share/doc/preview/changelog.Debian.gz"

# --- control ----------------------------------------------------------------
sed "s/^Version: .*/Version: $VERSION/" "$HERE/control" > "$STAGE/DEBIAN/control"
install -m 0755 "$HERE/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$HERE/postrm" "$STAGE/DEBIAN/postrm"

# Installed-Size keeps apt's disk estimate honest.
SIZE="$(du -ks "$STAGE" | cut -f1)"
echo "Installed-Size: $SIZE" >> "$STAGE/DEBIAN/control"

# --- permissions ------------------------------------------------------------
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE/usr/share/preview" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE/usr/bin/preview" "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$STAGE" "$OUT/$PKG.deb" >/dev/null
echo "==> $OUT/$PKG.deb"
dpkg-deb --info "$OUT/$PKG.deb" | sed -n '1,8p'
