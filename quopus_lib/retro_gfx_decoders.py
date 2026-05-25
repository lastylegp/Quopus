"""C64 graphics format decoders.

Reine Decode-Funktionen ohne Qt-Dependencies. Jede Funktion bekommt
den File-Path und gibt ein dict zurueck:
    {
        'mode':   'multicolor' | 'hires',
        'width':  Pixel-Breite (320 oder 640 fuer Interlace),
        'height': Pixel-Hoehe (200 oder 400 etc.),
        'pixels': bytes der Pixel als 4-bit-Palette-Indizes (1 Byte
                   pro Pixel, value 0..15 = C64-Farbindex),
        'note':   optionaler String mit Format-Namen / Hinweisen,
    }

Bei Interlace-Formaten wird per Default das Average der 2 Frames
geliefert; Caller kann optional 'pixels_a' / 'pixels_b' zusaetzlich
abfragen falls beide Frames separat angezeigt werden sollen.

Spezialfaelle:
- Pack-Formate (Amica, Drazpaint mit RLE) werden VOR dem decode
  entpackt.
- Doodle hat eine eigene Memory-Anordnung mit screen-color vor
  bitmap.
- FLI verwendet 8 Colormaps die per Rasterzeile rotieren.
- IFLI = 2 FLI-Frames mit x-shift = 1 Pixel.
"""

import os


# C64 palette (Pepto calibration, identisch zu u64_streamer + viewer)
C64_PALETTE = [
    (0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF), (0x88, 0x39, 0x32),
    (0x67, 0xB6, 0xBD), (0x8B, 0x3F, 0x96), (0x55, 0xA0, 0x49),
    (0x40, 0x31, 0x8D), (0xBF, 0xCE, 0x72), (0x8B, 0x54, 0x29),
    (0x57, 0x42, 0x00), (0xB8, 0x69, 0x62), (0x50, 0x50, 0x50),
    (0x78, 0x78, 0x78), (0x94, 0xE0, 0x89), (0x78, 0x69, 0xC4),
    (0x9F, 0x9F, 0x9F),
]


# -----------------------------------------------------------------
# Read helper - load address handling
# -----------------------------------------------------------------

