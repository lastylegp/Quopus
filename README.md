# Quopus Commander — A Directory Opus 4 inspired file manager in Python/PyQt6

**Quopus Commander v1.0 by lA-sTYLe/Quantum, 05/2026**

A modern Python/PyQt6 file manager inspired by the classic Amiga **Directory Opus 4** by Jonathan Potter / GPSoftware, extended with Total Commander / Norton Commander power-user workflows, BBS / demoscene / retro-computing tooling (CBM disk images, Amiga ADF, U64 streamer, Assembly64 browser, SID/MOD playback, PETSCII charset editor, ...), and a built-in FTP browser.

The credit for the original concept goes to **Jonathan Potter** and **GPSoftware** who created Directory Opus on the Amiga in the early 1990s — this is a tribute, not a port. None of the original Opus code is used; the UI metaphor (dual-pane lister + button grid + drive column + workbench-style chrome) is the inspiration.

---

## Quick start

### Requirements
- Python 3.10+
- PyQt6

### Install
```cmd
cd quopus_commander
python -m venv venv
venv\Scripts\activate
pip install PyQt6
python quopus.py
```

### Optional dependencies
```cmd
pip install paramiko      # SFTP support in the FTP browser
pip install lhafile       # LHA/LZH read (Amiga archives)
pip install rarfile       # RAR read (also needs unrar binary on PATH)
pip install py7zr         # 7Z archive read (used by the database scanner)
pip install watchdog      # Native filesystem watching for the database
pip install sounddevice numpy   # for the MOD and SID players
```

For **packing** RAR or LHA archives an external binary is required:
- **RAR**: install WinRAR — `rar.exe` must be on PATH or in `C:\Program Files\WinRAR\`
- **LHA**: install LHA32 / LhaForge for Windows, or original `lha` on Linux. Lhasa (Linux) cannot pack, only extract.

For the **Quopus Database** scanner, two extra command-line tools enable full disk-image / archive coverage. Both are optional — the scanner still works without them, files are indexed by MD5 but their internal contents won't be:
- **nibconv** from nibtools — converts G64/NIB/NBZ raw-track disk images to D64 so their directories can be BAM-walked. Drop into `<quopus>/external/nibconv[.exe]`. Source: https://c64preservation.com/dp.php?pg=nibtools
- **unlzx** — extracts LZX archives (common Amiga scene format). Drop into `<quopus>/external/unlzx[.exe]`. Source: Aminet `util/arc/unlzx`

For **MOD / XM / S3M / IT playback** you need libopenmpt as a system library or shipped DLL — see the ProTracker-style Module Player section below for download links and exact filenames.

For **U64 video recording (MP4 output)** the streamer's **Rec** button pipes raw frames through `ffmpeg` — install it via `apt install ffmpeg` (Linux), `brew install ffmpeg` (macOS), or grab a static build from https://www.gyan.dev/ffmpeg/builds/ (Windows) and put `ffmpeg.exe` on PATH. Without ffmpeg, Rec auto-falls-back to a PNG-sequence dump (one numbered PNG per frame, lossless but much larger on disk).

For **SID playback** the wrapper DLLs are already shipped:
- Linux: `libsidwrapper.so` ships with Quopus; system needs `libsidplayfp-dev` (`apt install libsidplayfp-dev sidplayfp`)
- Windows x86-64: `sidwrapper.dll` ships fully statically linked - drop next to `quopus.py`, no other DLLs needed
- macOS: build the wrapper yourself (one-line `clang++` invocation, see SID Player section)

### Fonts (recommended)
Drop the **C64 Pro Mono** TTF into `quopus_commander/fonts/` for authentic PETSCII rendering. Amiga-style rendering uses **Topaz-8** if available.

### Windows notes
- If `python -m venv venv` fails with a `WindowsApps / venvlauncher.exe` copy error, you're using Python from the Microsoft Store. Install Python from https://www.python.org/downloads/windows/ instead.
- If `pip install PyQt6` fails with long-path errors, enable Windows long paths (`LongPathsEnabled` registry key) or move the project into a shorter path like `C:\quopus\`.

---

## Features overview

### File listing
- Classic two-pane file manager with **4 sortable columns**: Name, Ext, Size, Date
- **Click column header** to sort, click again to reverse — sort indicator (↑/↓) shown in header
- **Right-click column header** for column-specific options. The Size column gets a bytes/blocks toggle (see below); other headers offer the same sort + reverse you'd get from a left-click.
- **Resizable columns** — drag any column boundary; widths persist per side across sessions
- **Sort state persists per side** — left and right can be sorted independently and the choice is remembered across restarts
- **Size column display modes** — bytes (default: `4K`, `1.2M`, `512B`) or **C64 disk blocks** (`1 bl`, `20 bl`, `140 bl`). Blocks use the CBM-DOS convention: 256 bytes = 1 block, always rounded up (a 1-byte file still occupies a full block on a 1541 disk). Toggle via three places, all equivalent: right-click on the Size column header, right-click on empty space in the lister body ("📏 Size: switch to..."), or Config menu → "Switch Size column to...". The choice persists in `quopus.cfg` across restarts and applies to both listers simultaneously.
- Tab or click to switch active pane
- Always-visible tag highlighting on both panels (orange background)
- Directory entries always sorted first
- Navigation history: Back / Forward / Root / Parent
- **Middle-mouse-click anywhere in the lister** = jump to parent directory (equivalent to Backspace or the Parent button). Faster than reaching for the keyboard, works regardless of where exactly you click in the view.
- Path edit line for direct navigation
- Drive column (left side) with up to 40 configurable folder/FTP bookmarks - see "Drive-button column" below for full details
- Branch view (Ctrl+B): flat-list every file in all subdirs
- **Filter dropdown** between `^` and the path edit. Click to pick a file extension to restrict the listing to (`*` = no filter). Top-level menu shows letters A-Z; each letter opens a submenu of matching extensions, drawn from `DEFAULT_ASSOC` + your user-configured `file_assoc` entries. Letters with no matches are hidden so the menu stays compact. Directories are always visible regardless of filter so navigation never gets blocked. Filter changes are instant — the directory walk is cached, only the filtered view is regenerated per click.
- **CRT-ID column** auto-appears when a listing contains `.crt` files. Shows the cartridge hardware type at a glance (`#21 Comal-80`, `#79 Hyper-BASIC`). Sortable by hardware ID. The hover tooltip on a `.crt` row shows the full info: machine (C64/C128/CBM2/VIC20/PLUS4), type number + long name, and the cart's internal ASCII name. When exactly one `.crt` is selected, the info bar also shows the same info. Backed by a lightweight 64-byte-per-file parser (`crt_quick_id()`) with `(path, mtime, size)` caching, so a directory of thousands of carts costs almost nothing to display.
- **Search-results path tooltip** — hovering any row in a search-results listing shows `Name: foo.prg` and `Folder: /full/path` separately; regular listings show the absolute path. Solves the "Folder column truncated, can't see the full path" problem without needing to widen the column.

### Drag & Drop
- **Drag from one lister to the other** — Copy by default, hold Shift to Move
- **Drag to/from Windows Explorer**, the desktop, or any other app that accepts file URLs
- **Multi-selection** carries through: tag/select files first, then drag any one of them
- **Progress dialog** appears for non-trivial transfers (same as F5/F6)
- **Cancellable mid-transfer** — chunked copy aborts within a few hundred ms

### Window state persistence
Window size, position, maximized/fullscreen state, column widths and sort order are saved on every change and restored at launch. Close Quopus while it's fullscreen and it'll come back fullscreen.

The geometry save uses `frameGeometry()` rather than `geometry()` so the saved x/y match what we hand back to `move()` on restore - this avoids the frame-width-of-drift-each-restart issue that's common when mixing X11 / Wayland / Windows window-decoration conventions. The actual `move()` call is also deferred to `QTimer.singleShot(0, ...)` after the window's `showEvent` because some window managers (X11 in particular) silently ignore `move()` calls before the window is mapped on screen.

**Wayland note**: Wayland's compositor protocol does not allow applications to set their own window position - this is a security/UX feature of Wayland itself, not a Quopus bug. Window size, maximized state and fullscreen state still persist correctly under Wayland; only the x/y position will be re-decided by the compositor on each launch. If exact position matters to you, run on X11 (`QT_QPA_PLATFORM=xcb`) instead.

### OS-aware default drives
The drive-button column is auto-populated from the host OS rather than hard-coded with Amiga-style names:
- **Linux/macOS**: `HOME` ($HOME), `ROOT` (/), `TMP` (/tmp), plus any of `/mnt`, `/media`, `/opt`, `/var`, `/etc` that exist. macOS additionally gets `/Volumes` and `/Users`.
- **Windows**: `HOME` (%USERPROFILE%), then every drive letter that's actually mounted (`C:`, `D:`, `E:`...), then `TEMP`.

The default `left_path` and `right_path` are also `Path.home()` rather than the install dir - users want to start in their own files, not in the unpack folder. You can still customise both panels via right-click on a drive button, or via the bookmark dialog (see below).

Drive probing uses a `_safe_exists()` helper that swallows `OSError` instead of letting it crash the startup. This handles a few real-world Windows cases that would otherwise abort Quopus before any window opens:
- Empty CD/DVD drives → `OSError 21` ("Gerät ist nicht bereit")
- Card-reader slots without a card → `OSError 87` ("Falscher Parameter")
- Disconnected mapped network drives → `OSError 67` ("Netzwerkname nicht mehr verfügbar")
- Stale autofs mounts on Linux → various `OSError`/`IOError`

In all those cases the drive is silently skipped and the rest of the column populates normally.

### Quopus Database (catalog & search)

A persistent SQLite catalog of every C64 / Amiga file in your collection — even files locked inside archives or disk images. Indexes filenames and disk-header strings for instant lookup across millions of entries; right-click a result to copy the file (or extract a single PRG out of its containing D64) into the inactive Lister. Bind the `database` action to a button or pick it from the right-click menu to open the browser.

**What gets indexed** — the scanner is *deliberately* C64-focused: only file extensions that mean something on Amiga or CBM hardware are walked. Everything else (`.txt`, `.exe`, `.jpg`, `.pdf`, ...) is skipped at the `os.walk()` boundary, before the file is even opened, so a `Scenebase` tree mixed with random PC files stays clean and fast.

The walker recognises three categories:

- **C64 files** — PRG, SEQ, USR, REL plus the `.Pxx` variants (`p00`, `s00`, ...)
- **Disk images** — D64, D71, D81, D80, D82 (BAM-walked directly), G64, G71, G81, NIB, NBZ (decoded to D64 in-memory via external `nibconv`, then BAM-walked)
- **Archives** — ZIP, LHA, LZH, LZX (via external `unlzx`), RAR, 7Z. Members are walked recursively (max 3 levels deep to keep zip-bombs from running away).

For every disk entry the scanner reads the file out via the standard `CbmDiskReader` path and computes an MD5 of the actual payload, so two PRGs that are byte-identical but live on different disks are found together.

