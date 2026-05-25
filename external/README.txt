Quopus External Helpers
======================

This directory holds optional third-party command-line tools that Quopus can use
if available. They are searched in the order:

  1. Explicit path set via the corresponding config setting
  2. <dopus>/external/<tool>[.exe]   (this directory - portable bundle)
  3. System PATH

Drop the executables here and Quopus picks them up automatically.

recoil2png
----------
Used by the Retro GFX Viewer to decode 500+ retro computer image formats
(Atari 8-bit/ST/Falcon, Amiga, Apple II/IIGS/Mac, MSX, ZX Spectrum, NEC PC-88/98,
SAM Coupe, TRS-80, BBC Micro, and more).

Download:
  https://recoil.sourceforge.net/

Windows:
  - Download recoil-6.4.5-win64.zip from the link above
  - Extract recoil2png.exe to this folder (next to this README)

Linux:
  - apt install (Debian/Ubuntu) puts recoil2png in /usr/bin - Quopus finds it
    via PATH automatically; no need to copy here.
  - Or extract the static binary to this folder and chmod +x it.

Native C64 decoders (Koala, FLI, AFLI, IFLI, Drazpaint, Doodle, Amica,
Art Studio, Adv Art Studio, CDU Paint, Interpaint, Vidcom, etc.) are
built-in to Quopus and don't need recoil2png.


nibconv (nibtools)
------------------
Used by the Quopus Database scanner to catalog raw-track disk images
(G64, NIB, NBZ). Without nibconv these formats are indexed by MD5
but their directory contents aren't searchable - the database
browser shows them under "Issues" with a hint to install this tool.

Download:
  https://c64preservation.com/dp.php?pg=nibtools
  https://github.com/markusC64/nibtools  (source)

Windows:
  - Download the nibtools binary release for Windows
  - Extract nibconv.exe to this folder (next to this README)
  - The other tools in the package (nibread, nibwrite, etc) are
    optional - Quopus only uses nibconv

Linux:
  - Build from source: git clone, make, sudo make install
  - Or extract the static binary to this folder and chmod +x it
  - apt: not typically packaged; build from source

macOS:
  - Build from source (requires libusb)

Usage by Quopus:
  Internally Quopus runs:
    nibconv input.nib output.d64
  to convert raw-track formats to standard D64 in a temp directory
  before parsing the BAM/directory. The temp D64 is deleted after.
  No persistent state is added to your disk; only the catalog entry
  in the SQLite DB is kept.


unlzx
-----
Used by the database scanner to catalog LZX-compressed archives
(common on Amiga-era scene releases). Without unlzx the file is
recorded by MD5 but contents aren't enumerated.

Download:
  http://aminet.net/util/arc/unlzx.lha    (original Amiga binary)
  Various Linux/Windows ports exist - search GitHub for "unlzx"

Drop the executable here as unlzx.exe (Windows) or unlzx (Linux).


rclone
------
Used by the Rclone Browser action ("Cloud storage" group in the
action picker). Rclone exposes 70+ cloud storage backends through
a unified command-line interface - Google Drive, OneDrive,
Dropbox, Box, Mega, pCloud, Amazon S3, Backblaze B2, Cloudflare R2,
Azure Blob, OpenStack Swift, WebDAV, SFTP, FTP, and many more.

Download:
  https://rclone.org/downloads/

Windows:
  - Download the appropriate "rclone-vX.YY.Z-windows-amd64.zip" or
    "-windows-386.zip" for your CPU architecture
  - Extract just rclone.exe to this folder (next to this README)
  - The .zip also contains documentation, manpage etc. - those
    aren't needed, only rclone.exe

Linux:
  - apt install rclone   (Debian / Ubuntu) - Quopus finds it via
    PATH automatically; no need to copy here.
  - Or extract the static binary to this folder and chmod +x it.

macOS:
  - brew install rclone  (Homebrew) - Quopus finds it via PATH.
  - Or extract the static binary to this folder and chmod +x it.

After dropping the binary here you still need to configure your
cloud accounts. Use either:
  - The "Rclone setup (configure cloud accounts)" action in
    Quopus - it spawns 'rclone config' in a terminal window for
    you. This is the recommended path.
  - Or run 'rclone config' yourself in a terminal at any time.

Either way, rclone's interactive wizard handles all 70+ backend
types and their OAuth / API-key / SSH-key flows. Once at least one
remote is configured, the "Rclone browser" action lets you copy,
move, rename, and delete files in the cloud from Quopus.