def _read_with_load(path):
    """Liest path. Returnt (load_addr_or_None, payload bytes).

    Wenn die ersten 2 Bytes wie eine Load-Address aussehen UND die
    Erweiterung sowas erwartet, strippen wir sie. Sonst returnen wir
    den ganzen Inhalt.
    """
    with open(path, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    # Liste der Endungen die typisch eine 2-Byte Load-Address haben
    LOAD_EXTS = {
        "prg", "koa", "kla", "art", "aas", "ocp", "fli", "afl", "ifl",
        "ifli", "iph", "ipt", "drp", "drz", "drl", "dlp", "ami", "fun",
        "fp2", "hed", "fpr", "hpc", "p64", "rp", "rpm", "him", "ism",
        "ish", "ims", "eci", "ecp", "gun", "shf", "shx", "shi", "ufl",
        "ufi", "uf2", "nuf", "p4i", "che", "fpt", "fcp", "fd2", "bml",
        "bfl", "bfli", "ffli", "dd", "jj", "pi", "pic", "64c", "mci",
        "mcp", "vbm", "bm", "vid", "wig", "a64", "cdu", "cle", "cwg",
        "dol", "eaf", "fgs", "fln", "gcd", "gig", "gih", "4bt", "hbm",
        "hir", "hpi", "hfc", "hlf", "hil", "ihe", "lp3", "mil", "mon",
        "mle", "pmg", "rp", "sar", "trp", "vic", "raw",
    }
    if ext in LOAD_EXTS and len(raw) >= 2:
        load = raw[0] | (raw[1] << 8)
        return load, raw[2:]
    # Raw / charset / unknown
    return None, raw


# -----------------------------------------------------------------
# Multicolor / Hires basic decode primitives
# -----------------------------------------------------------------

def _mc_block_render(pixels, bx, by, bitmap, c01, c10, c11, bg,
                       bmp_offset=0):
    """Rendert einen 8x8 Multicolor-Block in den pixels buffer.

    pixels: bytearray, 320*200 (1 Byte per pixel)
    bx, by: block-coordinates (0..39, 0..24)
    bitmap: 8000-byte bitmap data
    c01/c10/c11: 4-bit colors fuer bit-Paare 01/10/11
    bg: 4-bit color fuer bit-Paar 00
    bmp_offset: in den bitmap-Datenbereich gehoeriges Offset (used
                fuer IFLI's 2. Frame der nicht bei 0 startet)
    """
    base = bmp_offset + bx * 8 + by * 320
    for row in range(8):
        b = bitmap[base + row]
        sy = by * 8 + row
        for px in range(4):
            bits = (b >> (6 - px * 2)) & 0x03
            if bits == 0b00:
                color = bg
            elif bits == 0b01:
                color = c01
            elif bits == 0b10:
                color = c10
            else:
                color = c11
            sx = bx * 8 + px * 2
            pixels[sy * 320 + sx] = color
            pixels[sy * 320 + sx + 1] = color


def _hires_block_render(pixels, bx, by, bitmap, fg, bg,
                          bmp_offset=0):
    """Rendert einen 8x8 Hires-Block (single foreground/background)."""
    base = bmp_offset + bx * 8 + by * 320
    for row in range(8):
        b = bitmap[base + row]
        sy = by * 8 + row
        for col in range(8):
            color = fg if (b & (0x80 >> col)) else bg
            pixels[sy * 320 + (bx * 8 + col)] = color


def _make_pixels_buffer():
    """320x200 bytearray, alles 0 (black)."""
    return bytearray(320 * 200)


# -----------------------------------------------------------------
# Painter-Formate (einfach, aus BITMAP.TXT)
# -----------------------------------------------------------------

def decode_koala(path):
    """Koala / KoalaPainter - .koa, .kla (load $6000, 10003 bytes).
    Layout: bitmap(8000) + screen(1000) + color(1000) + bg(1)."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10001 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8000:9000]
    color  = payload[9000:10000]
    bg     = payload[10000] & 0x0F
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Koala Painter'}


def decode_art_studio(path):
    """Art Studio - .aas, .art (load $2000, 9009 bytes).
    Layout: bitmap(8000) + screen(1000) + 9 unused.
    Hires - kein Color-RAM. Screen byte: hi=fg, lo=bg pro 8x8 Block."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 9000 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8000:9000]
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            sb = screen[by * 40 + bx]
            _hires_block_render(pixels, bx, by, bitmap,
                                  (sb >> 4) & 0x0F, sb & 0x0F)
    return {'mode': 'hires', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Art Studio (Hires)'}


def decode_advanced_art_studio(path):
    """Advanced Art Studio (.ocp, .pic, .art) - load $2000, 10018.
    Layout: bitmap(8000) + screen(1000) + bg(1) + 15 + color(1000)."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10016 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8000:9000]
    bg = payload[9000] & 0x0F if len(payload) > 9000 else 0
    color = payload[9016:10016]
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx] if idx < len(color) else 0
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Adv Art Studio (MC)'}


def decode_doodle(path):
    """Doodle - .dd (load $5C00, 9218 bytes).
    Layout: screen(1000) + 24 padding + bitmap(8000+).
    Im Speicher: screen ab $5C00, bitmap ab $6000."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 9216 - len(payload)))
    # Erste 1000 Byte: screen ($5C00..$5FE7)
    # 24 padding bytes ($5FE8..$5FFF)
    # 8000 Byte bitmap ($6000..)
    screen = payload[0:1000]
    bitmap = payload[1024:9024]
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            sb = screen[by * 40 + bx]
            _hires_block_render(pixels, bx, by, bitmap,
                                  (sb >> 4) & 0x0F, sb & 0x0F)
    return {'mode': 'hires', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Doodle!'}


def decode_blazing_paddles(path):
    """Blazing Paddles - .pi (load $A000, 10242 bytes)."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10240 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8192:9192]
    color  = payload[9216:10216]
    bg     = payload[8064] & 0x0F if len(payload) > 8064 else 0
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Blazing Paddles'}


def decode_interpaint_hires(path):
    """Interpaint Hires - .iph (load $4000, 9002 bytes).
    Layout: bitmap(8000) + screen(1000)."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 9000 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8000:9000]
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            sb = screen[by * 40 + bx]
            _hires_block_render(pixels, bx, by, bitmap,
                                  (sb >> 4) & 0x0F, sb & 0x0F)
    return {'mode': 'hires', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Interpaint (Hires)'}


def decode_interpaint_mc(path):
    """Interpaint Multicolor - .ipt (load $4000, 10003 bytes).
    Layout wie Koala: bitmap(8000) + screen(1000) + color(1000) + bg(1)."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10001 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8000:9000]
    color  = payload[9000:10000]
    bg     = payload[10000] & 0x0F
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Interpaint (MC)'}


def decode_image_system_hires(path):
    """Image System Hires - .ism (load $4000, 9194 bytes).
    Layout: bitmap@0 + screen@8192."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 9192 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8192:9192]
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            sb = screen[by * 40 + bx]
            _hires_block_render(pixels, bx, by, bitmap,
                                  (sb >> 4) & 0x0F, sb & 0x0F)
    return {'mode': 'hires', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Image System (Hires)'}


def decode_image_system_mc(path):
    """Image System Multicolor - load $3C00, 10218 bytes.
    Layout: screen@1024 (??) - wir verwenden BITMAP.TXT values:
    bitmap=1024, screen=9216, colour=0, bg=9215."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10216 - len(payload)))
    color  = payload[0:1000]
    bitmap = payload[1024:9024]
    bg     = payload[9215] & 0x0F if len(payload) > 9215 else 0
    screen = payload[9216:10216]
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Image System (MC)'}


def decode_cdu_paint(path):
    """CDU Paint - load $7EEF, 10277 bytes.
    Layout aus BITMAP.TXT: bitmap=273, screen=8273, colour=9273,
    scrcol=10273."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10275 - len(payload)))
    bitmap = payload[273:8273]
    screen = payload[8273:9273]
    color  = payload[9273:10273]
    bg     = payload[10273] & 0x0F if len(payload) > 10273 else 0
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'CDU Paint'}


def decode_artist_64(path):
    """Artist 64 - load $4000, 10242 bytes."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10240 - len(payload)))
    bitmap = payload[0:8000]
    screen = payload[8192:9192]
    color  = payload[9216:10216]
    bg     = payload[10239] & 0x0F if len(payload) > 10239 else 0
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Artist 64 (Wigmore)'}


def decode_vidcom_64(path):
    """Vidcom 64 - load $5800, 10050 bytes.
    Layout aus BITMAP.TXT: bitmap=2048, screen=1024, colour=0,
    scrcol=2024."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 10048 - len(payload)))
    color  = payload[0:1000]
    screen = payload[1024:2024]
    bg     = payload[2024] & 0x0F if len(payload) > 2024 else 0
    bitmap = payload[2048:10048]
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Vidcom 64'}


# -----------------------------------------------------------------
# Amica Paint - RLE-packed multicolor
# -----------------------------------------------------------------

def _amica_unpack(packed):
    """Amica RLE-Unpack. Byte $C2 = RLE-Marker, gefolgt von count und
    value. count==0 -> EOF."""
    out = bytearray()
    i = 0
    while i < len(packed):
        b = packed[i]
        i += 1
        if b == 0xC2:
            if i + 1 >= len(packed):
                break
            cnt = packed[i]
            val = packed[i + 1]
            i += 2
            if cnt == 0:
                break    # EOF
            out.extend([val] * cnt)
        else:
            out.append(b)
    return bytes(out)


def decode_amica(path):
    """Amica Paint - .ami (load $4000, RLE-packed).
    Unpacked-Layout (10257 bytes): bitmap(8000) + videoram(1000) +
    farbram(1000) + bg(1) + color_cycle_table(256)."""
    _, payload = _read_with_load(path)
    unpacked = _amica_unpack(payload)
    unpacked = unpacked + bytes(max(0, 10001 - len(unpacked)))
    bitmap = unpacked[0:8000]
    screen = unpacked[8000:9000]
    color  = unpacked[9000:10000]
    bg     = unpacked[10000] & 0x0F
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels),
              'note': f'Amica Paint (unpacked {len(unpacked)} bytes)'}


# -----------------------------------------------------------------
# Drazpaint - .drp, .drz (multicolor, RLE-packed mit prefix)
# -----------------------------------------------------------------

def _drazpaint_unpack(packed):
    """Drazpaint RLE-Unpack. Magic-Bytes 'DRAZPAINT' am Anfang
    moeglich. RLE-Marker variabel (in den Magic-Bytes referenziert).
    Pragmatisch: wenn 'DRAZPAINT' am Anfang steht, skippen wir 13
    Header-Bytes, dann RLE-Marker = byte[12] des Headers.
    """
    if packed[:9] == b"DRAZPAINT":
        # Header: "DRAZPAINTx.x" (12) + RLE-Marker (1) = 13 Bytes
        # Format-Variant marker is at position 9..11; rle marker at 12.
        # Wir nehmen das byte direkt vor den Daten als RLE marker.
        marker = packed[12]
        data = packed[13:]
    else:
        # Kein Header - Daten roh, kein RLE
        return packed
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        i += 1
        if b == marker:
            if i + 1 >= len(data):
                break
            cnt = data[i]
            val = data[i + 1]
            i += 2
            if cnt == 0:
                break
            out.extend([val] * cnt)
        else:
            out.append(b)
    return bytes(out)


def decode_drazpaint(path):
    """Drazpaint - .drp, .drz (load $5800).

    Echtes Layout (gegen TEST.DRZ verifiziert):
        offset $0000..$03E7: screen RAM (1000 bytes)
        offset $03E8..$03FF: padding (24 bytes)
        offset $0400..$07E7: color RAM (1000 bytes)
        offset $07E8..$07FF: padding (24 bytes)
        offset $0800..$273F: bitmap (8000 bytes)
        offset $2740:        bg color (1 byte)
    Total: 10049 bytes (ohne 2-byte Load-Address).

    Falls die Datei mit 'DRAZPAINT' magic anfaengt, wird sie zuerst
    RLE-entpackt.
    """
    _, payload = _read_with_load(path)
    u = _drazpaint_unpack(payload)
    needed = 10049
    if len(u) < needed:
        u = u + bytes(needed - len(u))
    screen = u[0:1000]
    color  = u[1024:2024]
    bitmap = u[2048:10048]
    bg     = u[10048] & 0x0F
    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            _mc_block_render(pixels, bx, by, bitmap,
                              (sb >> 4) & 0x0F, sb & 0x0F,
                              cb & 0x0F, bg)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'Drazpaint'}


def decode_drazlace(path):
    """Drazlace - .drl, .dlp (Interlace-Variante von Drazpaint).
    2 Bitmaps + 1 screen + 1 color + 2 bgs.
    Average-Rendering der beiden Frames."""
    _, payload = _read_with_load(path)
    u = _drazpaint_unpack(payload)
    u = u + bytes(max(0, 18002 - len(u)))
    bg1    = u[0] & 0x0F
    bg2    = u[1] & 0x0F if len(u) > 1 else bg1
    color  = u[2:1002]
    screen = u[1002:2002]
    bitmap1 = u[2002:10002]
    bitmap2 = u[10002:18002]
    pixels1 = _make_pixels_buffer()
    pixels2 = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb = screen[idx]
            cb = color[idx]
            c01 = (sb >> 4) & 0x0F
            c10 = sb & 0x0F
            c11 = cb & 0x0F
            _mc_block_render(pixels1, bx, by, bitmap1,
                              c01, c10, c11, bg1)
            _mc_block_render(pixels2, bx, by, bitmap2,
                              c01, c10, c11, bg2)
    # Average der beiden Frames durch palette-blend index
    pixels = _blend_two_frames(pixels1, pixels2)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'pixels_a': bytes(pixels1),
              'pixels_b': bytes(pixels2), 'note': 'Drazlace (2 frames)'}


