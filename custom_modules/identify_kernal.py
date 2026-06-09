# date_time: 2026-06-09 23:49
"""
Custom module: Identify C64 KERNAL ROM.

Computes the classic 3-byte 6502 chkloop checksum over an 8 KiB
ROM image and looks it up against a database of known C64 KERNAL
replacements, fast-loaders and DOS systems (901227-xx, JiffyDOS,
SpeedDOS, Dolphin DOS, Turbo Access, SD2IEC, ...).

The checksum algorithm and the lookup table are NOT original work
here -- they come from Jani (World of Jani) and are reproduced
verbatim so results match 1:1. This file only wraps that logic
(taken from the standalone identify_kernal.py CLI script) as a
Quopus action that runs over the file(s) highlighted in the active
panel - or recursively over selected folders (size pre-filtered to
8 KiB) - and shows a colour-coded report (green = identified, red =
unknown), with a "Copy report" button.

Source / credit:
  The 6502 chkloop checksum routine and the KERNAL/DOS/fastloader
  identification database originate from Jani's work at
  World of Jani -- https://blog.worldofjani.com/
  (Commodore KERNAL preservation / identification). All credit for
  the checksum scheme and the ROM database goes there; this file
  only provides the Quopus integration.

Drop this file into your custom_modules/ user folder
(Config -> Open custom modules folder), then Config -> Reload
custom modules. Bind it to a button or run it via the action
picker. No external dependencies.
"""

# ---- required metadata --------------------------------------
ACTION_NAME = "identify_kernal"

# ---- optional metadata --------------------------------------
ACTION_LABEL = "Identify C64 KERNAL ROM"
ACTION_DESCRIPTION = (
    "Compute the 3-byte checksum of the selected 8K ROM image(s) "
    "and identify the C64 KERNAL / DOS / fastloader against a "
    "built-in database. Accepts raw 8192-byte dumps and 8194-byte "
    ".prg files (2-byte load address is stripped automatically). "
    "Selected folders are scanned recursively (size pre-filtered).")
# No per-button param.
ACTION_PARAM_LABEL = ""


# =====================================================================
# Reference data + algorithm (verbatim from identify_kernal.py)
# =====================================================================

