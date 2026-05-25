#!/usr/bin/env bash
# =====================================================================
# setup_c64_roms.sh
# =====================================================================
# Try to copy the three C64 ROMs (kernal/basic/chargen) into the
# Quopus roms/ folder so the SID player can play RSIDs and tunes that
# need the C64 KERNAL.
#
# Modern VICE packages on Ubuntu/Debian DROP the ROM dumps from the
# binary package (Commodore copyright concerns), so this script
# checks the most likely places where they might still live and
# copies them locally.
#
# If nothing is found, the script ends with download instructions.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p roms

echo "=== Quopus C64 ROM finder ==="
echo

# Where to look. Order = priority. We accept any file whose name
# contains kernal/basic/chargen (case-insensitive) and whose size
# matches the expected dump size exactly (8192/8192/4096).
SEARCH_DIRS=(
    /usr/share/vice/C64
    /usr/lib/vice/C64
    /usr/local/share/vice/C64
    /usr/share/sidplayfp
    /usr/local/share/sidplayfp
    /usr/share/vice-data/C64
    /usr/share/games/vice/C64
    "$HOME/.vice/C64"
    "$HOME/.config/sidplayfp"
    "$HOME/.sidplayfp"
    /opt/vice/C64
)

declare -A FOUND
declare -A EXPECTED
EXPECTED[kernal]=8192
EXPECTED[basic]=8192
EXPECTED[chargen]=4096

# Walk the search dirs and pick the first plausible match per ROM.
for d in "${SEARCH_DIRS[@]}"; do
    [ -d "$d" ] || continue
    for role in kernal basic chargen; do
        [ -n "${FOUND[$role]+x}" ] && continue
        # case-insensitive name match, exact size match
        while IFS= read -r -d '' f; do
            sz=$(stat -c%s "$f")
            want=${EXPECTED[$role]}
            if [ "$sz" = "$want" ]; then
                FOUND[$role]="$f"
                break
            fi
            # accept slightly larger files (header + payload) by
            # tail-extracting later
            if [ "$sz" -gt "$want" ]; then
                FOUND[$role]="$f"   # tentative; we'll check tail below
                break
            fi
        done < <(find "$d" -maxdepth 2 -type f \
                    -iname "*${role}*" -print0 2>/dev/null)
    done
done

echo "Search results:"
for role in kernal basic chargen; do
    if [ -n "${FOUND[$role]+x}" ]; then
        echo "  $role  -> ${FOUND[$role]}"
    else
        echo "  $role  -> NOT FOUND"
    fi
done
echo

# Copy the matches into roms/. Use canonical filenames so users can
# tell what's what at a glance.
declare -A TARGET_NAME
TARGET_NAME[kernal]=kernal.901227-03.bin
TARGET_NAME[basic]=basic.901226-01.bin
TARGET_NAME[chargen]=chargen.901225-01.bin

INSTALLED=0
for role in kernal basic chargen; do
    src="${FOUND[$role]:-}"
    [ -z "$src" ] && continue
    want=${EXPECTED[$role]}
    sz=$(stat -c%s "$src")
    out="roms/${TARGET_NAME[$role]}"
    if [ "$sz" = "$want" ]; then
        cp -f "$src" "$out"
        echo "Copied $role: $src -> $out"
        INSTALLED=$((INSTALLED+1))
    elif [ "$sz" -gt "$want" ]; then
        # Tail-extract: some VICE ROMs have a short header
        tail -c "$want" "$src" > "$out"
        echo "Copied $role (tail-extracted): $src -> $out"
        INSTALLED=$((INSTALLED+1))
    fi
done

echo
if [ "$INSTALLED" -eq 3 ]; then
    echo "=== All three ROMs installed in roms/. SID playback ready. ==="
elif [ "$INSTALLED" -gt 0 ]; then
    echo "=== Installed $INSTALLED of 3 ROMs. Some RSIDs may still fail. ==="
else
    cat <<'MSG'
=== No ROMs found on this system. ===

Modern Debian/Ubuntu VICE packages no longer ship the C64 ROM dumps
because of Commodore copyright concerns. You need to obtain them
yourself. The ROMs are part of the original C64 hardware (1982).

Fastest legal sources:

  1. Older VICE release (still bundled them):
       https://sourceforge.net/projects/vice-emu/files/releases/
     Download e.g. vice-3.6.1.tar.gz (or any older version), extract,
     and copy data/C64/kernal-901227-03.bin (or similar) plus the
     basic and chargen files into this script's roms/ directory.

  2. Debian non-free repository:
       sudo apt install vice-data-nonfree
     (only if your distro carries this package - Ubuntu does not)

  3. Build ROMs from C64 hardware dump tools - this is what
     enthusiasts do for full legal compliance.

Required filenames in roms/ (any naming OK as long as size matches):
    kernal.901227-03.bin    8192 bytes
    basic.901226-01.bin     8192 bytes
    chargen.901225-01.bin   4096 bytes

After dropping them in roms/, restart Quopus and open a SID. The
player's header strip should now read "ROMs: OK".
MSG
fi