# -----------------------------------------------------------------
# Hires Interlace - 2 hires-Frames
# -----------------------------------------------------------------

def decode_hires_interlace(path):
    """Hires Interlace - .hlf (Interlace Hires Editor).
    Wir nehmen an: bitmap1(8000) + bitmap2(8000) + screen1(1000)
    + screen2(1000) - das ist eine pragmatische Anordnung.
    Average rendering."""
    _, payload = _read_with_load(path)
    payload = payload + bytes(max(0, 18000 - len(payload)))
    bitmap1 = payload[0:8000]
    bitmap2 = payload[8000:16000]
    screen1 = payload[16000:17000]
    screen2 = payload[17000:18000]
    pixels1 = _make_pixels_buffer()
    pixels2 = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            idx = by * 40 + bx
            sb1 = screen1[idx]
            sb2 = screen2[idx]
            _hires_block_render(pixels1, bx, by, bitmap1,
                                  (sb1 >> 4) & 0x0F, sb1 & 0x0F)
            _hires_block_render(pixels2, bx, by, bitmap2,
                                  (sb2 >> 4) & 0x0F, sb2 & 0x0F)
    pixels = _blend_two_frames(pixels1, pixels2)
    return {'mode': 'hires', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'pixels_a': bytes(pixels1),
              'pixels_b': bytes(pixels2),
              'note': 'Hires Interlace (2 frames)'}