# KERNAL / DOS / fastloader checksum database.
# Source: Jani @ World of Jani -- https://blog.worldofjani.com/
KERNAL_DB = {
  (0xbd, 0xc7, 0x0b): "251104-04 sx64",
  (0xbc, 0xc7, 0x19): "251104-04 sx64 drive8",
  (0x56, 0xb5, 0xca): "390852-01 gs64",
  (0x54, 0x5b, 0x80): "64 turbodisk",
  (0xa7, 0x54, 0xdc): "64er v1 #1",
  (0x1c, 0x5c, 0x73): "64er v1 #2",
  (0xe4, 0x5c, 0x65): "64er v3",
  (0xa1, 0xc6, 0xb8): "64kernal sx64 dk",
  (0x60, 0x2c, 0x7a): "64kernal x2",
  (0xbb, 0xd4, 0xfd): "901227-01",
  (0x8b, 0xc7, 0x0b): "901227-02",
  (0xb6, 0xc7, 0x0a): "901227-03",
  (0xbd, 0xc6, 0xe3): "901227-03 2mhz",
  (0xc0, 0xc8, 0x20): "901227-03 3mhz",
  (0xc5, 0xc9, 0x62): "901227-03 4mhz",
  (0xb6, 0xc7, 0x18): "901227-03 defdrive8",
  (0xea, 0xc9, 0x09): "901227-03-dk",
  (0x58, 0xc2, 0x10): "901246-01 4064 & armageddon",
  (0x5c, 0xd1, 0x83): "906145-02-jp",
  (0x35, 0x64, 0x36): "beastsystem",
  (0x44, 0xa4, 0x32): "bs v1.31",
  (0x23, 0xa0, 0x6d): "bs v1.32",
  (0x67, 0x2c, 0xe9): "c64 kernal #1",
  (0xb0, 0xc6, 0x46): "c64 kernal #2",
  (0xb1, 0xc6, 0xa2): "c64 kernal #3",
  (0x6f, 0x6c, 0x47): "c64burstload_1571",
  (0xac, 0xb0, 0xdb): "cerrysoft",
  (0xae, 0x8e, 0x57): "cockroach turbo-rom v1 #0",
  (0xaf, 0x8e, 0x91): "cockroach turbo-rom v1 #1",
  (0xae, 0x8e, 0x5b): "cockroach turbo-rom v1 #2",
  (0x37, 0x77, 0xb6): "cyclone 1.0",
  (0xb4, 0xc0, 0xdd): "degussa sx64",
  (0x04, 0x7b, 0x5a): "delta electronics dos-rom v1.2",
  (0xdf, 0xab, 0x88): "digi-dos 1.0",
  (0xbf, 0xad, 0x19): "dolphin dos 1.0 mager",
  (0x4f, 0xc3, 0x0e): "dolphin dos 2.0 #0",
  (0x4a, 0xc2, 0xea): "dolphin dos 2.0 #0 sd",
  (0x43, 0xc2, 0x0f): "dolphin dos 2.0 #1",
  (0x46, 0xc3, 0x7b): "dolphin dos 2.0 #1 au",
  (0x48, 0xc2, 0xee): "dolphin dos 2.0 #2",
  (0x87, 0xc5, 0x0c): "dolphin dos 2.0 #3",
  (0x4f, 0xc3, 0x0b): "dolphin dos 3.0",
  (0x62, 0x99, 0x19): "dos-hypra-cent v1",
  (0x73, 0x96, 0x63): "dos-hypra-cent v2",
  (0x8c, 0xae, 0xe7): "dte fsd-system",
  (0x46, 0xca, 0x7b): "ewingdos v3.0",
  (0x46, 0x7b, 0x75): "exos v3 #1",
  (0x63, 0x7c, 0xf6): "exos v3 #2",
  (0xeb, 0x65, 0x2e): "exos v4",
  (0x53, 0xc2, 0xec): "flash 8",
  (0xfd, 0xc4, 0xb8): "flash! #0",
  (0x37, 0xe2, 0xcb): "flash! #1",
  (0x40, 0x9b, 0x87): "grischun system",
  (0x00, 0x56, 0xc9): "grischun turbo system",
  (0x12, 0x9b, 0x99): "hyper-dos +",
  (0x9f, 0xbd, 0xfb): "hypra system",
  (0xdd, 0x91, 0xf7): "hypra-speed 64er",
  (0x42, 0x85, 0x0a): "ieee488-8255 64er",
  (0x6f, 0xc9, 0x68): "jaffydos v1.0",
  (0x7a, 0xc8, 0xa6): "jaffydos v1.2",
  (0x59, 0xcb, 0xc1): "jaffydos v1.3 default",
  (0xc1, 0x61, 0x18): "jiffy dolphin",
  (0xf6, 0x6f, 0xd6): "jiffy dolphin sd2iec v1.0",
  (0xfe, 0x71, 0x0f): "jiffy dolphin sd2iec v1.1",
  (0x58, 0xb2, 0xab): "jiffydos f8",
  (0x68, 0xa9, 0xe5): "jiffydos6.01",
  (0x64, 0xa9, 0xcd): "jiffydos6.01 sx",
  (0xb1, 0x00, 0xdb): "mad max prism m5",
  (0x6f, 0x67, 0x81): "magnum load",
  (0x56, 0xd2, 0x6c): "masterrom v3",
  (0x6d, 0x67, 0x46): "megaload",
  (0xcd, 0x87, 0xfc): "mercury-rom v3.us",
  (0x7e, 0x9e, 0xbc): "piffydos",
  (0xa3, 0xdf, 0xbe): "powerload",
  (0x66, 0x0a, 0xf1): "powerload mod3",
  (0xdf, 0xf3, 0xe3): "professional dos 2/4l2",
  (0x5a, 0xdb, 0x14): "professional dos 3/5l2",
  (0x84, 0xbd, 0xfa): "professional dos v1",
  (0x42, 0x8f, 0x6b): "prologic dos classic",
  (0x63, 0x92, 0x90): "prologic dos classic userport",
  (0x41, 0x9b, 0x9d): "prologic dos r1",
  (0x09, 0x8f, 0x7d): "prologic system",
  (0xa6, 0x15, 0xad): "psw copy system",
  (0xb8, 0xb4, 0x90): "pswspeed v2.9",
  (0x94, 0x74, 0x29): "pswspeed v7.0",
  (0x12, 0x04, 0xe8): "rapidos 2.0",
  (0x1e, 0xb0, 0xf5): "rex dos",
  (0x2b, 0xec, 0x9a): "sd2iec kernal 1.0",
  (0xba, 0x64, 0x65): "sd2iec kernal 2.0",
  (0xa5, 0x64, 0xdb): "sd2iec kernal 2.1",
  (0xba, 0x64, 0x49): "sd2iec kernal 2.2",
  (0x00, 0x82, 0x0b): "sid image",
  (0xf9, 0x55, 0x55): "sjiffydos v1",
  (0xec, 0x56, 0x26): "sjiffydos v1 de",
  (0xd8, 0x56, 0x70): "sjiffydos v1 de f8",
  (0xec, 0x55, 0x9f): "sjiffydos v1 f8",
  (0x59, 0x66, 0x3e): "special rom v2.0",
  (0x5f, 0x97, 0xf4): "speeddos 85er",
  (0x24, 0x9c, 0x36): "speeddos expert",
  (0x05, 0xd2, 0x8d): "speeddos maba v3.78",
  (0xb2, 0xa6, 0x02): "speeddos plus 40track",
  (0xb2, 0xa6, 0x04): "speeddos plus 40track-grey",
  (0x9d, 0xae, 0x40): "speeddos plus blue",
  (0x99, 0xae, 0xca): "speeddos plus improved",
  (0x8c, 0xb3, 0xe3): "speeddos plus v2",
  (0x97, 0xaf, 0x3a): "speeddos plus v2.7 mr.z",
  (0x8a, 0xb3, 0xec): "speeddos plus+ v2",
  (0x8a, 0xb3, 0xcf): "speeddos plus+ v2 blk",
  (0x99, 0xae, 0xc9): "speeddos plus+ v2.7 blk",
  (0xb2, 0x8f, 0x0c): "speeddos system v1",
  (0x8e, 0xa4, 0x6f): "speeddos+ v1.1 40trk",
  (0xf3, 0x8c, 0x76): "stardos",
  (0xa8, 0x8e, 0x56): "stingkit dos v3",
  (0x91, 0xaf, 0x94): "superdos plus",
  (0xb3, 0xc5, 0xca): "swedish-02 325017-02",
  (0xb0, 0xc5, 0xc9): "swedish-03 325182-01",
  (0xba, 0xc7, 0x88): "sx64-scand",
  (0x7d, 0x7b, 0xcf): "tornado-dos",
  (0xf3, 0xd5, 0xd9): "tt-rom",
  (0x9f, 0xdc, 0x48): "turbo access 2.4",
  (0xea, 0xd5, 0x00): "turbo access 2.5",
  (0xe0, 0xd4, 0xe4): "turbo access 2.6",
  (0x20, 0xde, 0xd8): "turbo access 2.7",
  (0xa0, 0xdc, 0x2d): "turbo access 2.7 (2.4)",
  (0xa5, 0xdd, 0xf4): "turbo access 2.8",
  (0xdc, 0xa6, 0x34): "turbo kernal",
  (0xef, 0xc8, 0x0b): "turbo kernal normal #1",
  (0xee, 0xc7, 0xeb): "turbo kernal normal #2",
  (0xca, 0xa7, 0x11): "turbo kernal turbo",
  (0xcb, 0xe4, 0x51): "turbo process system",
  (0x15, 0x7d, 0x01): "turbo rom ii 3.2+",
  (0x25, 0xdf, 0xf7): "turbo trans 2.1",
  (0x25, 0xdf, 0xf8): "turbo trans 3.0",
  (0x33, 0x12, 0x12): "turbo-drive 1.0",
  (0x52, 0xc3, 0x3d): "turbo-process us",
  (0x94, 0x81, 0x5c): "turbo-rom mkiii"
}