**FTS5 substring search with trigram tokenizer** — searches like `turrican` find any filename containing "turrican" anywhere in the string, instantly, even across millions of entries. For very short queries (1-2 characters where the trigram index can't match) the scanner falls back to a `LIKE` scan so `aa` still finds `aass`; this is slower but stays correct. The status bar in the browser shows `indexed` vs `slow scan` so you can tell when the fallback path is being used.

**Live filesystem watching** keeps the catalog current as you download new releases. Drop a `.zip` into a watched folder and within ~2 seconds (debounce window for copy-bursts) it's in the index. Two backends:

- **`watchdog` package** (if installed) — uses native OS notifications: `inotify` on Linux, `FSEvents` on macOS, `ReadDirectoryChangesW` on Windows. Zero polling overhead, instant reaction.
- **Polling fallback** (every 60 seconds) when `watchdog` isn't installed.

Watched folders are persisted in `config/watched_folders.json` and auto-resumed on the next Quopus start. The watcher rejects non-C64 extensions before they even hit the debounce queue, so dropping a `.txt` into a watched folder doesn't trigger a wakeup.

**Parallel ingestion** via a worker-pool that's shared between the bulk Scan-Folder workflow and the live FS watcher. Default 2 workers — tunable through the `QUOPUS_INGEST_WORKERS` environment variable (range 1-16). On bulk scans this typically gives 2-3× speedup over single-threaded scanning; on a 1 TB Scenebase with lots of small PRGs the difference is hours. The queue is bounded (1024 jobs max) so memory stays flat even when the walker out-runs the workers.

**Crash recovery** — every file row in the DB carries a `scan_status` field (`pending` / `done` / `failed`). When ingestion of a multi-member archive starts, the parent file is written immediately with status `pending`; individual member rows are inserted as `done`, and the parent flips to `done` only after every member has been committed. If Quopus crashes mid-archive (kill -9, power loss, SQLite WAL corruption from disk-full, ...), the next start scans for `pending` rows, clears them, and re-enqueues them through the ingest pipeline. The user sees no missing files and no half-indexed archives in the Issues tab.

**Issues tab** — files that needed user attention but couldn't be fully indexed are surfaced here instead of silently dropped:

| Issue type | Color | What it means |
|---|---|---|
| `password` | blue | Archive (ZIP / RAR / 7Z) is encrypted and we don't have the password. ZIP per-entry encryption is also detected. |
| `corrupt_disk` | red | D64/D71/D81 header looks malformed or BAM-walk failed |
| `no_directory` | yellow | G64/NIB/NBZ but `nibconv` isn't installed → file is indexed by MD5 only |
| `extract_failed` | red | Archive opened but a member couldn't be extracted (corrupt) |
| `unknown_format` | grey | Recognised extension but content doesn't match (e.g. truncated D64) |

Filter dropdown by type; tab title shows the count. Double-click a row to copy the path to clipboard.

**Browser layout** — non-modal top-level window with **5 tabs**:

1. **Files** — substring search by filename, results grouped by file vs. disk-entry. 300 ms debounce keeps typing snappy. Bytes / blocks toggle in footer.
2. **Disks** — substring search by disk-header / DOS-ID. Top list is matching disks; lower pane is the directory of the selected disk.
3. **Watch** — manage live-watched folders. Add / Remove / Start / Stop, queue stats, status banner. Refreshes every 2 seconds.
4. **Issues** — see the table above. Filter by type, double-click to copy path.
5. **Stats** — DB size, scan history, pending / failed counts (the crash-recovery warning lights), Refresh / Vacuum / Clean Up Non-C64 / Reset DB buttons.

**Right-click context menu** on Files, Disks and Disk-entries — the bridge between the catalog and the real file system. The menu adapts to what you clicked:

- **A file on disk** → "Copy file 'X' to right Lister (path)", "Reveal in active Lister", "Copy Path"
- **An archive member** → "Copy archive 'X' to right Lister" (you get the whole archive; members are pulled apart in the archive viewer), "Reveal archive", "Copy Archive Path"
- **A disk entry** (PRG inside a D64) → "Extract 'NAME' to right Lister" (real CbmDiskReader.extract() call, file written to disk), "Copy whole disk 'X.d64' to right Lister", "Reveal disk"

The "right Lister" label is dynamic — it actually says whichever side isn't currently active, with a friendly path snippet so you don't end up copying to the wrong place.

**Database file management toolbar** at the top of the browser:

- **Open DB...** — load a catalog shared by another user (e.g. via Discord/USB) in **read-only** mode. The shared DB is treated as a viewer only — Scan/Watch/Cleanup/Vacuum/Reset buttons are all disabled while it's loaded so a stray click can't write to the friend's file. Window title and a coloured label make it obvious which DB you're on (🟢 green = your own, 🔵 blue = other writable, 🟠 orange = read-only).
- **Save As...** — copy your DB to a portable `.sqlite` via SQLite's backup API (correct even with an active watcher and uncommitted WAL). Useful for sharing your catalog with another sysop, or for taking a snapshot before a risky operation.
- **Open Own DB** — switch back to the personal DB in `config/quopus_db.sqlite`. Greyed out when already on your own.

**Non-modal browser** — opens as a separate top-level window with `WA_DeleteOnClose`. You can keep it open alongside the normal Quopus listers, scroll through search results in one window and drag-drop in the other. Closing Quopus also closes the browser via `atexit` cleanup.

**Performance numbers** for context:

- Bulk scan of 100 PRGs: ~0.5 seconds with 2 workers
- Bulk scan of 100 ZIPs (one PRG each): ~0.31 seconds (vs. 0.86 s single-threaded — 2.74× speedup)
- Search "turrican" across 50k filenames: <50 ms via FTS5
- Watcher reaction to new ZIP appearing in folder: ~2 seconds (debounce window)
- DB file size: ~170 MB per 100k files + 600k disk entries

**Scaling characteristics** — for collections with millions of files (typical Scenebase mirror = 1-2M files + 30-40M disk entries):

The schema is specifically designed to stay fast at this scale. Two architectural choices keep things responsive:

1. **Split FTS5 indexes**: separate `fts_names` (for files) and `fts_entries` (for disk entries), each with `rowid` identical to the source table's `id`. This makes DELETE on a disk image with 20 entries take **< 1 ms** (vs. ~2.7 s if both index types were combined under one virtual table — a pathological O(N) FTS scan per delete that would make "Reset DB" take hours at full scale).

2. **PRAGMA tuning per connection** — applied automatically by `_apply_scale_pragmas()`:
   - `cache_size = -262144` (256 MB) — keeps recently-touched pages hot in RAM. Default is ~2 MB which would mean every query hits disk on a 12 GB DB.
   - `mmap_size = 268435456` (256 MB) — memory-maps the read-hot pages so the FTS index (which is seek-heavy by design) doesn't pay `read()` syscall overhead per hit.
   - `temp_store = MEMORY` — keeps `GROUP BY` / `ORDER BY` scratch space in RAM instead of `/tmp` (where a slow USB stick or network mount would dominate the operation).
   - `synchronous = NORMAL` — trades a tiny bit of crash safety for 2-3× faster writes. With WAL+NORMAL only a power loss during a checkpoint can corrupt the DB; an OS crash leaves it consistent. For a re-scannable file catalog this is an obvious trade.

**Projected disk footprint** at 2M files + 40M disk entries: roughly **10-12 GB** total, dominated by the `disk_entries` table (~2.4 GB), its MD5 and name indexes (~1.5 GB and ~900 MB), and the `fts_entries` FTS5 trigram index (~1.6 GB). Plenty of room left on any 1 TB SSD; consider where you put the DB (`config/quopus_db.sqlite`) if you scan from a slower secondary disk — keeping the DB on your fastest SSD pays off.

**Maintenance commands** (Stats tab):
- **Vacuum** — runs `VACUUM` + FTS5 `optimize` (merges b-tree segments in both `fts_names` and `fts_entries`) + `PRAGMA optimize` (refreshes query-planner statistics). On a 12 GB DB this can take 1-2 minutes but only needs to run after large delete/rescan cycles. Status messages show the current step.
- **Clean Up Non-C64 Files** — for catalogs upgraded from a Quopus version that indexed everything, deletes file rows whose extension is outside the C64 filter list.
- **Reset Database** — drops every row, equivalent to deleting the .sqlite file and starting over. Requires confirmation.

**Schema migrations** are automatic on launch. Current version is v4 (FTS5-split for fast deletes). Upgrading a v3 DB to v4 rebuilds the FTS indexes from scratch — takes a couple of minutes on a 12 GB DB but only runs once.

### Viewers (built-in)
- **Text viewer** with ANSI / PETSCII / plain-text auto-detection
- **PETSCII viewer**: pixel-rendered to a QPixmap with QPainter — solid cell backgrounds, no font-padding "grid effect" between cells. C64 Pro Mono PUA pages used when available.
- **NFO viewer**: HTML-rendered with Cascadia Mono / Consolas / DejaVu Sans Mono so block-drawing chars (▀ ▄ █ ░ ▒ ▓) sit flush against each other. Negative letter-spacing eliminates pixel gaps between glyphs.
- **Hex viewer / editor** with side-by-side hex + ASCII columns. A **search bar** finds hex byte values (`A9 00 8D`) or text (case-insensitive); it scans the whole file in chunks, jumps to the page holding the match, highlights it in both columns, and Find next/prev wrap around. **Edit mode** (yellow `Edit` button toggles it on) lets you patch bytes directly in either column: type 0–9/a–f over a hex digit and the corresponding ASCII char to the right updates live; type a printable ASCII char (0x20–0x7e) over the right column and the hex pair to the left updates live. Layout chrome (offsets, column gaps) is protected — the cursor auto-snaps to the next editable position. Save (orange `Save` button) writes back via `'r+b'`; a one-shot `.bak` backup is created on first save per session. Backspace/Delete/Enter/Tab/paste/drag are all blocked in edit mode so you can't accidentally restructure the layout.
- **Image viewer** — PNG, JPG, GIF (animated), BMP, WEBP, TIF, SVG, ICO. Fit-to-window, 1:1, zoom, Ctrl+wheel
- **Retro GFX Viewer** — comprehensive retro graphics viewer for C64, Amiga, Atari, MSX, ZX Spectrum, Apple, NEC PC-88/98, SAM Coupé and more. Decodes 18 native C64 bitmap formats internally and 552+ additional formats via an optional RECOIL backend. See full details in the dedicated section below.
- **Archive viewer** — ZIP, TAR, TAR.GZ, TAR.BZ2, TAR.XZ, LHA/LZH, **RAR**, **GZ**. Browse without extracting (synthetic directory entries for LHA paths). Double-click files inside to view with the right viewer. Extract all / selected.
- **C64 6502 Disassembler / Editor / Assembler** — dual-pane code workbench for `.prg`, `.bin`, `.crt`, `.tap` files. Far more than a viewer: edit memory, write inline 6502 source with labels, compare original vs patched, and launch directly in your C64 emulator. (`.sid` files now open in the SID Player below — set the file association to `c64disasm` if you want to disassemble a SID's player code instead.)
- **TAP cassette toolkit** — for C64 `.tap` tape images: identify the loader, list every file on the tape, search the hex, and extract files as `.prg`. Powered by the bundled GPL TAPClean tool (all ~93 loader scanners) with a pure-Python fallback. See the dedicated section below.

  **Disassembly engine**
  - Linear-scan disassembly of all documented 6502 opcodes; unknown bytes shown as `.byte $XX`
  - **Optional illegal opcodes** (LAX, SAX, DCP, ISC, RLA, RRA, SLO, SRE, NOP variants, ANC, ALR, ARR, AXS, JAM, SHX, SHY, TAS, LAS, AHX, XAA) — toggle via the "Illegal opcodes" checkbox in the toolbar; illegal mnemonics are prefixed with `*` to make them visually distinct. Setting persists in `quopus.cfg`.
  - **Format-specific header handling**: PRG (2-byte load addr), SID (PSID/RSID header), CRT (cartridge image with CHIP packets), TAP (tape image)
  - **No truncation** — even very large files (32K+ ROMs, big demos with 30000+ instructions) are fully disassembled

  **Render cache** — for fast re-opens
  - First open of a file: disassemble + build render-data + write to `cache/disasm/<md5>.pkl`
  - Subsequent opens: cache lookup by file-bytes MD5 → load directly from disk → skip the heavy work
  - Cache key is `MD5(raw file bytes + show_illegal_flag + version_byte)` — different illegal-opcode states cache separately, file edits invalidate automatically (different bytes → different MD5)
  - Cache is opportunistic: failures are non-fatal, corrupt entries are deleted on next access
  - `[cache] hit/miss/wrote` lines on stderr for diagnosis
  - Typical numbers: 30000-line file goes from ~44 seconds first-open down to ~1.1 seconds on subsequent opens
  - Render-data cache is also held in-memory per pane; toggling between sides doesn't re-render

  **Render performance** — for files of any size
  - For files under ~5000 lines: full color formatting (addresses blue, bytes grey, opcodes orange, operands light grey, comments grey)
  - For larger files: color-formatting and anchor-name registration are skipped to keep apply-time under a second; click-jump links are still applied (sparse)
  - `jump_to(addr)` uses direct cursor positioning via `findBlockByNumber()` — no anchor-name lookup needed, target line snaps to the TOP of the viewport (not the bottom-anchor that `ensureCursorVisible()` produces)
  - "Rendering Memory Data, please wait..." overlay shown during the initial load

  **Dual-pane workflow**
  - LEFT pane = ORIGINAL (read-only)
  - RIGHT pane = EDIT COPY (auto-created as `<name>.edit.<ext>` next to the original on first open)
  - All edits go to the edit copy; the original is never modified
  - Per-pane mini-headers show what each pane contains

  **Click-to-jump navigation** — every absolute address in operands (`JMP $4550`, `JSR $FFD2`, `LDA $D020`, `STA $0400`, `LDX $1264`, branches) is a clickable hyperlink with pointing-hand cursor
  - Single-click on jump-target in LEFT pane → RIGHT pane scrolls to target (preview)
  - Double-click in LEFT pane → LEFT pane jumps to target (history pushed)
  - Single-click in RIGHT pane → LEFT pane preview
  - Double-click in RIGHT pane → RIGHT pane jumps
  - Right-double-click in either pane → Back (pop history)
  - Target line lands at the TOP of the viewport (not bottom)

  **Smart anchor resolution**: clicking `JMP $1234` works even when there's no instruction starting exactly at $1234 (e.g. JMP into the middle of an instruction) — falls back to the closest disassembled address before the target.

  **Independent panes**: Find/F3 only scrolls the side it was triggered on; the other pane is moved only by your scrollbar, mousewheel, link clicks, or the Sync button.

  **Find** (Ctrl+F per pane, F3 = next on the last-used pane)
  - Whitespace-tolerant: searching for `LDX $1264` matches the rendered `LDX    $1264` with multiple spaces
  - Case-insensitive
  - Two separate "Find Src" / "Find Edit" buttons — explicit pane targeting
  - Status bar reports which pane was found in (or not)

  **Edit dialog** (F2 or "Edit (F2)" toolbar button) — two tabs:
  - **Hex tab**: directly edit the bytes of the current instruction
  - **Assembly tab**: full multi-line 6502 source editor with **integrated assembler** (see below)

  **Edit address override**: the dialog has an "Edit address" field that defaults to the line under the cursor but can be changed to **any address**, including addresses past the end of the original file. The edit copy is automatically extended with `$00` padding to reach the target. Useful for adding new code at e.g. `$9000` when the original PRG ends at `$8000`.

  **Built-in 6502/65C02 mini-assembler** — native, no external toolchain. Three dialects supported (ACME, KickAssembler, 64tass) with ~98% coverage of real-world C64 demoscene/game sources.

  **CPUs**:
  - 6502 (all documented + optional illegal opcodes)
  - 65C02 (BRA, PHX/PHY/PLX/PLY, INC A / DEC A, STZ, TRB, TSB, BIT extensions, ORA/AND/EOR/ADC/STA/LDA/CMP/SBC `(zp)` indirect, JMP `(abs,X)`)
  - Rockwell/WDC bit operations: RMB0-7, SMB0-7, BBR0-7, BBS0-7
  - WDC: WAI, STP

  **Labels & symbols**:
  - Forward and backward references resolved in pass 2
  - `name:` colon-form, plus column-1 implicit labels (DASM/TASM-ish)
  - Anonymous labels: `+`, `-`, `++`, `--` for nearby unnamed targets (`bne -`, `jmp +`)
  - Equates: `SCREEN = $0400` / `SCREEN equ $0400` / `SCREEN .equ $0400`
  - Re-assignable variables: `.var x = 5` / `!set x = 5` / `x := 5`
  - `.weak NAME = value` (64tass) — silently overridden by a later non-weak definition

  **Origin**:
  - `*=$0801`, `*=$C000`, `.org $0801`, `.pc = $1000`
  - Backward `*=` truncates the output buffer (matches ACME `!binary` + relocate pattern)

  **Expressions**:
  - Arithmetic: `+ - * / %`
  - Bitwise: `& | ^ << >>`
  - Comparison: `== != < > <= >=` (return 1/0)
  - Logical: `&& ||`
  - Lo/hi byte: `<addr` / `>addr` or `lo(x)` / `hi(x)` / `bk(x)` (24-bit bank)
  - Current PC: `*`
  - Character literal: `'A'` returns ASCII code

  **Built-in functions**:
  - Math: `abs`, `min`, `max`, `floor`, `ceil`, `round`, `sqrt`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `log`, `exp`, `pow`, `mod`, `sgn`
  - Byte selection: `lo`/`low`, `hi`/`high`, `bk`/`bank`
  - String/list: `len("text")` or `len(a, b, c)`
  - Random: `random()`, `random(max)`, `random(min, max)`

  **Addressing modes** — auto-detected from the operand (immediate, zp, abs, zpx/zpy, abx/aby, izx, izy, izp, ind, iax, rel, zprel, acc, imp). Force width with 64tass-style overrides:
  - `lda @w$50` — force 3-byte absolute even when the value fits in zero page
  - `lda @b$1234` — force zero-page (truncated)

  **Macros** — three dialects, all supported:
  - ACME: `!macro NAME .arg1, .arg2 { body }` — call as `+NAME val1, val2`
  - KickAssembler: `.macro NAME(arg1, arg2) { body }` — call as `:NAME(val1, val2)`
  - 64tass: `.macro NAME / body / .endm` — call as `#NAME val1, val2`, body uses `\1`, `\2`, `\@`

  **Loops**:
  - ACME: `!for var, start, end { body }`
  - KickAssembler: `.for (var i=0; i<8; i++) { body }`
  - 64tass: `.for var=0, var<8, var+=1 / body / .next`
  - Repeat-N: `!rept N { body }` (ACME), `.rept N / body / .next` (64tass)

  **Conditionals** with constant evaluation:
  - `.if cond { body } else { body }`
  - `!if cond { body } else { body }` (ACME)
  - `.ifdef NAME` / `.ifndef NAME` — both branches kept conservatively

  **Functions** (KickAssembler / 64tass) — user-defined, callable in any expression:
  ```
  .function offset(row, col) {
      .return row * 40 + col
  }

  sta screen + offset(5, 10)        ; resolves to sta screen + 210
  ```

  **Namespaces** (KickAssembler) — labels get a dot-prefixed scope:
  ```
  .namespace io {
      border:    inc $d020
                 rts
      bg:        inc $d021
                 rts
  }
          jsr io.border       ; calls into the namespaced label
  ```
  Nested namespaces work too: `.namespace outer { .namespace inner { foo: ... } }` becomes `outer.inner.foo`.

  **Pseudo-commands** (KickAssembler) — custom mnemonics that look like opcodes:
  ```
  .pseudocommand setborder :col {
      lda #col
      sta $d020
  }
          setborder $05       ; expands inline to LDA #$05; STA $D020
  ```

  **Structs** (KickAssembler) — typed memory layouts:
  ```
  .struct Sprite {
      .byte x
      .byte y
      .word offset
  }
          .dstruct Sprite, 100, 50, $1234   ; emits 64 32 34 12
  ```

  **Compile-time messages**:
  - `.print expr [, expr...]` — echoes to stderr, useful for debugging values
  - `.assert cond, "msg"` — fails assembly with a clear error if cond is false
  - `.error "msg"` / `.cerror "msg"` — unconditional fail
  - `.warn "msg"` / `.cwarn "msg"` — non-fatal warning to stderr
  - `.errorif cond, "msg"` / `.warnif cond, "msg"` — conditional variants

  **String literals** with escape sequences:
  - `\n` → 0x0A, `\r` → 0x0D, `\t` → 0x09, `\0` → 0x00
  - `\\`, `\"`, `\'` — literal backslash/quote
  - `\xHH` — single byte from two hex digits

  **Data directives** — all three dialects:
  - 8-bit: `!byte` / `!by` / `!8` / `!08` / `.byte` / `.db` / `.by` / `.char` (signed)
  - 16-bit: `!word` / `!wo` / `!16` / `.word` / `.wo` / `.sint` (signed) / `.addr` / `.int`
  - 16-bit return-address: `.rta` (stores value-1, JSR/RTS table style)
  - 24-bit: `!24` / `.long` / `.lint`
  - 32-bit: `!32` / `.dword` / `.dw` / `.dint`
  - Text: `!text` / `!tx` / `.text` / `.asc` (uses current conversion table)
  - PETSCII text: `!pet` (a-z → A-Z)
  - C64 screencodes: `!scr` (`A`=$01, `B`=$02 …)
  - Raw text: `!raw`
  - Null-terminated: `.null "text"` (64tass)
  - Pascal-style length-prefixed: `.ptext "text"` (64tass)
  - Bit-shifted high-bit: `.shift "text"` (last char ORed with $80) / `.shiftl "text"` (left-shifted)
  - Conversion table: `!convtab pet|raw|scr` (alias `!ct`), `!ct_pet`, `!ct_raw`, `!ct_scr`
  - Fill: `!fill N [, value]` — N bytes with value (default $00); pattern fill via `!fill 8, [$55, $aa]` alternates bytes
  - Align: `!align AND, EQUAL [, value]` — pad until `(pc & AND) == EQUAL`; KickAss form `.align N` aligns to power-of-2
  - List literals: `.byte [1, 2, 3]` is equivalent to `.byte 1, 2, 3`

  **Includes**:
  - Source: `!source "file.asm"` / `!src "file.asm"` (ACME), `.include "file.asm"` (64tass), `#import "file.asm"` (KickAss)
  - Binary: `!binary "data.bin"` / `!bin "data.bin"` (ACME), `.binary "data.bin"` (64tass), `.importbinary "data.bin"` (KickAss) — all three argument orders supported (offset/length variations)

  **On error**: dialog stays open with your source preserved, error message shows line number and offending text — fix and re-Apply

  **Coverage**: passes typical C64 demoscene/game sources. Not implemented (sub-2% of real-world cases): KickAss `:foo()` macro return-values, `.section`/`.send` with linker output, `.cdef` custom char-encoding range maps.

  **Load .asm / Save .asm** buttons in the assembly tab let you load existing source files (`.asm`, `.s`, `.a`, `.txt`) into the editor or save your current source for later reuse.

  **Compare** ("Compare" toolbar button or workflow): byte-by-byte diff between LEFT (original) and RIGHT (edit copy)
  - Differing lines highlighted with red background (both sides have code, bytes differ) or orange (line exists on one side only)
  - Status bar shows `Diff 1/12 at $0820`
  - **F3 jumps to next diff** with both panes synchronized (unlike Find/F3 which only scrolls the active pane)
  - Compare highlights auto-clear when you edit bytes or toggle illegal opcodes

  **Run in C64 Emulator** (F5 or "Run in C64" toolbar button)
  - Configurable emulator path + arguments via the "Emu Config" dialog
  - Args support tokens: `{file}`, `{name}`, `{dir}` — paths with spaces stay intact as a single argument
  - Settings persist in `quopus.cfg` as `c64_emulator` and `c64_emulator_args`
  - Examples: VICE = `{file}`, VICE autostart = `-autostart {file}`, fullscreen = `{file} -fullscreen`
  - Emulator launched fully detached, Quopus stays responsive

  **Toolbar**: Top, Sync, Back L, Back R, Edit (F2), Run in C64 (F5), Emu Config, Compare, Find, Close
  **Hotkeys**: Ctrl+F (find), F3 (next find / next diff), F2 (edit), F5 (run), Home (top), Backspace (back), Esc (close)
- **Image viewer** — PNG, JPG, GIF (animated), BMP, WEBP, TIF, SVG, ICO. Fit-to-window, 1:1, zoom, Ctrl+wheel
- **AmigaGuide hypertext viewer** — `.guide`, `.hlp`, or any file beginning with `@DATABASE`
  - Clickable `@{"label" LINK "node"}` and `@{"label" GUIDE "file/node"}` links
  - Inline `@{b}/@{ub}` bold, `@{i}/@{ui}` italic, `@{u}/@{uu}` underline
  - Foreground colour tags `@{fg HIGHLIGHT}` etc., readable on the dark viewer background
  - Toolbar: Contents, Index, Help, Back, Forward, Prev, Next, Retrace, Find, Close
  - Hotkeys: Esc, Alt+Left/Right, Backspace, Home, F1, Ctrl+F
  - `SYSTEM`/`RX` links shown but never executed (security)

- **ProTracker-style Module Player** — `.mod`, `.xm`, `.s3m`, `.it`, `.mptm`, `.med`, `.mtm`, `.stm`, `.669`, `.okt` and other libopenmpt-supported tracker formats. Plays the file inline through your sound card on double-click; the UI is modeled directly on Amiga ProTracker 2.3D.

  **Engine**: libopenmpt via ctypes (no Python wrapper package needed - the system library or shipped DLL is loaded directly). Sample-accurate playback for everything libopenmpt understands: 4/6/8-channel MOD, 32-channel XM, all S3M effects, IT NNAs, MED OctaMED, etc.

  **Look** — Workbench-grey window with the classic blue title bar reading `Python ModPlayer V1.0 by lA-sTYLe`. Five LCD-style displays on the left (POSITION / PATTERN / LENGTH / SPEED / TEMPO) with green LED-segment digits on a sunken black panel. Pattern view on the right scrolls with the song; current row highlighted in white-on-blue. Sample list below in 1-3 columns (auto-fit) showing all 31 ProTracker samples in decimal numbering. ProTracker palette throughout: orange note text on black, grey-bevelled 3D buttons, blue title bar.

  **Animations** (live during playback):
  - LCDs count: position 00→ song length, pattern advances, speed/tempo update with `Fxx` effects
  - Pattern view scrolls row-by-row, beat rows (every 4th) highlighted brighter
  - Per-channel VU meters segmented green→yellow→red with peak-hold + decay, driven by `openmpt_module_get_current_channel_vu_mono()`
  - **Master spectrum analyzer** to the right of the pattern view — 10-band ISO equalizer (31/62/125/250/500/1k/2k/4k/8k/16k Hz). Computed from the master mix via Hann-windowed FFT, energy summed per band, mapped −60..0 dB → bar height 0..1, with peak-hold caps. Replaces the legacy stereo VU meter (VU-meter code is kept in `vumeter.py` for reference but is no longer wired into the player).
  - DISK LED blinks red on every row change (sample-trigger activity indicator)
  - Time counter `mm:ss / mm:ss`, position slider tracks song progress

  **Transport**: PLAY SONG / PAUSE / STOP / `<<` (back 5s) / `>>` (forward 5s). Position slider for direct seeking. Volume slider 0-100% with thread-safe live application.

  **Hotkeys**: Space (Play/Pause), Esc (Close, stops audio cleanly), Left/Right (5-second skip).

  **Library setup**:
  - **Linux**: `apt install libopenmpt0` (the modern `libopenmpt0t64` package on Ubuntu 24.04+ also works)
  - **macOS**: `brew install libopenmpt`
  - **Windows**: download the libopenmpt Windows release ZIP (NOT `*-dev.zip`) from <https://lib.openmpt.org/files/libopenmpt/bin/>, navigate to `bin/amd64/` for 64-bit Python (or `bin/x86/` for 32-bit), copy these 5 files into the same directory as `quopus.py`:
    - `libopenmpt.dll`
    - `openmpt-mpg123.dll`
    - `openmpt-ogg.dll`
    - `openmpt-vorbis.dll`
    - `openmpt-zlib.dll`

    Older releases use names like `libopenmpt-0.dll` and `libmpg123-0.dll` — both naming conventions are supported. The `.lib` files in the ZIP are static import libraries for C/C++ linkers and cannot be loaded by Python — ignore them.

  Plus `pip install sounddevice numpy` (required for audio output).

  If the library is not found, a detailed diagnostic dialog appears with directory listings, file sizes (so you can spot 32/64-bit mismatches: ~1.5 MB = x86, ~1.7 MB = x64), and the full Win32 loader error code.

- **Real-time SID Player with GoatTracker-style display** — `.sid` files (PSID / RSID, versions 1-4). Echte libsidplayfp-Emulation, kein WAV-Pre-Render. Plays through libsidplayfp via a custom C wrapper for true real-time emulation: instant subsong switching, live voice mute, per-physical-voice oscilloscopes. Multi-SID support: 1SID = 3 voices, 2SID = 6 voices, 3SID = 9 voices.

  **Engine architecture** — two libsidplayfp instances run in parallel ("Option B" parallel engine design):
  - **Audio engine** (`_engine`): untouched master mix, renders into the sounddevice output stream. Voice mute is only applied here when the user clicks a MUTE checkbox; otherwise this engine renders all voices for the audio output, exactly like a normal SID player.
  - **Visualization engine** (`_vis_engine`): separate libsidplayfp handle, loaded with the same tune and the same subsong. Per-voice waveforms are harvested by muting all voices except one (`play_chip_voice_only(num_frames, chip, voice)`) and rendering a short solo pass.

  Both engines render the same total number of frames per audio loop iteration, so they stay synchronized indefinitely. The synchronization math:
  ```
  per loop:
    audio: play(BLOCK_FRAMES = 1024)              # 1024 frames consumed
    every 2nd loop:
      vis: for chip in 0..n_sids:
             for voice in 0..2:
               play_chip_voice_only(256, ...)      # 256 frames per voice
      vis: play(2*BLOCK_FRAMES - n_voices*256)    # discard render, catch up
  ```

  For a 1SID tune with 3 voices: vis consumes `3*256 + (2048 - 768) = 2048` frames per cycle, exactly matching the 2 audio blocks of `2*1024 = 2048`. For 2SID: `6*256 + (2048 - 1536) = 2048`. For 3SID: `9*256 + (2048 - 2304)` = oops — 3SID needs more than 2 audio blocks per vis pass to fit, handled gracefully by clamping the discard to zero. CPU cost: ~25% extra over single-engine for 1SID, up to ~40% for 3SID. The vis engine NEVER touches the audio engine's state, so this can't introduce the speed/pitch bugs that a single-engine mute-trick approach has.

  **Why two engines instead of one** — earlier prototypes muted voices on the audio engine itself between blocks to harvest per-voice waveforms. This corrupted the audio engine state because libsidplayfp's `play()` is destructive: every call advances the internal 6502 emulation by the requested number of frames. Per-voice render passes consumed frames that the audio output never received, making playback run too fast (~10-20% sharp) with rhythm artifacts. The parallel-engine architecture eliminates this entirely — the audio path is mathematically identical to a no-visualization SID player.

  **Look** — pure black background, Amiga-blue title bar reading `Python SidPlayer V1.0 by lA-sTYLe`. PSID metadata displayed prominently: title in big yellow text, author and release line, plus a tech line showing PSID/RSID magic + version + chip model. GoatTracker palette: cyan note text, yellow instrument numbers, green command column, magenta/pink current-row highlight.

  **Layout adapts to chip count**:
  - 1SID: single horizontal row of 3 oscilloscopes labelled `VOICE 1/2/3`
  - 2SID: two rows of 3 oscilloscopes (`V1-V3 (SID1)` over `V4-V6 (SID2)`)
  - 3SID: three rows (`SID1` / `SID2` / `SID3`)

  Plus a **master 10-band spectrum analyzer** to the right of the pattern view — same setup as the MOD player (31/62/125/250/500/1k/2k/4k/8k/16k Hz, Hann-windowed FFT, peak-hold). Computed from the audio mix so it works regardless of how many SID chips the tune uses.

  **Pattern view** — adaptive width per voice count:
  - 1SID (3 voices): full GoatTracker format `C-4 0F 0820` (note + instrument + command + param)
  - 2SID (6 voices): compact `C-4 0F` (note + instrument only)
  - 3SID (9 voices): extra-compact

  Notes detected via FFT pitch detection on each voice's solo render output, not from SID register reads. Pattern scrolls one row per visualization update; current row highlighted in pink. Beat rows (every 4th) shown in bright white/yellow, off-rows in dim cyan.

  **Voice mute checkboxes** — one per physical voice, grouped by SID chip:
  - 1SID: `MUTE: V1 V2 V3`
  - 2SID: `MUTE: SID1: V1 V2 V3   SID2: V4 V5 V6`
  - 3SID: `MUTE: SID1: V1 V2 V3   SID2: V4 V5 V6   SID3: V7 V8 V9`

  Mutes apply to BOTH engines so the audio output and the oscilloscope stay in sync visually.

  **Subsong selection**:
  - SUBSONG spinbox shows `current of total`
  - Subsong switching is INSTANT - no re-render wait (this is the whole point of running the engine in real time)
  - Hotkeys: Right / N / + go forward, Left / P / - go backward, all clamped at 1..num_subsongs

  **Transport**: PLAY / PAUSE / STOP, time display `mm:ss / mm:ss` when a song length is known (HVSC database), `mm:ss` alone otherwise. Volume slider.

  **HVSC Songlengths database (optional)** — drop an HVSC `Songlengths.md5` file into `quopus_commander/config/Songlengths.md5` and the player will:
  - Display the real song duration in the time label as `00:42 / 02:15`
  - Auto-advance shuffle play exactly when the current subsong's real end is reached, instead of the 180-second fallback timeout

  The file is the standard HVSC `Songlengths.md5` (download from <https://www.hvsc.de/> or get it as part of any HVSC archive). Format is one line per SID:
  ```
  abc123def456...=2:30
  abc123def456...=1:35 2:10 0:45    (multi-subsong)
  ```
  We use libsidplayfp's `createMD5New()` (HVSC #68+ format) for lookup, falling back to `createMD5()` (legacy format) if no match. Duration data is parsed once per app session and cached in memory; ~60k entries fit in well under 10 MB. If the file isn't installed or a tune isn't in the database, the player simply omits the duration display and uses the 180-second fallback for shuffle skip.

  **Hotkeys**: Space (Play/Pause), Esc (Close), plus the subsong navigation set above.

  **Library setup** — Quopus ships precompiled wrappers for both Linux and Windows x86-64:
  - **Linux**: `libsidwrapper.so` is included (~22 KB). Requires the system libsidplayfp: `apt install libsidplayfp-dev sidplayfp` (Ubuntu/Debian).
  - **Windows x86-64**: `sidwrapper.dll` is included (~3 MB). It's **fully statically linked** — libsidplayfp itself, libstilview, libstdc++, libgcc, and pthread are all baked into the DLL. The only runtime dependencies are KERNEL32.dll and msvcrt.dll which exist on every Windows install. **No extra DLL setup needed** — drop the file next to `quopus.py` and it works.

    Source `sidwrapper.cpp` is also included alongside the DLL with full build instructions for MinGW-w64, MSYS2, and MSVC if you want to rebuild from source.
  - **macOS**: not pre-built. Build it yourself: `clang++ -O2 -fPIC -shared sidwrapper.cpp -o libsidwrapper.dylib -lsidplayfp -lstdc++` after `brew install libsidplayfp`.

  Plus `pip install sounddevice numpy`. If the wrapper is not found, a detailed diagnostic shows the searched paths, file sizes, and Python's reported architecture (so 32/64-bit mismatches are obvious).

- **Shuffle play mode** — point either player at a directory tree and play random tracks from it. Three entry points:
  - **From the lister right-click menu**: "Shuffle play SIDs from here" or "Shuffle play Modules from here" — recursively scans the current directory
  - **As a button action**: bind `shuffle_sids` or `shuffle_mods` to a toolbar button (uses the active lister's current directory)
  - **From inside an already-open player**: click the **SHUFFLE** button in the transport row to shuffle-play everything from the current track's parent directory (recursive). No folder picker — the assumption is you want to shuffle whatever was near the file you opened. Useful when you double-clicked one SID and now want random play from its containing folder without closing and reopening anything.

  **How it works**:
  - The directory walk runs in a background `ShuffleScanner` thread. A progress dialog shows "Found: 1247" etc. so you know it's working through big trees. Cap is 50,000 files to keep memory reasonable on enormous archives.
  - Once the scan finishes, the file list is shuffled with `random.shuffle()` and the appropriate player opens with the first random track plus the full playlist.
  - Both players grow two extra transport buttons: `|<<` (previous) and `>>|` (next). Title bar shows position: `track.sid  (47/1247 - SHUFFLE)`.
  - **Auto-advance**: the MOD player advances automatically when the song finishes (libopenmpt reports the end). The SID player uses **HVSC's Songlengths.md5** database for accurate per-subsong durations (drop the file in `config/Songlengths.md5`); when the song's real end is reached, the next track plays. If the file isn't installed or the current SID isn't in the database, falls back to a 180-second timeout. The time display shows `MM:SS / MM:SS` when a duration is known, otherwise just the running time.
  - **Hotkeys for navigation** (in both players): `Ctrl+Right`/`Ctrl+Left` for next/prev track. The MOD player also has plain `N`/`P` since arrow keys are seek; the SID player uses `Ctrl+N`/`Ctrl+P` since plain `N`/`P` are subsong navigation in that player.
  - **Skip-on-error**: if a file in the playlist won't load (corrupted, wrong format, etc.) the player skips silently to the next track instead of dying.

- **Multi-SID parallel playback** — for productions like _The Tuneful Eight_ (CSDb release 182735) where 2-4 separate SID files form one composition that's meant to play simultaneously. Tag 2-4 SID files in the lister, then **right-click → "▶ Play as multi-SID"** (the entry only appears when 2-4 SIDs are tagged and nothing else). The player opens in multi-mode and renders all tunes in lockstep:
  - **Audio mixing**: each tune gets its own libsidplayfp engine. Outputs are averaged (not summed) so the level matches a single-tune playback regardless of how many tunes are loaded — no clipping when all four tunes hit a loud transient at the same time.
  - **Synchronisation**: all engines start together and are advanced in identical block sizes. Subsong-select is broadcast to every engine (with per-engine clamping when their subsong counts differ).
  - **Layout**: one row per tune. Each row shows `TUNE N` label + filename (wrapped onto multiple lines if long, breaking at `_`/`-`/`.` separators) + a **per-tune 10-band spectrum analyzer**. Four tunes = four spectrum analyzers stacked vertically — no per-voice oscilloscopes in multi mode (24 simultaneous scopes was too CPU-heavy and the spectrum analyzer is more useful for telling tunes apart by frequency content anyway).
  - **Per-tune mute checkboxes** instead of per-voice — at 24 voices, individual voice mute is too noisy. The MUTE: row gets one box per tune so you can A/B which file contributes what.
  - **Auto-restart at song end**: the longest HVSC duration across the four tunes is used as the trigger; all four restart simultaneously when that one expires (with the standard 2-second grace).
  - **VIS default ON**: spectrum analyzers are cheap (one FFT per audio block, ~negligible compared to libsidplayfp itself), so the visualiser can stay on even in multi mode.
  - **Title bar** shows `MULTI-SID (4 tunes)` instead of a single filename.
  - **Header** shows per-file PSID metadata: `Title by Author (Year)  ·  N×SID` for each tune.
  - **Triggered ONLY via right-click** "▶ Play as multi-SID" - never via plain double-click. Double-click on a tagged SID always opens the single-SID player to avoid surprises during regular browsing.

### Retro GFX Viewer

Comprehensive retro graphics viewer for C64, Amiga, Atari, MSX, ZX Spectrum, Apple, NEC PC-88/98, SAM Coupé and more. Decodes 18 native C64 bitmap formats internally and 552+ additional formats via an optional RECOIL backend, with a built-in charset viewer and 8×8 pixel-grid char editor.

**Native C64 bitmap decoders** — no external tools needed, all built into `quopus_lib/retro_gfx_decoders.py`. Verified against the RECOIL test sample set:
- **Koala Painter** (`.kla`, `.koa`) — 10003-byte multicolor 320×200
- **Hi-Res Bitmap** (`.hbm`, `.hir`, `.hpi`, `.fgs`) — raw 8000-byte hires
- **Art Studio** (`.aas`, `.art`) — hires bitmap + screen RAM
- **Advanced Art Studio** (`.ocp`, `.mpi`, `.mpic`) — multicolor with separate color RAM (verified with Navy Seals samples)
- **Doodle** (`.dd`, `.ddp`, `.jj`) — 9218-byte hires
- **Amica Paint** (`.ami`) — RLE-compressed hires/multicolor (verified with Kennedy Approach)
- **Drazpaint** (`.drp`, `.drz`) — 10049-byte multicolor with explicit layout: 0..1000 screen RAM, 1024..2024 color RAM, 2048..10048 bitmap, 10048 bg color (verified with TEST.DRZ)
- **Drazlace** (`.drl`, `.dlp`) — interlaced variant
- **CDU Paint** (`.cdu`) — Compunet Doodle (verified with Scooby Doo samples)
- **Interpaint Hires** (`.iph`) / **Interpaint MC** (`.ipt`)
- **FLI / FLI Designer / FLI Graph** (`.fli`, `.flg`, `.bml`, `.fd2`, `.fed`) — verified against CPU.FLI sample, layout: $3C00..$5BFF = 8 screen RAMs (1024B each), $5C00..$5FE7 = color RAM, $6000..$7F3F = bitmap, bg=0 constant
- **AFLI** (`.afl`) — Advanced FLI (hires with FLI banks)
- **IFLI / Funpaint / Gunpaint** (`.ifl`, `.fun`, `.fp2`, `.gun`)
- **BFLI / Big FLI** (`.bfl`, `.bfli`)
- **Vidcom 64** (`.vid`)
- **Image System Hires/MC** (`.ish`, `.ism`)
- **Interlace Hires Editor** (`.ihe`)
- **Hires Interlace** (`.hlf`, `.hie`)

**RECOIL backend** — drop `recoil2png` (or `recoil2png.exe` on Windows) from https://recoil.sourceforge.net/ into the `<quopus>/external/` directory and Quopus picks it up automatically (no PATH or config changes needed). With the backend installed, the Retro GFX Viewer can decode 552+ additional formats including:
- **Atari 8-bit** (143 formats: MIC, MCP, RIP, ANI, AP3, APC, APV, BBG, BG9, CCI, CHR, CIN, CPR, DGP, DGU, ESC, FNT, FWA, GHG, GR8, GR9, HIP, HPM, ICE, ICN, IGE, ILC, IST, KOA, LDM, LUM, MCH, MCC, MGP, MIC, NLQ, PIC, PLA, PMD, PZM, RGB, RIP, SHC, SHP, SXS, TIP, VZI, ...)
- **Atari ST/STE/TT/Falcon** (~120 formats: NEO, PI1, PI2, PI3, PC1, PC2, PC3, DEGAS, TNY, CA1, CA2, CA3, ESM, FNT, ICN, IFF, IIM, IMG, GFB, GFA, ART, CRG, CPT, JPG, MPP, NLQ, PCS, PNT, RGB, SPC, SPS, SPU, STC, TG1, TNY, ...)
- **Amiga** ILBM/IFF/HAM/HAM-E/Sliced HAM, DCTV, ACBM, RGB8, RGBN
- **Apple II/IIe/IIGS/Macintosh** (HGR, DHR, A2R, BMC, BSL, FNT, MAC, ...)
- **MSX/MSX2/MSX2+** (35 formats: SC2, SC4, SC5, SC6, SC7, SC8, SCA, SCC, SR5, SR7, SR8, ...)
- **ZX Spectrum/Profi/Evolution/Next** (26 formats: SCR, ATR, MC, GIG, NXI, ZX0, ZXI, ...)
- **NEC PC-80/88/98** (B7G, CG2, CGM, P8M, P8C, ...)
- **SAM Coupé**, **BBC Micro**, **Oric**, **TRS-80**, **Sharp X1/X68000**, **Robotron KC85**, **Spectravideo**, **NEC PC-FX**, ... and dozens more

Full format list at https://recoil.sourceforge.net/formats.html. The launcher's `recoil2png path...` Configure button lets you point to an alternative binary location if needed; `quopus.cfg` remembers the path.

**Lookup order** for the RECOIL binary:
1. Explicit path in `quopus.cfg` (set via the Configure button)
2. `<quopus>/external/recoil2png[.exe]` — drop-in portable location
3. System `PATH`

**File-association defaults** — the `retrogfx` internal type is pre-mapped to 30+ extensions covering both native and RECOIL-handled formats: `.iff .ilbm .lbm .ham .sham .acbm .pi1 .pi2 .pi3 .neo .degas .pc1 .pc2 .pc3 .scr .sc2 .sc5 .sc7 .sc8 .aas .ocp .fli .afl .ifl .iph .ipt .drp .drz .drl .dlp .ami .dd .jj .gun .fun .fp2 .cdu .bfli .hed .vid` plus all C64 native extensions listed above. Right-click → Configure to add more.

**Folder browser mode** — click "Open folder..." in the launcher to scan a directory for all viewable graphics files. The first file opens immediately and you can step through the entire folder with the keyboard:
- **→** / **←** — next / previous file (+1 / -1)
- **↓** / **↑** — +10 / -10 files
- **PgDn** / **PgUp** — same as ↓/↑
- **Home** / **End** — first / last file in folder
- Auto-switches between native decoder and RECOIL backend depending on the current file's format
- Status bar shows `File 5/1028 in /your/folder` for orientation
- Works for both `BitmapViewer` (native C64) and `RecoilViewer` (RECOIL backend) via the shared `FolderBrowserMixin`

**Launcher** — when Quopus doesn't know whether a file should go to a specific native decoder or to RECOIL, the launcher dialog (`RetroGfxLauncherDialog`) appears with: file info, format guess based on extension, "Open with native decoder" + "Open with RECOIL" buttons, and the Configure button for `recoil2png`. Files with unambiguous extensions (e.g. `.koa`) skip the launcher and go straight to the right viewer.

### Charset viewer & editor

Loads any 2KB or 4KB C64 charset file (`.chr`, `.fnt`, `.64c`, `.bin`, `.art`) and renders all 256 characters in a 16×16 grid with C64-accurate colors. Single-click selects a char (gold highlight) and shows its 8-byte hex pattern. Double-click opens the **Char Editor**.

**Live text preview** — type a string in the input field and see it rendered with the loaded charset (so you can preview your custom font in context, not just one char at a time). FG/BG colors selectable from the C64 palette.

**Char Editor** — pixel-grid editor for individual characters:
- 8×8 grid with 32-pixel cells for easy clicking
- Mouse: **left-click** toggles, **left-drag** paints (set), **right-click/drag** clears
- Live previews at 1:1, 2×, and 4× scale alongside the editor — see exactly how the char will render on a C64 screen
- Hex-byte readout updates live as you edit, with **modified** indicator
- **Transforms**: Mirror H, Mirror V, Rotate 90° CW, Rotate 90° CCW, Rotate 180°
- **Shift**: ↑, ↓, ←, → buttons that rotate pixel rows/columns with wrap-around
- **Clear all** / **Fill all** / **Invert** / **Revert** to original
- **Copy/Paste** the current char as a hex sequence through the system clipboard; paste accepts multiple formats: `18 24 42`, `$18,$24,$42`, `0x18 0x24 0x42 0x66`
- **Apply** commits the edit to the charset (Undo-able), **Cancel** discards

**Undo/Redo** in the charset viewer — every char edit pushes a snapshot to a history stack (max 100 entries). Standard shortcuts:
- **Ctrl+Z** — Undo
- **Ctrl+Y** or **Ctrl+Shift+Z** — Redo
- Toolbar `⟲ Undo` and `⟳ Redo` buttons with enabled-state reflecting the current history position

**Save with auto-backup** — "Save charset..." preserves the original 2-byte load-address prefix (C64-typical `.prg` format). On overwrite:
- First time: rename existing file to `<file>.bak`
- Second time or later (if `.bak` already exists): timestamped `<file>.bak.YYYYMMDD_HHMMSS`
- The original is never silently overwritten without a backup. Useful when iterating on custom fonts without losing the previous version.

**PNG → Char Sequence converter** — "PNG → seq..." button in the Charset Viewer toolbar. Converts an arbitrary PNG (BMP, JPG also accepted) into a sequence of char codes from the current charset:
- Adjustable threshold + Invert toggle
- Max width / max height clip (default 320×200 = full C64 screen)
- For each 8×8 block of the thresholded source: Hamming-distance match against all 256 chars in the current charset, closest wins
- **Side-by-side preview**: source (thresholded) vs rendered (each char-cell replaced with the matched charset glyph)
- **Output formats**: Hex, Hex with `$` prefix, Decimal, ASM `.byte` directives, BASIC `DATA` lines with auto line-numbering, raw bytes
- **Save as** `.seq` (raw), `.txt`, `.asm`, `.bas`, or preview `.png`
- **Copy** the formatted output to the clipboard for paste-into-editor workflows

### CBM disk image editor (D64/D71/D81)

Browse, edit, validate, and extract from Commodore disk images. Pixel-accurate C64-style directory rendering using the C64 Pro Mono font with proper KERNAL-style header reverse-video, multi-line lister, 1541 BAM-aware free-block display.

**Operations** (all reachable from the toolbar):
- **Extract All** / **Extract Selected** to the current lister directory
- **Save as ZipCode** — write the disk image out as a classic ZipCode 1!/2!/3!/4! sequence
- **`+`** Add file from local filesystem (PRG/SEQ/USR), with type selector
- **`-`** Delete file (`*DEL`-marks; can be undone via Validate if no BAM corruption occurred)
- **`Aa`** Edit name in place (PETSCII glyph picker dialog)
- **Type-change**: PRG ↔ SEQ ↔ USR ↔ DEL ↔ REL
- **Lock** (set type bit 7) / **Splat** (clear it back) / **BFree** (free unused blocks in BAM)
- **Validate** — full disk-image validation with BAM reconstruction, mirrors the 1541 `V0:` command

**Separator Editor** (`Sep+` button) — the classic "Cracker scene" filename art editor for inserting decorative 16-PETSCII-byte separator lines between directory entries:
- 16-PETSCII-byte buffer with cursor-based insert/overwrite mode
- Live preview rendering matches the actual directory display (same code path as `render_directory_to_pixmap`)
- **Glyph picker**: 16×16 grid showing all 256 PETSCII codepoints in the current charset (upper/lower switchable). Each cell is a real rendered glyph, not a font character — works even if C64 Pro Mono isn't installed
- **Quick-fill buttons**: Dashes (`-`), Equals (`=`), Stars (`*`), Blocks ($A0), HLine ($C3), VBar ($DD), Diamond ($5A), Heart ($53), Spade ($41), Clear
- **Sample separators**: 25+ pre-built classic patterns (DirMaster-style) selectable from a dropdown — `=-=-=-=-=-=-=-=`, `*+*+*+*+*+*+*+*`, fancy diamond/heart/star patterns, the classic "------" line, etc.
- **PNG... import button** — import any PNG image and convert it to a 16-char separator:
  - The image is scaled to 128×8 pixels (16 chars × 8 pixels each), then each 8×8 block is matched to the closest of the 256 PETSCII glyphs using Hamming distance
  - **Fit modes**: Stretch (ignore aspect, force to 128×8), Fit horizontal (preserve aspect, scale to 8 high, pad with BG), Crop center (proportional scale + center crop to 128×8)
  - **Auto threshold** via Otsu's method (default ON) — analyzes the source histogram and picks the optimal threshold automatically. Toggle off for manual control via the spinner
  - **Invert** toggle for white-on-black source images
  - **Dual preview**: "Font path" shows what the directory listing will render via the C64 Pro Mono font, "ROM path" shows the matched glyphs rendered directly from the bundled `c64_chargen.bin` (4KB original VICE chargen-906143-02.bin) so you can spot any font-vs-ROM discrepancies
  - **Apply to separator** writes the 16 bytes into the editor buffer
  - Works best with horizontally-oriented banners (~512×16 or wider). Quadratic logos lose detail when squeezed into 16:1 aspect ratio — that's a fundamental format constraint, not a bug

**Inline file preview pane** — the disk dialog has a split layout: directory tree on the left, live preview pane on the right. Click any file in the directory and the preview pane shows what it actually is — no extraction required:

- **SEQ / USR files** → rendered as PETSCII text with full colour interpretation ($05/$1C/$9E/$9F PETSCII colour codes, reverse-video toggles, charset switches). The rendering uses the same module-level `render_petscii_grid_to_pixmap()` function as the rest of Quopus — pixel-perfect at any zoom level (DPR=1, anti-aliasing off, `setPixelSize+2` to prevent seam artifacts, reverse done via fg/bg swap rather than PUA codepoints).
- **PRG files (graphics)** → automatically detected and decoded inline. Detection uses load-address heuristics plus filename-prefix conventions used in BBS distribution:
  - `[B]NAME` prefix → Amica/Botticelli Paint
  - `[K]NAME` or heart-glyph ($53/$D3) prefix → Koala Paint
  - `[A]NAME` prefix → Advanced Art Studio
  - `[D]NAME` prefix → Doodle
  - `[H]NAME` prefix → Hires Bitmap
  - Load-address rules for unprefixed files: $6000 → Koala, $2000 → AAS / Art Studio, $5C00 → Doodle, $4000 → Amica
- **Two-file selection** → attempts logo+charset rendering. The bigger file (~2KB) is taken as a custom character set, the smaller as screen data, and they're composited into a bitmap on the fly — the way BBS art was distributed and viewed on the original machine.
- **PRG files (other)** → 8-line hex peek with PETSCII sidebar so you can identify SID dumps, charsets, sprites, BASIC programs by their signature bytes
- **DEL / REL** → "No preview" placeholder

A format label above the preview pane tells you what was detected (`Koala`, `Amica Paint`, `Logo+Charset`, etc.) so you know whether to trust the rendering. Click anywhere on the preview to open a fullscreen view that scales with the window; Esc or double-click closes it. The fullscreen window uses FastTransformation for crisp pixel-doubling at integer zoom levels.

**Save as PNG** — a button under the preview pane (and a matching button in the fullscreen dialog) writes the current preview to a PNG file. Rendered at 2× nearest-neighbor scaling so C64 fat pixels stay sharp at any zoom. Filename is suggested from the PETSCII title (sanitised for filesystem-safe chars), the last-used target directory is remembered per dialog. Disabled (greyed out) when no graphical preview is active (e.g. on REL / DEL files or when only the hex peek is showing). Useful for capturing demo previews for catalog screenshots, BBS file lists, social posts.

**Double-click actions** — right-click or double-click on a PRG file in the directory tree shows an action menu:
- **Extract '...'** — extract to the active lister directory
- **Run '...' on U64** — send the PRG directly to a configured Ultimate-64 via HTTP. If no U64 is configured, a dialog offers a "Configure now" button that opens the device-config dialog inline — you can add the U64's IP, click OK, and the Run action picks up from where it left off
- **Run '...' in VICE** — extract to a temp PRG and launch the configured VICE emulator

### TAP cassette toolkit
A dedicated workbench for C64 `.tap` cassette images — identify the loader, list the files on the tape, view/search their bytes, and extract them as `.prg`. Open it by double-clicking a `.tap` file (set the file association to `tap_toolkit`) or via the "TAP cassette toolkit" action.

**100% accurate via bundled TAPClean** — rather than approximating the dozens of commercial tape loaders, Quopus bundles the GPL **TAPClean** source (the reference C64/VIC20 tape preservation tool, based on Final TAP 2.76 by Subchrist Software, maintained by the TC Team) under `external/tapclean/`. On first use Quopus compiles the binary automatically (needs `gcc`/`cc` + `make`) and calls it for loader identification and file extraction. This gives the same results as running TAPClean by hand: all ~93 loader scanners (Ocean/Imagine, Novaload, Freeload, Turbotape 250, Visiload, Pavloda, System 3/IK, US Gold, Rack-It, Bleepload, Palace, Cyberload, and many more), correct file names, load/end addresses, checksums and CRC32s.

- **Build / fallback**: the binary is built once and cached. On Windows without a compiler you can drop a prebuilt `tapclean.exe` into `external/tapclean/src/`. If TAPClean can't be built or run, Quopus falls back to a built-in pure-Python analyzer (`tap_analyzer.py`) that handles the CBM ROM loader plus the most common turbo loaders — not as complete, but no compiler required.

**Block / file list** — the left pane lists every file found on the tape: the CBM ROM boot file(s) and each turbo-loaded part, with name, load–end address and size. Green = checksum OK, red = read errors. For a multi-stage game like R-Type Side 2 you'll see all 50 sub-files (M1–M8, G1–G8, S1–S8, …); a typical Ocean tape like Cobra lists 300+ blocks.

**Tabs on the right:**
- **Hex / ASCII** — side-by-side hex + ASCII dump of the selected block, with a **search bar**: type hex byte values (`A9 00 8D`, `A9008D`, or `0xA9 0x00`) with the **Hex** box ticked, or plain text (`LOAD`, `BOOT`) with it unticked. Text search is case-insensitive; hex is exact. Find next / Find prev wrap around, matches are highlighted yellow in both columns and scrolled into view.
- **Histogram** — pulse-width distribution; the CBM short/medium/long bands (384/528/688 cycles) are colour-coded so you can see at a glance whether a tape is CBM-only or carries a turbo loader.
- **Waveform** — the raw pulse stream (peak-per-pixel-column downsampling so even a 700 KB tape draws instantly).
- **Tape info** — TAP version, platform (C64/VIC20/C16), video standard + clock, pulse count, duration, detected loaders.
- **TAPClean report** — the full `tcreport.txt` (loader ID, recognition %, per-file detail, the five-part PASS/FAIL test suite). Saveable to a text file via **Save report**.

**Toolbar actions:**
- **Extract as PRG…** — save the selected block as a single `.prg` (2-byte LE load address + data).
- **Extract all files…** — dump every file on the tape into a `<tapename>_prg/` folder, using TAPClean's naming convention `<seq> (<start>-<end>) [name].prg` (with `BAD` appended on read errors). When TAPClean ran, these are its exact PRGs.
- **Run TAP in emulator** — hand the whole `.tap` to the configured C64 emulator (VICE autostarts a tape image directly, doing the real tape decode).
- **Run file on U64** — send a selected file as a PRG to a configured Ultimate-64.
- **Save report** / **Clean / optimize** / **Export WAV** — write the report, snap pulses to ideal widths and write a cleaned v1 TAP, or render the tape to a square-wave WAV.

### Ultimate 64 video stream viewer (Alt+U)
Receives the live VIC video + SID audio stream from an [Ultimate 64](https://ultimate64.com) over the network and displays it in a window. Modeled after [u64view](https://github.com/DusteDdk/u64view) (the C original) and the .NET-based [TSB U64 Streamer](https://tsb.space/sdm_downloads/u64streamer/) — 50 fps PAL video, 48 kHz stereo audio, low latency, with bidirectional control.

**To use**: enable the data streams in the U64's menu (F5 → Data Streams → enable Video Stream + Audio Stream + Web Remote Control). The Ultimate must be on firmware 3.11 or later for the REST API path to work.

**Open the streamer** via `Alt+U`, the `u64view` action on a button, or right-click menu. The first time, hit **Config...** to set the U64's IP address. Defaults match the U64 firmware: video UDP 11000, audio UDP 11001, telnet TCP 23, HTTP TCP 80.

#### Toolbar controls
- **Config...** — full settings dialog: host/IP, all four ports, optional network password (firmware 3.12+ feature, sent as `X-Password` header), `Video only` toggle (skip audio entirely), `Always on top` toggle. Restore-defaults button resets ports to firmware values but keeps host + password + toggles.
- **Start** — tells the U64 to begin streaming. Two paths, tried in order:
  1. **Modern REST API** (firmware ≥ 3.11): `PUT /v1/streams/video:start` + `:audio:start` over HTTP. One call each, no timing-sensitive byte sequences. Auto-detects the local IP that reaches the U64 so the firmware knows where to send packets.
  2. **Telnet menu navigation** (fallback): sends `F5 + 8×Down + 3×Enter` to TCP port 23, byte-at-a-time with 1ms delays — same trick u64view uses. Used when REST fails (e.g. HTTP service disabled, old firmware, or wrong network password).
- **Stop** — REST `PUT :stop` first, telnet stop sequence as fallback. Always shuts down our UDP receivers cleanly.
- **Reset** — sends a soft reset to the C64.
- **Scale 1x/2x/3x/4x** — sets the initial window size on open. After that the window is **fully resizable**: drag any edge and the picture scales to fit, keeping the 384:272 aspect ratio. Minimum size is 1:1 (384×272).
- **Cinema** — toggle off all chrome (toolbar, host info, type-line, F-key buttons) so only the C64 picture is visible. The window goes maximize, the picture fills the work area, and a translucent `Show controls (Esc)` button stays floating top-right so you can always come back. Esc anywhere also leaves cinema.
- **On top** — toggle `Qt.WindowStaysOnTopHint` so the streamer stays above other windows. Mirrored as a checkbox in Config.
- **Close** — close the streamer (cleans up workers, sends stream-stop to U64).

#### Drag-and-drop autostart
Drop files onto the streamer window to upload them to the U64 via the REST API and run them automatically. Mirrors the TSB U64Streamer's drop behaviour. Supported types:

| Extension | Action |
|-----------|--------|
| `.prg` | DMA-load + auto-run via `POST /v1/runners:run_prg` |
| `.crt` / `.bin` | Start as cartridge via `POST /v1/runners:run_crt` |
| `.sid` | Play tune via `POST /v1/runners:sidplay` |
| `.mod` | Play Amiga MOD via `POST /v1/runners:modplay` |
| `.d64` / `.d71` / `.d81` / `.g64` / `.g71` | Mount on Drive A (read-only) via `POST /v1/drives/a:mount` |

**Disk image modifier** (mirrors TSB):
- **Plain drop** of a disk → mount + soft reset (you land in BASIC; type `LOAD"*",8,1` + `RUN`)
- **Ctrl + drop** of a disk → mount only, no reset (manual disk swap while a program runs)

The drop runs in a background thread so the streamer doesn't freeze during the upload. Status appears in the stats line (`Sending xyz.prg -> run_prg...` → `PRG running` on success, error dialog on failure). 50 MB upload cap so dropping a DVD-ISO doesn't accidentally try to push half a gig.

#### Keyboard input — type to the C64
The streamer can inject keystrokes into the running C64 via the U64's REST API (`POST /v1/machine:writemem` to address `$0277`, the KERNAL keyboard buffer). This is Gideon's official socket trick and works with anything that uses standard KERNAL input — BASIC, FILEBROWSER, most utilities. It does **not** work with games or demos that scan the keyboard matrix directly.

Three ways to send keys:

- **`Type to U64:` line** — type a string + Enter (or click `Send`). The string gets ASCII→PETSCII converted and pushed into the keyboard buffer in 10-byte chunks with 180 ms gaps for the KERNAL IRQ to drain. RETURN is appended automatically so a typed BASIC line gets executed.
- **F-key / control-key buttons** — clickable buttons for `F1`–`F8`, `R/S` (RUN/STOP), `RET`, `DEL`, `CLR`, `HM` (HOME), and `↑ ↓ ← →`. Each click sends one PETSCII byte. These bypass any OS-level F-key conflicts.
- **`Capture keys` checkbox** — when checked, every keystroke while the streamer window has focus gets forwarded to the C64. F-keys, arrow keys, RUN/STOP (=Esc), Home, Insert are grabbed at the dialog level (via an `event()` override) so they reach the C64 even when the type-line has focus or when the OS would otherwise eat them.

  When Capture is on, **printable keys typed into the `Type to U64:` line also forward to the C64 in real time** — every character lands in the KERNAL keyboard buffer the moment you type it, the type-line edit stays empty. Hit Enter to send a RETURN to the C64; Tab and Backtab pass through normally. This makes the streamer feel like a real C64 keyboard plugged in: type `LOAD"*",8,1` and you see the characters appear on the C64 screen as you press them, not after you click Send.

  Disable Capture and the type-line goes back to its normal "buffer until I press Send" behaviour for when you want to compose long commands or scripts.

Keypress mapping: ASCII `a`–`z` → PETSCII lowercase block (0x41-0x5A), `A`–`Z` → uppercase block (0xC1-0xDA), F-keys `F1`-`F8` → 0x85, 0x89, 0x86, 0x8A, 0x87, 0x8B, 0x88, 0x8C. Single keypresses use a 0 ms inter-chunk delay; longer typed strings use 180 ms. A small queue (max 5 keys) buffers brief bursts when the worker is mid-transmit.

#### Performance notes
The UDP receiver lives in its own `QThread` with a 256 KB SO_RCVBUF (a full PAL frame = ~272 packets in one ~20ms burst, smaller buffers drop packets and tear the picture). The 4-bit-packed payload bytes are expanded to RGBA via a precomputed 256-entry LUT, decoded with `bytes.join` so the inner loop runs in C — about 6× faster than per-pixel Python iteration. GUI repaints are throttled: if a vsync arrives while the previous frame is still being painted, it gets dropped instead of queued, so the picture stays responsive at the cost of a few skipped frames at large window sizes. Audio packets (770 bytes each, 192 stereo S16LE samples) feed straight into a `QAudioSink` with a ~100 ms buffer.

#### Common issues
- **"REST failed, trying telnet menu..."** in the stats line → the U64's HTTP service is off. Enable it under U64 menu → Network Settings → Web Remote Control.
- **"start failed - listening anyway"** — both REST and telnet refused. The receiver is still listening on UDP, so if you start the stream from the U64's F5 menu manually, video should still appear.
- **No audio but video works** — make sure Audio Stream is enabled in the U64 settings (Data Streams section). Or check the `Video only` toggle in Config — if it's on, audio is intentionally suppressed.
- **F-keys do nothing when Capture-keys is on** — the OS or window manager may be grabbing them globally. Use the F-key buttons in the streamer instead.
- **Charset looks scrambled** — old bug where the LUT had nibble order reversed; fixed in the current version. If you still see this, your firmware may use a different pixel packing.

#### Space burst for intros
Some intros and demos scan the keyboard matrix (CIA1 $DC00/$DC01) directly and ignore the KERNAL keyboard buffer, so the normal `Send` button can't unstick them. The streamer has a dedicated **Space-burst worker** for this: clicking the `Space` button fires six writes in quick succession that together simulate a hardware Space-bar press:

| Address | Value | Purpose |
|---------|-------|---------|
| `$0277` | `$20` | KERNAL keybuf (in case the program uses KERNAL after all) |
| `$00C6` | `$01` | KERNAL keybuf length = 1 |
| `$00C5` | `$3C` | Last-key-pressed mirror, matrix code for Space |
| `$00CB` | `$3C` | Currently-pressed key mirror |
| `$DC00` | `$EF` | CIA1 Port A — row select for Space-bar row |
| `$DC01` | `$EF` | CIA1 Port B — bit pattern indicating Space is down |

Runs in a `_SpaceBurstWorker` thread so the GUI doesn't freeze during the writes. Most matrix-scanning intros react to this within one frame.

#### Read memory (REST API)
Toolbar button `Read mem...` opens an inline dialog asking for a start address (hex `$C000` / `0xC000` / decimal `49152`) and end address. The streamer then pulls that range from the running C64 via `GET /v1/machine/memory` and shows it in a **Memory Viewer dialog** (see next section).

### U64 Commander — Drive Mount, Config Editor, BASIC Editor, Discovery

Beyond live video streaming, the U64 viewer doubles as a remote-control dashboard for managing the Ultimate 64 over the REST API. The second toolbar row in the streamer window groups every device-management feature: **Mount... | Drives | Cfg Edit | Backup | BASIC | Asm64**. Plus the machine controls on row 1: **Reset | Pause | Menu | Reboot | Off | Snap | Rec**. Together this turns the streamer into a fully-fledged remote console — you rarely have to walk over to the actual hardware again.

All features here require the U64 firmware ≥ 3.11 (for the REST API) and the U64's IP set in the Config dialog. Network password (firmware 3.12+) is sent automatically as the `X-Password` header on every call.

#### Mount disk image — `U64MountDialog`
Click **Mount...** to mount a disk on Drive A or B with explicit control over mode. Three things to pick:
- **Disk image** — file picker for D64 / D71 / D81 / G64 / G71 / G81 (raw-track formats work without nibconv since the U64 firmware decodes them internally)
- **Target drive** — Drive A (default) or Drive B
- **Mount mode** —
  - **Read-only**: writes are silently dropped, your local file is never touched
  - **Read-write**: writes persist back to the local file (e.g. for testing a save-file-using game across sessions)
  - **Unlinked**: writes happen in the U64's RAM only, lost on unmount. Useful for cheap rollback-friendly experimentation.
- **Reset after mount** checkbox — if on, sends a soft reset right after the mount completes so the disk is detected automatically by KERNAL

#### Drive status monitor — `U64DriveStatusDialog`
Click **Drives** to open a live tree view of the U64's drives. For each drive (A, B, and IEC) you see:
- Mounted image (filename + mode)
- Drive mode (1541 / 1571 / 1581 / etc.)
- Power state (on / off)
- Bus ID (default 8 for A, 9 for B; configurable in firmware)
- Last activity timestamp

Auto-refreshes every 2 seconds while open. Useful for verifying that an autostart `LOAD"*",8,1` actually found the disk before you waste another minute typing.

#### U64 Configuration Editor — `U64ConfigEditorDialog`
Click **Cfg Edit** to open the most powerful feature: a full read-write GUI for the Ultimate firmware's hundreds of settings, fetched via `/v1/configs/`. Same settings you'd reach through the cartridge's on-screen menu, but with real keyboard and search.

Layout:
- **Left pane**: category list — Drive A, Drive B, U64 Specific (audio mix, palette, scanlines, SID2/SID3 addresses, ...), Network, Modem, Cartridge, RTC, Programmable Logic, etc.
- **Right pane**: form with one row per config item in the selected category. Widget type is auto-picked from the U64's metadata via `u64_get_config_definitions()`:
  - String / hex address → `QLineEdit`
  - Enum (Enabled/Disabled, Yes/No, list of choices) → `QComboBox` with the labelled values
  - Numeric with min/max → `QSpinBox` honoring the range
- **Bottom row**: dirty counter (e.g. `3 changes pending`) + buttons **Refresh all** / **Apply** / **Save to Flash** / **Load from Flash** / **Reset to Default**

Editing semantics:
- Edits accumulate in a `_dirty` dict — nothing is pushed to the device until you click **Apply**, which fires `u64_set_configs_bulk()` (one POST per category)
- **Apply** writes to volatile config — changes survive runtime but are lost on reboot
- **Save to Flash** is the second step: writes the current volatile state to non-volatile flash so it persists across reboots
- **Load from Flash** discards any in-progress edits and reloads from flash (useful as an "undo")
- **Reset to Default** restores factory defaults — confirmation required since this can include things like the network configuration

The category list is fetched once per session via `u64_get_config_categories()`; individual category items are lazy-fetched on first click to keep the dialog snappy.

#### Config Backup / Restore — `U64BackupDialog`
Click **Backup** to open a simple two-button dialog:
- **Backup to file...** — calls `u64_backup_all_configs()` which sweeps every category and dumps the full config to a JSON file. Default filename is `u64_config_<host>_<YYYYMMDD_HHMMSS>.json` so you can keep a chronological history. Lossless — works as a complete snapshot.
- **Restore from file...** — reads a JSON backup and writes every key back via `u64_set_configs_bulk()`. The device's flash is **not** touched by the restore alone; you have to follow up with **Save to Flash** in the Config Editor if you want the restore to survive a reboot.

Useful for:
- Migrating settings to a second U64
- Testing a risky config change with a known-good rollback point
- Snapshotting before firmware updates that might reset things

#### BASIC v2 Editor — `BasicEditorDialog`
Click **BASIC** to open an inline editor for writing C64 BASIC v2 programs with **petcat-compatible PETSCII control codes**, syntax highlighting, validation, tokenization, and **Send & Run** straight to the running C64.

Editor features:
- **Syntax highlighting** — line numbers in cyan, BASIC keywords (`PRINT`, `GOTO`, `IF`, `FOR`, ...) in red, strings in green, REM-comments in grey, `{ctrl codes}` in orange
- **PETSCII control codes** inside strings, written as `{NAME}` or `{count NAME}`:
  - Colors: `{BLK} {WHT} {RED} {CYAN} {PUR} {GRN} {BLU} {YEL} {ORG} {BRN} {PINK} {DARK_GREY} {GREY} {LT_GREEN} {LT_BLUE} {LT_GREY}`
  - Cursor: `{UP} {DOWN} {LEFT} {RIGHT} {HOME} {CLR}`
  - Formatting: `{RVS_ON} {RVS_OFF}`
  - Numeric: `{$XX}` = raw PETSCII byte (hex), `{NNN}` = raw PETSCII byte (decimal 0-255)
  - Repeat shortcut: `{3 SPACE}`, `{5 RIGHT}` — emits the code N times
- **Validate** — checks line numbers are monotonically increasing, parses control codes, reports the offending line on error
- **Tokenize** — produces a real C64 BASIC `.prg` with 2-byte load address `$0801` + the tokenized program body. Tokenization handles abbreviations (`?` → `PRINT`, `gO` → `GOTO`) just like the C64 BASIC ROM
- **Send & Run** — tokenizes, uploads via `u64_run_prg()`, U64 autostarts the program. End-to-end "type → run on real hardware" in a fraction of a second

The dialog has Open / Save buttons for `.bas` source files (plain text with the `{CODE}` notation) so you can keep a library of demo programs.

#### Auto-discover U64 devices — `u64_discover()`
The Config dialog has a **Discover...** button that broadcasts a UDP probe on port 64 (the "Ultimate Ident" service) and lists every Ultimate device that responds. For each device shown:
- IP address
- Hostname (as set in the U64's network config)
- Product (Ultimate 64, Ultimate II+, Ultimate II+L, ...)
- Firmware version

Double-click an entry to fill the host field automatically — no more typing IPs by hand. Requires "Ultimate Ident" service to be enabled in the U64's Network settings (default on).

#### Machine controls (row 1 of the toolbar)
Apart from the streaming toolbar essentials (Start/Stop, scale picker, Cinema, On-top), row 1 has six direct-action machine buttons:

| Button | Color | What it does |
|---|---|---|
| **Reset** | orange | Send reset sequence to the C64 (`u64_reset`). Like pressing the reset button on a Black Box cartridge — soft reset of the 6510, KERNAL re-init, cartridges retained |
| **Pause** | blue (toggle) | DMA-freeze the C64 mid-execution via `u64_pause` / `u64_resume`. The CPU stops at a safe moment; VIC timers keep running. Useful for screenshots and memory inspection while a program is mid-frame |
| **Menu** | blue | Simulate pressing the Multi Button / Ultimate Menu key (`u64_menu_button`) — enters/exits the firmware's on-screen menu |
| **Reboot** | orange | Reboot the Ultimate firmware itself (`u64_reboot`) — heavier than Reset, re-initializes cartridge config and all settings |
| **Off** | red | Power off the Ultimate 64 (`u64_poweroff`). Confirmation required. U64-only; Ultimate II+ ignores this. You'll have to walk over and physically power it back on. |
| **Snap** | blue | Capture the current video frame as a PNG. Saved to `~/Pictures/Ultimate64/` (Linux/macOS) or `%USERPROFILE%\Pictures\Ultimate64\` (Windows) with a timestamped filename. Screenshot folder is configurable in the Config dialog. |
| **Rec** | blue / red when active | Record the live VIC stream **with synced 48 kHz stereo audio** to a self-contained video file. Click to start, click again to stop. Saved to the same folder as Snap. Two output formats, switchable via right-click on the Rec button: **MP4** (H.264 video via `ffmpeg` + AAC audio at 192 kbps, web-streamable, single file — default if ffmpeg is on PATH) or **PNG sequence** (one numbered PNG per frame in a per-capture folder plus a `frames.json` sidecar with per-frame wall-clock timestamps for exact-timing post-processing; PNG-seq is video-only). MP4 auto-falls-back to PNG-seq if ffmpeg isn't found, with a status-bar warning. Audio is captured directly from the U64's UDP audio stream (the same source the live playback uses) into a temp WAV sidecar, then muxed into the final MP4 in a second ffmpeg pass with `-c:v copy` (no re-encode of the H.264 stream) + `-c:a aac` + `-shortest`. If the mux step fails for any reason the video-only MP4 and WAV sidecar are left in place next to each other so nothing is lost. Audio is automatically disabled if the streamer is in Video-only mode (no audio packets arrive in that case); the status line shows `+audio` vs `video-only` when the recording starts. While recording, the stats label shows `● REC <seconds> <frames> <avg fps>` — average fps should stay close to 50; if it drops noticeably the GUI is being out-paced by the encoder. The encoder runs in a background QThread with a 60-frame bounded queue (oldest-frame-drop on overflow) so a slow encoder cannot stall the live video display. Closing the streamer or pressing Stop ends the recording cleanly (ffmpeg gets 5 seconds to flush the trailer + 120 seconds for the mux pass). |

#### Asm64 button
Same as the standalone `asm64` Quopus action — opens the [Assembly64 Browser](#assembly64-browser) but with the streamer's host/password already wired in, so "Run on U64" and "Mount on U64" work without re-configuring.

### Memory Viewer / Editor / Cheat Engine

The memory dialog is opened via two paths:
- **From the U64 streamer**: toolbar `Read mem...` button — backend is the Ultimate 64's REST API
- **Stand-alone via action `vice_memory`**: bindable to a button or hotkey. Talks to a running VICE emulator over its **Binary Monitor** TCP protocol (config keys `vice_host` / `vice_port`, defaults `127.0.0.1:6502`)

Both paths produce identical UI. The dialog title shows which backend is active (`U64@host: $C000..$CFFF` or `VICE@127.0.0.1:6502: $C000..$CFFF`).

#### Detached window
Memory dialogs open with `parent=None` and the streamer/Quopus mainwindow keeps a reference in `_detached_dialogs`. Net result: **minimize Quopus → memory viewer stays visible**, separate taskbar entry, fully independent window. The `destroyed` signal removes the entry from the list when the dialog closes, so no memory leaks.

#### HEX / ASM toggle
Radio buttons switch between two views of the same memory:
- **HEX**: classic 16-bytes-per-row hex dump with ASCII pane. Identical-byte rows are collapsed into a single line with `x16` annotation to save space.
- **ASM**: 6502 disassembly with separate columns for PC, B0/B1/B2 opcode bytes, and the mnemonic. Indicates illegal opcodes when `c64_show_illegal` is enabled in config.

In both views every cell is **double-click editable**: type 1–2 hex digits, Enter pokes the new value back to the backend immediately. Editor cancels safely if Live mode is active or the dialog re-renders mid-edit (no Qt warnings).

#### Diff coloring
Before every read (Refresh, filter click, or Live tick), the previous `_data` is snapshotted as `_prev_data`. The render then compares byte-by-byte and **highlights changed bytes in red** in the cell foreground. Initial render: nothing red. Subsequent updates: only the bytes that actually changed since last render light up — so you see at a glance which addresses are moving in a running program.

#### Cheat Engine search bar
Below the toolbar is a search bar with a workflow modeled after [Cheat Engine](https://www.cheatengine.org/) / [ICU64](https://icu64.blogspot.com/):

```
[Start] [Changed] [Increased] [Decreased] [Unchanged]  [= Value][ box ]  [Find loads]  [✓] Live  [Reset]
```

- **Start** — snapshot current memory as the initial state. All addresses become candidates.
- **Changed / Increased / Decreased / Unchanged** — each click reads memory again and *narrows* the candidate set: keep only addresses where the value matches the predicate vs. the LAST snapshot (not the start). Allows iterative narrowing — e.g. Decreased twice in a row to find a counter that's decrementing through frames.
- **= Value** — type a byte value (decimal `3` or hex `$03` / `0x03`) and click. Keeps only addresses currently holding that value. Auto-starts the snapshot if no search is active yet, so the typical "I see 3 lives — find the lifecount" workflow is one click.
- **Find loads** — opens a separate dialog (see "Code Pattern Search" below) searching the *code* for immediate loads/compares of the value, not the data.
- **Live (100ms)** — when checked, the last filter button you clicked re-fires every 100ms with smart partial-range reads (see below). **In Live mode the candidate set is NEVER reduced** — Live is read-only refresh, so a counter that briefly takes a non-matching value won't get dropped from candidates. Manual removal via right-click is the only way to shrink the set while Live is active.
- **Reset** — clear all search state. Blocked while Live is checked (uncheck Live first); prevents accidental wipe of carefully narrowed candidate lists.

Status label shows `N candidates after <filter>  (partial $0800..$2000)` so you can see both the filter result and which range was actually read.

#### Smart partial range read
When the candidate set is smaller than half the original range, reads only cover `min(candidates) .. max(candidates)` instead of the full dump. With 5 candidates clustered in $0800..$2000, reads drop from 64 KB to 6 KB; with one candidate left, reads drop to **1 byte**. The `_last_values` dict is updated only for the read range — the rest remains stale, which is fine because filters only iterate over the candidate set.

#### Manual remove via right-click
Right-click any HEX or ASM cell of a current candidate → context menu offers `Remove $XXXX from candidates`. Useful while Live runs and you spot an address that's clearly noise (random IRQ counter, screen RAM, etc.) — flip it out without affecting the rest of the search.

### Find references (cell right-click)
Right-click any HEX or ASM cell → `Find references to $XXXX...` opens a non-modal **ReferencesDialog** that scans `$0000..$FFFF` and finds every 6502 instruction touching that address.

The dialog reads the full RAM on open (via the same backend) and caches it as `_full_dump`. Two tables side by side:

- **Reads** — `LDA / LDX / LDY / CMP / CPX / CPY / BIT / ADC / SBC / AND / ORA / EOR / JMP (ind)` plus RMW ops (`INC / DEC / ASL / LSR / ROL / ROR`)
- **Writes** — `STA / STX / STY / STZ` plus the same RMW ops (they read AND write)

Each row shows PC, bytes, mnemonic, operand, and a note. Indexed addressing is handled with **fuzzy matching**: `LDA $BF80,Y` is flagged as a possible read of `$C000` with note `if Y=$80`. Fuzzy hits render in gray, exact hits in normal foreground. Indirect modes `LDA ($FB),Y` are resolved using the current pointer value from the dump.

Summary line: `$C000: reads 4 (4 exact, 0 fuzzy), writes 2 (2 exact, 0 fuzzy)`.

Buttons:
- **Re-analyze** — runs the static analysis again on the cached dump. Cheap, no I/O.
- **Re-read RAM** — fresh 64 KB read from backend, then re-analyze. Use when the program has changed memory since the dialog opened.

Caveat: linear disassembly may reinterpret data bytes as opcodes — header text warns about false positives. Exact-match results in known code regions are reliable; everything else needs sanity-checking against context.

### Code Pattern Search (Find loads / Find counter ops)
Two related searches that scan code, not data:

#### Find loads of $VV
Toolbar button `Find loads` (next to `= Value`). Uses the value in the Value box, scans the full address space for **immediate-mode** instructions that load or compare that exact byte:

| Opcode | Mnemonic |
|--------|----------|
| `A9 VV` | `LDA #$VV` |
| `A2 VV` | `LDX #$VV` |
| `A0 VV` | `LDY #$VV` |
| `C9 VV` | `CMP #$VV` |
| `E0 VV` | `CPX #$VV` |
| `C0 VV` | `CPY #$VV` |
| `69 VV` | `ADC #$VV` |
| `E9 VV` | `SBC #$VV` |
| `29 VV` | `AND #$VV` |
| `09 VV` | `ORA #$VV` |
| `49 VV` | `EOR #$VV` |

Useful for finding where a game's initial "3 lives" gets loaded — search for `LDA #$03` and one of the hits is usually the lives initializer.

#### Find counter ops on $addr
Cell right-click → `Find counter ops on $XXXX...`. Pattern-matches against multi-instruction sequences that manipulate a counter at `$addr`:

**Modifications table**:
- `INC $addr` / `DEC $addr` (absolute, abs,X, and zeropage)
- `LDA #$VV / STA $addr` — immediate store (also zeropage variant `LDA #$VV / STA $LL`)

**Comparisons table**:
- `LDA $addr / CMP #$VV / BEQ/BNE/BPL/BMI $target` — with resolved branch target

For finding lives logic: the `LDA $C000 / CMP #$00 / BEQ gameover` pattern shows up directly with the branch target — patching the `BEQ` to `BNE` (or `EA EA` NOPs) at the listed PC is the classic infinite-lives cheat.

Both pattern dialogs share a **Live tracking** mode:
- Calculates `min..max PC` of all hits as the watch range
- Polls only that range every 100ms
- If a pattern byte changes (self-modifying code, runtime crack patches, NOP-injection): the row turns **red** and the status says `PATTERN BYTES CHANGED — self-modifying code detected`

### Assembly64 Browser

Built-in browser for the **Assembly64** scene archive at `hackerswithstyle.se` — a public service that aggregates C64 releases from CSDb, HVSC, c64.org, OneLoad64, Gamebase64 and others. The same backend is used by the Ultimate 64 firmware (≥3.11) and the official `sandlbn/ultimate64-manager` Rust desktop app. Action `asm64` (default action button "Asm64 Browse") opens the dialog.

**Find releases by**:
- **Name** — three search modes via prefix syntax (May 2026 server update):
  - `archon` — substring match (default)
  - `@mason` — exact match: filename must be exactly "mason"
  - `-fairlight` — wildcard: searches name + group + handle + event fields all at once (the fastest way to find anything related to a scener or group without picking the right field first)
- **Group** — release / cracker group (`fairlight`, `triad`, `quantum`). Also supports `@finnish gold` for exact match.
- **Handle** — coder / musician handle (`jch`, `goto80`, `glenn rune gallefoss`). Also supports `@JCH` for exact match.
- **Year** — exact match (`1986`) or open-ended via the Year-sweep helper
- **Category** — Assembly64's integer codes (0 = any, see the dropdown for the labelled list of demos / games / music / tools)
- **File type** — PRG, D64, SID, T64, TAP, CRT (browse the dropdown for the full list)

All form fields can be combined; press Enter in any field or click Search to fire the query. The dialog tracks an inflight worker via a `QThread` so the UI stays responsive even when the backend is slow.

**Top XX dropdown** — a blue "Top..." button next to the Search button opens a menu of pre-configured rating-sorted lists, fetched directly from the server's `/charts/<category>` endpoint:
- Top 100 Demos
- Top 100 Onefile Demos
- Top 100 Games
- Top 100 Music
- Top 100 Graphics
- Top 50 Tools

Each pick replaces the result list with the server's authoritative top-rated entries for that category — same ranking the official Assembly64 client and the U64 firmware menus show.

**Results table** — 4 columns (Name, Group, Year, Category) with click-to-sort on every column. Alternating row colors for legibility; column widths and sort order persist across sessions via `install_table_state()`. Rating values aren't shown in the table itself but feed the Top-XX dropdown's server-side ranking and the Favorites view shows them in the detail panel.

**Details panel** (right side of splitter) — when you click a result row, the dialog fetches the file list for that release in a background worker. Each entry shows filename + size + a hint about file type. PRG / D64 / SID files get distinct icons in the file table. The details panel auto-reveals as soon as data arrives — no manual click needed.

**Three actions per file** (each in its own toolbar button):
- **Run on U64** — uses the same U64 REST API path as the streamer's drag-drop: PRG via `run_prg`, CRT via `run_crt`, SID via `sidplay`, D64 mounted on Drive A. Requires that the U64's IP is set in Config (re-uses the streamer's config keys: `u64_host`, `u64_password`).
- **Mount on U64 Drive A** — for disk images (D64/D71/D81/G64/G71); plain mount with no auto-reset. Useful for swapping disks while a multi-disk game runs.
- **Download to disk** — saves the file to the inactive Lister's directory and refreshes the panel so you can immediately interact with it. Threaded with a Cancel button; HTTP errors surface as message-box dialogs.

**Favorites** — click the star on a result row to add it to favorites. Favorites are persisted in `config/asm64_favorites.json` (per-entry: `id`, `name`, `group`, `category`, `year`). A separate "Favorites" tab lists everything you've starred — runnable / mountable / downloadable just like search results. Useful as a personal pick-list across many search sessions.

**Saved searches** — the form gets a "Save..." button that stores the current set of form values as a named saved search in `config/asm64_searches.json`. A dropdown above the table lists every saved search; picking one repopulates the form and fires the search. Right-click → Delete to clean up. Lets you build a personal library of useful queries (`"All Triad releases 1986-1988"`, `"Quantum demos"`, `"GameBase64 SIDs with rating >= 8"`).

**Last-results cache** — the most recent result list is auto-saved to `config/asm64_last_results.json` when you close the dialog, and auto-restored when you re-open it. Reopening feels like resuming where you left off instead of starting blank.

**Save / Load Results** — independent of the auto-cache. Two buttons above the results tree let you snapshot the current result list to a named JSON file ("My Quantum picks.json", "Demoparty haul.json"), and restore it later. Useful for keeping curated collections separate from automatic state.

**Top XX vs Year-sweep** — earlier builds had a "Get all years" button that walked the entire 1982-current year range to work around the server's per-query result cap. That button has been removed in favor of the `/charts/<category>` endpoint, which the Assembly64 server pre-computes server-side and returns in one request. The internal `_YearSweepWorker` class is still in the codebase for future re-use (e.g. wiring it into the saved-searches menu) but no longer reachable from the UI.

**Endpoint override** — the dialog's "Endpoints..." button opens a config dialog where you can hand-edit the URL paths (search, detail, download) in case `hackerswithstyle.se` changes its routes between versions. Overrides are persisted in `quopus.cfg` under `asm64_endpoints`. The defaults are inferred from the Ultimate firmware's `assembly.cc` source and the sandlbn/ultimate64-manager Rust client.

**CSDb cross-link** — every Assembly64 entry has a CSDb ID (Assembly64 IDs *are* CSDb IDs, by design). Right-click → "Open on CSDb" opens `csdb.dk/release/?id=...` in your browser for the full release page (NFO, votes, screenshots, downloads).

**Common issues**:
- **HTTP 404 on search** — the server may have moved its route. Click Endpoints... and try `/leet/search/aql?query=` (default) vs `/leet/aql/search?query=` (alternative).
- **HTTP 403 "Host not in allowlist"** — your IP isn't whitelisted by hackerswithstyle.se. Rare for home connections, common for cloud / VPN ranges. Try a different network.
- **"Run on U64" fails with no host** — set the U64's IP via the streamer's Config dialog first (Alt+U → Config...). The Assembly64 browser re-uses those settings.

### Standalone U64 Streamer (`quopus_streamer.py`)
Top-level script that launches the U64 streamer **without Quopus**, using the same config (`~/.quopus/quopus.cfg`) — host, ports, password are read from there. Useful when integrating with a BBS or other process that wants to spy on the C64.

```cmd
python quopus_streamer.py                    # plain start, no auto-close
python quopus_streamer.py --minimal          # kiosk mode (video-only, scale 4 = 768x544)
python quopus_streamer.py --minimal=1        # kiosk at smallest size (192x136)
python quopus_streamer.py --minimal=8        # kiosk at largest size (~FullHD 1920x1360)
python quopus_streamer.py BBS 0400 0         # start + auto-close watch
python quopus_streamer.py --minimal=4 BBS 0400 0   # combined: kiosk at 768x544 + auto-close
```

**Minimal / kiosk mode** (`--minimal` or `--minimal=N` where N is 1..8): hides every UI element including the OS window decoration — titlebar, toolbar, host info, type-line, F-keys row, all buttons, AND the system-provided window frame. Only the raw C64 video frame is visible. Three mouse interactions:

- **Left-click and drag anywhere in the picture** — moves the window. Since there is no titlebar, the picture IS the drag handle.
- **Right-click anywhere in the picture** — pops a "Close Streamer?" Yes/No confirmation. The only way to dismiss the window via the mouse.
- **Mouse-release at the end of a drag** — persists the new window position to `<config-dir>/u64_streamer_minimal_pos.json`. Next time you launch with `--minimal` the window appears at the same spot. The position is its own small file rather than inside `quopus.cfg` so writes on every drag don't churn through the much larger main config.

The optional `=N` value picks the window size from a fixed table. The aspect ratio stays at the C64's 7:5 (1.41:1) at every step:

| Scale | Window size | Note |
|---|---|---|
| `--minimal=1` | 192 × 136 | Half VIC-II, smallest sensible |
| `--minimal=2` | 384 × 272 | Native VIC-II 1:1 |
| `--minimal=3` | 576 × 408 | |
| `--minimal=4` (or just `--minimal`) | 768 × 544 | Default |
| `--minimal=5` | 960 × 680 | |
| `--minimal=6` | 1152 × 816 | |
| `--minimal=7` | 1536 × 1088 | |
| `--minimal=8` | 1920 × 1360 | ~FullHD |

Combined with a BBS auto-close watch the streamer becomes a kiosk-style live feed: positioned where you want at the size you want, no chrome, no accidental config-clicking, auto-dismissed on logout.

**Wayland caveat**: Wayland's design forbids applications from setting their own window position. Drag-and-drop of the streamer window itself still works (via Qt's `startSystemMove()` which asks the Wayland compositor to handle the drag), but the saved position cannot be restored on the next launch — the compositor decides where the window first appears. You drag it into place once per session. The position is still saved to JSON (in case you switch to X11 later), and the streamer prints a one-line console hint on startup explaining the limitation. Under X11, Windows, and macOS the position survives across launches normally.

**BBS mode** (3 args, first must be `BBS`): the streamer starts as normal and additionally polls the given address every 60 seconds. As soon as the byte equals the trigger value, the streamer window closes itself.

Typical workflow:
1. User logs in to BBS — BBS door spawns `python quopus_streamer.py --minimal BBS 0400 0` in the background
2. Sysop sees the live C64 stream with no controls cluttering the view
3. User logs out — BBS door writes `0` to `$0400`
4. Within max 60 seconds the streamer notices and closes

Address and value parsing accepts `$0400`, `0x0400`, `0400` (leading-zero hex convention), or plain decimal. Values out of range (`> $FF` for the trigger value, `> $FFFF` for the address) give a usage error before the window opens.

### C64 emulator integration
Beyond VICE memory inspection, Quopus integrates an external C64 emulator for "Run in emulator" workflows:

**Config dialog** — accessed via the action `c64_emu_config` (bindable to a button) or via Config menu → `C64 emulator (path/args)...`. Two fields:
- **Executable** — path to `x64sc.exe`, `x64.exe`, Hoxs64, CCS64, or any other C64 emulator
- **Arguments template** — uses `{file}`, `{name}`, `{dir}` placeholders. Typical for VICE: `-binarymonitor -autostart {file}` (starts both the emulator AND enables the binary monitor so the memory viewer can connect immediately)

Stored in config keys `c64_emulator` and `c64_emulator_args`.

**Internal viewer type `c64emu`** — File Associations dialog → select an extension (`.prg`, `.crt`, `.t64`, `.tap`, ...) → set Mode to `internal`, Internal type to `c64emu`. The Args field for the extension is *not* used — args come from the global config above so you only configure them once for all C64 file types. The dialog hint reminds you of that when `c64emu` is selected.

Once configured, F3 (or your bound viewer action) on a `.prg` launches the emulator with the file as autostart target — and if the binary monitor is enabled in the args, you can immediately switch over to Quopus and `vice_memory` to inspect what the running program is doing.

### Telnet / Raw TCP / SSH terminal client

Built-in terminal emulator designed for **BBS hopping** and Unix-box administration. Renders ANSI escape sequences and PETSCII into a fixed-width pixel-perfect grid using the bundled Topaz / C64 Pro Mono fonts. Action `telnet` opens the dialog; bind to a button or hotkey.

**Three protocols**:
- **telnet** — RFC 854 with IAC negotiation (terminal-type, window-size, echo). Default port 23.
- **raw** — plain TCP socket, no negotiation. Useful for AmiExpress door integration, embedded systems, or anything that speaks "just bytes". Default port 23.
- **ssh** — SSH-2 via `paramiko` (optional dep: `pip install paramiko`). Password or key auth. Default port 22.

**Terminal emulation**:
- **VT100 / ANSI** mode with CSI escape sequences: cursor positioning, clear-screen/line, scroll regions, SGR colors (fg/bg/bold/inverse), erase-display, save/restore cursor. Not a full VT220 — just enough for typical BBS UIs, midnight commander, mc-style file managers, `htop`, `vim` (basic editing).
- **PETSCII** mode for C64 BBS-es (Centronian, Antidote, etc.): each byte mapped through `petscii_byte_to_unicode()` with inline C64 color control codes (`{0x90}` = black, `{0x05}` = white, all 16 VIC-II colors). Reverse-video bit (high-bit ORed) handled correctly.
- **ATASCII** support for Atari BBS scene legacy systems.

**Encoding picker** in the connection form: CP437 (default — DOS-era BBS art with box-drawing chars), UTF-8, Latin-1, PETSCII. Switchable mid-session via the toolbar.

**Connection form** fields:
- Session name (free-text label, used in the saved-sessions list)
- Host / IP, Port, Protocol
- Encoding picker, Terminal type (PETSCII / ANSI / ATASCII)
- Font picker (Topaz / C64 Pro Mono / Mono fallback)
- Window size (columns × rows — sent in the telnet NAWS negotiation if the server asks)
- Auto-login: username + password + post-login command sequence
- Username + Password fields for SSH (or path to SSH private key)

**Saved sessions** — every dialog has Save / Load buttons. Sessions are persisted in `config/telnet_sessions.json` with `_meta.format_version` for forward-compatibility. The list manager supports rename / duplicate / delete / reorder. Pass a session name as the action's `param` to bind a one-click reconnect to a button (action `telnet`, param = session name).

**Architecture** (for context when troubleshooting):
- **Network thread** (`_NetworkWorker` QThread) owns the TCP socket and handles telnet IAC negotiation; forwards incoming bytes via Qt signal so the UI thread keeps drawing at full frame rate even on slow links
- **Terminal emulator** (`_TerminalScreen`) — software VT100 state machine over a fixed character grid, cursor + attribute tracking, ANSI parser
- **Widget** (`_TerminalWidget`) paints the cell buffer with the chosen pixel font, captures keystrokes, sends them through the network thread queue

**BBS-specific features**:
- **PETSCII color and reverse-video** — accurate C64 rendering, RVS ON/OFF tracking
- **Topaz font** for Amiga-style BBS connections (drop the TTF into `fonts/`)
- **Auto-reconnect** option — if the server disconnects within N seconds of connecting (typical "log in failed", "session expired", "max users reached" walls), Quopus auto-reconnects after a configurable backoff. Configurable per saved session.
- **Logging** — toggle in the toolbar to capture the entire session to a text file (`<session>_YYYYMMDD_HHMMSS.log`). Useful for reviewing what scrolled past.

**Hotkeys**:
- Standard typing → goes to the connection
- `Ctrl+C`, `Ctrl+D`, `Ctrl+Z` etc. → forwarded to the remote (not local Qt shortcuts) when terminal has focus
- `Ctrl+Shift+C` / `Ctrl+Shift+V` → local copy / paste (instead of `Ctrl+C` which sends `^C`)
- `Esc` does **not** close the dialog (it's sent to the remote). Click the toolbar Close button or use window-X to disconnect.


For BBS upload prep where you want to see only filenames that are still too long for DOS 8+3. Toggle three ways, all equivalent:

- **Column header right-click** → `✓ Hide 8+3 filenames (DOS conform)` (checkbox-style with checkmark)
- **Button or hotkey action**: `toggle_non_dos83` (registered in both action lists)

Rule: a name "fits 8+3" and gets hidden when:
- No dot in name → length ≤ 8
- Exactly one dot → name part ≤ 8 AND ext ≤ 3
- More than one dot (`foo.tar.gz`) → never 8+3, always shown
- Hidden-style names (`.bashrc`, no basename) → never 8+3, always shown

The info bar's `X dirs, Y files` count is recomputed after filtering, so it always matches what you see. Per-lister state — left and right can have different filter settings — and not persisted, so a fresh Quopus start always shows everything.

### File compare (Alt+F11)


- **Two files selected** in one panel → compare those two
- **Two files tagged** in one panel → compare those two
- **One file** on each side (selected or tagged) → compare across panels
- **Two files** in the OPPOSITE panel → mirror the comparison from there

Selection beats tags: if you have 2 files mouse-clicked AND 8 tagged from earlier, the dialog uses the explicit click pair, not the stale tag list.

**Two modes**, auto-picked or manual:
- **Text mode** — line-based diff via `difflib.SequenceMatcher`. Whole-line changes/inserts/deletes get highlighted with three colour variants (red = removed, green = added, amber = changed on both sides).
- **Hex mode** — 16-byte-per-row hex+ASCII layout, byte-level diff. Differing bytes are highlighted in both the hex column and the ASCII column.

Auto-detection counts NUL/control bytes in the first 4 KB of each file: >1% triggers binary heuristic and selects Hex mode automatically. You can flip manually via the toolbar regardless.

**Lockstep scrolling** — both vertical AND horizontal scroll positions track between panes so the same line/byte is always visible in both.

**Prev/Next Diff** buttons jump straight to the next differing region, skipping over identical stretches. Hex consecutive-difference lines collapse into single jump-targets so you don't have to hammer the button through a 200-byte run of differences. Wraps around at end/beginning. Files larger than 50 MB get truncated with a warning so the dialog opens responsively.


Total Commander-style file search with **four** modes in one tabbed window. Press `Alt+F7` (or trigger the `search` action via right-click menu / button binding) to open it.

- **Filename** tab — glob pattern matched against file names. Examples: `*.sid`, `foo*`, `*backup*.zip`. Case-sensitive checkbox if needed.
- **Text in files** tab — substring match against decoded file content. Optional filename glob to restrict which files are read (e.g. only search `*.txt`). Tries both UTF-8 and Latin-1 byte encodings of the needle so it matches whether the target file uses Unicode or legacy Amiga/DOS text.
- **Hex in files** tab — raw byte-pattern search. Accepts `DE AD BE EF`, `deadbeef`, `0xDE 0xAD ...` — whitespace and `0x` prefixes are stripped automatically. Invalid hex (odd nibble count, non-hex chars) gets caught with a warning before search starts.
- **Assembly (6502)** tab — search for **6502/6510 assembly snippets** in any binary. Type lines like `lda #$00`, `sta $d021`, `jsr $c000` — the dialog assembles them to bytes and searches for that byte sequence. All 6502 addressing modes supported (immediate, zero-page, absolute, indexed, indirect, branch). **Wildcards** via `?` for unknown operands:
  ```
  lda #?           matches any LDA-immediate
  sta $????        matches any STA-absolute (any 16-bit target)
  bne $??          matches any BNE branch
  lda $?d2?        partial-byte wildcards work too
  ```
  Multi-line input — assemble several instructions at once. Comments via `;` or `//`. The assembled byte pattern is shown in the status line so you see exactly what's being searched (e.g. `A9 ?? 8D 21 D0`). Assembler errors (unknown mnemonic, malformed operand) report the exact line that failed.

The search runs in a background `FindWorker` thread, so the UI stays responsive on big trees. A black status bar above the results live-updates with `Searching for X in: /current/folder` so you see exactly where the walker is. Results stream into the list as they're found — no waiting for a full scan to complete before seeing anything. Counter shows running tally; final summary reads `Found N (M files scanned)`.

**Cancel button** stops the search at the next file boundary (typically within 50ms). Closing the dialog also cancels.

**Double-click a result** to jump the originating lister to that file's parent directory, with the file selected and scrolled into view.

**Feed to listbox** — purple button next to Cancel. Sends all current matches to the originating lister as a flat virtual directory, similar to Total Commander's "feed to listbox" feature. The lister switches into search-results mode:
- Five columns instead of four — a new **Folder** column is appended showing where each file came from
- Title bar shows `🔎 search: <pattern>  (47 files - right-click for Close search)`
- Path field shows `🔎 Search: <pattern>` instead of a real path
- All normal lister operations work: double-click to view/play, drag, copy/move via F5/F6, hex view, etc.
- The listing is read-only at the directory level (you can't `cd` into it or create new files), but individual files can still be renamed/deleted — those operations affect the real files at their original locations

**Right-click → ✕ Close search results** returns the lister to the directory you were in when you opened the search dialog.

**Limits**:
- Max scan: 50,000 directories deep (effectively unlimited for normal use)
- Content searches skip files larger than 16 MB to avoid choking on log files / archives
- Symlinks are not followed (prevents loops)

### FILE_ID.DIZ preview panel (Alt+F)
Toggle a live DIZ panel over the opposite lister. Updates on every cursor change in the active lister. Recognises:
- **ZIP / TAR / LHA archives** — searches `FILE_ID.DIZ` / `FILE_ID.TXT` inside
- **DMS files** — extracts the FILE_ID.DIZ track (block #80) from the Amiga DMS format, falls back to the aPuS header banner
- **NFO / TXT / DIZ files** — shows content directly, extracts the section between `@BEGIN_FILE_ID.DIZ` and `@END_FILE_ID.DIZ` markers when present
- **Sidecar files** — `foo.zip` will show `foo.diz` if no DIZ is found inside
- **Directory** — shows `FILE_ID.DIZ` / `FILE_ID.TXT` inside it
- CP437 decoding for Scene-ASCII art with box-drawing characters
- ANSI escape codes stripped for clean display

Press Alt+F again to close the panel.

### File associations
Configurable per extension:
- **Viewer** (F3 / Read): internal auto / text / image / archive / hex, OR external program
- **Editor** (F4 / Edit): internal or external
- Tokens in args: `%f` (file path), `%F` (all selected), `%n` (basename), `%p` (current dir), `%d` (other-side dir)
- Default associations preloaded for 19 common file types
- Wildcard `*` entry as fallback
- Programs are launched with **cwd set to the program's own directory** (or the script's directory) so relative paths in scripts work

Config menu: Config button → File associations...

### File operations
- **Copy / Move with progress dialog** — shows `File N of M`, percent, throughput, ETA
- **Cancellable mid-transfer** — Cancel button or window-X aborts within ~1 MB; partial files are deleted
- **Detailed overwrite dialog** with side-by-side comparison:
  - Existing target: size + date
  - New source: size + date
  - Hint summary: "Source is NEWER than target", "Same size", etc.
  - Yes / Yes to all / No / No to all / Rename / Cancel
- Chunked copy (1 MiB blocks) for big files keeps the dialog ticking
- Cross-FS transfers: Local ↔ FTP/SFTP transparently
- Delete (direct or recycle bin per OS)
- System clipboard copy/cut/paste with Explorer interop
- Tag/untag with Space, by wildcard (Num +/-), invert (Num *)

### Archive packing (Alt+F5)
**Threaded** — Quopus stays interactive while packing. Visible progress dialog with file counter, throughput, ETA, Cancel button. Partial archive removed on Cancel.

Format picker offers 8 options:

| Format | Library | Notes |
|--------|---------|-------|
| ZIP | Python `zipfile` | Always available, ZIP64 for >4 GiB |
| TAR + GZIP | Python `tarfile` | Multi-file with gzip |
| TAR + BZIP2 | Python `tarfile` | Better compression |
| TAR + XZ | Python `tarfile` | Best compression |
| TAR plain | Python `tarfile` | Uncompressed |
| GZIP single file | Python `gzip` | One file only |
| LHA | external `lha.exe` | Needs LHA32/LhaForge on Windows or `lha` on Linux |
| RAR | external `rar.exe` | Needs WinRAR (rarlab.com) |

### Archive extraction (Alt+F9 / Arc Ext button)
Supports: ZIP, TAR (+gz/bz2/xz), LHA/LZH, **RAR**, **GZ** (bare gzip)

### Multi-rename tool (Ctrl+M)
Full-featured batch rename with **live preview table** (Old name → New name, Status):
- **Template tokens:** `[N]`, `[N1]`, `[N2-5]`, `[N3-]`, `[E]`, `[E1-2]`, `[C]`, `[C3]`, `[YMD]`, `[hms]`, `[Yf]`, `[Mf]`, `[Df]`, `[hf]`, `[mf]`, `[sf]`, `[P]`
- **Counter:** configurable start, step, digit count
- **Case:** unchanged / UPPER / lower / First cap / Each Word, separately for name and extension
- **Search & Replace:** plain text or regex, case-sensitive toggle, tokens expand in replacement string too
- **Two-phase rename** via temp names — cyclic swaps (A→B, B→A) work safely
- **Conflict detection** with visual markers (red = conflict, orange = exists/overwrite)

### ASCII ↔ PETSCII converter
Text-file conversion between ASCII and C64 PETSCII. Right-click → `ASCII ↔ PETSCII convert...`

- **Auto-detect** direction per file
- **Auto-detect charset mode** per file: Mixed / Upper / Hybrid / Smart
- **Live preview window**
- **Output options:** append extension, replace extension, or write to separate folder

### Internal FTP / FTPS / SFTP client
Action `ftp` — connects and browses remote filesystems.

> **See also**: [Quopus Drive](#quopus-drive-remote-machine-mount) for a custom client/server protocol that exposes folders on another machine with TLS + HMAC + MAC-based auth. Use FTP for talking to existing FTP servers (scene sites, web hosts, NAS appliances); use Quopus Drive when you control both ends and want stronger auth than FTP can provide.

**Supported protocols:** FTP (port 21), FTPS explicit TLS (port 21), FTPS implicit TLS (port 990), SFTP (port 22, needs paramiko)

**Connect dialog:** protocol picker, host, user, password, optional SSH private key for SFTP, save as named bookmark (max 30). The dialog has three buttons:
- **Connect** — open the connection now and mount it into the active lister
- **Save Bookmark** — store the entry as a saved bookmark *without* connecting (handy for building a bookmark list when you're not ready to open a session)
- **Cancel** — close without saving anything

Plus two optional checkboxes that work with both Save and Connect:
- **also add as drive button (left panel)** — after the action, also persist this FTP location as a drive button in the left column. Drive label defaults to the host name in upper-case if left blank. One-click reconnect later.
- **also add as action button (6x6 grid)** — also save it into the action-button bank. When ticked, you get a cell-picker dialog (with Main, Shift, and Shift+Alt layer tabs) to choose which empty cell to place it in. The button uses the `ftp_site` action with the bookmark name as its param, so a click reconnects to that bookmark directly.

Passwords are NOT auto-persisted with bookmarks by default — they're prompted at connect time. Tick the explicit "Save password (in plain text)" checkbox in the bookmark editor if you really want them stored.

**Browser:** mounted into the active lister, F5/F6 work cross-FS automatically. Connection-loss detection with auto-fallback. Right-click → `🔌 Disconnect FTP`, or **Esc** in the remote lister, or **Ctrl+Shift+F** anywhere.

**Bookmark from current FTP location:** while browsing a remote filesystem, right-click in the lister body and choose **"Add this FTP location as drive button"**. The FTP-bookmark dialog opens pre-populated with the active connection's host/port/user and current remote path - just confirm the label and save. Saves you the trouble of re-typing connection details for sites you reach via the dialog once and want to revisit later with one click.

**Defensive host cleanup:** the `ftp_site` action strips common URL-prefix typos (`ftp://`, `ftps://`, `sftp://`, `http://`, `https://`) and trailing path components from the bookmark host before connecting, so `ftp://scene.org/pub` in the host field becomes just `scene.org`. Connection failures show a verbose error including host/port/user so you can spot configuration mistakes immediately.

### Quopus Drive (remote machine mount)
A custom client/server protocol for exposing arbitrary folders on another machine inside Quopus's normal dual-pane view, with stronger auth than plain FTP and zero dependency on SMB/NFS or commercial remote-desktop tools. Use it when you want to manage a sysop machine, a NAS, a development box, or your own desktop from another PC without poking SMB shares through every network.

The remote side runs a small Python script (`qdrive_server/quopus_drive_server.py`, stdlib-only). Quopus then connects to it over **TLS 1.2+** with a self-signed server certificate that the client pins by SHA-256 fingerprint (no public CA, no MitM exposure). Authentication uses **HMAC-SHA256** with a per-client shared secret combined with the **MAC address of the client's physical LAN adapter** as a second factor - a stolen secret won't authenticate from a different machine because its MAC is not on the server's allowlist. Virtual adapters (VMware, VirtualBox, Hyper-V, Tailscale, WireGuard, Docker, loopback, ...) are automatically excluded from MAC enumeration on both sides.

**Server-side setup** (on the remote PC):

```
python quopus_drive_server.py setup
```

The wizard walks you through:
1. **Port + bind address** — default port is `2000`, bind defaults to `0.0.0.0` (all interfaces). Pick `127.0.0.1` if you only want local-only access (over an SSH tunnel for example).
2. **TLS certificate** — auto-generated self-signed RSA 2048 cert valid for 10 years. The wizard prints the **SHA-256 fingerprint** which you paste into Quopus on the client side. Cert + key are stored under the server's user config dir (`~/.config/quopus_drive_server/` on Linux, `%APPDATA%\quopus_drive_server\` on Windows, `~/Library/Application Support/quopus_drive_server/` on macOS).
3. **Exposed drives** — any number of directories, each with a short name and a read-only flag. Only paths you explicitly add show up to clients; the server enforces a whitelist with path canonicalization, so `../../etc/passwd`-style traversal attempts are rejected before touching the filesystem.
4. **First client authorization** — pick a unique client name, enter the MAC address(es) of the client's physical LAN card(s), and the wizard generates a 256-bit random secret. The secret is printed once and never again - paste it into Quopus's bookmark immediately.

To find the **client machine's MAC**, run on the Quopus client:

```
python quopus_drive_server.py whoami
```

This enumerates physical LAN cards on the local machine, filtering out the virtual adapters listed above, and prints each as `(interface_name, mac_address)`. Use one of those MACs in the server-side `setup` wizard. For a laptop that switches between Ethernet and Wi-Fi, register both MACs - the wizard accepts a list.

**Running the server**:

```
python quopus_drive_server.py run
```

Stays in the foreground, logs every connect / authentication attempt / deny to stdout with the client's IP. Ctrl-C to stop. For unattended operation wrap it in a systemd unit (Linux), Windows Task Scheduler entry, or launchd plist (macOS) - the script intentionally doesn't fork or daemonize itself, the surrounding init system handles that better.

**Other server commands**:

| Command | Purpose |
|---|---|
| `info` | Full config dump: port, cert fingerprint, exposed drives, all clients and their authorized MACs |
| `listclients` | Compact one-line-per-client listing |
| `addclient` | Add another client (wizard) |
| `delclient [name]` | Remove a client; the client's secret is wiped immediately. Confirmation requires typing `yes`, not just `y`, to prevent accidental clicks |
| `addmac [client] [mac]` | Add another MAC to an existing client. MAC accepted in any format (colons, hyphens, no separators), normalized internally. Duplicates are silently skipped |
| `adddrive` | Add another exposed directory after initial setup |
| `deldrive [name]` | Stop exposing a directory (doesn't touch the files themselves) |
| `whoami` | Print this machine's physical LAN MACs (useful when this machine will itself be a client of a different server) |

Every command that writes to `server.json` (addclient, delclient, addmac, adddrive, deldrive) requires a server restart to take effect — Ctrl+C the running `run` command and start it again.

**Client side** (in Quopus):

Bind the action `qdrive` to any action button to open the Quopus Drive connect dialog. The dialog shows:
- **Saved-bookmark list** at the top, with double-click to prefill the form
- A connection form with fields for **Server host**, **Server port** (default 2000), **Client name**, **Shared secret** (password-masked), **Cert fingerprint**, and an optional **Force local MAC** field for cases where MAC autodetection picks the wrong adapter

After filling in the form, **Save bookmark** persists it to `qdrive_bookmarks.json` in your Quopus config dir (with mode 600 where the OS supports it) and immediately offers to add it as a one-click action button - if you say yes, the cell-picker (with Main / Shift / Shift+Alt layer tabs) opens so you choose where to place it. **Connect** establishes the connection and mounts the chosen drive into the OTHER lister pane (same convention as FTP: source/active stays where you are, the remote drive lands in the destination panel).

The mounted drive shows up in the lister exactly like FTP/SFTP — `[REMOTE]` prefix in the title, the right-click context menu offers `🔌 Disconnect Quopus Drive (back to local)`, and a red **Disconnect** button appears next to the path bar for one-click drop back to local filesystem. Double-click on a file downloads it to a session temp dir and opens it through the normal viewer dispatch (TextReader, image viewer, SID player, ...) — Quopus does NOT try to `Path.stat()` remote paths directly.

**Direct-connect action `qdrive_site`**: takes a saved bookmark name as its `param`. Use this when you've created one-click reconnect buttons via the "Save as action button" prompt in the connect dialog. Click the button, Quopus reads the bookmark, performs the TLS + HMAC handshake, mounts the drive that the bookmark's `initial_drive` field names (or asks which drive if none is set). No dialog, no UI interaction.

**Sicherheit honestly described** — defends against:
- Passive eavesdropping (TLS 1.2+ encryption)
- MitM with a forged certificate (SHA-256 pinning, no CA chain trust)
- Replay attacks (server nonce + 30s timestamp window)
- Stolen-secret-on-different-machine (MAC must also be on the allowlist)
- Path traversal (server canonicalizes every path against the drive's whitelisted root before any filesystem access)

Does **not** defend against:
- An attacker on the same LAN segment who can sniff your MAC AND steal your secret. MAC spoofing is trivial - the MAC tie is a hardening layer, not a primary defense. Treat the secret like a password and rotate (`delclient` + `addclient`) if you suspect leakage.
- Anyone with shell access on the server machine itself. The server runs with the privileges of the user who started it, so the exposed drives are already accessible to that user without any client.

**Network setup hints**:
- The server binds locally; **getting traffic to it from the internet is your responsibility**. Don't expose port 2000 directly to the open internet without a VPN layer.
- The most robust setup is **Tailscale**, **WireGuard**, or **ZeroTier** between client and server: install on both machines, both pick up a `100.x.x.x` (Tailscale) or similar private address, point the client's bookmark `Server host` field at that address. No router port-forwarding, no DynDNS stress, end-to-end-encrypted on top of TLS, works on the road too.
- If you do want direct-internet exposure (LAN-only port forwarding through one router is usually fine), set up a **DynDNS** alias for your home IP and configure your router to forward TCP/2000 to the server PC's LAN IP. **Double-NAT** setups (router behind router) need port-forwarding on both routers, which is fragile; prefer a VPN.

**Sample protocol**:
1. Server sends 32-byte random nonce + UTC timestamp
2. Client computes `HMAC-SHA256(secret, nonce || timestamp || mac)` and sends it back along with the claimed MAC and client name
3. Server verifies client name is known, MAC is on the allowlist for that name, timestamp is within ±30s of now, and the HMAC matches its own computation - all four must pass
4. On success, server sends the list of allowed drives and the session is open. Commands are length-prefixed JSON over the TLS stream; file payloads use a separate length-prefixed binary frame after the command

The protocol is intentionally simple and uses only well-known cryptographic primitives - the actual file-transfer code is < 1000 lines on each side.

### Drive-button column (left panel)
A vertical scrollable list of up to 40 drive buttons - the second-from-left column with HOME/ROOT/TMP/etc. by default (auto-populated from the host OS as described in the OS-aware drives section above). Right-click any button for the full management menu:
- **Add folder bookmark...** — opens a rich dialog with these fields:
  - **Label** (button text)
  - **Left-panel path** (where the LEFT lister navigates)
  - **Right-panel path** (where the RIGHT lister navigates; leave empty to reuse the left path)
  - **Default click target**: `active panel` / `both panels` / `left only` / `right only`
- **Add FTP bookmark...** — opens an FTP-bookmark form: host, port, user, password (optional), initial path, transfer mode (passive/active). Type-tag is `ftp` so the entry connects rather than navigates.
- **Edit...** — routes to the right dialog based on the entry's type
- **Remove**, **Move up/down**, **Move to top/bottom**, **Edit all...** — list management

**Click behaviour** — same shortcuts apply to both folder and FTP bookmarks:
- **Plain Click** → uses the bookmark's configured Default click target
- **Shift+Click** → opens in BOTH panels regardless of default
- **Middle-Click** → opens in RIGHT panel regardless of default

For folder bookmarks with separate left/right paths configured, "both panels" mode opens *different* paths in the two panels simultaneously - useful for project workflows like opening a source tree on the left and a build folder on the right with one click.

### Configurable buttons
6 rows × 6 columns = 36 action buttons under the listers, **tripled with two modifier layers**: hold Shift to swap to a second 6×6 grid, hold Shift+Alt to swap to a third. Effective capacity: 108 actions.

**Per-button settings:**
- Label, Action, Color (40 presets), Param (with `%f`/`%F`/`%n`/`%p`/`%d`/`%i` tokens)
- **Extension gate `{file|ext1,ext2,...}`** in the Param. Before the action runs, every selected file is checked against the comma-list. If any file doesn't match, a warning popup names the offender(s) plus the allowed list and the action is aborted. On pass, the token is rewritten to `%f` (so use it anywhere you'd use `%f`). Keyword and extension list are both case-insensitive, leading dot optional — so `{file|crt}`, `{FILE|.crt}`, `{File|CRT}` and `{file|crt,prg}` all mean the same. Example: `notepad.exe {file|nfo,asm}` refuses to run when a `.zip` is in the selection and silently launches notepad on `.nfo` / `.asm`. The check happens in the central dispatcher so every action benefits, not just `run`.
- **Hover text** — optional tooltip
- **Hover image** — optional PNG/JPG; appears centered in the upper window area
- **Hotkey** — global keyboard shortcut bound to the button. Two ways:
  1. **Pick from the dropdown of built-in Quopus hotkeys** (61 entries: F1–F10, Shift+F-keys, Alt+letter combos, Ctrl+letter combos, Ctrl+F-keys, etc.). When you pick e.g. `Alt+U  (u64view)`, the dialog auto-fills the Action column with `u64view` and suggests a label like `U64 View`. Press the same combo on the keyboard later → the button's action runs. Buttons-with-hotkey == "press this key, click that button" being equivalent.
  2. **Type a custom combo** like `Ctrl+Shift+P` or `F12` for shortcuts that Quopus doesn't already bind. Saved as a global QShortcut on the dialog; rebuilt on every button-grid change.
- **Show output** / **Refresh after** / **In terminal** — three independent toggles that wrap the launched command. `Show output` captures stdout/stderr in a non-modal output window. `Refresh after` re-reads both panels when the command exits (useful for tools that drop files into the current dir). `In terminal` spawns the command in a new visible terminal window (xterm/cmd.exe).

For built-in hotkeys whose handler isn't a simple `actions.dispatch(...)` call (e.g. `Ctrl+B = branch view`, `Ctrl+Q = quick view in opposite panel`, `F1 = README`, `Ctrl+T = layer toggle`), the dialog stores them under the special action `hotkey` with the combo as the `param`. The button click path then calls a single helper that fires the same handler the actual key press would fire — so button clicks and key presses are guaranteed to do the same thing, no logic duplication needed. The dropdown shows these as `<combo>  (<action>)` next to the dispatchable ones, but their internal `(action)` is just `hotkey`.

**Custom hotkeys** (combos you typed in yourself rather than picking from the dropdown) are stored verbatim and bound to the button's normal action. So binding `Ctrl+Shift+R` to a button with action `external_script` and param `python pack.py %p` makes Ctrl+Shift+R run that script anywhere in Quopus.

**Editing options:**
- **Right-click any cell** (filled or empty) → full edit dialog. The dialog title shows whether you're editing the *main layer*, the *Shift-layer*, or the *Shift+Alt-layer* so it's clear which one gets written, even if a modifier slipped while you were clicking. The Action picker is a hierarchical submenu grouped by purpose (Viewers, File operations, CBM/C64 tools, Networking, ...) so you don't have to scroll through 80+ actions in a flat list.
- **Config → Action buttons...** — bulk editor with three tabs (Main layer / Shift-layer / Shift+Alt-layer), one tab per layer. Edit all three grids in the same window, OK saves all. The Action column is the same hierarchical submenu picker as the right-click dialog — both editors now read the action list from a single shared catalog so the ordering and grouping is guaranteed identical between them.
- **Action `ftp_site`** — direct-connect FTP action button. Param = bookmark name; click connects to that bookmark immediately without opening any dialog. Created automatically when you tick "also add as action button" in the FTP connect dialog.

**Layer mechanics**: a global `QApplication` event filter watches for Shift / Alt press/release on any focused widget. On every press/release it queries the live modifier state and derives the active layer: no modifier → main, Shift alone → shift, Shift+Alt → shift_alt. Auto-repeat key events are filtered out to keep the layer steady while keys are held. **Ctrl+T** cycles persistently through `main → shift → shift_alt → main` — release the modifiers and the layer stays where you cycled it, until you Ctrl+T again. The status bar shows the current layer name while non-main is sticky.

**Button assignment mode:** right-click file → `Assign to button ►` → pick action → click target button. Right-click folder → `Assign '<name>' to action button...` for one-click `goto_dir`.

### Custom modules (user plugins)

Beyond the 80+ built-in actions, you can drop your own Python files into a `custom_modules/` folder and have them appear in the action picker as new buttons. Each plugin is a regular `.py` file with two requirements: an `ACTION_NAME` string at module level, and a `run(api)` function. Everything else (label, description, parameter hint) is optional metadata.

**Where to put them.** Two directories are scanned, in this priority order:

1. **`<exe-dir>/custom_modules/`** — portable / shipped alongside the application. Useful if you're distributing Quopus + a set of plugins as a bundle.
2. **`<user-config-dir>/custom_modules/`** — your private modules. This is the default place for new plugins because it survives Quopus updates and lives in your normal config tree (`~/.config/quopus/custom_modules/` on Linux, `~/Library/Application Support/quopus/custom_modules/` on macOS, `%APPDATA%\quopus\custom_modules\` on Windows). The folder is created automatically with a quickstart `README.txt` on first run.

If a module with the same `ACTION_NAME` exists in both directories, the user-config one wins.

**Quickstart.** Pick `Config → Open custom modules folder` to land in your user directory. Create a new file like `my_action.py`:

```python
ACTION_NAME = "my_first_action"
ACTION_LABEL = "Say Hello"
ACTION_DESCRIPTION = "Greet the user and show selection details"

def run(api):
    api.notify("Hi", f"You selected {len(api.selected)} file(s)")
```

Then `Config → Reload custom modules` (no restart needed). Your action now appears under a new **"Custom Modules"** group in the action picker — right-click any button → pick it → bind it. The group is only shown when at least one module is loaded.

**The `api` object** that gets passed into `run()` is a stable adapter so plugins don't need to import any Qt internals. It exposes:

| Attribute / method | Returns / does |
|---|---|
| `api.src_path` | `Path` of the active panel's current directory |
| `api.dst_path` | `Path` of the other panel's current directory |
| `api.selected` | `list[Path]` of the highlighted files in the active panel |
| `api.param` | `str` — the per-button "Param" field (set in the button editor) |
| `api.config` | `dict` of Quopus's live runtime config (don't mutate unless you save it back) |
| `api.parent_widget` | The main window — pass this as the parent for any `QDialog` you open |
| `api.log(msg)` | Write to the Quopus status bar |
| `api.refresh()` | Force a re-list of both panels (call after creating/moving/deleting files) |
| `api.notify(title, body, kind='info'│'warn'│'error')` | Pop a `QMessageBox` |
| `api.input(title, prompt, default='')` | Text-input dialog → `Optional[str]` (None on cancel) |
| `api.ask_yes_no(title, body)` | Confirmation dialog → `bool` |
| `api.pick_file(title, save=False, filters=...)` | Native file dialog → `Optional[Path]` |
| `api.pick_dir(title)` | Native folder dialog → `Optional[Path]` |

**Optional metadata** (any of these can be omitted; sensible defaults are derived from `ACTION_NAME`):

```python
ACTION_LABEL       = "My Action"        # shown in the picker and on the button
ACTION_DESCRIPTION = "What it does"     # appears as a tooltip / status hint
ACTION_PARAM_LABEL = "Folder name"      # placeholder text for the button-editor Param field
```

**File naming conventions.** Files whose names start with an underscore (`_helpers.py`, `_shared.py`) are treated as helper modules and **not** loaded as actions — use those for shared code that your real action modules import from. Plugin files are also identified by extension: only `.py` is scanned.

**Reloading.** `Config → Reload custom modules` re-runs the discovery scan. It rebuilds the catalog from scratch, evicts the previously-loaded plugin modules from `sys.modules` so a Python re-import actually re-executes the file, and refreshes the button bank so cells bound to a custom action pick up changed labels and handlers. Modules that fail to load (syntax error, missing `ACTION_NAME`, missing `run()`) are reported as a list in a dialog so you can find and fix the typo without hunting through stderr.

**Two sample modules** ship in `<exe-dir>/custom_modules/`:

- **`example_hello.py`** — minimal "Hello, World" that shows how to read `api.selected` and `api.param`, and how to pop a `notify()` dialog. Use this as a starting point.
- **`text_reader_sample.py`** — read-only text viewer for the file currently highlighted in the active panel. Demonstrates: lazy Qt imports inside `run()` to keep startup cheap, encoding fallback (UTF-8 → CP1252 → Latin-1), building a custom `QDialog` parented to Quopus's main window, `QShortcut`-based hotkeys (`Ctrl+F` find, `F3` find-next, `Escape` close), and `QFileDialog.getSaveFileName()` for "Save copy as..." with re-encoding to UTF-8.

**Security note.** Custom modules execute in the same Python process as Quopus. They have the same filesystem and network privileges Quopus itself runs with — there is no sandboxing. Treat `custom_modules/` the way you'd treat `~/.bashrc`: only put code there that you wrote yourself or got from a source you trust. The first-run `README.txt` that Quopus drops into the folder repeats this warning so anyone you share your config tree with sees it too.

### Subprocess handling
External programs (Run, Shell, External Script, Execute Command, file associations) are launched **fully detached**:
- Quopus is never blocked
- Launched programs survive Quopus closing
- Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
- POSIX: `start_new_session=True`
- stdin/stdout/stderr → DEVNULL
- Shell-button gets a visible cmd window (`CREATE_NEW_CONSOLE`)

### All button actions

**Viewers / Editors:** `read` (F3), `hexread` (F9), `edit` (F4), `show`, `play`, `compare` (Alt+F11), `image_viewer` (force-open file in the image viewer regardless of extension), `archive_viewer` (force-open in archive browser), `disasm` (force-open in the 6502 disassembler), `d64editor` (CBM disk-image editor), `basic_editor` (BASIC v2 editor with Send & Run via U64 REST API), `retrogfx_file` / `retrogfx` / `retrogfx_browser` (Retro GFX Viewer entry points for a specific file, the launcher dialog, and the folder browser).

**File ops:** `copy` (F5), `move` (F6), `delete` (F8), `rename` (Shift+F6), `multi_rename` (Ctrl+M), `makedir` (F7), `comment` (Alt+F8), `datestamp`, `protect`, `checkfit`, `getsizes` (Ctrl+L), `assign` (used internally by the button-assignment context-menu workflow).

**Archive:** `archive` (Alt+F5, pack), `extract` (Alt+F9, unpack)

**Convert:** `petscii_convert` (interactive picker), `ascii_to_petscii` (direct), `petscii_to_ascii` (direct)

**Nav/system:** `parent` (Backspace), `root` (Ctrl+\), `goto_dir`, `reread` (F2), `swap` (Ctrl+U), `back`, `forward`, `info` (Alt+Enter), `search` (Ctrl+S), `find` (Alt+F7), `run`, `shell`, `print`, `ftp` (Ctrl+F connect dialog), `ftp_site` (direct reconnect to a saved bookmark by name), `ftp_upload` (upload selected files to a saved bookmark), `qdrive` (Quopus Drive connect dialog), `qdrive_site` (direct reconnect to a saved Quopus Drive bookmark), `buffers`, `dir_reverse`, `select_all` (Ctrl+A), `select_none`.

`ftp` opens the connect dialog; `ftp_site` is the direct-connect variant that takes a saved bookmark name as its `param` and connects without any UI - useful for one-click reconnect to a frequently-used server. `ftp_upload` skips the browser and directly uploads tagged/selected files to a named bookmark's `path` - useful for "publish to my BBS dropzone" buttons.

`qdrive` and `qdrive_site` work the same way for [Quopus Drive](#quopus-drive-remote-machine-mount) — a custom-protocol equivalent of FTP that talks to a Python server you run on the remote machine, with TLS + HMAC + MAC-binding auth. `qdrive` opens a connect dialog with the saved-bookmark list and a new-connection form; `qdrive_site` takes a bookmark name as its `param` and connects in one click. After connect, the bookmark's `initial_drive` is used if set, otherwise Quopus asks which of the server's exposed drives to mount in the target lister pane.

**Custom:** `external_script`, `execute_command`, `custom_cmd`

**Music:** `shuffle_sids` — pick and play a random SID from the current panel. `shuffle_mods` — same for tracker modules. `sidplayer` — force-open a `.sid` in the SID Player (e.g. when the file's extension isn't `.sid` for some reason). `sidplayer_playlist` — open the SID Player with a pre-built playlist passed in param (advanced). `multi_sid` — open the multi-SID parallel player with 2-4 tagged SIDs (also reachable from the right-click "▶ Play as multi-SID" menu entry). `modplayer` / `modplayer_playlist` — same flavours for tracker modules.

**Special:** `hotkey` — fires a built-in Quopus key-combo handler. Param = the combo string (`Alt+U`, `Ctrl+B`, `F1`, …). Used internally by the Button Config dialog when you pick from the Hotkey dropdown — you usually don't set this manually.

**Retro:** `u64view` — opens the [Ultimate 64 stream viewer](#ultimate-64-video-stream-viewer-altu) (Alt+U). Non-modal, drag-and-drop autostart of `.prg` / `.d64` / etc. via the U64 REST API. `asm64` — opens the [Assembly64 Browser](#assembly64-browser) for searching the hackerswithstyle.se scene archive (CSDb / HVSC / OneLoad64 / Gamebase64). `vice_memory` — opens the [Memory Viewer](#memory-viewer--editor--cheat-engine) connected to a running VICE emulator via binary monitor (config: `vice_host`/`vice_port`). `c64_emu_config` — opens the [C64 emulator config dialog](#c64-emulator-integration) (executable path + args template). `toggle_non_dos83` — toggles the [Hide 8+3 filenames filter](#filename-filter-hide-83-names) on the active lister.

**Terminal:** `telnet` — opens the [Telnet / Raw TCP / SSH terminal client](#telnet--raw-tcp--ssh-terminal-client). Pass a saved session name as the `param` to one-click-reconnect to that BBS / shell. Bind to a button + hotkey for instant access to your favourite boards.

**Database:** `database` — opens the [Quopus Database browser](#quopus-database-catalog--search). Non-modal browser with Files / Disks / Watch / Issues / Stats tabs; right-click results for copy-to-inactive-Lister, extract-from-D64, or reveal-in-active-Lister.

**Misc:** `about`, `config`, `quit`

### Directory reverse (AmiExpress)
Action `dir_reverse` — reverses AmiExpress BBS-style multi-line file listings.

### Comments, Datestamp, Protect
- **Comments:** Amiga-style filenotes stored as `<filename>.comment` sidecar files next to the originals. The `Comment` button (Alt+F8 / right-click → Comment) opens a multi-line editor; saving writes/replaces the sidecar, leaving an empty comment removes it.
- **Shift+Double-click on a file** — fast path to *view* an existing comment without going through the edit dialog. Pops up a read-and-save viewer if `<filename>.comment` exists, or offers to create one if not. Plain double-click still opens the file as usual.
- **Datestamp:** change file modification time
- **Protect:** set file permissions/attributes

---

## Hotkeys (Total Commander-compatible)

### Function keys
| Key | Action |
|-----|--------|
| `F1` | Help / About |
| `F2` | Refresh both listers |
| `F3` | View (internal auto: text/hex/image/archive/guide) |
| `F4` | Edit (uses configured editor) |
| `F5` | Copy to other side |
| `F6` | Move to other side |
| `F7` | Make directory |
| `F8` / `Del` | Delete |
| `F9` | Hex view |
| `F10` | Config menu |

### Shift + F-keys
| Key | Action |
|-----|--------|
| `Shift+F4` | Create new text file + open in editor |
| `Shift+F5` | Copy with new name in same directory |
| `Shift+F6` | Inline rename |
| `Shift+Del` | Permanent delete (no recycle bin) |
| `Shift+F10` | Context menu of current row |

### Alt + F-keys
| Key | Action |
|-----|--------|
| `Alt+F` | **FILE_ID.DIZ preview toggle** |
| `Alt+F1` | Drive picker for left lister |
| `Alt+F2` | Drive picker for right lister |
| `Alt+F3` | Open with system default app |
| `Alt+F4` | Exit |
| `Alt+F5` | Pack (create archive, threaded) |
| `Alt+F7` | Find Files dialog (filename / text / hex search, recursive, threaded) |
| `Alt+F9` | Unpack (extract archive) |
| `Alt+F11` | **Compare** two files (text/hex side-by-side diff) |
| `Alt+Enter` | Info / properties |
| `Alt+U` | **Ultimate 64 stream viewer** (live VIC video + audio over LAN) |

### Ctrl combinations
| Key | Action |
|-----|--------|
| `Ctrl+A` | Select all |
| `Ctrl+B` | Branch view (flat-list all files in subtrees); Ctrl+B again to exit |
| `Ctrl+C` | Clipboard copy (real file-URLs, works with Explorer) |
| `Ctrl+D` | Directory hotlist / bookmarks |
| `Ctrl+F` | FTP connect |
| `Ctrl+H` | Hunt (find) |
| `Ctrl+I` | Invert tags |
| `Ctrl+L` | Get sizes (computed) |
| `Ctrl+M` | Multi-rename tool |
| `Ctrl+N` | New FTP connection |
| `Ctrl+Q` | Quick view |
| `Ctrl+R` | Refresh both listers |
| `Ctrl+S` | Search filter (tag by pattern) |
| `Ctrl+U` | Swap sides |
| `Ctrl+V` | Clipboard paste (copy or move depending on cut flag) |
| `Ctrl+X` | Clipboard cut |
| `Ctrl+Z` | Edit file comment |
| `Ctrl+Space` | Toggle tag on current row |
| `Ctrl+\` | Go to root |
| `Ctrl+PgUp` / `Backspace` | Parent directory |
| `Ctrl+Enter` | Copy filename (basename) to clipboard |
| `Ctrl+Shift+Enter` | Copy full path to clipboard |
| `Ctrl+Shift+F` | FTP disconnect |
| `Ctrl+Left` | Send current path to left lister |
| `Ctrl+Right` | Send current path to right lister |
| `Ctrl+F3` | Sort by name |
| `Ctrl+F4` | Sort by extension |
| `Ctrl+F5` | Sort by date |
| `Ctrl+F6` | Sort by size |

### Numpad (Norton / Total Commander classic tagging)
| Key | Action |
|-----|--------|
| `Num +` | Tag files by wildcard (e.g. `*.lha`) |
| `Num -` | Untag files by wildcard |
| `Num *` | Invert tags |

### Lister-local (while a lister has focus)
| Key | Action |
|-----|--------|
| `Tab` | Switch to other lister |
| `Space` | Toggle tag on current row, move down |
| `Enter` | Enter directory / open file via configured viewer |
| `Esc` | Disconnect remote FTP lister |

### Module Player (MOD/XM/S3M/IT)
| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `Esc` | Close (stops audio cleanly) |
| `Left` | Skip back 5 seconds |
| `Right` | Skip forward 5 seconds |
| `Ctrl+Right` / `N` | Next track (shuffle mode) |
| `Ctrl+Left` / `P` | Previous track (shuffle mode) |

### SID Player
| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `Esc` | Close (stops audio cleanly) |
| `Right` / `N` / `+` | Next subsong (clamps at max) |
| `Left` / `P` / `-` | Previous subsong (clamps at 1) |
| `Ctrl+Right` / `Ctrl+N` | Next track (shuffle mode) |
| `Ctrl+Left` / `Ctrl+P` | Previous track (shuffle mode) |

### Mouse
| Action | Function |
|--------|----------|
| Left-click | Select / move cursor |
| Shift+Click | Range-select |
| Ctrl+Click | Toggle item in selection |
| Left-drag | Drag&Drop (Copy default, Shift held = Move) |
| Right-click | Context menu (incl. Size: bytes/blocks toggle) |
| **Middle-click** | **Jump to parent directory** (anywhere in lister body) |
| Double-click | Open file (or enter directory) |
| **Shift+Double-click** | **Open the file's `.comment` sidecar** if one exists; offer to create one if not |
| Right-click column header | Column-specific menu (Size: bytes/blocks toggle, sort, reverse) |
| Drag column boundary | Resize column (persists) |
| Click column header | Sort by column (toggle reverse) |
| Click drive button | Navigate active panel (default - configurable per-bookmark to active/both/left/right) |
| Shift+Click drive button | Navigate BOTH panels at once |
| Middle-click drive button | Navigate RIGHT panel only |

---

## Configuration

Config file: `config/quopus.cfg` (JSON).

**Stored keys:**
- `left_path`, `right_path` — last directories (default: `Path.home()`)
- `drives` — list of drive entries. Each entry is one of:
  - `{type: "local", label, path, path_right (optional), open_in: "active"|"both"|"left"|"right"}`
  - `{type: "ftp", label, host, port, user, path, mode: "passive"|"active", password (optional)}`
- `buttons` — main 6×6 grid of action button configs (label, action, color, param, hover_text, hover_image)
- `buttons_shift` — second 6×6 grid, shown while Shift is held. Same shape as `buttons`. Empty by default.
- `file_assoc` — extension → handler mapping
- `ftp_bookmarks` — saved FTP connections (max 30, used by the connect dialog's bookmark dropdown and the `ftp_site` action)
- `hotlist` — directory bookmarks (Ctrl+D)
- `buffers` — navigation history buffers
- `column_widths` — per-side per-column pixel widths (`{"QUOPUS.1": {...}, "QUOPUS.2": {...}}`)
- `sort_state` — per-side sort key + reverse flag (`{"QUOPUS.1": {"key": 1, "reverse": true}, ...}`)
- `window_geometry` — last window x/y/w/h + state (normal/maximized/fullscreen). Saved using `frameGeometry()` for cross-platform consistency.
- `size_display` — Lister Size column mode: `"bytes"` (default, human-readable) or `"blocks"` (C64 disk blocks, 256 B = 1 bl)
- `text_reader_font_size`, `text_reader_fg`, `text_reader_bg` — TextReader appearance (zoom + colors), persisted across sessions
- `app_font_scale_percent` — global UI font scale (50–300, default 100). Multiplies every stylesheet base size; changes apply live via Settings → Appearance without restart.
- `app_font_pointsize_override` — body-text base-size override (0 = off, else 6–30). Replaces the BASE for the 10–12 px stylesheets so e.g. setting 14 here gives "14 px body text scaled by the % above". Larger headings keep their relative differentiation.
- `app_font_family` — global font family for widgets that don't have inline CSS (menus, dialog labels, drive buttons, ...). Empty = platform default. Topaz / Topaz-8 stay hardcoded for the lister content and viewers — only the SIZE scales there.
- `c64_show_illegal` — boolean: show illegal opcodes in the C64 disassembler
- `c64_emulator` — path to C64 emulator executable (e.g. `C:\VICE\x64sc.exe`)
- `c64_emulator_args` — argument template with `{file}`, `{name}`, `{dir}` tokens
- `vice_host` — host/IP of the VICE binary monitor, default `127.0.0.1`. Used by the `vice_memory` action to connect to a running VICE.
- `vice_port` — TCP port of the VICE binary monitor, default `6502`. Enable in VICE with `-binarymonitor` command-line flag (also reachable through the C64 emulator config args template).
- `u64_host` — IP / hostname of the Ultimate 64. Used by the U64 streamer, Commander dialogs (Mount, Cfg Edit, Backup, ...), Assembly64 browser's Run/Mount actions, and the standalone streamer. Auto-discoverable via the Discover button in the Config dialog.
- `u64_video_port` — UDP port for the video stream, default `11000`.
- `u64_audio_port` — UDP port for the audio stream, default `11001`.
- `u64_telnet_port` — TCP port for the telnet menu fallback path, default `23`.
- `u64_http_port` — TCP port for the REST API, default `80`. Override for custom firmware ports.
- `u64_password` — network password (Ultimate firmware 3.12+ feature), sent as the `X-Password` header on every REST call. Empty by default.
- `u64_video_only` — skip the audio receiver entirely when streaming.
- `u64_always_on_top` — keep the streamer window above other windows.
- `u64_screenshot_dir` — where the **Snap** button saves PNG captures. Empty means use the default (`~/Pictures/Ultimate64/` on Linux/macOS, `%USERPROFILE%\Pictures\Ultimate64\` on Windows). The **Rec** button writes recordings to the same folder.
- `u64_record_format` — `"mp4"` (default) or `"png_seq"`. Selects the output format for the streamer's **Rec** button. MP4 uses libx264 via an ffmpeg subprocess (requires `ffmpeg` on PATH); PNG-seq writes one numbered PNG per frame into a per-capture folder. Toggleable via right-click on the Rec button.
- `asm64_endpoints` — optional dict overriding the URL paths used by the Assembly64 browser. Defaults are inferred from the Ultimate firmware's source; set via the **Endpoints...** button in the Asm64 dialog when the hackerswithstyle.se server changes routes.

**Database sidecar files** (auto-created in `config/`):
- `quopus_db.sqlite` — the SQLite catalog (plus `quopus_db.sqlite-wal` and `quopus_db.sqlite-shm` while WAL is active)
- `watched_folders.json` — list of folders the FS watcher monitors, restored on launch
- `db_browser_size_mode.txt` — bytes-vs-blocks display preference for the DB browser
- `db_browser_last_open.txt` — remembers the directory of the last `Open DB...` so the dialog doesn't always start from $HOME
- `db_browser_dedupe.txt` — MD5-dedupe toggle state for the Files tab (`on` / `off`)
- `asm64_favorites.json` — starred Assembly64 releases
- `asm64_searches.json` — named saved searches for the Assembly64 browser
- `asm64_last_results.json` — auto-cache of the most recent Assembly64 result list so reopening the dialog resumes where you left off

**Environment variables:**
- `QUOPUS_INGEST_WORKERS` — number of parallel scanner workers (default `2`, range 1-16). On fast SSDs setting to 4-8 speeds bulk scans up noticeably; on spinning HDDs leave at 2 to avoid seek thrashing.

Settings auto-save on change.

---

## External tool integration

Buttons can be configured for `external_script` (direct Popen) or `execute_command` (shell=True for pipes/redirects). Both are launched fully detached so Quopus stays responsive and the program survives Quopus closing.

> Note: For C64 emulator launch from inside the disassembler workbench, the **F5 / Run in C64** integration is the recommended path — see the Disassembler section. The external-tool buttons below are for general-purpose tool integration (text editors, packers, scripts).

**Substitution tokens** in the param string get **shell-quoted automatically** for the host platform — `shlex.quote()` on POSIX, double-quote with `""`-escape on Windows. So a button param like `ef3usb b %f` works correctly even when the file path contains spaces, like `/home/soenke/combat school/main.prg`. (Earlier versions did not quote token expansions, so paths with spaces broke at the first space.)

| Token | Expands to |
|-------|------------|
| `%f` | First selected/tagged file (full path, **quoted**) |
| `%F` | All selected/tagged files, space-separated, each quoted |
| `%n` | First selected file (basename only, **quoted**) |
| `%p` | Current source directory (**quoted**) |
| `%d` | Current destination / other-side directory (**quoted**) |
| `%i` | **Prompted user input** — when this token appears in the param, an "Enter filename" dialog pops up before the command runs. The default suggestion is the current date+time as `YYYYMMDD-HHMMSS` (so multiple captures don't overwrite each other and the file order matches capture order alphabetically). User cancels → command is not executed. User leaves the field empty → the auto-name is used. The result is **shell-quoted** so spaces in the typed name don't break parsing. |
| `%%` | Literal `%` |

**Use cases:**
- `external_script`: `C:\VICE\x64sc.exe %f` — open C64 disk/tape in VICE
- `external_script`: `"C:\Program Files\Notepad++\notepad++.exe" -n %f` — edit in Notepad++
- `external_script`: `python C:\scripts\packrelease.py %p` — run pack script on current dir
- `external_script`: `ef3usb b %f` — flash PRG via EasyFlash3 USB (works with paths that have spaces)
- `external_script`: `ef3usb r %i.d64` — read a disk from real Floppy via EF3-USB; pops "Enter filename" dialog so you can name the dump (or leave empty for an auto date-time name)
- `external_script`: `nibtools -r %i.nib` — same idea for nibtools, name the nib file at run-time
- `execute_command`: `scp %F user@bbs.example.com:/incoming/` — SCP-upload tagged files
- `execute_command`: `dir %p > %d\listing.txt` — redirect dir listing to other side
- `execute_command`: `md5sum %F > %d\checksums.txt` — checksum to file
- `execute_command`: `tar czf %d/%i.tar.gz %F` — pack tagged files into a tarball, prompt for archive name

Set via Config → Action buttons, or right-click a file → Assign to button → Custom → External script.

### Optional external binaries auto-discovered in `<quopus>/external/`

Quopus looks for these binaries in a fixed order: (1) explicit path in `quopus.cfg`, (2) `<quopus>/external/<tool>[.exe]` — the **portable drop-in location**, (3) system `PATH`. So you can carry Quopus around on a USB stick with all its helpers, without polluting the system PATH.

| Binary | Used for | Source |
|---|---|---|
| `recoil2png` / `recoil2png.exe` | RECOIL backend in the Retro GFX Viewer — adds 552+ retro graphics formats (Atari, Amiga IFF, MSX, ZX Spectrum, Apple, NEC PC, SAM Coupé, ...) | https://recoil.sourceforge.net/ |
| `nibconv` / `nibconv.exe` | Quopus Database scanner — converts G64/NIB/NBZ raw-track disk images to D64 in-memory so their directories can be BAM-walked and entries indexed. Without this, those files are stored by MD5 only. | https://c64preservation.com/dp.php?pg=nibtools |
| `unlzx` / `unlzx.exe` | Quopus Database scanner — extracts LZX archives so their members can be indexed. Common on Amiga scene releases. | Aminet `util/arc/unlzx` |
| `rar.exe` | RAR archive packing | WinRAR install or standalone download |
| `lha` | LHA archive packing | LhaForge (Windows), liblhasa (Linux) |
| `unrar` | RAR archive reading | WinRAR / unrar binary |

Drop the binary into `<quopus>/external/` and Quopus picks it up on the next launch. No restart needed for viewers — the lookup happens each time you open a file. The directory contains a `README.txt` describing the convention.

**TAPClean** is handled differently — its GPL **source** ships under `external/tapclean/src/` and Quopus compiles it on first use (needs `gcc`/`cc` + `make`), caching the binary. It powers the TAP cassette toolkit's loader identification and file extraction. On a machine without a compiler, drop a prebuilt `tapclean[.exe]` into `external/tapclean/src/`, or rely on the built-in Python fallback analyzer. See `external/tapclean/QUOPUS_INTEGRATION.md`.

> **See also:** if `external_script` and `execute_command` aren't enough — e.g. you want a real Python function that pops dialogs, parses files, talks to APIs, then re-lists the panel — write it as a [custom module](#custom-modules-user-plugins) instead. Custom modules run in the Quopus process with full Qt access, get the active panel's selection passed in directly, and don't need any inter-process plumbing.

---

## File format support summary

### Archives (browse + extract)
- ZIP (read+write)
- TAR / TAR.GZ / TAR.BZ2 / TAR.XZ
- LHA / LZH (read-only, requires `lhafile`)
- LZX (read-only via external `unlzx`, used by the database scanner)
- RAR (read-only, requires `rarfile` + unrar binary)
- 7Z (read-only, requires `py7zr`)
- GZ (bare gzip single file)

### Archives (pack)
- ZIP, TAR, TAR.GZ, TAR.BZ2, TAR.XZ, GZ — built-in (Python stdlib)
- LHA — external `lha` binary required
- RAR — external `rar.exe` binary required (WinRAR)

### Images
- PNG, JPG/JPEG, GIF (animated), BMP, WEBP
- TIFF, PPM, PGM, PBM, ICO, ICNS, SVG

### Retro graphics (Retro GFX Viewer)
**Native C64 decoders** (no external dependencies):
- Koala Painter: `.kla`, `.koa`
- Hi-Res Bitmap: `.hbm`, `.hir`, `.hpi`, `.fgs`
- Art Studio: `.aas`, `.art`
- Advanced Art Studio: `.ocp`, `.mpi`, `.mpic`
- Doodle: `.dd`, `.ddp`, `.jj`
- Amica Paint: `.ami` (RLE-compressed)
- Drazpaint / Drazlace: `.drp`, `.drz`, `.drl`, `.dlp`
- CDU Paint (Compunet Doodle): `.cdu`
- Interpaint Hires/MC: `.iph`, `.ipt`
- FLI / FLI Designer / FLI Graph: `.fli`, `.flg`, `.bml`, `.fd2`, `.fed`
- AFLI: `.afl`
- IFLI / Funpaint / Gunpaint: `.ifl`, `.fun`, `.fp2`, `.gun`
- BFLI / Big FLI: `.bfl`, `.bfli`
- Vidcom 64: `.vid`
- Image System Hires/MC: `.ish`, `.ism`
- Interlace Hires Editor: `.ihe`
- Hires Interlace: `.hlf`, `.hie`

**With RECOIL backend** (drop `recoil2png` into `<quopus>/external/`):
- Atari 8-bit (143 formats), Atari ST/STE/TT/Falcon (~120 formats: NEO, PI1/PI2/PI3, PC1/PC2/PC3, DEGAS, TNY, CA1/CA2/CA3, ESM, IFF, IIM, IMG, ...)
- Amiga ILBM/IFF/HAM/HAM-E/Sliced HAM, DCTV, ACBM, RGB8, RGBN
- Apple II/IIe/IIGS/Macintosh: HGR, DHR, A2R, BMC, BSL, ...
- MSX/MSX2/MSX2+ (35 formats: SC2-SC8, SCA, SCC, SR5-SR8, ...)
- ZX Spectrum/Profi/Evolution/Next (26 formats: SCR, ATR, MC, GIG, NXI, ...)
- NEC PC-80/88/98, SAM Coupé, BBC Micro, Oric, TRS-80, Sharp X1/X68000, Robotron KC85, Spectravideo, NEC PC-FX
- 552+ total formats — see https://recoil.sourceforge.net/formats.html for the complete list

### C64 charsets (Charset Viewer + Editor)
- `.chr`, `.fnt`, `.64c`, `.bin`, `.art` — 2KB or 4KB charset binaries
- Optional 2-byte load-address prefix (C64 PRG-style) auto-detected and preserved on save
- PNG-to-char-sequence conversion: bitmap → matched char codes with multiple output formats (hex, ASM, BASIC DATA, raw bytes)

### Text with encoding detection
- Plain ASCII/UTF-8/Latin-1
- ANSI/ANSI-BBS with color codes
- PETSCII (C64 SEQ, PET files) in mixed / upper / hybrid / smart modes
- Topaz-encoded Amiga files

### Hypertext
- AmigaGuide (`.guide`, `.hlp`, or any file with `@DATABASE`)

### Tracker module audio
- ProTracker MOD (4-channel + extended: `M.K.`, `M!K!`, `M&K!`, `FLT4`, `FLT8`, `4CHN`, `6CHN`, `8CHN`, `CD81`, `OKTA`)
- FastTracker II XM
- ScreamTracker 2/3 STM/S3M
- Impulse Tracker IT
- OpenMPT MPTM
- OctaMED MED
- MultiTracker MTM
- Composer 669
- Oktalyzer OKT
- Plus everything else libopenmpt supports (DBM, DSM, FAR, ULT, AMF, PSM, etc.) - configurable via the file-association dialog

### SID music
- PSID v1 (1 SID, no metadata)
- PSID v2 (1 SID, name/author/released, chip model preference)
- PSID v3 (1 or 2 SIDs)
- PSID v4 (up to 3 SIDs)
- RSID (real C64 init - BASIC ROM hooks, full 6502 reset sequence)

### C64 binaries (6502 disassembler / editor / assembler)
- PRG (Commodore 64 program with 2-byte load address) — fully editable in-place
- BIN (raw binary - treated as PRG) — fully editable in-place
- CRT (cartridge image with `C64 CARTRIDGE` header + CHIP packets) — view only
- TAP (tape image with `C64-TAPE-RAW` header) — view in the disassembler, or open in the **TAP cassette toolkit** for loader ID, file listing, hex search and `.prg` extraction (see the dedicated section above)

(SID files default to the SID Player above. To disassemble a SID's player code instead, change the `.sid` association to `c64disasm` in the File Associations dialog.)

### Disk images
- **D64** — 1541 single-sided 35-track (and 40-track extended variants) — full browse / edit / extract / validate / Sep+ separator editor
- **D71** — 1571 double-sided
- **D81** — 1581 3.5"
- **D80** / **D82** — 8050/8250 IEEE-488 drives (read-only browsing, indexed by the database scanner)
- **G64** / **G71** / **G81** — raw-track variants (need external `nibconv` for full indexing in the database)
- **NIB** / **NBZ** — MNIB raw-track dumps (need external `nibconv`)
- **DMS** — Amiga DiskMasher (FILE_ID.DIZ extraction from track 80)

### Remote
- FTP, FTPS explicit, FTPS implicit, SFTP
- **Ultimate 64** live VIC video stream (UDP), SID audio stream (UDP), REST API control (HTTP) — see the U64 Streamer section above

---

## Architecture

```
quopus_commander/
├── quopus.py                    # Main entry point
├── fonts/                      # User drops TTFs here (C64 Pro Mono, Topaz)
├── config/                     # Auto-created config dir
│   ├── quopus.cfg              #   Settings (JSON)
│   ├── quopus_db.sqlite        #   Database catalog (+ -wal/-shm sidecars)
│   ├── watched_folders.json    #   Folders the FS watcher monitors
│   ├── db_browser_size_mode.txt  # Bytes/blocks toggle preference
│   ├── db_browser_dedupe.txt   #   MD5-dedupe toggle for Files tab
│   ├── db_browser_last_open.txt  # Last "Open DB..." dialog dir
│   ├── asm64_favorites.json    #   Starred Assembly64 releases
│   ├── asm64_searches.json     #   Named saved searches
│   └── asm64_last_results.json #   Last Assembly64 result list
├── cache/                      # Auto-created on first disasm
│   └── disasm/<md5>.pkl        #   Cached lines + render-data per file
├── external/                   # Optional drop-in CLI tools
│   ├── recoil2png[.exe]        #   552+ retro gfx formats for Retro GFX viewer
│   ├── nibconv[.exe]           #   G64/NIB/NBZ → D64 for database scanner
│   ├── unlzx[.exe]             #   LZX extraction for database scanner
│   └── README.txt              #   Documents drop-in convention
├── libsidwrapper.so            # SID engine wrapper (Linux x86-64, shipped)
├── sidwrapper.dll              # SID engine wrapper (Windows x86-64,
│                                 #   shipped, fully statically linked)
├── sidwrapper.cpp              # Source for the SID wrapper - C ABI shim
│                                 #   around libsidplayfp's C++ classes
└── quopus_lib/
    ├── __init__.py
    ├── palette.py              # Colors, QSS, fonts, 40 button presets
    ├── config.py               # Load/save config, defaults
    ├── dirmodel.py             # QAbstractTableModel + TaggedItemDelegate (4 cols)
    ├── device_panel.py         # Drive column (DeviceColumn)
    ├── lister.py               # FileLister with QTreeView, drag&drop, sort, right-click menu
    ├── main_window.py          # QuopusMain, button grid, hotkeys, DIZ panel,
    │                           #   hover overlay, window-geom persistence
    ├── actions.py              # ActionDispatcher (45+ act_* methods,
    │                           #   detached spawn, cancellable transfers,
    │                           #   threaded archive workers, overwrite dialog)
    ├── dialogs.py              # ButtonConfig, Buffers, DirReverse
    ├── readers.py              # TextReader (incl. pixmap PETSCII renderer), HexReader
    ├── amigaguide_viewer.py    # AmigaGuide hypertext parser + viewer
    ├── archive_viewer.py       # ZIP/TAR/LHA/RAR/GZ browser
    ├── c64_disasm.py           # 6502 disassembler with dual-pane viewer
    ├── tap_decoder.py          # C64 .tap pulse decoder (CBM ROM +
    │                           #   turbo), file/block reconstruction
    ├── tap_analyzer.py         # pure-Python TAPClean-style analyzer
    │                           #   (fallback loader detection + report)
    ├── tap_loaders.py          # 124 loader fingerprints (from the
    │                           #   TAPClean ft[] table)
    ├── tap_scanners.py         # loader-specific Python scanners
    │                           #   (Turbotape 250) used in fallback mode
    ├── tap_tapclean.py         # wrapper for the bundled GPL TAPClean
    │                           #   binary (built from external/tapclean/);
    │                           #   primary loader ID + PRG extraction
    ├── tap_toolkit.py          # TAP cassette toolkit dialog (block list,
    │                           #   hex+search, histogram, waveform, report)
    ├── mod_player.py           # ProTracker-style module player
    │                           #   (libopenmpt via ctypes + sounddevice)
    ├── sid_player.py           # GoatTracker-style SID player
    │                           #   (libsidplayfp via libsidwrapper +
    │                           #    parallel vis engine for per-voice scopes)
    ├── image_viewer.py         # PNG/JPG/GIF/BMP/WEBP with zoom
    ├── retro_gfx_decoders.py   # 18 native C64 bitmap decoders (Koala,
    │                           #   FLI, AFLI, BFLI, Drazpaint, Doodle,
    │                           #   CDU, Amica, Interpaint, Vidcom, ...)
    │                           #   plus RecoilBackend wrapper for the
    │                           #   external recoil2png binary (552+
    │                           #   formats Atari/Amiga/MSX/ZX/Apple)
    ├── retro_gfx_viewer.py     # BitmapViewer + RecoilViewer + folder
    │                           #   browser mode (arrow-key nav), plus
    │                           #   CharsetViewer with grid + live text
    │                           #   preview + Char Editor (transforms,
    │                           #   undo/redo, copy/paste hex, backup-
    │                           #   on-save), plus PNG→char-sequence
    │                           #   converter (Hex/ASM/BASIC DATA output)
    ├── c64_chargen.bin         # Original VICE chargen-906143-02.bin
    │                           #   (4096 bytes: 2KB upper + 2KB lower).
    │                           #   Used by the CBM separator editor's
    │                           #   PNG-to-PETSCII matcher for font-
    │                           #   independent ROM-direct glyph lookup
    ├── dir_reverse.py          # AmiExpress dir-listing parser
    ├── encodings.py            # ANSI/PETSCII parsers
    ├── petscii_tables.py       # CGTerm SCCONV, C64 color palette
    ├── petscii_convert.py      # ASCII↔PETSCII text conversion
    ├── petscii_dialog.py       # Conversion dialog with live preview
    ├── multi_rename.py         # Batch rename with template tokens
    ├── compare_dialog.py       # Side-by-side text/hex diff viewer
    │                           #   with prev/next-diff navigation
    ├── u64_streamer.py         # Ultimate 64 VIC video + SID audio
    │                           #   stream receiver, REST API drag-drop,
    │                           #   U64 Commander dialogs: Mount,
    │                           #   DriveStatus, Config Editor, Backup,
    │                           #   Discover broadcast, machine control
    │                           #   buttons (Reset/Pause/Menu/Reboot/Off/
    │                           #   Snap screenshot)
    ├── basic_editor.py         # BASIC v2 editor with petcat-style
    │                           #   PETSCII control codes, syntax
    │                           #   highlighting, tokenizer, Send&Run
    │                           #   via U64 REST API
    ├── asm64_browser.py        # Assembly64 browser - search the
    │                           #   hackerswithstyle.se scene archive
    │                           #   (CSDb / HVSC / OneLoad64 / Gamebase64)
    │                           #   with filter form, favorites,
    │                           #   saved-searches, year-sweep, and
    │                           #   run/mount/download via U64 REST
    ├── cbmfiles.py             # CBM disk-image (D64/D71/D81) reader +
    │                           #   editor, LNX archives, ZipCode encoder/
    │                           #   decoder, separator editor with PNG
    │                           #   import (Otsu auto-threshold, ROM-
    │                           #   direct PETSCII glyph matching, dual
    │                           #   font/ROM preview).  Also exposes
    │                           #   parse_disk_image() for the database
    │                           #   scanner.
    ├── database.py             # SQLite catalog: schema, FTS5 trigram
    │                           #   search, scan_status crash recovery,
    │                           #   set_db_path / switch_to_default for
    │                           #   read-only loading of shared DBs
    ├── db_scanner.py           # Bulk scanner + AsyncScanner: tree walk
    │                           #   with C64-only filter, archive walk
    │                           #   (ZIP/LHA/LZX/RAR/7Z), BAM-parse of
    │                           #   D64/D71/D81/D80/D82, external
    │                           #   nibconv for G64/NIB/NBZ, password +
    │                           #   corruption issue logging
    ├── db_watcher.py           # Live FS watcher (watchdog package if
    │                           #   installed, else polling). Debounce,
    │                           #   non-C64 pre-rejection, persistent
    │                           #   folder list in watched_folders.json
    ├── ingest_queue.py         # Shared worker pool for scanner +
    │                           #   watcher. Bounded queue, configurable
    │                           #   worker count via QUOPUS_INGEST_WORKERS
    ├── db_browser.py           # Non-modal DB browser dialog: 5 tabs
    │                           #   (Files/Disks/Watch/Issues/Stats),
    │                           #   right-click context menu with copy-
    │                           #   to-Lister / extract / reveal, top
    │                           #   toolbar with Open DB / Save As /
    │                           #   Open Own DB for sharing DBs between
    │                           #   sysops
    ├── file_assoc.py           # Extension → handler mapping
    │                           #   (auto-upgrades old .sid/c64disasm
    │                           #   to sidplay on first run)
    ├── file_assoc_dialog.py    # File-association config UI
    ├── fs_backend.py           # LocalFs / RemoteFs adapters
    ├── ftp_backend.py          # FTP/FTPS/SFTP backends
    ├── ftp_browser.py          # FTP connect + browser UI
    ├── telnet_client.py        # Telnet / Raw TCP / SSH terminal
    │                           #   with VT100 + PETSCII + ATASCII,
    │                           #   saved-sessions, auto-reconnect,
    │                           #   session logging, Topaz/C64 font
    ├── asm6502.py              # Standalone mini 6502/6510 assembler
    │                           #   used by FindDialog's Assembly tab
    │                           #   for byte-pattern search with
    │                           #   wildcards
    ├── find_dialog.py          # Alt+F7 - 4-tabbed Find Files
    │                           #   (filename / text / hex / 6502 asm)
    │                           #   with threaded walker, live results,
    │                           #   feed-to-listbox
    ├── vice_monitor.py         # VICE binary monitor TCP client.
    │                           #   Used by act_vice_memory to grab
    │                           #   RAM from a running VICE instance
    ├── shuffle.py              # Shuffle-mode helper for SID + MOD
    │                           #   players. Threaded scan, random
    │                           #   playlist build, prev/next nav
    ├── songlengths.py          # HVSC Songlengths.md5 parser for
    │                           #   accurate per-subsong durations
    ├── spectrum.py             # 10-band ISO equalizer spectrum
    │                           #   analyzer widget (FFT, peak-hold)
    │                           #   used in MOD + SID players
    ├── vumeter.py              # Stereo VU meter widget (legacy -
    │                           #   replaced by spectrum.py in the
    │                           #   players, kept for reference)
    ├── window_state.py         # Window geometry + table column
    │                           #   width persistence helpers (every
    │                           #   dialog calls install_window_state
    │                           #   with a stable key)
    ├── license.py              # Ed25519 license signature check
    │                           #   (trial / pro / lifetime tiers)
    ├── license_ui.py           # Trial-mode nag screen, title-bar
    │                           #   watermark, registration dialog
    └── crypto.py               # Premium-module decryption hook
                                #   (retained for future Model B
                                #   licensing; not used in Model C)
```

---

## Trial release build system (.qpe encrypted modules)

For shipping a patch-resistant trial build, Quopus has a runtime AES-GCM module-encryption layer + custom import hook. Premium-feature-bearing modules ship as `.qpe` files instead of `.py`, decrypted in memory at import time. Trial users can run them — the runtime `license.has_feature()` checks inside still gate features — but they can't open the source and patch those checks out.

**Components**:

- `build_trial_release.py` — the build driver, sits in the project root. Stages the project into a temp directory, encrypts the listed modules into `.qpe` in the staging copy only (source tree untouched), bakes the runtime keys into `quopus_keys.cfg`, sanity-checks for forbidden files, ZIPs the result as `quopus_build_YYYYMMDD_HHMMSS.zip`, deletes the staging dir.

- `quopus_lib/_qpe_loader.py` — Python `sys.meta_path` finder/loader pair. When asked to import `quopus_lib.<name>` and there's no matching `.py`, it looks for `<name>.qpe` at the same location, decrypts via `crypto._decrypt_qpe()`, compiles + exec's the resulting bytes as a module. Falls through to the default Python finder when a real `.py` is present, so dev workflow is unaffected — the hook only fires in distribution builds.

- `quopus_lib/__init__.py` — imports `_qpe_loader` first thing so the hook is registered before any other module loads.

- `quopus_keys.cfg` — runtime configuration with two hex strings:
  - `public_key_hex` — the production license-signature verification key (so Pro licenses verify correctly; without this, the verifier falls back to the demo key which can only validate Trial licenses)
  - `master_key_hex` — the AES-256 key that decrypts the `.qpe` modules

**Module list** — configured at the top of `build_trial_release.py` in `MODULES_TO_ENCRYPT`. Default protected set:
- `license.py`, `license_ui.py` (license verification & nag screens)
- `telnet_client.py`, `sid_player.py` (premium-feature modules)
- `asm64_browser.py`, `db_browser.py`, `db_scanner.py` (modules with feature-gated paths)
- `actions.py` (orchestration with multiple license gates)

**Not encrypted by design**: `crypto.py` (the decryptor itself — encrypting it would create a chicken-and-egg circular dependency), `_qpe_loader.py` (the hook itself), `__init__.py` (package bootstrap).

**Workflow**:
1. Dev runs `python build_trial_release.py` in the project root
2. First run creates `license_keygen_master.key` — back it up, this key encrypts every future shipped `.qpe`
3. ZIP is produced in `<project>/quopus_build_*.zip`
4. Customer extracts the ZIP, places their `.lic` file in `<quopus>/config/quopus.lic`
5. Quopus boots, the import hook decrypts `.qpe` modules on demand, license verifier matches the embedded `public_key_hex` against the `.lic` signature

**BUILD_INFO.txt** in the ZIP root lists the build timestamp, which modules are encrypted, and a checklist for diagnosing "module not found" errors after extraction.

**CLI flags**:
- `--skip-encrypt` — stage and ZIP the project as-is (no encryption), useful as a comparison baseline
- `--keep-plaintext-in-zip` — debug aid: ship the `.py` alongside the `.qpe`. The import hook prefers `.py` when both exist, so this lets you verify the hook is wired up correctly before locking down to encrypted-only.
- `--output-dir PATH` — where to write the ZIP (defaults to the project root, or `/mnt/user-data/outputs` if that exists)

**Source tree stays clean** — the build script never modifies the original `.py` files. Encryption happens entirely in a temp staging directory under `%TEMP%/quopus_trial_staging/` which is deleted after the ZIP is written. Your dev workflow keeps unencrypted source for normal day-to-day editing.

**Common issues**:

- **"License invalid" / "Signature mismatch" after extracting a trial ZIP**: the `quopus_keys.cfg` in the ZIP is missing or has no `public_key_hex` line. Without that key Quopus falls back to the embedded demo public key which can only verify Trial licenses — Pro licenses signed by the production secret key get rejected as forgeries. Fix: rebuild with the current `build_trial_release.py` (which derives `public_key_hex` from `license_keygen_secret.key` via `license_keygen.py keys-cfg` and writes both `public_key_hex` and `master_key_hex` into the staging `quopus_keys.cfg`). Verify by `cat quopus_keys.cfg` in the extracted ZIP — both lines must be present, non-empty, and 64 hex chars each.

- **`ModuleNotFoundError: No module named 'quopus_lib.actions'` at startup**: the import hook isn't running. Either `quopus_lib/_qpe_loader.py` is missing from the extracted ZIP, or `quopus_lib/__init__.py` doesn't import it as the first thing. Open `quopus_lib/__init__.py` and confirm it has `from . import _qpe_loader` on its first non-docstring line. Check `BUILD_INFO.txt` in the ZIP root to confirm you're running an actual trial build, not an older plaintext one.

- **`ImportError: Failed to decrypt foo.qpe`**: the `master_key_hex` in `quopus_keys.cfg` doesn't match the key used to encrypt the `.qpe`. Most likely cause: the trial ZIP was rebuilt with a fresh `license_keygen_master.key` after the customer already extracted an earlier ZIP. Send the customer the matching new ZIP; alternatively keep a single master key file long-term (back it up immediately) and never let it get regenerated.

---

## Differences from Amiga Directory Opus 4
- Drive list is scrollable (up to 40 drives) instead of fixed
- Operations run on host OS filesystem, not AmigaOS
- Comments via NTFS-ADS on Windows / xattr on Linux instead of Amiga filenotes
- `Protect` dialog shows OS-native file permissions
- Real ZIP/TAR/RAR/GZ/SFTP support added
- PETSCII conversion for BBS use
- Multi-rename tool is new (Quopus 4 had single rename)
- AmigaGuide viewer integrated
- DIZ preview panel for archive browsing
- Drag & Drop into and out of the application
- **6502 disassembler / editor / mini-assembler** with dual-pane workflow and 65C02 support — far beyond what Quopus 4 offered
- **ProTracker-style module player** — Quopus 4 needed external tools (PT, EaglePlayer); this one renders MOD/XM/S3M/IT inline with full UI animations
- **Real-time SID player** with GoatTracker-style multi-voice display — emulates 1/2/3 SID chips, instant subsong switching, per-voice oscilloscopes
- **Retro GFX Viewer** — 18 native C64 bitmap decoders (Koala, FLI, AFLI, BFLI, Drazpaint, Amica, ...) plus optional RECOIL backend for 552+ Atari/Amiga/MSX/ZX/Apple formats. Quopus 4 only viewed IFF.
- **CBM disk image editor** — full D64/D71/D81 browse/edit/extract/validate with the famous Cracker-scene separator editor (Sep+ button) including PNG-to-PETSCII import using a bundled C64 character ROM for font-independent glyph matching
- **Charset Viewer + Char Editor** — pixel-grid editor for 8×8 C64 character fonts with transforms (mirror, rotate, shift), undo/redo (Ctrl+Z), auto-backup on save, and PNG-to-char-sequence conversion (output as hex / ASM `.byte` / BASIC DATA)
- **Quopus Database** — built-in SQLite catalog of every C64/Amiga file across the entire collection, with FTS5 substring search, live FS watching, parallel ingestion, archive & disk-image content indexing, crash recovery, and shareable read-only DB files. No analogue in Quopus 4 (or any other classic file manager).

## Differences from Total Commander
- Workbench / Quopus 4 color scheme
- No tabs (Ctrl+T / Ctrl+W are stubs)
- Single-pane preview (Ctrl+Q) rather than split-pane
- Drives displayed as a scrollable column, not a row of combo boxes
- AmiExpress-specific features (`dir_reverse`)
- Buttons can have hover-image previews
- DIZ preview replaces TC's Lister Plugin previews

---

## Troubleshooting

**Q: Drag & Drop into Explorer doesn't always work**
A: Windows blocks drag/drop between processes running with different privilege levels. Run Quopus as the same user as Explorer (or both as Admin / both as Standard).

**Q: Config button doesn't exist / no File Associations menu**
A: Delete `config/quopus.cfg` and restart — an old config may have the wrong button layout.

**Q: `.asm` files open as binary in hex viewer**
A: All common source/text extensions are handled internally. If a specific type opens wrong, go to Config → File associations → Add your extension, set viewer to internal/text.

**Q: PETSCII conversion shows all lowercase**
A: The file may be in `upper` charset mode. In the converter dialog, switch from `Auto-detect` to `Upper (all UC)` or `Smart (sUBS → Subs)`.

**Q: FTP disconnects during transfer**
A: Check whether the server requires passive mode (default on) or specific timeouts. For TLS servers with self-signed certs, Quopus already disables certificate verification.

**Q: `pip install PyQt6` fails on Windows**
A: Enable long paths in Windows (`LongPathsEnabled` registry key) or use a short installation path.

**Q: Venv creation fails with `venvlauncher.exe` copy error**
A: You have Python from Microsoft Store. Install from python.org instead, or use `pip install --user` without a venv.

**Q: Cancel button on copy doesn't work for large files**
A: Cancel takes effect at the next chunk boundary (1 MiB). On a fast SSD you'll see partial files removed within a few hundred ms; on slow networks the FTP-side might take longer to interrupt.

**Q: Window doesn't restore the size I had**
A: Geometry is saved on every resize/move/state-change; if it's stuck, delete `config/quopus.cfg` and let Quopus rebuild.

**Q: RAR/LHA pack says "binary not found"**
A: These formats need external command-line tools — Python libs only decompress. Install WinRAR (`rar.exe`) for RAR, LHA32/LhaForge for LHA on Windows. Add to PATH or place in `C:\Program Files\WinRAR\`.

**Q: Database: G64/NIB/NBZ files show up in the Issues tab as `no_directory`**
A: Raw-track disk images (G64 from VICE, NIB / NBZ from MNIB) aren't directly BAM-walkable — they have to be converted to D64 first. Quopus uses the external `nibconv` tool (from nibtools) for this conversion. Download nibtools from https://c64preservation.com/dp.php?pg=nibtools and drop `nibconv` (or `nibconv.exe` on Windows) into `<quopus>/external/`. After the next scan the directories of those disk images will be indexed properly. Without nibconv the files are still indexed by MD5 (you can find them) but their internal PRGs won't be visible in the Disks tab.

**Q: Database: a ZIP/RAR/7Z archive shows up in the Issues tab as `password`**
A: The archive is encrypted and the scanner can't index its contents without the password. The Issues tab lets you filter by `password` to see all affected files in one place — useful when reviewing a Scenebase collection. The scanner safely continues past these files; they're not blockers. Quopus doesn't prompt for passwords during scanning because that would block the worker pool for hours on a big tree; if you want a specific encrypted archive indexed, extract it manually first.

**Q: Database: search for `aa` finds nothing but `aass` exists**
A: SQLite's FTS5 trigram tokenizer indexes substrings of length 3+, so 1-2 character queries can't use the index. Quopus falls back to a `LIKE '%aa%'` scan in that case — slower but correct. The browser's status bar shows `slow scan` when this fallback is being used. If you see `slow scan` for queries that should be fast (3+ chars), the index may be out of date; click Vacuum in the Stats tab to rebuild.

**Q: Database: I crashed Quopus during a big scan. Will I lose data?**
A: No. Every file row in the DB has a `scan_status` field (`pending` / `done` / `failed`). When ingestion of a multi-member archive starts, the parent file is written immediately with status `pending`, members are inserted as `done`, and the parent flips to `done` only at the end. On next launch, Quopus scans for `pending` rows, clears them, and re-enqueues them — so the previously-half-indexed archive gets redone properly. You'll see no missing files. The Stats tab shows `pending` and `failed` counts so you can verify the recovery ran clean.

**Q: Database: bulk scan feels slow on my big SSD**
A: Increase the worker count: set the `QUOPUS_INGEST_WORKERS` environment variable to 4 or 8 before launching Quopus. The default is 2, which is the sweet spot for spinning HDDs (more would cause seek thrashing). On NVMe SSDs you can usually go higher without losing ground. Range is 1-16.

**Q: Database: how do I share my catalog with another sysop?**
A: Open the database browser, click **Save As...** in the toolbar, pick a location. This produces a portable `.sqlite` file (via SQLite's backup API — works even with the watcher running and uncommitted WAL entries). Send it to your friend via Discord/email/USB. They click **Open DB...** in their Quopus and pick your file. It loads in read-only mode (Scan/Watch/Cleanup/Reset are all disabled while a shared DB is active) so they can search and right-click-copy without any risk of modifying your catalog.

**Q: Double-clicking a `.mod` file does nothing / shows hex**
A: The MOD player needs `libopenmpt` and Python's `sounddevice` package. Install via `pip install sounddevice numpy` for Python deps, then provide the libopenmpt library: on Linux `apt install libopenmpt0t64` (or `libopenmpt0` on older systems), on macOS `brew install libopenmpt`, on Windows download the binary release from <https://lib.openmpt.org/files/libopenmpt/bin/> and drop the 5 DLLs (`libopenmpt.dll`, `openmpt-mpg123.dll`, `openmpt-ogg.dll`, `openmpt-vorbis.dll`, `openmpt-zlib.dll`) next to `quopus.py`. See the ProTracker-style Module Player section above for download details.

**Q: `.sid` opens in the disassembler instead of the SID Player**
A: You probably have an older `quopus.cfg` from before the SID Player was added. The auto-upgrade should fix this on the next launch. If it doesn't: open Config → File associations, find `.sid`, change Viewer to Internal → `sidplay`. Or simply delete `config/quopus.cfg` and let Quopus rebuild defaults.

**Q: SID Player error "0xC000012F" or "STATUS_INVALID_IMAGE_FORMAT"**
A: This was a Windows-specific bug where the loader tried `libsidwrapper.so` (Linux ELF) instead of `sidwrapper.dll`. Fixed in the current release — make sure you're running the latest `sid_player.py`. If you still see it: confirm `sidwrapper.dll` (~3 MB) is present next to `quopus.py`, and that you're using **64-bit Python** (`python -c "import platform; print(platform.architecture())"` should show `64bit`). The shipped DLL is x86-64; for 32-bit Python you'd need to rebuild.

**Q: SID sounds wrong — too fast, off-key, distorted**
A: Pre-1.0 versions had a bug where per-voice oscilloscope rendering corrupted the audio engine state, making playback run at the wrong tempo. The current release uses two parallel libsidplayfp engines (one for audio, one for visualization) so this can't happen. If you still hear it on the latest release: open the SID file in another player (e.g. SIDPLAY/W) to confirm it's not the tune itself, then check that your soundcard is set to 44100 Hz native (Windows audio control panel → device properties → Advanced tab).

**Q: 2SID / 3SID tunes only play 3 voices**
A: Make sure you have the latest `sidwrapper.dll` / `libsidwrapper.so` — the older versions didn't pass the `secondSidAddress` / `thirdSidAddress` config to libsidplayfp. The current release reads `info->sidChipBase(1/2)` from the tune and patches the engine config before load. The pattern view should show 6 columns for 2SID and 9 for 3SID; if it shows 3 then either the tune is actually 1SID despite its filename, or the wrapper is outdated.

**Q: SID Player shows no waveforms in the oscilloscopes**
A: The visualization engine is a separate libsidplayfp instance. If it failed to load (e.g. out of memory), the audio still works but scopes stay flat. Check stderr for any error messages on startup. Also: very quiet passages or held single notes may produce flat-looking waveforms - try a track with active leads.

**Q: SID Player CPU usage seems high**
A: Each physical voice runs in its own libsidplayfp engine to give correctly-synchronized per-voice oscilloscopes. That's 1 audio + 3 vis engines for 1SID (4 total), 7 for 2SID, 10 for 3SID. On modern hardware a single libsidplayfp engine costs 1-3% CPU at 44 kHz; 7 engines means ~10-20% CPU is normal for 2SID. **Toggle the `VIS` checkbox** in the bottom-right of the player to disable per-voice visualization entirely - this drops CPU usage to just the audio engine cost (single-engine baseline). The audio output is unaffected; only the per-voice oscilloscopes stop updating.

**Q: Audio output is silent / "audio open: ..." error**
A: `sounddevice` couldn't open the default output device. Possible causes: another app has exclusive lock (e.g. WASAPI exclusive mode), the default output is set to a disconnected device, or the sample rate doesn't match. The MOD player uses 48 kHz, the SID player uses 44.1 kHz. If your system default rate is different, Windows should resample automatically; if it doesn't, change the device's default rate in the Windows Sound control panel.

**Q: Retro GFX Viewer says "RECOIL backend not available"**
A: Download `recoil2png.exe` (Windows) or `recoil2png` (Linux/macOS) from https://recoil.sourceforge.net/ and drop the binary into `<quopus>/external/`. No PATH or config change needed — Quopus auto-discovers it on the next viewer open. Alternatively, click the launcher's "recoil2png path..." button and pick the binary location explicitly. Native C64 formats (Koala, FLI, AFLI, Drazpaint, Doodle, CDU, Amica, Interpaint, Vidcom, BFLI, IFLI, Funpaint, Gunpaint, Art Studio, Advanced Art Studio, Hi-Res Bitmap) work without RECOIL.

**Q: A multicolor C64 image opens in the wrong colors**
A: Some legacy formats embed the background color in a non-standard position. If the native decoder shows wrong colors, try the file in RECOIL (right-click → Open With → Retro GFX (RECOIL)). If RECOIL also gets it wrong, the file may be a non-standard variant — please report with a sample.

**Q: Drazpaint files load but look corrupted**
A: The Drazpaint layout requires a specific byte order: 0..1000 = screen RAM, 1024..2024 = color RAM, 2048..10048 = bitmap, 10048 = bg color. Total expected size 10049 bytes. If your file has the bitmap interleaved with the color RAM (some old Vandalism News uses a different variant), it won't decode correctly in the native decoder — open it via RECOIL for those variants.

**Q: Charset Viewer doesn't show my .bin file**
A: The viewer expects 2048 or 4096 raw bytes (one or two charsets). If the file has a 2-byte load-address prefix (C64 PRG-style), it auto-detects and strips it. Files outside these sizes won't open as charsets — they'll go through the standard hex viewer instead. Use the File Associations dialog to override.

**Q: Char Editor click hits the wrong cell**
A: Fixed in build 20260516_120403. The label was sized to the parent's AlignCenter container which was wider than the pixmap, causing click coordinates to be relative to the wrong origin. The fix uses `setFixedSize(pm.size())` plus a pixel-accurate click-to-cell helper. If you see this on an older build, update.

**Q: Separator editor's PNG import produces unrecognizable output**
A: Two common causes: (1) the source image's aspect ratio is far from 16:1 — a 200×200 logo squeezed into 128×8 loses 95% of its vertical detail. Use horizontal banner sources (~512×16 or wider) for best results. (2) The threshold is wrong for the image's brightness range — disable "Auto threshold" and manually pick a value that brings out the features. The dual preview (Font path / ROM path) helps spot which step is failing.

**Q: PNG → char sequence converter produces 30+ space chars**
A: The PNG's threshold is putting most pixels into background. Increase or decrease the threshold (try 64 or 192) and watch the source preview — when the source detail shows up clearly, the matched chars will too. Also try the Invert toggle if your source is white-on-black.

**Q: Charset save overwrote my file without backup**
A: That's a bug — the current code creates `<file>.bak` (and time-stamped `<file>.bak.YYYYMMDD_HHMMSS` if `.bak` already exists) on every save. If you don't see a backup, check the directory listing — `.bak` files are sometimes hidden by the file filter. If still missing, please report which version you're running.

---

## Building the SID wrapper from source

The shipped `sidwrapper.dll` (Windows x86-64) and `libsidwrapper.so` (Linux x86-64) are built from `sidwrapper.cpp` against libsidplayfp 2.16.1 with both the ReSIDfp and ReSID emulators embedded. If you want to rebuild — e.g. for ARM, for 32-bit Python, with a different libsidplayfp version, or with custom compile flags:

### Linux

```bash
sudo apt install libsidplayfp-dev
g++ -O2 -fPIC -shared sidwrapper.cpp \
    -o libsidwrapper.so \
    -lsidplayfp -lstdc++
```

The system `libsidplayfp.so` is dynamically linked. Output ~22 KB.

### macOS

```bash
brew install libsidplayfp
clang++ -O2 -fPIC -shared sidwrapper.cpp \
    -o libsidwrapper.dylib \
    -lsidplayfp -lstdc++
```

### Windows x86-64 (cross-compile from Linux with MinGW-w64)

This is how the shipped `sidwrapper.dll` is built. Requires xa65 (6502 cross-assembler used by libsidplayfp's build) and the MinGW-w64 g++ toolchain.

```bash
sudo apt install g++-mingw-w64-x86-64-posix autoconf automake libtool xa65

# Get libsidplayfp source
mkdir -p /tmp/sidbuild && cd /tmp/sidbuild
git clone --depth 1 --branch v2.16.1 https://github.com/libsidplayfp/libsidplayfp.git
cd libsidplayfp
git submodule update --init src/builders/resid-builder/resid
mkdir -p src/builders/exsid-builder/driver/m4   # workaround for autoreconf
autoreconf -i

# Cross-compile static libs for Windows
mkdir build-win64 && cd build-win64
../configure --host=x86_64-w64-mingw32 \
    --prefix=/tmp/sidbuild/install-win64 \
    --disable-shared --enable-static \
    --without-exsid \
    CXXFLAGS="-O2" CFLAGS="-O2"
make -j4
make install

# Build the wrapper DLL, fully static
cd /path/to/quopus_commander
x86_64-w64-mingw32-g++ -O2 \
    -I/tmp/sidbuild/install-win64/include \
    -shared sidwrapper.cpp \
    /tmp/sidbuild/install-win64/lib/libsidplayfp.a \
    /tmp/sidbuild/install-win64/lib/libstilview.a \
    -o sidwrapper.dll \
    -Wl,--export-all-symbols \
    -static -lpthread
```

Output ~3 MB DLL, only depends on `KERNEL32.dll` and `msvcrt.dll`. Verify with:
```bash
x86_64-w64-mingw32-objdump -p sidwrapper.dll | grep "DLL Name"
```

### Windows native build (MSYS2)

In an MSYS2 MinGW64 shell:
```bash
pacman -S mingw-w64-x86_64-libsidplayfp mingw-w64-x86_64-gcc
g++ -O2 -shared sidwrapper.cpp \
    -o sidwrapper.dll \
    -lsidplayfp -lstdc++ -static -lpthread
```

Note: the MSYS2 build dynamically links libsidplayfp by default — copy the relevant DLLs from `/mingw64/bin/` next to `quopus.py` if you go this route. The cross-compile-from-Linux path produces a fully self-contained DLL with no extra deps.

### Wrapper API

`sidwrapper.cpp` exposes a flat C ABI around libsidplayfp's C++ classes:

| Function | Description |
|----------|-------------|
| `sid_create(sample_rate)` | Create engine handle |
| `sid_destroy(handle)` | Release engine + tune + builder |
| `sid_load(handle, data, len)` | Load PSID/RSID bytes |
| `sid_select_subsong(handle, n)` | Set current subsong (1-based); also patches secondSidAddress / thirdSidAddress from the tune for multi-SID support |
| `sid_get_subsongs(handle)` | Total subsong count |
| `sid_get_default_subsong(handle)` | Tune's preferred starting subsong |
| `sid_get_info_string(handle, idx)` | idx 0=title, 1=author, 2=released |
| `sid_get_num_sids(handle)` | 1, 2, or 3 |
| `sid_get_chip_model(handle, sid_num)` | "MOS6581", "MOS8580", "either" |
| `sid_play(handle, buf, samples)` | Render `samples` int16s (stereo interleaved) |
| `sid_is_playing(handle)` | Engine state |
| `sid_stop(handle)` | Reset playback |
| `sid_get_time_ms(handle)` | Playback position in ms |
| `sid_mute(handle, sid_num, voice, enable)` | Mute control - voice 0..2, sid_num 0..2 |
| `sid_get_error(handle)` | Last error message string |

Used as `ctypes.CDLL` from `quopus_lib/sid_player.py`.

---

## Credits

- **Directory Opus 4** by Jonathan Potter (Amiga, GPSoftware)
- **C64 Pro Mono** font by Style64
- **C64 character ROM** (chargen-906143-02.bin) from the VICE distribution (Commodore International)
- **PETSCII table** from c64-wiki.de/wiki/PETSCII-Tabelle
- **Total Commander** by Christian Ghisler (hotkey reference)
- **AmigaGuide** format reference from AmigaOS docs (Commodore/Escom)
- **RECOIL** (Retro Computer Image Library) by Piotr Fusik — backend for 552+ retro graphics formats
- **nibtools** by Pete Rittwage and contributors — `nibconv` is used by the Quopus Database scanner to decode G64/NIB/NBZ raw-track disk images
- **libsidplayfp** by Leandro Nini, Antti Lankila, Simon White et al. (SID emulation engine)
- **ReSID / ReSIDfp** by Dag Lem, Antti Lankila (cycle-accurate SID emulation)
- **libopenmpt** by OpenMPT contributors (tracker module playback)
- **GoatTracker** by Cadaver (UI inspiration for the SID Player)
- **ProTracker 2.3D** by Lars Hamre (UI inspiration for the Module Player)
