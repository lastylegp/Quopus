C64 ROMs for libsidplayfp
=========================

Drop the three C64 ROM files into THIS folder so Quopus' SID player can
play RSIDs and KERNAL-using PSIDs. Without them, those tunes either
fail to load or play silence.

Required files (any naming - Quopus matches by content + size):

    KERNAL   8192 bytes   typical name: kernal.901227-03.bin
    BASIC    8192 bytes   typical name: basic.901226-01.bin
    CHARGEN  4096 bytes   typical name: chargen.901225-01.bin

EASY WAY: run the helper script next to quopus.py
------------------------------------------------
    Linux/macOS:   ./setup_c64_roms.sh
    Windows:       setup_c64_roms.bat

The helper checks all the standard VICE install paths on your system
and copies any ROMs it finds into this folder. If nothing turns up,
it prints download instructions.

Where to get them manually
--------------------------
Modern Linux VICE packages (apt install vice on Ubuntu) drop the ROM
dumps for licensing reasons. Easiest sources:

  1. Older VICE release that still bundles them:
       https://sourceforge.net/projects/vice-emu/files/releases/
     Any 3.x .tar.gz or .zip has them under data/C64/.

  2. Debian non-free repo:
       sudo apt install vice-data-nonfree
     (only if your distro carries this package - Ubuntu does not)

  3. Windows VICE binary distributions still ship the ROMs - just
     download the Windows zip and grab the C64 subdirectory.

How Quopus finds them
--------------------
The loader in sid_player.py searches, in priority order:

    1. <quopus>/roms/                  <-- this folder
    2. <quopus>/                       (next to quopus.py)
    3. ~/.config/sidplayfp/
    4. ~/.sidplayfp/
    5. ~/.vice/C64/
    6. /usr/share/vice/C64/
    7. /usr/lib/vice/C64/
    8. /usr/local/share/vice/C64/
    9. /usr/share/sidplayfp/
   10. /usr/local/share/sidplayfp/
   11. C:\Program Files\WinVICE\C64\
   12. C:\Program Files (x86)\WinVICE\C64\
   13. C:\vice\C64\

Filename matching is case-insensitive and just checks for the words
"kernal" / "basic" / "chargen" anywhere in the name. Size must match
exactly (8192/8192/4096).

The SID player's header strip shows "ROMs: OK" / "partial" / "missing"
so you can see at a glance whether the ROMs were picked up. When
missing, the label is a clickable link that opens setup help.