# Raw KERNAL image size and the .prg variant (2-byte load address).
ROM_SIZE = 8192
PRG_SIZE = ROM_SIZE + 2


def calculate_checksum(data):
    """Reproduction of the 6502 asm chkloop algorithm.

    Sums 32 pages x 256 bytes (8192 total). chk2 counts the
    high-byte carries of the running sum; whenever chk2 itself
    wraps, the page offset y is XOR-folded into chk3. Returns
    (chk3, chk2, chk1) = (Hi, Mid, Lo), matching the key tuples
    in KERNAL_DB."""
    chk1 = 0  # lo
    chk2 = 0  # mid
    chk3 = 0  # hi
    a = 0

    # 32 Pages a 256 Bytes = 8192 Bytes total
    for page in range(32):
        for y in range(256):
            byte = data[page * 256 + y]
            a += byte
            if a > 255:
                a &= 0xFF
                chk2 += 1
                if chk2 > 255:
                    chk2 &= 0xFF
                    chk3 ^= y
    chk1 = a
    return (chk3, chk2, chk1)


# =====================================================================
# ROM loading
# =====================================================================

def _load_rom_bytes(path):
    """Read `path` and normalise it to exactly 8192 bytes.

    Returns (data, note) where data is a 8192-byte bytes object on
    success (note may carry an informational hint such as the .prg
    strip), or (None, reason) when the file can't be used."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        return None, f"cannot read: {e}"

    n = len(raw)
    if n == PRG_SIZE:
        # .prg with 2-byte load address -> strip it
        return raw[2:], "8194-byte .prg (load address stripped)"
    if n == ROM_SIZE:
        return raw, ""
    return None, (f"wrong size {n} bytes "
                  f"(need {ROM_SIZE}, or {PRG_SIZE} as .prg)")


def _identify(path):
    """Identify a single ROM file. Returns a dict with keys:
    name, ok (bool), checksum (tuple|None), kernal (str|None),
    note (str)."""
    data, note = _load_rom_bytes(path)
    if data is None:
        return {"name": path.name, "ok": False,
                "checksum": None, "kernal": None, "note": note}
    csum = calculate_checksum(data)
    kernal = KERNAL_DB.get(csum)
    return {"name": path.name, "ok": True, "checksum": csum,
            "kernal": kernal, "note": note}


# =====================================================================
# Quopus action entry point
# =====================================================================

def run(api):
    import os
    from pathlib import Path

    selected = list(api.selected or [])
    explicit_files = [p for p in selected if p.is_file()]
    folders = [p for p in selected if p.is_dir()]

    # Nothing useful selected -> offer a file picker.
    if not explicit_files and not folders:
        picked = api.pick_file(
            "Pick a C64 KERNAL ROM",
            filters="ROM images (*.bin *.rom *.prg *.901227* *.65* );;"
                    "All files (*)")
        if picked is None:
            return
        explicit_files = [picked]

    # De-dupe while preserving order: explicit files first (in
    # selection order), then the folder-walk candidates.
    seen = set()
    ordered = []

    def _add(p):
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            return
        seen.add(key)
        ordered.append(p)

    # Explicitly selected files are identified no matter their size -
    # the user picked them, so a wrong-size file is reported as
    # "skipped" rather than silently dropped.
    for p in explicit_files:
        _add(p)

    # Selected folders are walked RECURSIVELY. Here we DO pre-filter
    # by size (8192 raw / 8194 .prg) so a folder full of unrelated
    # files doesn't flood the report with "skipped" rows.
    if folders:
        api.log("Scanning folder(s) recursively for 8K ROM images...")
        candidates = []
        for folder in folders:
            for root, _dirs, files in os.walk(folder):
                for fn in files:
                    fp = Path(root) / fn
                    try:
                        sz = fp.stat().st_size
                    except OSError:
                        continue
                    if sz in (ROM_SIZE, PRG_SIZE):
                        candidates.append(fp)
        candidates.sort(key=lambda p: str(p).lower())
        # Soft guard against accidentally scanning a huge tree.
        if len(candidates) > 1000:
            if not api.ask_yes_no(
                    "Many candidates",
                    f"Found {len(candidates)} ROM-sized files in the "
                    f"selected folder(s).\n\nIdentify all of them?"):
                return
        for fp in candidates:
            _add(fp)

    if not ordered:
        api.notify(
            "Identify C64 KERNAL ROM",
            "No 8 KiB ROM images found in the selection.\n\n"
            "Selected folders are searched recursively for files of "
            f"exactly {ROM_SIZE} bytes (or {PRG_SIZE} as .prg).",
            kind="warn")
        return

    # Identify each candidate. For files found inside a selected
    # folder, show their path relative to that folder so same-named
    # ROMs in different subfolders stay distinguishable.
    results = []
    for p in ordered:
        r = _identify(p)
        r["name"] = _display_name(p, folders)
        results.append(r)

    _show_report(api, results)


def _display_name(p, roots):
    """Path relative to the first matching selected folder, else the
    bare filename (for top-level / explicitly-selected files)."""
    for root in roots:
        try:
            return str(p.relative_to(root))
        except ValueError:
            continue
    return p.name


# =====================================================================
# Report dialog
# =====================================================================

def _show_report(api, results):
    import html
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QTextEdit,
        QLabel, QPushButton, QApplication,
    )
    from PyQt6.QtGui import QFont, QKeySequence, QShortcut
    from PyQt6.QtCore import Qt

    total = len(results)
    n_ok = sum(1 for r in results if r["kernal"])
    n_unknown = sum(1 for r in results if r["ok"] and not r["kernal"])
    n_bad = sum(1 for r in results if not r["ok"])

    # ---- build the HTML body + a plain-text twin for "Copy" -----
    rows_html = []
    plain_lines = []
    for r in results:
        name = html.escape(r["name"])
        if not r["ok"]:
            rows_html.append(
                f"<div style='margin-bottom:10px;'>"
                f"<b>{name}</b><br>"
                f"<span style='color:#ffcc44;'>&nbsp;&nbsp;skipped &mdash; "
                f"{html.escape(r['note'])}</span></div>")
            plain_lines.append(f"{r['name']}\n  skipped - {r['note']}\n")
            continue

        hi, mid, lo = r["checksum"]
        csum_txt = f"Hi=${hi:02x}  Mid=${mid:02x}  Lo=${lo:02x}"
        note_html = ""
        if r["note"]:
            note_html = (f"<br><span style='color:#9aa;'>"
                         f"&nbsp;&nbsp;{html.escape(r['note'])}</span>")
        if r["kernal"]:
            verdict = (f"<span style='color:#5dff5d;'>"
                       f"{html.escape(r['kernal'])}</span>")
            plain_verdict = r["kernal"]
        else:
            verdict = ("<span style='color:#ff6060;'>"
                       "unknown (not in database)</span>")
            plain_verdict = "unknown (not in database)"
        rows_html.append(
            f"<div style='margin-bottom:10px;'>"
            f"<b>{name}</b><br>"
            f"<span style='color:#cfcfcf;'>&nbsp;&nbsp;{csum_txt}</span><br>"
            f"&nbsp;&nbsp;KERNAL: {verdict}{note_html}</div>")
        plain_lines.append(
            f"{r['name']}\n  {csum_txt}\n  KERNAL: {plain_verdict}\n")

    body_html = (
        "<div style='font-family:\"Topaz-8\",\"Topaz\",\"Courier New\","
        "monospace; font-size:13px; color:#e0e0e0;'>"
        + "".join(rows_html) + "</div>")
    plain_report = "\n".join(plain_lines)

    # ---- dialog scaffold ----------------------------------------
    dlg = QDialog(api.parent_widget)
    dlg.setWindowTitle("Identify C64 KERNAL ROM")
    dlg.resize(620, 460)
    dlg.setStyleSheet("QDialog { background-color: #999999; }")

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(6)

    summary = (f"  {total} file(s)  ·  "
               f"{n_ok} identified  ·  {n_unknown} unknown  ·  "
               f"{n_bad} skipped  ·  DB: {len(KERNAL_DB)} entries")
    header = QLabel(summary)
    header.setStyleSheet(
        "QLabel { background-color: #2040a0; color: white; "
        "padding: 4px 8px; "
        "font-family: 'Topaz','Courier New',monospace; }")
    layout.addWidget(header)

    view = QTextEdit()
    view.setReadOnly(True)
    view.setHtml(body_html)
    font = QFont("Topaz", 11)
    if not font.exactMatch():
        font = QFont("Courier New", 11)
        font.setStyleHint(QFont.StyleHint.TypeWriter)
    view.setFont(font)
    view.setStyleSheet(
        "QTextEdit { background-color: #1a1a1a; color: #e0e0e0; "
        "selection-background-color: #5566ff; selection-color: white; "
        "padding: 6px; }")
    layout.addWidget(view, 1)

    # ---- button bar ---------------------------------------------
    bb = QHBoxLayout()
    bb.setContentsMargins(0, 0, 0, 0)
    bb.setSpacing(6)

    btn_copy = QPushButton("Copy report")

    def _copy():
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(plain_report)
            api.log("KERNAL report copied to clipboard")
    btn_copy.clicked.connect(_copy)
    bb.addWidget(btn_copy)

    credit = QLabel("DB: World of Jani · blog.worldofjani.com")
    credit.setStyleSheet(
        "QLabel { color: #555; padding-left: 8px; "
        "font-family: 'Topaz','Courier New',monospace; font-size: 10px; }")
    bb.addWidget(credit)

    bb.addStretch(1)

    btn_close = QPushButton("Close")
    btn_close.clicked.connect(dlg.accept)
    bb.addWidget(btn_close)
    layout.addLayout(bb)

    QShortcut(QKeySequence("Escape"), dlg, activated=dlg.accept)

    # Also drop a one-line summary into the status bar.
    api.log(f"KERNAL: {n_ok} identified, {n_unknown} unknown, "
            f"{n_bad} skipped")

    dlg.exec()
