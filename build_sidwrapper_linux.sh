#!/usr/bin/env bash
# =====================================================================
# Build libsidwrapper.so on Linux.
#
# Required: g++ + libsidplayfp dev headers/lib. Install with:
#   Debian/Ubuntu:  sudo apt install build-essential libsidplayfp-dev
#   Fedora:         sudo dnf install gcc-c++ libsidplayfp-devel
#   Arch:           sudo pacman -S base-devel libsidplayfp
#
# After build, libsidwrapper.so will sit next to quopus.py and the
# Python loader in sid_player.py picks it up automatically.
#
# This wrapper exposes sid_set_roms() so that PSIDs/RSIDs that need
# the C64 KERNAL/BASIC/CHARGEN ROMs can actually play. Drop the ROM
# files into a `roms/` subdirectory next to quopus.py - see
# roms/README.txt for sources and exact filenames.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v g++ >/dev/null 2>&1; then
    echo "ERROR: g++ not found. Install build-essential first." >&2
    exit 1
fi
if ! pkg-config --exists libsidplayfp 2>/dev/null \
        && [ ! -f /usr/include/sidplayfp/sidplayfp.h ] \
        && [ ! -f /usr/local/include/sidplayfp/sidplayfp.h ]; then
    echo "ERROR: libsidplayfp dev headers not found." >&2
    echo "Install:  sudo apt install libsidplayfp-dev" >&2
    exit 1
fi

PKG_CFLAGS="$(pkg-config --cflags libsidplayfp 2>/dev/null || true)"
PKG_LIBS="$(pkg-config --libs libsidplayfp 2>/dev/null || echo '-lsidplayfp')"

echo "=== using CFLAGS: $PKG_CFLAGS"
echo "=== using LIBS:   $PKG_LIBS"
echo "Building libsidwrapper.so ..."
g++ -O2 -fPIC -shared \
    sidwrapper.cpp \
    $PKG_CFLAGS \
    -o libsidwrapper.so \
    $PKG_LIBS -lstdc++

echo "Done.  $(stat -c '%n  %s bytes' libsidwrapper.so)"
echo
echo "Sanity check:  symbols exported by libsidwrapper.so"
nm -D libsidwrapper.so | grep -E ' T sid_' | awk '{print "  " $3}'