# -----------------------------------------------------------------
# FLI - Flexible Line Interpretation
# -----------------------------------------------------------------

def decode_fli(path):
    """FLI (Flexible Line Interpretation) - meist load $3C00,
    17409 bytes (also 17407 nach Load-Address).

    Korrektes Layout (FLI Designer / FLI Graph, verifiziert gegen
    echte Samples):
        $3C00..$5BFF: 8 screen RAMs (a 1024 bytes, davon 1000 genutzt)
                      Layout: pro Screen 1000 Bytes Data + 24 Padding
        $5C00..$5FE7: color RAM (1000 bytes) - c11 fuer Bit-Paar "11"
        $5FE8..$5FFF: padding
        $6000..$7F3F: bitmap (8000 bytes)
    Total: 0x4400 = 17408 Bytes (ohne Load-Address)

    Es gibt KEINE bg_table im File - bg ist meist konstant 0 (black)
    oder ein einzelnes Byte am Anfang. Bei FLI Designer ist bg=0.

    Die 8 Screen-RAMs rotieren pro Rasterzeile - Zeile 0/8/16/... liest
    aus screen[0], Zeile 1/9/17/... aus screen[1], etc.
    """
    _, payload = _read_with_load(path)
    needed = 0x4400    # 17408 bytes
    if len(payload) < needed:
        payload = payload + bytes(needed - len(payload))

    # Bei FLI Designer: bg meist 0 (black). Manche Editoren haben
    # bg im ersten Byte vom Screen-RAM oder als separates Byte.
    # Wir nehmen bg=0 als Default, das matched fuer die meisten Files.
    bg = 0

    screens = [payload[i * 0x400:i * 0x400 + 1000] for i in range(8)]
    color   = payload[0x2000:0x2000 + 1000]
    bitmap  = payload[0x2400:0x2400 + 8000]

    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            blk = by * 40 + bx
            c11 = color[blk] & 0x0F
            for row in range(8):
                sy = by * 8 + row
                sb = screens[row][blk]
                c01 = (sb >> 4) & 0x0F
                c10 = sb & 0x0F
                # Bitmap-byte fuer diese Zeile
                bmp_b = bitmap[bx * 8 + by * 320 + row]
                for px in range(4):
                    bits = (bmp_b >> (6 - px * 2)) & 0x03
                    if bits == 0b00:
                        color_idx = bg
                    elif bits == 0b01:
                        color_idx = c01
                    elif bits == 0b10:
                        color_idx = c10
                    else:
                        color_idx = c11
                    sx = bx * 8 + px * 2
                    pixels[sy * 320 + sx] = color_idx
                    pixels[sy * 320 + sx + 1] = color_idx

    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'FLI (8 colormaps/line)'}


def decode_afli(path):
    """AFLI (Advanced FLI) - .afl, ~16385 bytes (load $4000).
    Hires-Version von FLI: 8 screen-RAMs (statt MC die direkt 8x1
    Hires-Colors-per-Block), kein color-RAM (Hires nutzt keinen).

    Layout (gegen LOGO.AFL verifiziert):
        $0000..$1FFF: 8 screen RAMs (a 1024 bytes, 1000 davon genutzt)
        $2000..$3F3F: bitmap (8000 bytes)
    """
    _, payload = _read_with_load(path)
    needed = 0x4000
    if len(payload) < needed:
        payload = payload + bytes(needed - len(payload))
    screens = [payload[i * 0x400:i * 0x400 + 1000] for i in range(8)]
    bitmap = payload[0x2000:0x2000 + 8000]

    pixels = _make_pixels_buffer()
    for by in range(25):
        for bx in range(40):
            blk = by * 40 + bx
            for row in range(8):
                sb = screens[row][blk]
                fg = (sb >> 4) & 0x0F
                bg = sb & 0x0F
                bmp_b = bitmap[bx * 8 + by * 320 + row]
                sy = by * 8 + row
                for col in range(8):
                    color = fg if (bmp_b & (0x80 >> col)) else bg
                    pixels[sy * 320 + (bx * 8 + col)] = color

    return {'mode': 'hires', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'note': 'AFLI (Hires FLI)'}


def decode_ifli(path):
    """IFLI - Interlaced FLI. 2 FLI-Frames, x-shifted by 1 pixel.

    Vereinfachte Annahme: das File enthaelt 2 hintereinander
    angeordnete FLI-Datensaetze (jeweils ~17K). Wir dekodieren beide
    und blenden sie.

    Falls die Datei nur Groesse fuer EIN FLI hat, ist's wahrscheinlich
    Gunpaint o.ae. - dann verwenden wir das mit einem 'half-IFLI'
    Decode (nur eine Frame).
    """
    _, payload = _read_with_load(path)
    half = 0x4400
    if len(payload) < 2 * half:
        # Fallback: single FLI
        return decode_fli(path)
    # Frame A
    bg_a = payload[:0x00C8]
    screens_a = [payload[0x0100 + i * 0x400:0x0100 + (i + 1) * 0x400][:1000]
                    for i in range(8)]
    color_a = payload[0x2100:0x2100 + 1000]
    bitmap_a = payload[0x2400:0x2400 + 8000]
    # Frame B
    off = half
    bg_b = payload[off:off + 0x00C8]
    screens_b = [payload[off + 0x0100 + i * 0x400:
                            off + 0x0100 + (i + 1) * 0x400][:1000]
                    for i in range(8)]
    color_b = payload[off + 0x2100:off + 0x2100 + 1000]
    bitmap_b = payload[off + 0x2400:off + 0x2400 + 8000]

    def render(bg_t, scrs, col, bmp, x_shift=0):
        px = _make_pixels_buffer()
        for by in range(25):
            for bx in range(40):
                blk = by * 40 + bx
                c11 = col[blk] & 0x0F
                for row in range(8):
                    sy = by * 8 + row
                    bg = bg_t[sy] & 0x0F if sy < len(bg_t) else 0
                    sb = scrs[row][blk]
                    c01 = (sb >> 4) & 0x0F
                    c10 = sb & 0x0F
                    bmp_b = bmp[bx * 8 + by * 320 + row]
                    for pxi in range(4):
                        bits = (bmp_b >> (6 - pxi * 2)) & 0x03
                        if bits == 0b00:
                            color_idx = bg
                        elif bits == 0b01:
                            color_idx = c01
                        elif bits == 0b10:
                            color_idx = c10
                        else:
                            color_idx = c11
                        sx = bx * 8 + pxi * 2 + x_shift
                        if 0 <= sx < 320:
                            px[sy * 320 + sx] = color_idx
                        if 0 <= sx + 1 < 320:
                            px[sy * 320 + sx + 1] = color_idx
        return px

    pa = render(bg_a, screens_a, color_a, bitmap_a, x_shift=0)
    pb = render(bg_b, screens_b, color_b, bitmap_b, x_shift=1)
    pixels = _blend_two_frames(pa, pb)
    return {'mode': 'multicolor', 'width': 320, 'height': 200,
              'pixels': bytes(pixels), 'pixels_a': bytes(pa),
              'pixels_b': bytes(pb),
              'note': 'IFLI (Interlaced FLI, 2 frames)'}


def decode_funpaint(path):
    """Funpaint II (.fun, .fp2) - IFLI editor format.
    Layout (load $3FF0, total ~33790 bytes):
        $3FF0..$3FFF (16): header / bg-table info
        $4000..$5FFF (8K): bitmap frame 1
        $6000..$7F3F (8000): screen RAMs ... etc.

    Pragmatisch: behandeln wie IFLI mit pragmatischen Offsets.
    """
    _, payload = _read_with_load(path)
    # Fallback auf decode_ifli wenn payload gross genug
    if len(payload) >= 0x8000:
        return decode_ifli(path)
    return decode_fli(path)


# -----------------------------------------------------------------
# Average two pixel buffers (perceptual blend via palette indices)
# -----------------------------------------------------------------

def _blend_two_frames(p1, p2):
    """Blend zwei Pixel-Buffer durch RGB-Average. Returnt 'best match'
    Palette-Index pro Pixel. Das ist nicht perfekt (closest C64 color
    nach RGB), aber sehr gut fuer Vorschau-Zwecke.

    Caching: wir berechnen die Average-Lookup-Tabelle einmal."""
    if not hasattr(_blend_two_frames, '_cache'):
        # 16x16 Tabelle: (idx_a, idx_b) -> best matching 0..15
        cache = [[0] * 16 for _ in range(16)]
        for a in range(16):
            for b in range(16):
                ra, ga, ba = C64_PALETTE[a]
                rb, gb, bb = C64_PALETTE[b]
                avg = ((ra + rb) // 2, (ga + gb) // 2, (ba + bb) // 2)
                best = 0
                best_d = 1 << 30
                for c in range(16):
                    rc, gc, bc = C64_PALETTE[c]
                    d = ((rc - avg[0]) ** 2 + (gc - avg[1]) ** 2
                          + (bc - avg[2]) ** 2)
                    if d < best_d:
                        best_d = d
                        best = c
                cache[a][b] = best
        _blend_two_frames._cache = cache
    cache = _blend_two_frames._cache
    out = bytearray(len(p1))
    for i in range(len(p1)):
        out[i] = cache[p1[i]][p2[i]]
    return out


# -----------------------------------------------------------------
# Format dispatch helper
# -----------------------------------------------------------------

# Liste aller Format-Decoder mit erkenungsregeln. Jeder Eintrag:
#   (decode_function, file_size_or_None, extensions_set, name)
# size=None bedeutet "nur per Extension matchen, keine Groesse erforderlich".
DECODERS = [
    # (key, decode_fn, expected_size, extensions, display_name)
    ('koala',       decode_koala,       10003, {'kla', 'koa', 'gg'},   "Koala Painter"),
    ('artstudio',   decode_art_studio,  9009,  {'aas', 'art'},          "Art Studio (Hires)"),
    ('advartstudio',decode_advanced_art_studio, 10018, {'ocp', 'pic'},  "Adv. Art Studio (MC)"),
    ('doodle',      decode_doodle,      9218,  {'dd', 'jj'},            "Doodle!"),
    ('blazingpad',  decode_blazing_paddles, 10242, {'pi'},              "Blazing Paddles"),
    ('imagesyshir', decode_image_system_hires, 9194,  {'ism', 'ish'},   "Image System (Hires)"),
    ('imagesysmc',  decode_image_system_mc, 10218, {'ims'},             "Image System (MC)"),
    ('interpainthi',decode_interpaint_hires,9002,  {'iph'},             "Interpaint (Hires)"),
    ('interpaintmc',decode_interpaint_mc,   10003, {'ipt'},             "Interpaint (MC)"),
    ('cdupaint',    decode_cdu_paint,   10277, {'cdu'},                 "CDU Paint"),
    ('artist64',    decode_artist_64,   10242, {'wig', 'a64'},          "Artist 64"),
    ('vidcom64',    decode_vidcom_64,   10050, {'vid'},                 "Vidcom 64"),
    ('amica',       decode_amica,       None,  {'ami'},                 "Amica Paint (RLE)"),
    ('drazpaint',   decode_drazpaint,   None,  {'drp', 'drz'},          "Drazpaint"),
    ('drazlace',    decode_drazlace,    None,  {'drl', 'dlp'},          "Drazlace"),
    ('hireslace',   decode_hires_interlace, None, {'hlf', 'ihe'},       "Hires Interlace"),
    ('fli',         decode_fli,         None,  {'fli', 'fpr', 'fd2',
                                                  'bml', 'fpr', 'bfl',
                                                  'bfli', 'ffli'},      "FLI (Multicolor)"),
    ('afli',        decode_afli,        None,  {'afl', 'hfc'},          "AFLI (Hires FLI)"),
    ('ifli',        decode_ifli,        None,  {'ifl', 'ifli', 'iph'},  "IFLI (Interlaced FLI)"),
    ('funpaint',    decode_funpaint,    None,  {'fun', 'fp2', 'gun'},   "Funpaint II / Gunpaint"),
]


def detect_format(path):
    """Erkenne das Format und gibt den decoder-key zurueck.
    Fallback: None wenn nichts passt."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    ext = os.path.splitext(path)[1].lower().lstrip(".")

    # Extension match zuerst
    for key, fn, sz, exts, name in DECODERS:
        if ext in exts:
            return key

    # Size-Match wenn Extension nichts ergab
    for key, fn, sz, exts, name in DECODERS:
        if sz is not None and size in (sz, sz - 2, sz + 2):
            # +/- 2 fuer Variation mit/ohne Load-Address
            return key

    return None


def decode_by_key(path, key):
    """Decode ein File mit dem gegebenen Decoder-key."""
    for k, fn, sz, exts, name in DECODERS:
        if k == key:
            return fn(path), name
    raise ValueError(f"Unknown decoder key: {key}")


def get_decoder_list():
    """Returnt eine sortierte Liste (key, display_name) fuer UI."""
    return [(k, name) for k, fn, sz, exts, name in DECODERS]


# -----------------------------------------------------------------
# RECOIL Backend - extern recoil2png aufrufen
# -----------------------------------------------------------------

# Liste aller von RECOIL unterstuetzten Extensions. Wird benutzt:
#  - im File-Dialog Filter
#  - in detect_recoil_format(path) um zu wissen ob recoil zustaendig
#    ist
RECOIL_EXTENSIONS = frozenset([
    # Amiga + DCTV/HAM-E
    'abk', 'acbm', 'deep', 'flf', 'ham', 'ham6', 'ham8', 'iff', '256',
    'info', 'lbm', 'ilbm', 'dhr', 'dr', 'mp', 'beam', 'rgb8', 'rgbn',
    'sham', 'dct', 'dctv',
    # Amstrad CPC
    'cm5', 'gfx', 'hgb', 'pph', 'odd', 'eve', 'scr', 'pal', 'sgx',
    'win',
    # Apple II/IIe/IIGS/Mac
    'hgr', 'dhgr', '3201', '32k', 'gs', 'iigs', 'pnt', 'shr', 'sh3',
    '3200', 'mac', 'pntg',
    # Atari 8-bit (143 Formate)
    'ap2', '4mi', '4pl', '4pm', 'a4r', 'acs', 'agp', 'ags', 'all',
    'an2', 'an4', 'an5', 'ap3', 'apv', 'dgi', 'dgp', 'esc', 'ilc',
    'pzm', 'apa', 'apc', 'plm', 'apl', 'app', 'aps', 'asc', 'bg9',
    'g09', 'bgp', 'bkg', 'cci', 'cin', 'cpi', 'cpr', 'cut', 'din',
    'dit', 'dlm', 'drg', 'f80', 'fge', 'fn2', 'fwa', 'g10', 'g11',
    'g2f', 'g9s', 'sfd', 'ged', 'ghg', 'gr0', 'gr1', 'gr2', 'gr3',
    'gr7', 'gr8', 'gr9', 'gr9p', 'hci', 'hr2', 'hcm', 'hpm', 'hps',
    'ice', 'icn', 'ige', 'ild', 'ils', 'imn', 'ing', 'inp', 'ins',
    'int', 'ip2', 'ipc', 'ir2', 'irg', 'ist', 'jgp', 'kpr', 'kss',
    'ldm', 'leo', 'lum', 'kfx', 'mga', 'mgp', 'mic', 'mis', 'mpl',
    'msl', 'nlq', 'odf', 'pgr', 'pi8', 'pla', 'pls', 'pmd', 'psf',
    'rap', 'rip', 'rm0', 'rm1', 'rm2', 'rm3', 'rm4', 'rys', 'sg3',
    'sge', 'shp', 'sif', 'skp', 'sxs', 'tip', 'tl4', 'tx0', 'txe',
    'txs', 'vsc', 'vzi', 'wnd', 'xlp', 'zm4', 'dap', 'pgc', 'pgf',
    # Atari ST/STE/TT/Falcon (~120 Formate)
    'bil', 'bl1', 'bl2', 'bl3', 'bld', 'bp1', 'bp2', 'bp4', 'c01',
    'c02', 'c04', 'bru', 'ca1', 'ca2', 'ca3', 'ce1', 'ce2', 'ce3',
    'cel', 'cmp', 'cp3', 'cpt', 'hbl', 'crg', 'da4', 'doo', 'du1',
    'duo', 'du2', 'eza', 'ful', 'gfb', 'grx', 'hpk', 'hrm', 'ic1',
    'ic2', 'ic3', 'im', 'img', 'kid', 'lpk', 'mpk', 'mpp', 'mur',
    'neo', 'rst', 'obj', 'p3c', 'pa3', 'pac', 'pbx', 'pc1', 'pc2',
    'pc3', 'pci', 'pcs', 'pg0', 'pg1', 'pg2', 'pg3', 'pi1', 'pi2',
    'pi3', 'suh', 'pl4', 'ppp', 'psc', 'rgh', 'cl0', 'sc0', 'cl1',
    'sc1', 'cl2', 'sc2', 'sd0', 'sd1', 'sd2', 'sps', 'spu', 'spx',
    'srt', 'ssb', 'tn1', 'tn4', 'tn2', 'tn5', 'tn3', 'tn6', 'tny',
    'ximg', 'pi4', 'pi5', 'pi6', 'pi7', 'pi9', 'b&w', 'b_w', 'bp6',
    'bp8', 'c06', 'c08', 'c16', 'c24', 'c32', 'dc1', 'del', 'dg1',
    'dph', 'esm', 'ftc', 'god', 'hir', 'ib3', 'ibi', 'iim', 'tpi',
    'rag', 'ragc', 'rwh', 'rwl', 'tcp', 'tg1', 'timg', 'tre', 'trp',
    'tru', 'xga',
    # BBC Micro
    'bb0', 'bb1', 'bb2', 'bb4', 'bb5', 'bbg',
    # Commodore VIC-20
    'pic0', 'pic1', 'mg',
    # Commodore 64 (108 Formate)
    '4bt', '64c', 'a64', 'wig', 'aas', 'art', 'afl', 'ami', 'bdp',
    'bfl', 'bfli', 'bml', 'fli', 'flg', 'bs', 'cdu', 'cfli', 'cgx',
    'che', 'cle', 'clp', 'ctm', 'cwg', 'dd', 'ddp', 'dol', 'bed',
    'drl', 'dlp', 'drz', 'drp', 'eci', 'ecp', 'emc', 'esh', 'fbi',
    'fcp', 'fpt', 'fd2', 'fed', 'ffl', 'ffli', 'flm', 'fp', 'fp2',
    'fpr', 'gb', 'gcd', 'mon', 'gg', 'gun', 'ifl', 'hbm', 'hpi',
    'fgs', 'hcb', 'hed', 'het', 'hfc', 'hfd', 'him', 'hle', 'hlf',
    'hie', 'hpc', 'ihe', 'ile', 'iph', 'gig', 'hre', 'ipt', 'lre',
    'ish', 'ism', 'jj', 'kla', 'koa', 'lp3', 'mci', 'mil', 'mle',
    'muf', 'mui', 'mup', 'mwi', 'mwin', 'nuf', 'nup', 'ocp', 'mpi',
    'mpic', 'p64', 'fly', 'pbot', 'pdr', 'pet', 'pg', 'pi', 'bpl',
    'pp', 'ppp', 'rp', 'rph', 'rpm', 'rpo', 'gih', 'sar', 'sh1',
    'sh2', 'she', 'shf', 'shi', 'shs', 'shx', 'spd', 'ufl', 'uif',
    'vhi', 'vic', 'vid', 'xfl', 'zom', 'zs', 'fnt',
    # Commodore 16/116/Plus4
    'p4i',
    # Commodore 128
    'bm', 'vbm', 'brus', 'pict', 'ip',
    # Electronika BK
    'bks',
    # HP 48
    'grb', 'gro',
    # MSX (35 Formate)
    'grp', 'sc3', 'pl5', 'sh5', 'gl5', 'gl6', 'sh6', 'pl6', 'gl7',
    'sh7', 'pl7', 'gl8', 'sh8', 'mag', 'mif', 'mig', 'pct', 'sc4',
    'sc5', 'ge5', 's15', 'sc6', 'ge6', 's16', 'sc7', 'ge7', 's17',
    'sc8', 'ge8', 's18', 'sr5', 'sr6', 'sr7', 'sr8', 'sri', 'stp',
    'gla', 'glb', 'sha', 'shb', 'pla', 'glc', 'gls', 'shc', 'sca',
    'scb', 's1a', 'scc', 'srs', 'yjk', 's1c', 'g9b',
    # NEC PC-80/88/98
    'kty', 'kt4', 'arv', 'ebd', 'mki', 'ml1', 'mx1', 'nl3', 'q4',
    'zim',
    # Oric/SAM Coupe/etc.
    'chs', 'hrs', 'epa', 'hs2', 'msp', 'tim', 'lce', 'ss1', 'ss2',
    'ss3', 'ss4', 'scs4', 'ssx', 'hrg', 'rle', 'grf', 'p41', 'p11',
    # ZX81/Spectrum
    'p', 'zp1', 'atr', 'bmc4', 'bsc', 'bsp', 'ch$', 'ch4', 'ch6',
    'ch8', 'chx', 'hlr', 'mc', 'mg1', 'mg2', 'mg4', 'mg8', 'mlt',
    'sev', 'stl', 'zxp', 'zxs', 'sxg', 'nxi',
    # ZX Spectrum '3' formats
    '3',
    # Atari Falcon 'fun' = Funny Paint (note: also C64 Funpaint .fun)
    'fun',
    # PC formats
    # PlayStation TIM
    # Generic
])


class RecoilBackend:
    """RECOIL-Wrapper. Konvertiert eine retro-Computer-Bilddatei zu
    PNG via externes recoil2png-CLI-Tool.

    Detection erfolgt zur Laufzeit:
        1. Wenn config['recoil2png_path'] gesetzt -> dort suchen
        2. Sonst PATH durchsuchen
        3. Sonst nicht verfuegbar
    """

    def __init__(self, exe_path=None):
        self._exe = self._find_exe(exe_path)

    @property
    def available(self):
        return self._exe is not None

    @property
    def executable_path(self):
        return self._exe

    @staticmethod
    def _find_exe(hint=None):
        """Sucht recoil2png in folgender Reihenfolge:
        1. hint (vom User in Config angegebener Pfad)
        2. <quopus-project>/external/recoil2png[.exe] - portable bundled
        3. PATH-Suche als Fallback
        """
        import shutil
        import os
        if hint:
            if os.path.isfile(hint) and os.access(hint, os.X_OK):
                return hint
            # Bei Windows: koennte hint ohne .exe sein
            if os.name == 'nt' and not hint.lower().endswith('.exe'):
                if os.path.isfile(hint + '.exe'):
                    return hint + '.exe'
        # external/ neben quopus_lib/ suchen - portable bundle
        # Dieses File ist in <project>/quopus_lib/retro_gfx_decoders.py,
        # also project = parent of dirname(__file__)
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            project = os.path.dirname(here)
            for name in ('recoil2png.exe', 'recoil2png'):
                cand = os.path.join(project, 'external', name)
                if os.path.isfile(cand):
                    return cand
        except Exception:
            pass
        # PATH search
        for name in ('recoil2png', 'recoil2png.exe'):
            found = shutil.which(name)
            if found:
                return found
        return None

    def decode_to_png(self, input_path, output_dir=None):
        """Konvertiert input_path mit recoil2png nach PNG.
        Returns: path zum erzeugten PNG, oder None bei Fehler.

        output_dir: wo das PNG hin soll. Default = system tempdir.
        """
        if not self._exe:
            raise RuntimeError("recoil2png not available")
        import subprocess
        import tempfile
        import os

        if output_dir is None:
            output_dir = tempfile.gettempdir()
        os.makedirs(output_dir, exist_ok=True)

        base = os.path.splitext(os.path.basename(input_path))[0]
        out_png = os.path.join(output_dir, base + '.png')

        # recoil2png CLI:  recoil2png -o output.png input.file
        # Manche Versionen: recoil2png input.file -> erzeugt input.png
        # neben dem Original. Wir versuchen explizit -o.
        try:
            result = subprocess.run(
                [self._exe, '-o', out_png, input_path],
                capture_output=True, timeout=10, text=True)
            if result.returncode == 0 and os.path.isfile(out_png):
                return out_png
            # Fallback: ohne -o, schauen ob das Original neben dem
            # File ein PNG bekommen hat
            result = subprocess.run(
                [self._exe, input_path],
                capture_output=True, timeout=10, text=True,
                cwd=output_dir)
            implicit = os.path.join(output_dir, base + '.png')
            if os.path.isfile(implicit):
                return implicit
            # Try in original directory
            implicit2 = os.path.join(
                os.path.dirname(input_path), base + '.png')
            if os.path.isfile(implicit2):
                return implicit2
            # Wirklich nichts erzeugt
            err = result.stderr or result.stdout or "(no output)"
            raise RuntimeError(
                f"recoil2png returned {result.returncode}: {err}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "recoil2png timed out (>10s) - file may be invalid")


def can_recoil_handle(path):
    """Schnellcheck via Extension ob RECOIL zustaendig waere.
    Macht keinen Subprocess-Aufruf - nur Extension-Match."""
    import os
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    return ext in RECOIL_EXTENSIONS
