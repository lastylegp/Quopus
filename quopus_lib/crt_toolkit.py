"""
Commodore 64 .CRT cartridge image toolkit.

Reads VICE-format CRT files: header inspection, hardware-type
identification, per-bank CHIP-packet listing, raw bank extraction,
hex / ASCII / disassembly viewing of individual banks.

CRT format (VICE manual ch. 17.14):

  Header (64 bytes):
    $00-$0F  16-byte signature "C64 CARTRIDGE   " or "C128 CARTRIDGE  "
                                  or "CBM2 CARTRIDGE  " or VIC20/PLUS4 strings
    $10-$13  Header length (BIG-ENDIAN ULONG, normally $00000040)
    $14-$15  CRT version (high/low: $0100 = v1.0, $0200 = v2.0)
    $16-$17  Hardware type (BIG-ENDIAN UWORD)
    $18      EXROM line state (0 = inactive / 1 = active)
    $19      GAME line state  (0 = inactive / 1 = active)
    $1A      Cartridge sub-type / revision (CRT v2.0+, was reserved)
    $1B-$1F  Reserved for future use (5 bytes)
    $20-$3F  32-byte cartridge name, null-padded ASCII

  Then one or more CHIP packets:
    $00-$03  "CHIP" signature
    $04-$07  Total packet length incl. header (BIG-ENDIAN ULONG)
                = ROM data size + 16
    $08-$09  Chip type (BIG-ENDIAN UWORD): 0=ROM, 1=RAM no data, 2=Flash
    $0A-$0B  Bank number (BIG-ENDIAN UWORD)
    $0C-$0D  Load address in C64 memory (BIG-ENDIAN UWORD)
              typically $8000 (LO ROM) or $A000/$E000 (HI ROM/Ultimax)
    $0E-$0F  ROM image size (BIG-ENDIAN UWORD), typically $2000 / $4000
    $10-...  ROM data of size <ROM image size>

A CRT file therefore has:
  * one header
  * any number of CHIP packets, possibly multiple banks per "slot"

Header endianness note: ULONG / UWORD in CRT format are stored
HIGH-byte first (big-endian on disk), unlike most C64 file formats
which are little-endian. This trips up everyone who reads a quick
example. We use struct '>I' / '>H' explicitly.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from .config import scaled_font_px


# Signatures we accept. Padded with spaces to 16 bytes.
_C64_SIGNATURE  = b"C64 CARTRIDGE   "
_C128_SIGNATURE = b"C128 CARTRIDGE  "
_CBM2_SIGNATURE = b"CBM2 CARTRIDGE  "
_VIC20_SIGNATURE = b"VIC20 CARTRIDGE "
_PLUS4_SIGNATURE = b"PLUS4 CARTRIDGE "
_VALID_SIGNATURES = (
    _C64_SIGNATURE, _C128_SIGNATURE, _CBM2_SIGNATURE,
    _VIC20_SIGNATURE, _PLUS4_SIGNATURE,
)

# Hardware-type table for C64 cartridges, taken from VICE's
# cartridge.h / cartconv.c. Each entry is:
#   id -> (short_name, long_name, description)
# This is the source of truth for "what is this cartridge?".
# IDs not in the table show as "Unknown / future type".
#
# Last synced from VICE source: September 2024 (rr.pokefinder.org).
CRT_TYPES = {
    0:   ("Generic",       "Generic Cartridge",
          "Plain 4/8/12/16 KiB ROM with no bank-switching. EXROM/GAME "
          "lines hard-wired in the header. The huge majority of "
          "small cartridge dumps use this ID."),
    1:   ("Action Replay", "Action Replay 4.2/5/6/7",
          "32 KiB freezer cartridge with 4×8 KiB banks switched via "
          "$DE00. Has a freeze button and on-cartridge RAM. NOT the "
          "original AR1 - that is type 50."),
    2:   ("KCS Power",     "KCS Power Cartridge",
          "16 KiB cartridge in 2 separate $8000/$A000 8 KiB blocks. "
          "Switches to ROM at $8000 only on $DE00 access."),
    3:   ("Final Cart 3",  "The Final Cartridge III",
          "64 KiB freezer/utility cart with 4×16 KiB banks at "
          "$8000-$BFFF, switched via $DFFF. BASIC + DOS + freezer."),
    4:   ("Simons BASIC",  "Simons' BASIC",
          "16 KiB BASIC extension. Switches the upper 8 KiB at "
          "$A000 in/out via $DE00."),
    5:   ("Ocean",         "Ocean Type 1",
          "32 / 128 / 256 / 512 KiB game cartridge. 8 KiB banks at "
          "$8000 selected via $DE00 register write. Many Ocean and "
          "Imagine titles."),
    6:   ("Expert",        "Expert Cartridge",
          "8 KiB freezer cart with on-cartridge RAM that holds the "
          "frozen image. Mode set via $DE01."),
    7:   ("Fun Play",      "Fun Play / Power Play",
          "128 KiB game compilation cart. 16×8 KiB banks at $8000 "
          "selected via $DE00 with a non-linear bank-number scheme."),
    8:   ("Super Games",   "Super Games",
          "64 KiB game compilation cart. 4×16 KiB banks at "
          "$8000-$BFFF switched via $DF00."),
    9:   ("Atomic Power",  "Atomic Power / Nordic Power",
          "32 KiB freezer cart, similar to Action Replay. 4×8 KiB "
          "banks at $8000 via $DE00."),
    10:  ("Epyx Fastload", "Epyx Fastload",
          "8 KiB disk-loader cart. Hides itself unless $DE00 is "
          "read frequently (capacitor-discharge trick) - the famous "
          "'invisible' fastloader."),
    11:  ("Westermann",    "Westermann Learning",
          "16 KiB educational cart. ROM disabled by reading $DF00."),
    12:  ("Rex Utility",   "Rex Utility",
          "8 KiB cart enabled/disabled via $DFC0/$DFE0."),
    13:  ("Final Cart 1",  "Final Cartridge 1 (FCC)",
          "16 KiB version of the Final Cartridge series."),
    14:  ("Magic Formula", "Magic Formula",
          "64 KiB cart with 4×16 KiB banks via $DE00."),
    15:  ("C64GS",         "C64 Game System / System 3",
          "512 KiB game cart, 64×8 KiB banks at $8000 via $DE00 + "
          "register address."),
    16:  ("Warpspeed",     "WarpSpeed",
          "16 KiB DOS/utility cart with $DE00/$DF00 control."),
    17:  ("Dinamic",       "Dinamic",
          "128 KiB Spanish-game cart. 16×8 KiB banks at $8000 via "
          "$DE00 with the bank number = address LSBs."),
    18:  ("Zaxxon",        "Zaxxon / Super Zaxxon (SEGA)",
          "20 KiB cart with split mapping: 4 KiB at $8000 (mirrored "
          "into $9000) plus 2×8 KiB banks at $A000."),
    19:  ("Magic Desk",    "Magic Desk / Domark / HES Australia",
          "32-128 KiB cart with bank-switched 8 KiB at $8000 via "
          "$DE00. Used by many later Codemasters releases too."),
    20:  ("Super Snap 5",  "Super Snapshot V5",
          "64 KiB freezer with 4×16 KiB banks ($8000-$BFFF), "
          "on-cart RAM, freeze button, and ROM/RAM toggle."),
    21:  ("Comal-80",      "Comal-80",
          "64 KiB programming-language cart. 4×16 KiB banks via "
          "$DE00."),
    22:  ("Structured BASIC","Structured BASIC",
          "16 KiB BASIC extension cart."),
    23:  ("Ross",          "Ross Cartridge",
          "16 / 32 KiB cart, $DE00 control."),
    24:  ("Dela EP64",     "Dela EP64",
          "EPROM cart, 64 KiB."),
    25:  ("Dela EP7x8",    "Dela EP7x8",
          "EPROM cart, 7×8 KiB EPROMs."),
    26:  ("Dela EP256",    "Dela EP256",
          "EPROM cart, 256 KiB."),
    27:  ("Rex EP256",     "Rex EP256",
          "EPROM cart, 256 KiB. Stores up to 32×8 KiB CRT images."),
    28:  ("Mikro Assembler","Mikro Assembler",
          "8 KiB assembler/utility cart."),
    29:  ("Final Cart Plus","Final Cartridge Plus",
          "32 KiB version of the Final Cartridge."),
    30:  ("Action Replay 4","Action Replay 4 / 4.x",
          "32 KiB Action Replay version 4 (predecessor to type 1)."),
    31:  ("Stardos",       "Stardos",
          "16 KiB freezer + DOS, $DE61/$DF61 control."),
    32:  ("EasyFlash",     "EasyFlash",
          "Up to 1 MiB flash cart with 64×8 KiB banks at both $8000 "
          "and $A000 (so 64×16 KiB effective). Bank register at "
          "$DE00, control register at $DE02. Supports EAPI write-back. "
          "Most modern cracker / scene releases use this format."),
    33:  ("EasyFlash XBank","EasyFlash XBank (deprecated)",
          "Older variant of EasyFlash, never widely used."),
    34:  ("Capture",        "Capture",
          "16 KiB freezer/snapshot cart by Jason Ranheim."),
    35:  ("Action Replay 3","Action Replay 3",
          "16 KiB AR variant."),
    36:  ("Retro Replay",  "Retro Replay",
          "Modern Action Replay clone, 64 KiB Flash, with extra "
          "registers. The 'rr' / 'fc3-clone' family."),
    37:  ("MMC64",          "MMC64",
          "SD/MMC card reader cart, 8 KiB ROM + bridge to MMC."),
    38:  ("MMC Replay",    "MMC Replay",
          "Combination of Retro Replay + MMC64. 64-512 KiB Flash + "
          "RAM + SD-card slot. Modern multi-tool freezer."),
    39:  ("IDE64",         "IDE64",
          "32 KiB+ Flash + IDE/ATA host adapter cart. CMD-style "
          "drive emulation."),
    40:  ("Super Snap 4",  "Super Snapshot V4",
          "32 KiB freezer (predecessor to V5)."),
    41:  ("IEEE-488",      "IEEE-488 Interface",
          "4 KiB cart that adds an IEEE-488 port for CBM disk "
          "drives like the 4040, 8050."),
    42:  ("Game Killer",   "Game Killer",
          "8 KiB cheat cart. Freezes and displays POKE menu."),
    43:  ("Prophet 64",    "Prophet 64",
          "Cart-based music sequencer."),
    44:  ("EXOS",          "EXOS",
          "8 KiB cart at $E000-$FFFF in Ultimax mode."),
    45:  ("Freeze Frame",  "Freeze Frame",
          "8 KiB freezer cart."),
    46:  ("Freeze Machine","Freeze Machine",
          "16/32 KiB freezer cart."),
    47:  ("Snapshot 64",   "Snapshot 64",
          "4 KiB freezer cart."),
    48:  ("Super Explode 5","Super Explode V5.0",
          "16 KiB utility cart."),
    49:  ("Magic Voice",   "Magic Voice",
          "16 KiB speech-synthesis cart."),
    50:  ("Action Replay 2","Action Replay 2",
          "Original AR2 (predecessor of type 1's AR4-7)."),
    51:  ("MACH 5",         "MACH 5",
          "8 KiB DOS speedup cart."),
    52:  ("Diashow Maker", "Diashow Maker",
          "8 KiB image-slideshow utility cart."),
    53:  ("Pagefox",        "Pagefox",
          "64 KiB DTP cart."),
    54:  ("Kingsoft",       "Kingsoft",
          "24 KiB cart."),
    55:  ("Silverrock 128", "Silverrock 128",
          "Compilation cart, 128 KiB, 16×8 KiB banks."),
    56:  ("Formel 64",      "Formel 64",
          "Math/utility cart."),
    57:  ("RGCD",           "RGCD",
          "Modern game-release cart, up to 64 KiB."),
    58:  ("RR-Net MK3",    "RR-Net MK3",
          "Network cart with cs8900a chip + flash. Ethernet for the "
          "C64."),
    59:  ("EasyCalc",      "EasyCalc",
          "Spreadsheet cart, 64 KiB."),
    60:  ("GMod2",          "GMod2",
          "Modern multi-purpose cart with M93C86 EEPROM for save "
          "games. Up to 512 KiB Flash. Used by many recent scene "
          "releases (e.g. The Sarcophaser, Sam's Journey)."),
    61:  ("MAX Basic",      "MAX Basic",
          "BASIC cart for the Commodore MAX (Ultimax) machine."),
    62:  ("GMod3",          "GMod3",
          "Successor to GMod2 with larger Flash."),
    63:  ("ZIPP-CODE 48",  "ZIPP-CODE 48",
          "Programming utility cart."),
    64:  ("Blackbox V8",   "Blackbox V8",
          "Freezer/utility cart variant."),
    65:  ("Blackbox V3",   "Blackbox V3",
          "Earlier Blackbox version."),
    66:  ("Blackbox V4",   "Blackbox V4",
          "Mid-version Blackbox."),
    67:  ("REX RAM-Floppy","REX RAM-Floppy",
          "RAM-disk cart with battery backup."),
    68:  ("BIS-Plus",       "BIS-Plus",
          "Utility cart."),
    69:  ("SD-BOX",         "SD-BOX",
          "SD-card storage cart."),
    70:  ("MultiMAX",       "MultiMAX",
          "Multi-image MAX cart."),
    71:  ("Blackbox V9",   "Blackbox V9",
          "Latest Blackbox revision."),
    72:  ("Lt. Kernal",    "Lt. Kernal Host Adaptor",
          "Hard-drive host adaptor cart."),
    73:  ("RAMLink",        "RAMLink",
          "CMD RAM expansion cart."),
    74:  ("Drean",          "Drean",
          "Argentine C64 clone cart. Drean was the South American "
          "Commodore distributor that produced a PAL-N variant of "
          "the C64 - some Drean-only carts use a slightly different "
          "bank-switching scheme that ID 74 covers."),
    75:  ("IEEE Flash 64", "IEEE Flash! 64",
          "IEEE-488 interface + Flash combo cart for CBM disk "
          "drives."),
    76:  ("Turtle Gfx II", "Turtle Graphics II",
          "Educational graphics cart."),
    77:  ("Freeze Frame MK2", "Freeze Frame MK2",
          "Updated Freeze Frame freezer."),
    78:  ("Partner 64",    "Partner 64",
          "Productivity cart."),
    79:  ("Hyper-BASIC",   "Hyper-BASIC",
          "BASIC extension cart. Note: ID 79 was historically also "
          "used by some older Magic Desk 2 carts before MD2 was "
          "assigned its own ID 85; modern emulators detect MD2 by "
          "ROM size > 1 MB to disambiguate."),
    80:  ("Univ Cart 1",   "Universal Cartridge 1",
          "First version of the Universal Cartridge generic "
          "flash-rewritable platform."),
    81:  ("Univ Cart 1.5", "Universal Cartridge 1.5",
          "Revision 1.5 of the Universal Cartridge."),
    82:  ("Univ Cart 2",   "Universal Cartridge 2",
          "Second-generation Universal Cartridge."),
    83:  ("BMP Data Turbo","BMP Data Turbo 2000",
          "Data-transfer cart."),
    84:  ("Profi-DOS",     "Profi-DOS",
          "DOS/utility cart."),
    85:  ("Magic Desk 16", "Magic Desk 16",
          "Magic Desk variant with up to 16 MiB Flash. Also known "
          "as MD2 - some older CRTs use ID 79 for the same "
          "hardware, see notes there. Used by Denise emulator and "
          "modern multi-game compilation carts."),
    86:  ("Megabyter",     "Protovision \"Megabyter\"",
          "Protovision's Protocart One platform - the host cart "
          "for non-GMod2 Protovision releases like A Pig Quest "
          "and Lykia."),
}

# Cartridge sub-type / revision table (CRT v2.0+, header offset $1A).
# Mostly used by Ultimate / VICE for EAPI compatibility flags.
CRT_SUBTYPES = {
    32: {  # EasyFlash
        0: "Standard",
        1: "Cross-blanked (compatible with REU)",
    },
    36: {  # Retro Replay
        0: "Standard",
        1: "Nordic Replay",
    },
    51: {  # MACH 5
        0: "Standard",
        1: "Hi-bit set variant",
    },
}


@dataclass
class CrtChipPacket:
    """One CHIP packet inside a CRT image. Each cartridge bank maps
    to one packet. Multi-bank carts (e.g. 64-bank Ocean games, 64-bank
    EasyFlash) have one packet per bank."""

    # Position in the .crt file.
    file_offset: int
    # Total length of the packet (header + ROM data) as written
    # in the packet header at offset $04-$07.
    packet_length: int
    # Chip type: 0 = ROM, 1 = RAM (no data), 2 = Flash ROM
    chip_type: int
    # Bank number this packet belongs to. For a single-bank generic
    # cart this is 0. For multi-bank carts this is the bank index.
    bank: int
    # Where this bank loads in C64 memory: $8000 (LO ROM, $8000-$9FFF
    # or $8000-$BFFF), $A000 (HI ROM, $A000-$BFFF), or $E000
    # (Ultimax-mode HI ROM, $E000-$FFFF).
    load_addr: int
    # ROM image size in bytes (typically $2000 = 8 KiB or $4000 = 16 KiB).
    rom_size: int
    # The actual ROM bytes. For chip_type=1 (RAM-no-data) this is
    # an empty bytes.
    data: bytes = field(repr=False)

    @property
    def chip_type_label(self) -> str:
        return {0: "ROM", 1: "RAM", 2: "Flash"}.get(self.chip_type, f"?{self.chip_type}")

    @property
    def end_addr(self) -> int:
        """Last byte address (inclusive) this bank occupies in C64 memory."""
        return self.load_addr + self.rom_size - 1

    @property
    def addr_range_str(self) -> str:
        return f"${self.load_addr:04X}-${self.end_addr:04X}"


@dataclass
class CrtFile:
    """Parsed CRT cartridge file."""
    path: Path
    file_size: int
    signature: bytes        # the 16-byte header signature, raw
    machine: str             # 'C64' / 'C128' / 'CBM2' / 'VIC20' / 'PLUS4'
    header_length: int       # bytes (typically 64)
    crt_version: tuple        # (major, minor)
    hardware_id: int          # type ID (see CRT_TYPES)
    exrom: int                # 0 / 1
    game: int                 # 0 / 1
    subtype: int              # CRT v2.0+: subtype/revision byte
    name: str                 # cartridge name as ASCII
    name_raw: bytes           # raw 32-byte name field
    chips: list               # list[CrtChipPacket]

    # EasyFlash-specific extras, populated by detect_eapi() for
    # cartridges of hardware_id 32 (EasyFlash) or 33 (XBank). All
    # default to None when no EAPI block is found, when the cart isn't
    # an EasyFlash, or when the EAPI area is corrupt.
    eapi_present: bool = False
    eapi_version: Optional[str] = None      # e.g. "EAPI/M29F040" or "v4.10"
    eapi_chip_label: Optional[str] = None   # parsed flash chip name from version string
    ef_name: Optional[str] = None           # cartridge name from $BB00 area

    # Loader / file-system detection results, populated by
    # detect_loaders(). Each field is None unless that loader was
    # detected. See `detect_loaders` for the heuristics used.
    has_easyfs: bool = False
    easyfs_entries: list = field(default_factory=list)  # list[EasyFsEntry]
    has_yeti_filetable: bool = False
    yeti_entries: list = field(default_factory=list)    # list[YetiFileEntry]
    detected_loaders: list = field(default_factory=list) # list[str]
    is_magic_desk_layout: bool = False  # heuristic for OneLoad64-style

    @property
    def hardware_name(self) -> str:
        info = CRT_TYPES.get(self.hardware_id)
        if info:
            return info[1]
        return f"Unknown type {self.hardware_id}"

    @property
    def hardware_short(self) -> str:
        info = CRT_TYPES.get(self.hardware_id)
        if info:
            return info[0]
        return f"id{self.hardware_id}"

    @property
    def hardware_description(self) -> str:
        info = CRT_TYPES.get(self.hardware_id)
        if info:
            return info[2]
        return ("This hardware ID is not in the toolkit's database. "
                "It might be a newer cartridge type or a malformed CRT.")

    @property
    def total_rom_size(self) -> int:
        """Sum of all ROM/Flash banks (excludes RAM-only packets)."""
        return sum(p.rom_size for p in self.chips if p.chip_type != 1)

    @property
    def num_banks(self) -> int:
        """Number of distinct bank numbers across all CHIP packets."""
        return len({p.bank for p in self.chips})

    @property
    def mode_label(self) -> str:
        """Human-readable EXROM/GAME line summary. The four
        combinations select different memory mapping behaviours -
        these names match the C64 cartridge port spec."""
        e, g = self.exrom, self.game
        if e == 0 and g == 1: return "8K Game (8K ROM at $8000)"
        if e == 0 and g == 0: return "16K Game (ROM $8000-$BFFF)"
        if e == 1 and g == 0: return "Ultimax (ROM $E000-$FFFF, BASIC+KERNAL off)"
        if e == 1 and g == 1: return "Off (no ROM mapped, soft-cart)"
        return f"exrom={e} game={g}"

    @property
    def subtype_label(self) -> str:
        d = CRT_SUBTYPES.get(self.hardware_id)
        if d:
            return d.get(self.subtype, f"Subtype {self.subtype} (unknown)")
        if self.subtype != 0:
            return f"Subtype {self.subtype}"
        return ""


class CrtParseError(Exception):
    pass


def parse_crt(path) -> CrtFile:
    """Parse a CRT file from disk. Raises CrtParseError on bad
    signature or truncated header. Also auto-handles raw `.bin`
    cartridge ROM dumps (no CRT header) by wrapping them as a
    synthetic CRT - see `parse_raw_bin` for details."""
    p = Path(path)
    raw = p.read_bytes()
    # Auto-detect: real CRT files start with "C64 CARTRIDGE   " /
    # "C128 CARTRIDGE  " etc. Anything else we treat as a raw binary
    # ROM dump and synthesise a generic-cartridge CRT around it.
    if len(raw) >= 16 and raw[:16] in _VALID_SIGNATURES:
        return parse_crt_strict(raw, source_path=p)
    return parse_raw_bin(raw, source_path=p)


def parse_raw_bin(raw: bytes,
                    source_path: Optional[Path] = None) -> CrtFile:
    """Wrap a raw cartridge ROM dump (no CRT header) as a synthetic
    `CrtFile` so the toolkit UI works on `.bin` files too.

    Raw .bin dumps are common for:
      - Retro Replay / MMC Replay Flash backups (typically 64 KiB)
      - EasyFlash Flash backups (256 KiB / 512 KiB / 1 MiB)
      - GMod2 / GMod3 backups
      - 8 / 16 KiB plain Generic-cartridge dumps
      - dumps made with an EPROM burner

    We can't always know the *exact* hardware type from a raw bin
    (that's exactly what the CRT format adds), but we can:
      - look for the CBM80 magic at +$0004 ("$C3 $C2 $CD $38 $30")
        which marks an autostart cartridge with cold/warm vectors
        in the first 4 bytes
      - guess the bank layout from the size: 8/16 KiB = single bank
        Generic, 64 KiB = could be Final III / Retro Replay /
        Action Replay / Atomic Power, 32 KiB = Action Replay,
        128/256/512 KiB = Ocean / EasyFlash, 1 MiB = EasyFlash 1MiB.
      - synthesise CHIP packets at $8000 in 8 KiB slices so the
        user can browse banks individually.

    The toolkit shows "(synthesised from raw .bin)" in the info pane
    so the user knows the hardware type is a guess.
    """
    size = len(raw)

    # CBM80 magic check at +4: 5 bytes "$C3 $C2 $CD $38 $30" = "CBM80"
    # Some carts have the cold-start vector ($8000-$8001), warm-start
    # vector ($8002-$8003), then "CBM80" at $8004-$8008. This marks
    # an autostart cartridge.
    has_cbm80 = (size >= 9 and
                   raw[4:9] == b"\xC3\xC2\xCD\x38\x30")

    # Guess hardware type and bank layout from size.
    if size <= 0x1000:
        hw_id = 0   # Generic, 4 KiB
        bank_size = size
        addr0 = 0x8000
    elif size <= 0x2000:
        hw_id = 0   # Generic, 8 KiB
        bank_size = size
        addr0 = 0x8000
    elif size == 0x4000:
        hw_id = 0   # Generic, 16 KiB ($8000-$BFFF)
        bank_size = 0x4000
        addr0 = 0x8000
    elif size == 0x8000:
        # 32 KiB - could be AR4-7 (4 banks of 8 KiB at $8000) or
        # plain 32 KiB Generic. Assume AR-style banking is more
        # informative; the user can verify in the bytes view.
        hw_id = 1   # Action Replay
        bank_size = 0x2000
        addr0 = 0x8000
    elif size == 0x10000:
        # 64 KiB - typical for Retro Replay, MMC Replay, Final III,
        # Atomic Power. Guess Retro Replay since the dumps we see
        # most are RR. Lay out as 8 banks of 8 KiB at $8000.
        hw_id = 36   # Retro Replay
        bank_size = 0x2000
        addr0 = 0x8000
    elif size == 0x20000:
        hw_id = 5    # Ocean 128 KiB
        bank_size = 0x2000
        addr0 = 0x8000
    elif size == 0x40000:
        # 256 KiB - EasyFlash standard
        hw_id = 32
        bank_size = 0x2000
        addr0 = 0x8000
    elif size == 0x80000:
        hw_id = 32   # EasyFlash 512 KiB
        bank_size = 0x2000
        addr0 = 0x8000
    elif size == 0x100000:
        hw_id = 32   # EasyFlash 1 MiB
        bank_size = 0x2000
        addr0 = 0x8000
    else:
        # Unknown - fall back to Generic with a single huge bank.
        hw_id = 0
        bank_size = size if size > 0 else 1
        addr0 = 0x8000

    # Synthesise CHIP packets, 8 KiB / 16 KiB at a time.
    chips = []
    if size > 0:
        n = (size + bank_size - 1) // bank_size
        for i in range(n):
            chunk = raw[i * bank_size:(i + 1) * bank_size]
            chips.append(CrtChipPacket(
                file_offset=i * bank_size,
                packet_length=len(chunk) + 16,
                chip_type=0,         # treat all as ROM
                bank=i,
                load_addr=addr0,
                rom_size=len(chunk),
                data=chunk,
            ))

    name = (source_path.stem if source_path else "raw").upper()[:32]
    name_raw = name.encode('ascii', 'replace').ljust(32, b'\x00')[:32]

    crt = CrtFile(
        path=source_path if source_path is not None else Path("<bytes>"),
        file_size=size,
        signature=b"<RAW BIN>       ",
        machine="C64",
        header_length=0,           # marker: no real CRT header
        crt_version=(0, 0),
        hardware_id=hw_id,
        exrom=0,
        game=1,                    # 8K Game by default
        subtype=0,
        name=name,
        name_raw=name_raw,
        chips=chips,
    )
    # Stash a flag for the UI so it can show a banner.
    crt.is_raw_bin = True
    crt.cbm80_detected = has_cbm80
    # If we guessed EasyFlash from the size, look for EAPI signature
    # inside what would be ROMH bank 0. For raw .bin dumps this is
    # bytes $0000-$1FFF (the first 8 KiB) when the dump is in bank-
    # interleaved order, or split across two flash chips when from
    # an EPROM dump - we only handle the simple bank-0 case here.
    if crt.hardware_id in (32, 33):
        detect_eapi(crt)
    # Loader detection runs for all cart types - many use loader
    # libraries even outside EasyFlash.
    detect_loaders(crt)
    return crt


def parse_crt_bytes(raw: bytes,
                      source_path: Optional[Path] = None) -> CrtFile:
    """Parse a CRT image from a bytes object. If the buffer doesn't
    start with a valid CRT signature, falls back to `parse_raw_bin`
    so callers get a usable CrtFile either way. Useful for testing
    and for in-memory inspection of generated CRTs.

    To force strict CRT-only parsing (e.g. to validate that a
    generated file really IS a CRT), use `parse_crt_strict`.
    """
    if (len(raw) >= 16 and raw[:16] in _VALID_SIGNATURES):
        return parse_crt_strict(raw, source_path=source_path)
    return parse_raw_bin(raw, source_path=source_path)


def parse_crt_strict(raw: bytes,
                       source_path: Optional[Path] = None) -> CrtFile:
    """Parse a CRT image from a bytes object. Useful for testing
    and for in-memory inspection of generated CRTs."""
    if len(raw) < 0x40:
        raise CrtParseError(
            f"File too small ({len(raw)} bytes) to be a CRT - "
            "header alone is 64 bytes")

    sig = raw[0:16]
    if sig not in _VALID_SIGNATURES:
        raise CrtParseError(
            f"Bad CRT signature: {sig!r}. Expected one of: "
            f"{', '.join(s.decode().strip() for s in _VALID_SIGNATURES)}")

    if sig == _C64_SIGNATURE:
        machine = "C64"
    elif sig == _C128_SIGNATURE:
        machine = "C128"
    elif sig == _CBM2_SIGNATURE:
        machine = "CBM2"
    elif sig == _VIC20_SIGNATURE:
        machine = "VIC20"
    else:
        machine = "PLUS4"

    # All multi-byte fields in the CRT header are BIG-ENDIAN.
    header_length, = struct.unpack(">I", raw[0x10:0x14])
    crt_major  = raw[0x14]
    crt_minor  = raw[0x15]
    hardware_id, = struct.unpack(">H", raw[0x16:0x18])
    exrom    = raw[0x18]
    game     = raw[0x19]
    subtype  = raw[0x1A]

    name_raw = raw[0x20:0x40]
    # Trim trailing nulls and spaces; keep printable ASCII only.
    name_clean = name_raw.rstrip(b"\x00 ").decode("ascii", "replace")

    # Some malformed CRTs have header_length other than 0x40. Trust
    # the field but clamp to >= 0x40 to avoid reading into nothing.
    actual_header_len = max(header_length, 0x40)

    # Now walk CHIP packets starting from `actual_header_len`.
    chips: list[CrtChipPacket] = []
    pos = actual_header_len
    while pos < len(raw):
        if pos + 16 > len(raw):
            # Stray bytes at end - common in slightly-corrupt CRTs.
            # We just stop walking; the chips list still has what
            # we managed to parse.
            break
        hdr = raw[pos:pos + 16]
        if hdr[0:4] != b"CHIP":
            # Lost sync. Could be padding or junk after the last
            # chip. Stop.
            break
        # All big-endian
        packet_length, = struct.unpack(">I", hdr[4:8])
        chip_type,    = struct.unpack(">H", hdr[8:10])
        bank,         = struct.unpack(">H", hdr[10:12])
        load_addr,    = struct.unpack(">H", hdr[12:14])
        rom_size,     = struct.unpack(">H", hdr[14:16])

        # ROM data follows the 16-byte chip header. Some buggy CRTs
        # have packet_length != rom_size + 16 - we use rom_size to
        # extract the data and packet_length to advance the cursor.
        data_end = pos + 16 + rom_size
        if data_end > len(raw):
            # Truncated. Take whatever's there.
            data = raw[pos + 16:]
        else:
            data = raw[pos + 16:data_end]

        chips.append(CrtChipPacket(
            file_offset=pos,
            packet_length=packet_length,
            chip_type=chip_type,
            bank=bank,
            load_addr=load_addr,
            rom_size=rom_size,
            data=data,
        ))

        # Advance by packet_length (per the spec). Fallback to
        # rom_size + 16 if packet_length is 0/garbage.
        step = packet_length if packet_length >= 16 else (rom_size + 16)
        pos += step

    crt = CrtFile(
        path=source_path if source_path is not None else Path("<bytes>"),
        file_size=len(raw),
        signature=sig,
        machine=machine,
        header_length=header_length,
        crt_version=(crt_major, crt_minor),
        hardware_id=hardware_id,
        exrom=exrom,
        game=game,
        subtype=subtype,
        name=name_clean,
        name_raw=bytes(name_raw),
        chips=chips,
    )
    # For EasyFlash and XBank cartridges, look inside the ROMH bank-0
    # data for the EAPI signature + version string and the embedded
    # cartridge name.
    if hardware_id in (32, 33):
        detect_eapi(crt)
    # Loader detection runs for all cart types - many use loader
    # libraries even outside EasyFlash.
    detect_loaders(crt)
    return crt


def detect_eapi(crt: CrtFile) -> None:
    """Look for the EasyFlash EAPI signature and embedded cart name
    inside the ROMH bank-0 chunk of an EasyFlash CRT.

    Layout convention from skoe's EasyFlash spec:
      - ROMH bank 0 occupies the upper 8 KiB ($A000-$BFFF in 16K
        cartridge mode, or $E000-$FFFF in Ultimax).
      - Inside that 8 KiB chunk, offset $1800-$1AFF (= addresses
        $B800-$BAFF / $F800-$FAFF) is reserved for the EAPI driver
        code. The EAPI binary starts with the ASCII signature "EAPI"
        followed by a version string.
      - Offset $1B00 (= $BB00 / $FB00) and onwards is free for the
        cartridge author. By scene convention this often holds a
        printable ASCII name in the form "EF-NAME:GROUP/TITLE",
        terminated by a $00 or $FF byte.

    If no EAPI signature is found at the expected offset, sets
    `crt.eapi_present = False`. The function never raises - parse
    failures just leave the fields as None.
    """
    # Find the ROMH bank-0 packet. The ROMH chunk has load_addr
    # $A000 (or, less commonly, $E000 in Ultimax dumps).
    romh_b0 = None
    for p in crt.chips:
        if p.bank != 0:
            continue
        if p.load_addr in (0xA000, 0xE000):
            romh_b0 = p
            break
        # Also handle the case where the EasyFlash CRT has a single
        # 16K packet at $8000-$BFFF (some tools produce this) - the
        # EAPI/name area is then in the upper half.
        if p.load_addr == 0x8000 and p.rom_size == 0x4000:
            romh_b0 = p
            break

    if romh_b0 is None or len(romh_b0.data) < 0x2000:
        crt.eapi_present = False
        return

    # If we got the 16K packet, slice out the upper 8K for analysis.
    if romh_b0.load_addr == 0x8000 and romh_b0.rom_size == 0x4000:
        chunk = romh_b0.data[0x2000:0x4000]
    else:
        chunk = romh_b0.data[:0x2000]

    # EAPI signature can be either uppercase "EAPI" or lowercase
    # "eapi" depending on which tool created the cart. We accept
    # both.
    sig_at = 0x1800
    if len(chunk) < sig_at + 4:
        crt.eapi_present = False
        return
    sig_bytes = chunk[sig_at:sig_at + 4]
    if sig_bytes not in (b"EAPI", b"eapi"):
        # Some tools put the signature a few bytes later or earlier
        # in the EAPI area - scan a small window for it.
        scan_window = chunk[0x1800:0x1B00]
        idx = scan_window.find(b"EAPI")
        if idx < 0:
            idx = scan_window.find(b"eapi")
        if idx < 0:
            crt.eapi_present = False
            return
        sig_at = 0x1800 + idx

    crt.eapi_present = True

    # The version string follows the 4-byte signature. EasyProg
    # convention is null-terminated ASCII like "EAPI/M29F040 v4.0"
    # or just "M29F040 v4.0". We grab printable bytes up to the
    # first null / $FF / control character, max 32 chars.
    #
    # Some loaders (notably the Yeti Mountain EF release and other
    # custom Onslaught-era boot stubs) store the ASCII letters with
    # bit 7 set - i.e. 'A' shows up as $C1 instead of $41. This
    # appears to be either a leftover from PETSCII inverted-screen
    # encoding or just a "don't accidentally execute as code" trick.
    # Either way, the high bit is purely cosmetic to our purposes,
    # so we mask it off when classifying / decoding the version.
    ver_start = sig_at + 4
    ver_bytes = bytearray()
    for i in range(32):
        if ver_start + i >= len(chunk):
            break
        b = chunk[ver_start + i]
        if b == 0x00 or b == 0xFF:
            break
        # Mask off hi-bit before printability check
        b7 = b & 0x7F
        if b7 < 0x20:
            break
        ver_bytes.append(b7)
    if ver_bytes:
        crt.eapi_version = ver_bytes.decode('ascii', 'replace').strip()
        # Try to extract the flash chip name. The EAPI version
        # string has several formats in the wild:
        #   "EAPI/M29F040 v4.10"     - EasyProg standard
        #   "M29F040 V1.4"            - older/minimal form
        #   "AM/M29F040 V1.4"         - Yeti-style with manufacturer
        #                               prefix (A = AMD)
        # Extract the part that includes the chip code (M29F040 etc.)
        # but preserves manufacturer prefix when present.
        ver = crt.eapi_version
        # Strip a leading "EAPI/" if present (case-insensitive)
        if ver.upper().startswith("EAPI/"):
            ver = ver[5:]
        elif ver.upper().startswith("EAPI "):
            ver = ver[5:]
        # Split on whitespace - everything before the version number
        # ("V1.4", "v4.10", etc.) is the chip identifier.
        parts = ver.split()
        chip_parts = []
        for p in parts:
            # Stop at the first token that looks like "v<digit>" /
            # "V<digit>" - that's the version number, not the chip.
            if (len(p) >= 2 and p[0] in 'vV'
                    and p[1].isdigit()):
                break
            chip_parts.append(p)
        if chip_parts:
            crt.eapi_chip_label = ' '.join(chip_parts)

    # Embedded cart name at $1B00 (= $BB00). Convention used by the
    # cracker scene: printable ASCII like "EF-Name:Shooters/Onslaught"
    # null-terminated, often with $A0 or $00 padding to fill the
    # rest of the bank. Same hi-bit-set quirk as the EAPI version
    # field (Yeti et al.), so we mask off the top bit before
    # classifying.
    #
    # Some releases (e.g. muddyracers_ef.crt) split the name into
    # chunks separated by single $00 bytes plus 1-byte length markers,
    # like:
    #     "ef-nam" $00 $04 "MUDD" $00 $05 "ACER" $00 $06 "pt"
    # We accommodate this by skipping isolated single-$00 bytes (and
    # one trailing length byte) when followed by more printable text.
    # The walk only stops at $FF or at a long run of $00s.
    name_start = 0x1B00
    if name_start < len(chunk):
        name_bytes = bytearray()
        i = 0
        max_scan = 256
        while i < max_scan and name_start + i < len(chunk):
            b = chunk[name_start + i]
            if b == 0xFF:
                break
            if b == 0x00:
                # Single-null may be a chunk separator. Look ahead
                # 1-3 bytes for the next printable byte; if found,
                # consume the gap and treat as a single space.
                lookahead_end = min(name_start + i + 4, len(chunk))
                lookahead = chunk[name_start + i + 1:lookahead_end]
                printable_idx = -1
                for j, lb in enumerate(lookahead):
                    if lb in (0x00, 0xFF):
                        continue
                    lb7 = lb & 0x7F
                    if 0x20 <= lb7 < 0x7F:
                        printable_idx = j
                        break
                if printable_idx < 0:
                    break
                if name_bytes and not name_bytes.endswith(b' '):
                    name_bytes.append(0x20)
                i += 1 + printable_idx
                continue
            b7 = b & 0x7F
            if b7 < 0x20 and b7 not in (0x0A, 0x0D):
                break
            name_bytes.append(b7)
            i += 1
        name_str = name_bytes.decode('ascii', 'replace').strip()
        # Collapse multiple spaces to one
        while '  ' in name_str:
            name_str = name_str.replace('  ', ' ')
        # Strip the conventional "EF-Name:" / "EF-NAME:" / "ef-name:"
        # prefix. Variants we strip (case-insensitive):
        #   "EF-Name:" / "EF-NAME:" / "ef-name:" / "EF Name:" / "EFNAME:"
        #   "ef-nam"  - the muddyracers chunked-format prefix where
        #               the colon was eaten by the chunk separator
        low = name_str.lower()
        for prefix in ("ef-name:", "ef name:", "efname:", "ef:",
                        "ef-name ", "ef-nam ", "ef-name", "ef-nam"):
            if low.startswith(prefix):
                name_str = name_str[len(prefix):].lstrip(": ")
                break
        # Trim trailing garbage. After the actual cart title there's
        # often more bytes that look printable (because they're
        # opcodes whose ASCII glyphs happen to render). Heuristic:
        # walk word-by-word and stop when a word looks like garbage.
        # A "real" cart-name token has only letters, digits, and a
        # small set of allowed punctuation (-, ', :, ., /). If a
        # token contains other symbols ($, `, [, etc.), treat it
        # as garbage and truncate.
        if name_str:
            ALLOWED_PUNCT = set("-'.:_/&!()")
            kept = []
            for tok in name_str.split(' '):
                if not tok:
                    continue
                # Check character composition
                bad = sum(1 for c in tok
                           if not c.isalnum() and c not in ALLOWED_PUNCT)
                if bad > 0 and kept:
                    # Stop at the first token containing garbage chars
                    break
                if bad > 0 and not kept:
                    # First token with garbage - skip but try the next
                    continue
                kept.append(tok)
                if sum(len(k) for k in kept) > 40:
                    break
            name_str = ' '.join(kept).strip()

        # Sanity: reject uniform-byte fills (e.g. "nnnnnnnnnn..." from
        # bytes that were $EE = 'n'+0x80 - a flash erase / unused
        # marker, not an actual name). Also reject very short results.
        if name_str:
            unique_chars = set(name_str.replace(' ', ''))
            if len(unique_chars) < 3:
                # Too few distinct characters - probably uniform
                # padding bytes that happened to map to printable
                # glyphs.
                name_str = ""

        if name_str and len(name_str) >= 2:
            crt.ef_name = name_str


def format_crt_summary(crt: CrtFile) -> str:
    """Multi-line text summary for the info textbox. Includes header
    fields, hardware-type description, EXROM/GAME mapping mode, and a
    bank-by-bank table of CHIP packets."""
    lines = []
    is_raw = getattr(crt, 'is_raw_bin', False)
    if is_raw:
        lines.append("=" * 72)
        lines.append("This file has NO CRT header - it's a raw .bin "
                     "cartridge ROM dump.")
        lines.append("Header fields below are SYNTHESISED from the file "
                     "size and content;")
        lines.append("the real hardware type may differ. Use VICE's "
                     "cartconv to make a proper")
        lines.append(".crt with verified header info.")
        if getattr(crt, 'cbm80_detected', False):
            lines.append("")
            lines.append("CBM80 autostart magic detected at offset $0004 - "
                         "this IS a real cartridge")
            lines.append("ROM (cold/warm vectors + 'CBM80' signature at "
                         "$8000-$8008).")
        lines.append("=" * 72)
        lines.append("")

    lines.append(f"File:            {crt.path.name}")
    lines.append(f"Size:            {crt.file_size:,} bytes "
                 f"({crt.file_size / 1024:.1f} KiB)")
    lines.append(f"Signature:       {crt.signature.decode('ascii', 'replace')!r}")
    lines.append(f"Machine:         {crt.machine}")
    lines.append(f"CRT version:     {crt.crt_version[0]}.{crt.crt_version[1]:02d}")
    lines.append(f"Header length:   ${crt.header_length:08X} ({crt.header_length} bytes)")
    lines.append("")

    lines.append(f"Hardware ID:     {crt.hardware_id}  ({crt.hardware_short})")
    lines.append(f"Hardware name:   {crt.hardware_name}")
    if crt.subtype_label:
        lines.append(f"Subtype:         {crt.subtype} - {crt.subtype_label}")
    lines.append(f"EXROM line:      {crt.exrom}")
    lines.append(f"GAME line:       {crt.game}")
    lines.append(f"Memory mode:     {crt.mode_label}")
    lines.append(f"Cart name:       {crt.name!r}")

    # EasyFlash-specific extras: EAPI driver version + embedded
    # cart name from the bank-0 ROMH area at $1B00 / $BB00.
    if crt.hardware_id in (32, 33) and crt.eapi_present:
        lines.append("")
        lines.append("--- EasyFlash EAPI ---")
        if crt.eapi_version:
            lines.append(f"EAPI version:    {crt.eapi_version}")
        if crt.eapi_chip_label:
            lines.append(f"EAPI flash chip: {crt.eapi_chip_label}")
        if crt.ef_name:
            lines.append(f"EF name:         {crt.ef_name}")
    elif crt.hardware_id in (32, 33):
        lines.append("")
        lines.append("--- EasyFlash EAPI ---")
        lines.append("(no EAPI signature found at $B800 in bank-0 ROMH)")

    # GMod2 EEPROM banner.
    if crt.hardware_id == 60:
        eeprom_pkt = find_gmod2_eeprom_packet(crt)
        lines.append("")
        lines.append("--- GMod2 EEPROM ---")
        if eeprom_pkt is not None:
            lines.append(f"EEPROM packet:   present at file offset "
                         f"${eeprom_pkt.file_offset:08X}")
            lines.append(f"EEPROM size:     {len(eeprom_pkt.data):,} bytes "
                         f"(M93C86 capacity = 2048 bytes)")
            lines.append(f"Save data:       embedded in this CRT")
        else:
            lines.append("EEPROM packet:   not present")
            lines.append("Save data:       initialised with $FF on first run")
            lines.append("                 (cartconv / VICE only embed the "
                         "EEPROM if it was part")
            lines.append("                 of the original CRT). Use "
                         "'Replace GMod2 EEPROM...'")
            lines.append("                 to add an EEPROM image.")

    # Loader detection results: list of detected scene loaders /
    # filesystem markers. Each entry is a short label, e.g.
    # "EAPI v4.10 (M29F040)" or "OneLoad64 boot ($olo1870)".
    if crt.detected_loaders:
        lines.append("")
        lines.append("--- Detected loaders / file systems ---")
        for label in crt.detected_loaders:
            lines.append(f"  - {label}")

    # EasyFS directory listing if the cart contains a file system.
    # Up to 32 entries shown inline; if more, a count is given and
    # the user can use the dedicated "List EasyFS files" toolbar
    # button (or just look at the bytes view of bank 0) for more.
    if crt.has_easyfs and crt.easyfs_entries:
        lines.append("")
        lines.append(f"--- EasyFS directory ({len(crt.easyfs_entries)} entries) ---")
        lines.append(f"  {'#':>3}  {'name':<16}  {'type':<8}  "
                     f"{'bank':>4}  {'offset':>6}  {'size':>9}")
        lines.append("  " + ("-" * 60))
        max_show = 32
        for i, e in enumerate(crt.easyfs_entries[:max_show]):
            hidden = " (hidden)" if e.is_hidden else ""
            lines.append(
                f"  {i:>3}  {e.name:<16}  {e.type_label:<8}  "
                f"{e.bank:>4}  ${e.offset:04X}  "
                f"{e.size:>9,}{hidden}")
        if len(crt.easyfs_entries) > max_show:
            lines.append(f"  ... and {len(crt.easyfs_entries) - max_show} "
                         f"more entries")

    lines.append("")
    lines.append(f"--- Hardware notes ---")
    # Word-wrap the description to ~70 chars.
    desc = crt.hardware_description
    while desc:
        if len(desc) <= 72:
            lines.append(desc)
            break
        # Find the last space before col 72
        cut = desc.rfind(' ', 0, 72)
        if cut < 0: cut = 72
        lines.append(desc[:cut])
        desc = desc[cut:].lstrip()
    lines.append("")

    lines.append(f"--- CHIP packets ({len(crt.chips)} total, "
                 f"{crt.num_banks} unique bank(s), "
                 f"{crt.total_rom_size:,} bytes total ROM/Flash) ---")
    lines.append(f"{'#':>3}  {'offset':>10}  {'type':<5}  "
                 f"{'bank':>4}  {'address':<11}  {'size':>9}")
    lines.append('-' * 60)
    for i, p in enumerate(crt.chips):
        lines.append(
            f"{i:>3}  ${p.file_offset:08X}  {p.chip_type_label:<5}  "
            f"{p.bank:>4}  {p.addr_range_str:<11}  "
            f"${p.rom_size:04X} ({p.rom_size:>5,})")

    return "\n".join(lines)


def hex_dump_bank(packet: CrtChipPacket, bytes_per_line: int = 16,
                  start: int = 0, length: Optional[int] = None) -> str:
    """ASCII hex+text dump of a CHIP packet's data, formatted like
    a classic monitor (offset, hex bytes, ASCII gutter)."""
    if length is None:
        length = len(packet.data) - start
    end = min(start + length, len(packet.data))
    out = []
    base = packet.load_addr + start
    for off in range(start, end, bytes_per_line):
        chunk = packet.data[off:off + bytes_per_line]
        addr  = base + (off - start)
        hexstr = ' '.join(f"{b:02X}" for b in chunk)
        ascii_str = ''.join(chr(b) if 0x20 <= b <= 0x7E else '.'
                             for b in chunk)
        out.append(f"${addr:04X}: {hexstr:<{bytes_per_line * 3 - 1}}  "
                   f"{ascii_str}")
    return "\n".join(out)


def disasm_bank(packet: CrtChipPacket, show_illegal: bool = False,
                  label_mode: bool = False) -> str:
    """6502 disassembly of a CHIP packet, one line per instruction.
    Uses the disassembler from c64_disasm to share opcode tables.

    Format per line:
      $ADDR: BB BB BB    MNEM operand    ; comment
    or in label mode:
      LADDR: BB BB BB    MNEM Lxxxx       ; comment
    where every branch / jump / JSR target that lands inside the
    bank is replaced with a synthetic label `Lxxxx` (xxxx = the
    target address in hex). Targets that fall outside the bank
    (e.g. JSR $E544 to KERNAL CLEAR) keep their `$xxxx` form so it's
    obvious they exit the bank.

    The c64_disasm._DisasmLine slot names (pc / bytes / mnemonic /
    mode / operand / target / comment) don't match the obvious
    names you might expect — read those carefully if you're hacking
    on this.
    """
    from .c64_disasm import disassemble
    lines = disassemble(packet.data, packet.load_addr,
                          show_illegal=show_illegal)
    if not lines:
        return ""

    bank_lo = packet.load_addr
    bank_hi = packet.load_addr + len(packet.data)

    if label_mode:
        # First pass: collect all in-bank targets so we know which
        # instructions need a leading label.
        targets = set()
        for ln in lines:
            t = ln.target
            if t is None:
                continue
            if bank_lo <= t < bank_hi:
                targets.add(t)
        # Also mark the bank entry point as a label (helpful when
        # the user copies the disasm out).
        targets.add(bank_lo)

        # Second pass: emit, with leading "Lxxxx:" labels and
        # operand rewriting for in-bank targets.
        out = []
        for ln in lines:
            # Leading label line if this PC is a branch/jump target
            if ln.pc in targets:
                out.append(f"L{ln.pc:04X}:")
            byte_str = ' '.join(f"{b:02X}" for b in ln.bytes)
            comment = f"  ; {ln.comment}" if ln.comment else ""
            # Operand: rewrite if target is in-bank.
            operand = ln.operand
            if ln.target is not None and bank_lo <= ln.target < bank_hi:
                # Replace the absolute address in the operand string
                # with the synthetic label. The disassembler emits
                # operands like "$8042", "($8042)", "$8042,X" etc.;
                # we do a simple textual swap of the hex addr.
                addr_str = f"${ln.target:04X}"
                if addr_str in operand:
                    operand = operand.replace(addr_str, f"L{ln.target:04X}")
            out.append(f"  L{ln.pc:04X}: {byte_str:<10}  "
                       f"{ln.mnemonic:<5} {operand}{comment}")
        return "\n".join(out)

    # Address mode (default): plain $xxxx everywhere.
    out = []
    for ln in lines:
        byte_str = ' '.join(f"{b:02X}" for b in ln.bytes)
        comment = f"  ; {ln.comment}" if ln.comment else ""
        out.append(f"${ln.pc:04X}: {byte_str:<10}  "
                   f"{ln.mnemonic:<5} {ln.operand}{comment}")
    return "\n".join(out)


def extract_bank_to_bin(packet: CrtChipPacket, out_path) -> Path:
    """Write a single CHIP packet's raw ROM bytes to a .bin file.
    Returns the actual path written. The bank's load_addr is NOT
    prepended (unlike .prg) - this is a pure binary dump for use
    with eprom burners, cartconv, etc."""
    p = Path(out_path)
    p.write_bytes(packet.data)
    return p


def extract_bank_to_prg(packet: CrtChipPacket, out_path) -> Path:
    """Write a single CHIP packet as a .prg file with the load
    address prepended. Useful for loading into VICE's monitor or
    inspecting a single bank as a standalone program."""
    p = Path(out_path)
    hdr = struct.pack("<H", packet.load_addr)
    p.write_bytes(hdr + packet.data)
    return p


def extract_all_banks(crt: CrtFile, out_dir,
                       prefix: Optional[str] = None,
                       as_prg: bool = False) -> list:
    """Extract every CHIP packet to its own .bin (or .prg) file in
    `out_dir`. Filenames are <prefix>_b<bank>_<addr>.bin so they
    sort by bank. Returns the list of written paths.

    The default prefix is the CRT file basename without extension.
    """
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    if prefix is None:
        prefix = crt.path.stem or "crt"

    written = []
    ext = "prg" if as_prg else "bin"
    for i, p in enumerate(crt.chips):
        fn = (f"{prefix}_chip{i:03d}_b{p.bank:02d}_"
              f"{p.load_addr:04x}.{ext}")
        target = d / fn
        if as_prg:
            extract_bank_to_prg(p, target)
        else:
            extract_bank_to_bin(p, target)
        written.append(target)
    return written


# =====================================================================
# Loader / file-system detection
# =====================================================================
# Heuristics for spotting cartridge-side loader libraries and embedded
# file systems. All detection is best-effort - false positives are rare
# but possible, and signature-based detection can miss heavily
# obfuscated / packed loaders. The results show up in the Info pane so
# the user can see at a glance "this cart uses EAPI + EasyFS with 12
# files inside".


@dataclass
@dataclass
class EasyFsEntry:
    """One directory entry in an EasyFS file system inside a cart.

    On-disk format per skoe's EasyFlash-AppSupport.pdf, table 1.1:
      $00-$0F  16 bytes    Name (PETSCII, $00-padded, 16 chars max)
      $10      1 byte      Flags: bit 7 = Hidden, bits 0-5 = Type
                                     (see _easyfs_type_label())
      $11      1 byte      Bank where the file starts (0..63)
      $12      1 byte      Bank-High (reserved, always 0)
      $13-$14  2 bytes LE  Offset within the bank ($0000..$3FFF) for
                            files; CrtUsage for cartridge-typed entries
      $15-$17  3 bytes LE  Size in bytes (24-bit; max 16 MiB)
    Total: 24 bytes.

    Notes on the layout:
      - Name is at offset 0 (NOT offset 1 as in some older / wrong
        descriptions); the type byte sits AFTER the name at $10.
      - Size is a 24-bit little-endian integer, not 16-bit. Files
        can be larger than 64 KiB (e.g. cartridge images).
      - The R bits in flags should be 1 per spec but many EasyFS
        writers leave them 0; we tolerate either.
    """
    type_byte: int
    name: str        # decoded ASCII name
    name_raw: bytes  # raw 16 bytes
    bank: int        # 1 byte, 0..63
    offset: int      # 2 bytes LE, $0000..$3FFF (or CrtUsage word)
    size: int        # 3 bytes LE, file size in bytes

    @property
    def type_label(self) -> str:
        # Per spec table 1.3: bits 0-5 = type, bit 7 = hidden,
        # bits 5-6 = reserved (should be 1 but often 0).
        t = self.type_byte & 0x3F
        labels = {
            0x00: "Invalid",     # marked invalid (skip)
            0x01: "PRG",         # PRG with 2-byte load addr
            0x02: "PRG-LO",      # PRG, ROML only (EasyFS2)
            0x03: "PRG-HI",      # PRG, ROMH only (EasyFS2)
            0x10: "Cart8K",      # 8 KiB cart ($8000-$9FFF)
            0x11: "Cart16K",     # 16 KiB cart
            0x12: "Ultimax",     # Ultimax cart ($8000+$E000)
            0x13: "Ultimax-HI",  # Ultimax, ROML not used
            0x14: "OceanT1-512", # Ocean Type 1, 512 KiB (EasyFS2)
            0x15: "OcMagic",     # Ocean Type 1, 16-256 KiB (EasyFS2)
            0x1C: "xbank-8K",    # xbank 8K mode (EasyFS2)
            0x1D: "xbank-16K",   # xbank 16K mode (EasyFS2)
            0x1E: "xbank-Umax",  # xbank Ultimax (EasyFS2)
            0x1F: "End",         # end-of-directory marker
        }
        return labels.get(t, f"T{t:02X}")

    @property
    def is_hidden(self) -> bool:
        # Bit 7 of the type byte is the hidden flag (skoe's spec).
        return bool(self.type_byte & 0x80)


# EasyFS magic constants
_EASYFS_END_TYPE  = 0x1F     # spec says $1F = End of directory
_EASYFS_FREE_TYPE = 0xFF     # erased flash = all bits 1 = $FF
                              # (and entry's type field will read $1F)
_EASYFS_INVALID   = 0x00     # entries marked invalid by writer


@dataclass
class YetiFileEntry:
    """One entry in the Yeti-style file table used by the Yeti
    Mountain EF release (and possibly other Onslaught-era custom
    EF loaders that share the same format).

    On-disk layout: 8 bytes per entry, all little-endian:
      $00-$01  start_addr   - 14-bit byte offset within the entry's
                              EF bank. 0..$1FFF is ROML-side data,
                              0$2000..$3FFF would be ROMH-side
                              (rarely used by Yeti; banks 1..$3E
                              have only ROML data, so all observed
                              entries have start < $2000).
      $02      bank         - the EF bank holding this entry's
                              start. Files larger than the bank's
                              remaining bytes spill into bank+1.
      $03-$04  data_length  - file length in bytes.
      $05-$06  c64_loc      - target address in C64 RAM where the
                              loader copies the bytes (used for
                              displaying / for repacking; not
                              needed for raw extraction).
      $07      reserved     - $00 in all observed releases.

    The "raw" position formula matching Yeti's filetable.txt
    (treating the cart as a flat ROM image with bank 0 as the
    first 16 KiB and banks 1..N as 8 KiB each) is:
       raw_offset = $4000 + (bank - 1) * $2000 + start
    For our purposes we just keep `bank` and `start` and let the
    extractor walk CHIP packets directly.
    """
    bank: int
    start: int             # offset within the bank
    size: int              # length in bytes
    c64_loc: int           # target C64 RAM address
    index: int = 0         # 0-based index in the table

    @property
    def name(self) -> str:
        # Yeti's filetable doesn't store names; we synthesise one
        # from the entry index for display so the UI table has
        # something readable.
        return f"file_{self.index:02X}"

    @property
    def type_label(self) -> str:
        return "Yeti"


# Yeti loader filetable position (in bank 0 ROML data, byte
# offset within the 8 KiB packet). Verified against the
# YetiMountain_v1.05_EF.crt release; the loader is hard-coded
# to read the table from C64 mem $8100 onwards which corresponds
# to packet offset $0100.
_YETI_FILETABLE_OFFSET = 0x0100


def _romlbank0(crt: CrtFile) -> Optional[bytes]:
    """Return the data of the bank-0 ROML CHIP packet (the boot
    bank), or None if not found. Used by detect_yeti_filetable."""
    for p in crt.chips:
        if p.bank == 0 and p.load_addr == 0x8000:
            return p.data
    return None


def detect_yeti_filetable(crt: CrtFile) -> None:
    """Detect the Yeti-style file table at bank 0 ROML offset
    $0100. Populates crt.has_yeti_filetable + crt.yeti_entries
    if a plausible table is found.

    Heuristic for "this is a Yeti filetable":
      - At least 16 consecutive 8-byte entries pass the sanity
        check: bank < $80, length 1..$3FFF, c64_loc in $0200..$FF00,
        reserved byte == $00.
      - The first entry's start is small (< $0100) because the
        first file usually loads to a low position in its bank.
      - The first entry's bank is 0..3 (boot-time files live in
        low banks).

    These checks are conservative: a random ROM region passing all
    of them is statistically very unlikely.
    """
    crt.has_yeti_filetable = False
    crt.yeti_entries = []

    data = _romlbank0(crt)
    if data is None or len(data) < _YETI_FILETABLE_OFFSET + 16 * 8:
        return

    # Quick sanity-check on the first 16 entries
    section_start = _YETI_FILETABLE_OFFSET
    plausible = 0
    for i in range(16):
        off = section_start + i * 8
        if off + 8 > len(data):
            break
        chunk = data[off:off + 8]
        start  = chunk[0] | (chunk[1] << 8)
        bank   = chunk[2]
        length = chunk[3] | (chunk[4] << 8)
        loc    = chunk[5] | (chunk[6] << 8)
        extra  = chunk[7]
        # Length can exceed one bank ($2000) since Yeti files spill
        # across banks - accept up to $8000 (would span 4 banks).
        if (bank < 0x80 and 1 <= length < 0x8000
                and 0x0200 <= loc <= 0xFF00
                and extra == 0x00
                and start < 0x4000):
            plausible += 1

    if plausible < 14:   # require at least 14 of 16 to look right
        return

    # Walk the full table. Stop at the first all-zero entry
    # (terminator) or when we run off the end of the packet.
    entries = []
    i = 0
    while True:
        off = section_start + i * 8
        if off + 8 > len(data):
            break
        chunk = data[off:off + 8]
        if chunk == b'\x00' * 8 and i > 0:
            # All-zero terminator (only meaningful after the first
            # entry, since entry #0 itself can have start=0/bank=0)
            break
        start  = chunk[0] | (chunk[1] << 8)
        bank   = chunk[2]
        length = chunk[3] | (chunk[4] << 8)
        loc    = chunk[5] | (chunk[6] << 8)
        extra  = chunk[7]
        # Stop if we hit obviously non-table data
        if (bank >= 0x80 or length == 0 or length >= 0x8000
                or loc < 0x0100 or loc >= 0xFF00 or extra != 0x00):
            break
        entries.append(YetiFileEntry(
            bank=bank, start=start, size=length,
            c64_loc=loc, index=i))
        i += 1
        if i > 1024:  # paranoid cap
            break

    if entries:
        crt.has_yeti_filetable = True
        crt.yeti_entries = entries


def get_yeti_file_bytes(crt: CrtFile, entry: YetiFileEntry) -> bytes:
    """Read the bytes of a Yeti file entry, walking bank boundaries
    if the file exceeds the available bytes in its starting bank.

    Yeti banks 1..$3E carry only 8 KiB of ROML data each; bank 0 has
    16 KiB (ROML + ROMH); bank $3F (last) also has 16 KiB. We follow
    the convention used by Yeti's loader: reads continue at offset
    $0000 of bank+1 once the current bank is exhausted, and within
    a 16K-bank we read ROML first then ROMH.
    """
    if entry.size <= 0:
        return b''
    out = bytearray()
    remaining = entry.size
    cur_bank = entry.bank
    cur_off = entry.start

    def packet_data(bank, addr):
        for p in crt.chips:
            if p.bank == bank and p.load_addr == addr:
                return p.data
        return None

    # Within a bank: first ROML ($0000-$1FFF), then ROMH ($2000-$3FFF)
    # if `start` was already in ROMH range (>= $2000), start there.
    while remaining > 0 and cur_bank < 0x100:
        # Determine which packet covers cur_off
        if cur_off < 0x2000:
            d = packet_data(cur_bank, 0x8000)
            base_offset = 0
            local = cur_off
            packet_size = 0x2000
        else:
            d = packet_data(cur_bank, 0xA000)
            base_offset = 0x2000
            local = cur_off - 0x2000
            packet_size = 0x2000
        if d is None:
            # Move to next bank, ROML start
            cur_bank += 1
            cur_off = 0
            continue
        avail = packet_size - local
        if avail <= 0:
            # Out of this packet, advance
            if cur_off < 0x2000:
                # Try ROMH of the same bank
                cur_off = 0x2000
            else:
                cur_bank += 1
                cur_off = 0
            continue
        take = min(avail, remaining)
        out.extend(d[local:local + take])
        remaining -= take
        cur_off += take
        if remaining > 0:
            # Decide where to continue
            if cur_off >= 0x4000:
                # End of bank (16K), advance to next
                cur_bank += 1
                cur_off = 0
            elif cur_off >= 0x2000 and base_offset == 0:
                # Crossed ROML/ROMH boundary within same bank;
                # next read should be ROMH if available, else
                # next bank ROML
                pass  # the next iteration handles this via base_offset check
    return bytes(out)


def detect_loaders(crt: CrtFile) -> None:
    """Run all loader-detection heuristics and populate the
    `crt.detected_loaders`, `crt.has_easyfs`, `crt.easyfs_entries`
    and `crt.is_magic_desk_layout` fields.

    Adds human-readable strings to `detected_loaders` like:
      "EAPI v4.10 (M29F040)"
      "EasyFS (12 entries)"
      "EasyLoader menu"
      "OneLoad64-style Magic Desk loader"
      "Krill's loader (suspected)"

    All checks are silent / non-throwing - if anything goes wrong
    parsing data we just don't add the marker.
    """
    crt.detected_loaders = []

    # 1. EAPI - already detected separately, but surface it here too
    #    so the user has one place to see "what loaders are in this
    #    cart". detect_eapi() runs before us during parse_crt(), so
    #    eapi_present is reliable by now.
    if crt.eapi_present:
        ver = crt.eapi_version or "unknown version"
        # Only append the chip label if it's different from the
        # version string. For carts where the version field is just
        # "v4.10" with no chip name, the chip-detection heuristic
        # would set both to the same string and produce noise like
        # "EAPI v4.10 (v4.10)".
        chip = crt.eapi_chip_label
        if chip and chip != ver and chip not in ver:
            crt.detected_loaders.append(f"EAPI {ver} ({chip})")
        else:
            crt.detected_loaders.append(f"EAPI {ver}")

    # 2. EasyFS directory at $A000 in bank 0 ROMH (= file offset
    #    $0000 inside the ROMH bank-0 8KB chunk).
    _detect_easyfs(crt)
    if crt.has_easyfs:
        crt.detected_loaders.append(
            f"EasyFS file system ({len(crt.easyfs_entries)} entries)")

    # 2b. Yeti-style file table at bank 0 ROML offset $0100. Used
    # by the Yeti Mountain EF release and possibly other custom
    # Onslaught-era EF loaders that share the format.
    detect_yeti_filetable(crt)
    if crt.has_yeti_filetable:
        crt.detected_loaders.append(
            f"Yeti loader file table "
            f"({len(crt.yeti_entries)} entries)")

    # 2c. Ocean Type 1 (cart_type 5) - John Meegan's adaptation of
    # Paul Hughes' Freeload tape system. Used by Navy SEALs, Batman,
    # Pang, Toki, Robocop 2/3, Double Dragon, Chase HQ II, SOTB,
    # Battle Command, Lemmings and other Ocean cartridges.
    #
    # We only flag the cart family here based on the CRT header; we
    # do NOT attempt to decode the file table, because Ocean shipped
    # at least 4 different boot/loader variants across these titles
    # (different decompressor styles, different boot table layouts,
    # some uncompressed) and a single file-walking heuristic would
    # produce wrong results for most of them. File extraction needs
    # a per-title emulator-driven approach that's out of scope for
    # the static toolkit.
    if crt.hardware_id == 5:
        kb = crt.total_rom_size // 1024
        crt.detected_loaders.append(
            f"Ocean Type 1 cartridge ({kb} KiB, "
            f"{crt.num_banks} banks)")

    # 3. Scene-marker / loader signature scan. We look for ASCII
    #    strings in the bank-0 data that identify common loaders.
    found_markers = _scan_loader_strings(crt)
    crt.detected_loaders.extend(found_markers)

    # 4. Magic Desk layout check (OneLoad64-style or original
    #    Magic Desk).
    if _looks_like_magic_desk(crt):
        crt.is_magic_desk_layout = True
        if crt.hardware_id == 19:
            # Already labeled as Magic Desk by hardware_id — no need
            # to add a redundant marker.
            pass
        else:
            crt.detected_loaders.append(
                "Magic Desk-style boot layout (OneLoad64?)")


def _detect_easyfs(crt: CrtFile) -> None:
    """Look for a 24-byte-per-entry EasyFS directory at the start of
    bank 0 ROMH (= load_addr $A000 / $E000) per skoe's spec.

    The directory table starts immediately at the bank's beginning
    and runs until an entry with type byte $1E (End) is encountered,
    or up to 255 entries. Type $1F means "free / erased flash slot"
    and is skipped silently. Names are 16-byte PETSCII strings
    null-padded.

    Heuristic guard: we require at least one non-Free entry whose
    name decodes to printable ASCII for at least 2 chars - random
    flash data trivially passes the type-byte check otherwise.
    """
    crt.has_easyfs = False
    crt.easyfs_entries = []

    # Find ROMH bank 0 (or the upper half of a 16K bank-0 packet at
    # $8000 - same trick as detect_eapi).
    chunk = _romh_bank0(crt)
    if chunk is None or len(chunk) < 24:
        return

    entries = []
    pos = 0
    seen_real = 0
    for _ in range(256):  # generous cap; spec says max 255
        if pos + 24 > len(chunk):
            break
        # Per skoe's spec table 1.1:
        #   $00-$0F  name (16 bytes, PETSCII, $00-padded)
        #   $10      flags/type (bit 7=hidden, bits 0-5=type)
        #   $11      bank (1 byte, 0..63)
        #   $12      bank-high (reserved, always 0)
        #   $13-$14  offset within bank (LE 16-bit, 0..$3FFF)
        #   $15-$17  size (LE 24-bit)
        name_raw = bytes(chunk[pos:pos + 0x10])
        type_byte = chunk[pos + 0x10]
        # End-of-directory markers:
        #   - type=$1F per spec
        #   - type=$FF (erased flash) since erased entries' type
        #     bits read all-1, which extends to $1F when masked but
        #     the full $FF byte plus all-$FF surrounding data is
        #     a more reliable end signal
        if (type_byte & 0x3F) == 0x1F or type_byte == 0xFF:
            break
        # Invalid entry - skip without counting (spec says these
        # have type=$00 but other flags may be set)
        if (type_byte & 0x3F) == 0x00:
            pos += 24
            continue
        bank = chunk[pos + 0x11]
        # bank_high = chunk[pos + 0x12]  # reserved, ignored
        offset = int.from_bytes(chunk[pos + 0x13:pos + 0x15], 'little')
        size = int.from_bytes(chunk[pos + 0x15:pos + 0x18], 'little')

        # Decode the name. The 16-byte name field is C-string-
        # style: terminated by the first $00 byte. Stop there to
        # avoid rendering padding/junk as trailing dots.
        #
        # PETSCII display convention: EasyLoader (and the C64 itself
        # when it boots into mixed-case "shifted" mode) renders
        # ASCII letters with their case flipped:
        #   stored byte $61-$7A ('a'-'z') -> displayed as 'A'-'Z'
        #   stored byte $41-$5A ('A'-'Z') -> displayed as 'a'-'z'
        # So a CRT name byte sequence "bADlANDS" actually appears
        # as "BadLands" on the C64 screen, which is also how users
        # think of the title. We apply the case-flip to match the
        # familiar form. Other ASCII characters (digits, punctuation,
        # space) pass through unchanged.
        null_idx = name_raw.find(b'\x00')
        if null_idx >= 0:
            usable = name_raw[:null_idx]
        else:
            usable = name_raw
        name_raw_str = usable.rstrip(b' ').decode('ascii', 'replace')
        # Replace non-printable mid-string chars with '.' for safety
        name_raw_str = ''.join(
            c if 0x20 <= ord(c) <= 0x7E else '.'
            for c in name_raw_str)
        # Strip trailing dots/spaces (often padding bytes that got
        # turned into dots by the non-printable filter).
        name_raw_str = name_raw_str.rstrip('. ')
        # Apply C64 display-case mapping
        name_clean = name_raw_str.swapcase()
        entry = EasyFsEntry(
            type_byte=type_byte,
            name=name_clean,
            name_raw=name_raw,
            bank=bank,
            offset=offset,
            size=size,
        )
        entries.append(entry)
        # Guard for the post-loop sanity check:
        # bank<64, size 1..16MiB, offset 0..$3FFF, printable name
        if (len(name_clean) >= 2 and bank < 64 and
                1 <= size <= 0x1000000 and offset < 0x4000):
            seen_real += 1
        pos += 24

    # Require at least 3 plausible-looking entries before we
    # claim it's EasyFS. With our stricter per-entry checks
    # (bank<64, size 1..$10000, offset<$4000, printable name)
    # this already rejects most random ROM bytes; demanding 3
    # such entries in a row pushes the false-positive rate down
    # further. The ratio of real-to-total also has to be high
    # (>= 70%) so that a directory-shaped chunk with mostly
    # garbage entries doesn't get flagged.
    if entries and seen_real >= 3 and seen_real / len(entries) >= 0.7:
        crt.has_easyfs = True
        crt.easyfs_entries = entries


def _romh_bank0(crt: CrtFile) -> Optional[bytes]:
    """Return the 8 KiB chunk of ROMH bank 0, or None if not found.

    Mirrors the logic from detect_eapi() but returned as raw bytes
    so the caller can pick offsets. Handles the three common
    layouts:
      - Separate $A000 packet for ROMH bank 0
      - Separate $E000 packet (Ultimax-mode dumps)
      - 16K $8000 packet that includes both ROML+ROMH
    """
    for p in crt.chips:
        if p.bank != 0:
            continue
        if p.load_addr in (0xA000, 0xE000) and len(p.data) >= 0x2000:
            return p.data[:0x2000]
        if p.load_addr == 0x8000 and p.rom_size == 0x4000 and len(p.data) >= 0x4000:
            return p.data[0x2000:0x4000]
    return None


# Loader-signature strings to scan for. Order matters - we report
# the first match per category. Strings should be unique enough
# that random ROM data hits them rarely. All are case-sensitive
# ASCII; some loaders use mixed case in the binary.
#
# Sources: searched scene EF release dumps + the OneLoad64
# collection's known loader fingerprints + scene-loader source
# repos (Krill, Bitfire, Spindle, $olo1870 conventions).
_LOADER_SIGNATURES = [
    # (label, byte sequence)
    #
    # === Cart-side loaders ===
    # EasyLoader (alx) menu - the classic 8-bit-Era EF menu
    ("EasyLoader menu (alx)",      b"EasyLoader"),
    # $olo1870's OneLoad64 boot-code marker
    ("OneLoad64 boot ($olo1870)",  b"$olo1870"),
    ("OneLoad64 boot ($olo1870)",  b"OneLoad64"),
    # NDEF / DCM Multi-Cart Builder marker
    ("NDEF / DCM multi-cart",      b"NDEF"),
    # EasyProg signature (rare in actual game CRTs but possible
    # in saved-back EF dumps)
    ("EasyProg-modified CRT",      b"EasyProg"),
    # GMod2 cartridge skeleton (icomp.de) marker
    ("GMod2 cartridge-skeleton",   b"icomp.de"),

    # === Cart-bundled drive-loader code (informational only) ===
    # These loaders are normally drive-side fastloaders for the
    # 1541 / SD2IEC / Ultimate-II+ - they need a real (or emulated)
    # serial drive to function and CANNOT replace the cart bank
    # mechanism. We still scan for them because some hybrid CRT
    # releases SHIP a drive loader along with cart code so the cart
    # can subsequently load extra files from disk (e.g. multi-load
    # games where the cart only holds the boot stub + IRQ loader,
    # and the actual game data lives on a 1541-format disk image
    # the user is expected to have in their drive). When we see one
    # of these strings inside a CRT, the most likely interpretation
    # is "drive-loader code present" rather than "this cart is
    # loaded via Krill", so the labels are tagged accordingly.
    ("Drive-loader code: Krill's Loader",
                                       b"krill"),
    ("Drive-loader code: Krill's Loader",
                                       b"KRILL"),
    ("Drive-loader code: Bitfire",     b"bitfire"),
    ("Drive-loader code: Bitfire",     b"BITFIRE"),
    ("Drive-loader code: Spindle (lft)",
                                       b"spindle"),
    ("Drive-loader code: BoozeLoader",
                                       b"BOOZELOADER"),
    ("Drive-loader code: Sparkle",     b"SPARKLE"),
    ("Drive-loader code: Bongo (Bonzai)",
                                       b"BONGO LOADER"),
    # Excess group's Lemmings EF cart-side loader.
    # NOTE: this IS a cart-side loader (the EF Lemmings release
    # uses a custom cart-resident loader for level streaming),
    # unlike the drive-loaders above. We keep the marker because
    # "EXCESS" alone would also fire on cracker-intro greetings,
    # so we're picky about the context.
    ("Excess EF cart loader",          b"EXCESS LOADER"),

    # NOTE: Tape loaders (Cyberload, Novaload, Vorpal, Invade-A-Load,
    # Hypra-Load) have been removed from this list. Tape loaders
    # operate on Datasette tape signals and have no meaning inside
    # a CRT cartridge image - a CRT is a memory-mapped ROM, not a
    # tape stream. If a CRT contains the literal string "NOVALOAD"
    # it's almost certainly because the title was originally
    # tape-based and the cart includes a credits screen mentioning
    # the original loader, not because the cart "uses" the loader.

    # === Compressors / depackers (often in cart boot stubs) ===
    # Exomizer (Magnus Lind) - one of the most common; magic
    # "exo" appears in several decrunch routines.
    ("Exomizer depacker",          b"exomizer"),
    ("Exomizer depacker",          b"EXOMIZER"),
    # Pucrunch (Pasi Ojala) - "PuCrunch" or "pucrunch"
    ("Pucrunch depacker",          b"pucrunch"),
    ("Pucrunch depacker",          b"PUCRUNCH"),
    ("Pucrunch depacker",          b"PuCrunch"),
    # ByteBoozer (HCL/Booze Design)
    ("ByteBoozer depacker",        b"ByteBoozer"),
    ("ByteBoozer depacker",        b"BYTEBOOZER"),
    # Doynamite (Doynax)
    ("Doynamite depacker",         b"Doynamite"),
    ("Doynamite depacker",         b"DOYNAMITE"),
    # Subsizer (1-byte-state LZ)
    ("Subsizer depacker",          b"subsizer"),
    ("Subsizer depacker",          b"Subsizer"),
    # Nucrunch
    ("Nucrunch depacker",          b"nucrunch"),
    ("Nucrunch depacker",          b"Nucrunch"),
    # tscrunch (recent fast cruncher)
    ("tscrunch depacker",          b"tscrunch"),
    ("tscrunch depacker",          b"TSCRUNCH"),
    # Time Crunch (matcham)
    ("Time Crunch depacker",       b"TimeCrunch"),
    ("Time Crunch depacker",       b"TIMECRUNCH"),
    # Cruel Cruncher
    ("Cruel Cruncher",             b"CruelCruncher"),
    ("Cruel Cruncher",             b"CRUEL CRUNCHER"),
    # Equinoxe / The Sharks Darksqueezer
    ("Darksqueezer (The Sharks)",  b"Darksqueezer"),
    # Shrinkler (cross-system, rare on c64 but occurs)
    ("Shrinkler depacker",         b"Shrinkler"),
    # Trilogic File Press / Expert
    ("Trilogic File Press",        b"FilePress"),
    # Speedpacker
    ("Speedpacker (Matcham)",      b"Speedpacker"),

    # === SID players (player code embedded in cart for music) ===
    # GoatTracker player
    ("GoatTracker player",         b"GoatTracker"),
    ("GoatTracker player",         b"GOATTRACK"),
    # JCH NewPlayer (Jens Christian Huus) - "JCH" is too short and
    # would false-positive in scrolltexts; use a longer marker
    ("JCH player (Vibrants)",      b"JCH "),  # space-suffixed
    ("JCH player (Vibrants)",      b"JCH-"),
    ("JCH player (Vibrants)",      b"NEWPLAYER"),
    # Future Composer (SoundMon style)
    ("Future Composer player",     b"Future Composer"),
    ("Future Composer player",     b"FUTURE COMPOSER"),
    # SDI (Sound Demon Interface)
    ("SDI player",                 b"SDI Player"),
    # CYBERTRACKER
    ("Cybertracker player",        b"CYBERTRACKER"),
    # Music Assembler / Voice Tracker
    ("Music Assembler player",     b"MUSIC ASSEMBLER"),
    # SID Factory II
    ("SID Factory II player",      b"SID Factory"),
    # DMC (Demo Music Creator) - more contextual
    ("DMC player (Graffity)",      b"DMC V"),

    # NOTE: Cracker-intro / scene-group markers (Nostalgia, Triad,
    # F4CG, Hokuto Force, etc.), as well as generic string markers
    # ("cracked by", "trainer by", "FILE_ID.DIZ", "Protovision",
    # "RGCD", "Psytronik"), have been removed from the detection
    # list. They are not loaders, file systems, packers or players -
    # just text strings in scrolltexts, greetings tables and credit
    # screens. Surfacing them here mixes orthogonal concepts (what
    # technical mechanism does the cart use?) with unrelated
    # metadata (whose scrolltext is embedded?). If the user wants
    # to find such strings, the Hex tab's Find feature is the right
    # tool - it can search for any byte pattern or quoted ASCII
    # string and is much more flexible than a hardcoded list.
]


def _scan_loader_strings(crt: CrtFile) -> list:
    """Scan the cartridge data for known loader / packer / scene
    markers. Returns a de-duplicated list of human-readable labels.

    The scan is bounded by SCAN_BUDGET_BYTES (currently 1 MiB - the
    full size of a maxed-out EasyFlash) so it's fast even on huge
    multi-cart compilations. Bank 0 is always scanned in full
    because that's where boot stubs and EAPI sit. Higher banks are
    scanned in numerical order until the budget is consumed - this
    biases us toward finding loader code over level data, since
    level data is typically in higher banks.
    """
    SCAN_BUDGET_BYTES = 0x100000  # 1 MiB
    found = set()
    parts = []
    consumed = 0
    # Bank 0 first (always full), then ascending bank order
    sorted_chips = sorted(crt.chips,
                            key=lambda p: (p.bank != 0, p.bank, p.load_addr))
    for p in sorted_chips:
        if consumed >= SCAN_BUDGET_BYTES:
            break
        parts.append(p.data)
        consumed += len(p.data)
    haystack = b''.join(parts)

    for label, sig in _LOADER_SIGNATURES:
        if sig in haystack and label not in found:
            found.add(label)

    # Preserve insertion order from _LOADER_SIGNATURES.
    seen = set()
    out = []
    for label, _ in _LOADER_SIGNATURES:
        if label in found and label not in seen:
            out.append(label)
            seen.add(label)
    return out


def _looks_like_magic_desk(crt: CrtFile) -> bool:
    """Heuristic for OneLoad64-style Magic Desk loader.

    OneLoad64 by StatMat (et al.) uses a single-load Magic-Desk-format
    CRT for ~2,000 games. Distinctive markers:
      - hardware_id is either 19 (Magic Desk) or 5 (Ocean) - some
        OL64 entries got mis-tagged
      - small total ROM size (typically <= 64 KiB)
      - bank-0 starts with the standard Magic Desk init code which
        writes to $DE00 to switch banks then jumps into BASIC ROM
    Rather than fingerprinting the exact code (would break across
    OL64 versions), we just check the hardware_id + size envelope.
    """
    if crt.hardware_id == 19:
        return True
    # Some unusual cases: hw=5 (Ocean) but only 64 KB and 8 banks
    if (crt.hardware_id == 5 and crt.total_rom_size <= 0x10000
            and len(crt.chips) <= 8):
        # Possible OL64 mis-tagged entry
        return True
    return False


# =====================================================================
# EasyFS file extraction
# =====================================================================
# Helpers for reading file content out of an EasyFS directory entry.
# An EasyFS file lives at (bank, offset) and may span multiple banks
# if its size exceeds (bank_size - offset). We follow the "linear"
# convention used by EasyLoader: the file continues at offset 0 of
# the next bank if it overflows the current one. ROML banks ($8000)
# come before ROMH banks ($A000) of the same bank-number, but in
# practice EasyFS files are stored in ROML, so we walk ROML banks
# in numeric order.

def get_bank_data(crt: CrtFile, bank_no: int,
                    addr_pref: int = 0x8000) -> bytes:
    """Return the data of the CHIP packet for the given bank number
    at the given preferred load address ($8000 ROML / $A000 ROMH).
    Returns b'' if no matching packet exists."""
    for p in crt.chips:
        if p.bank == bank_no and p.load_addr == addr_pref:
            return p.data
    # Fallback: any bank with that number
    for p in crt.chips:
        if p.bank == bank_no:
            return p.data
    return b''


def extract_easyfs_entry(crt: CrtFile, entry: EasyFsEntry) -> bytes:
    """Read all bytes of an EasyFS file out of the cart, walking
    bank boundaries if needed. Returns exactly entry.size bytes
    (or fewer if the cart truncates).

    EasyFS files address the cart as 16 KiB banks (ROML = $0000..
    $1FFF + ROMH = $2000..$3FFF). The entry's `offset` is in this
    16 KiB address space:
       offset $0000..$1FFF -> ROML data of bank N
       offset $2000..$3FFF -> ROMH data of bank N (offset - $2000)
    When the read would cross $4000, we wrap to bank N+1 offset 0
    (= ROML start of next bank). For files that don't have a ROMH
    packet, we still return whatever ROML data is available and
    advance to the next bank.

    Crucial: only entry.size bytes are returned. The previous
    implementation kept reading until the bank chain ended, which
    produced files much larger than the spec'd size."""
    if entry.size == 0:
        return b''
    out = bytearray()
    remaining = entry.size
    cur_bank = entry.bank
    cur_off = entry.offset    # 0..$3FFF address within the bank
    # Cap the bank walk to avoid runaway loops on corrupt directory
    # entries that claim huge sizes.
    for _ in range(1024):
        # Decide which packet (ROML / ROMH) holds cur_off
        if cur_off < 0x2000:
            packet = get_bank_data(crt, cur_bank, 0x8000)
            local = cur_off
            packet_end = 0x2000
        elif cur_off < 0x4000:
            packet = get_bank_data(crt, cur_bank, 0xA000)
            local = cur_off - 0x2000
            packet_end = 0x4000
        else:
            # Shouldn't happen; advance to next bank defensively
            cur_bank += 1
            cur_off = 0
            continue

        if not packet:
            # No data in this packet. If we were trying ROML,
            # try ROMH next. If ROMH is also missing, advance to
            # next bank.
            if cur_off < 0x2000:
                cur_off = 0x2000
            else:
                cur_bank += 1
                cur_off = 0
            continue

        avail = len(packet) - local
        if avail <= 0:
            # This packet is exhausted. Advance to next packet
            # within the same bank (ROML -> ROMH) or next bank.
            if cur_off < 0x2000:
                cur_off = 0x2000
            else:
                cur_bank += 1
                cur_off = 0
            continue

        # How many bytes can we take before crossing into the next
        # packet (ROML -> ROMH boundary at $2000, or end of bank
        # at $4000)?
        bytes_until_packet_end = packet_end - cur_off
        take = min(avail, bytes_until_packet_end, remaining)
        out.extend(packet[local:local + take])
        remaining -= take
        cur_off += take
        if remaining <= 0:
            break
        # If we hit the end of bank ($4000), wrap to next bank ROML
        if cur_off >= 0x4000:
            cur_bank += 1
            cur_off = 0
    return bytes(out)


def write_easyfs_entry(crt: CrtFile, entry: EasyFsEntry,
                         out_path, prepend_load_addr: bool = False):
    """Write a single EasyFS entry's bytes to disk.

    Filename sanitisation: PETSCII names can contain spaces and
    other chars that are awkward on host filesystems; we leave them
    alone except for forbidden Windows chars (`< > : " / \\ | ? *`)
    which are replaced with '_'. Entry names are 16 chars max so
    truncation isn't a worry.

    If `prepend_load_addr` is True and the entry doesn't already
    look like a PRG (first 2 bytes look like a sane load addr),
    prepends $0801 (BASIC start). The default is False because most
    EasyFS files of type PRG already have the load address as their
    first 2 bytes - that's the convention.
    """
    data = extract_easyfs_entry(crt, entry)
    if prepend_load_addr and len(data) >= 2:
        lo, hi = data[0], data[1]
        addr = lo | (hi << 8)
        # If the first 2 bytes look like a sane load addr ($0400-$FFFE),
        # they're already a PRG header - don't re-add. Otherwise add
        # $0801 so the file at least loads as BASIC.
        if not (0x0400 <= addr <= 0xFFFE):
            data = b'\x01\x08' + data
    Path(out_path).write_bytes(data)
    return out_path


def sanitize_easyfs_name(name: str, ext: str = "prg") -> str:
    """Make a file name from an EasyFS entry name that's safe on
    Windows / macOS / Linux. Replaces forbidden chars with '_',
    strips trailing dots and spaces (Windows refuses those),
    appends the given extension if not present."""
    if not name:
        return f"unnamed.{ext}"
    forbidden = set('<>:"/\\|?*')
    cleaned = ''.join('_' if c in forbidden or ord(c) < 0x20 else c
                       for c in name)
    cleaned = cleaned.rstrip(' .')
    if not cleaned:
        cleaned = "unnamed"
    if not cleaned.lower().endswith(f".{ext}"):
        cleaned = f"{cleaned}.{ext}"
    return cleaned


# =====================================================================
# Embedded data-blob scanner
# =====================================================================
# Find sub-files embedded inside a CHIP bank's data: SID tunes, Koala
# bitmaps, sprite blocks, PRG-style payloads, and likely-compressed
# blobs (Exomizer / Pucrunch decruncher stubs).
#
# Why this is useful
# ------------------
# Many cart-based releases bundle their assets as raw blobs inside a
# bank rather than going through EasyFS. To rip the music or graphics
# out of a cart you usually need to:
#
#   1. Find where the asset starts (which bank, which offset).
#   2. Recognise what kind of asset it is.
#   3. Decompress it if it's packed.
#
# Step 3 (decompression) is genuinely hard - the c64 cruncher
# ecosystem is hostile to generic unpacking because most of them
# don't put a magic-byte header in front of their output. The
# decompressor stub itself is more easily fingerprinted because the
# first few dozen bytes of code are stable across many crunched
# files, and the user can then either:
#   - extract the raw compressed blob and run it through a desktop
#     unpacker (UnpackerKK, exomizer-cli, pucrunch -u, etc), or
#   - load the bank directly into VICE and dump the decompressed
#     RAM after the cart has unpacked itself.
#
# So this module's job is "point at the interesting blobs"; the
# user decides what to do next.


@dataclass
class EmbeddedBlob:
    """One embedded data blob found by the blob scanner."""
    chip_index: int   # which CHIP packet it lives in (the first
                      # one for multi-bank blobs)
    bank: int         # bank number (= packet.bank, first one for
                      # multi-bank blobs)
    offset: int       # byte offset within the (first) packet
    kind: str         # "PSID", "Koala bitmap?", "Hires bitmap?",
                      # "Sprite block", "Charset (2KB)",
                      # "PRG payload", "Possible Exomizer SFX stub",
                      # "CBM80 autostart" etc.
    size: int         # size in bytes (for multi-bank blobs, the
                      # total spliced size across all banks)
    note: str = ""    # human-readable extra info
    spans_banks: list = field(default_factory=list)  # list of chip
                      # indices the blob spans, in order. Empty for
                      # single-bank blobs (the chip_index field is
                      # the canonical location).


# Decompressor stub fingerprints. We look for short distinctive
# byte sequences that appear early in 6502-style decruncher code.
# These are by no means exhaustive - obfuscated / patched stubs
# will not match - but on un-modified scene crunchers they are
# very reliable. Each entry is (label, bytes).
#
# The byte sequences here are derived from looking at the standard
# decruncher source files shipped with each cruncher and identifying
# distinctive opcode sequences that appear inside the JSR
# decrunch / get-crunched-byte routines. They are short enough
# (4-6 bytes) to not need to be at offset 0 - we search the whole
# packet.
_DEPACKER_STUB_PATTERNS = [
    # Exomizer 2.x mem/level/sfx decruncher signature.
    # The decruncher's "get_crunched_byte" routine starts with
    # LDA $xxxx / BNE / DEC $xxxx / DEC $xxxx-1 ... we use the
    # specific 5-byte init pattern that's stable across exo 2/3.
    # (taken from exodecrunch.s in the exomizer distribution)
    ("Exomizer",         b"\xa2\x00\xa0\x00\xbd"),  # ldx#0 ldy#0 lda abs,x
    ("Exomizer",         b"\xa9\x00\x85\xfd\x85\xfe"),  # init zp $fd/$fe
    # Pucrunch decruncher signature: starts with LDX #$lo / LDA #$hi
    # / STX zp / STA zp+1 then a fixed JSR sequence
    ("Pucrunch",         b"\xa2\xff\xa0\xff\x86"),  # variant of init
    # Doynamite - distinctive 4-byte init pattern
    ("Doynamite",        b"\xa9\xee\x85"),       # rare opcode lda#$EE sta
    # Note: All of the above are necessarily rough - any cart
    # boot code that initialises zero-page can match. We list
    # the matches as "possible <name> SFX stub" rather than
    # certain finds.
    #
    # ByteBoozer was previously listed here with the pattern
    # \xa9\x37\x85\x01 (LDA #$37 / STA $01 = "RAM under ROM"
    # bank-switch). Removed because that's a generic C64 cart
    # boot prologue used by countless cracker intros, loaders
    # and demos - the pattern matched far too aggressively to
    # be useful as a ByteBoozer indicator specifically.
]


def scan_embedded_blobs(packet: CrtChipPacket) -> list:
    """Scan a single CHIP packet's bytes for recognisable embedded
    blobs. Returns a list of EmbeddedBlob entries sorted by offset.

    The scanner runs several detectors in parallel and reports any
    confident finds. Overlapping detections are kept (a SID file
    that happens to start with what looks like a PRG header will
    show up as both, which is informative rather than wrong).
    """
    found = []
    data = packet.data
    if not data:
        return found

    n = len(data)

    # 1. SID files - "PSID" / "RSID" magic at offset 0 of the file
    #    (so we look for it anywhere within the packet, alignment
    #    doesn't matter as long as the rest of the header parses).
    for sig in (b"PSID", b"RSID"):
        pos = 0
        while True:
            i = data.find(sig, pos)
            if i < 0:
                break
            # Validate: SID v1 header is 0x76 bytes, v2/v3/v4 is 0x7C.
            # We need at least 0x76 bytes available.
            if i + 0x76 <= n:
                # Header version at offset +4 is BIG-endian word
                version = (data[i + 4] << 8) | data[i + 5]
                if 1 <= version <= 4:
                    # Header data offset at +6 is BIG-endian
                    data_off = (data[i + 6] << 8) | data[i + 7]
                    # Pull out chip ID / song name from SID header
                    name_b = data[i + 0x16:i + 0x36]
                    name = name_b.split(b'\x00', 1)[0].decode(
                        'latin-1', 'replace')
                    # Songs count etc. are in the header but rough
                    # size estimate is whole-packet-tail
                    size = n - i
                    found.append(EmbeddedBlob(
                        chip_index=-1, bank=packet.bank,
                        offset=i, kind=f"{sig.decode()}",
                        size=size,
                        note=f'v{version}, "{name[:24]}"'))
            pos = i + 4

    # 2. Koala bitmap files - 10001 bytes raw. Distinguishing a
    #    real Koala from random ROM bytes requires several signals:
    #
    #    a) Color-RAM upper nibble: VIC-II only reads the lower 4
    #       bits of color RAM. Real Koala writers always set the
    #       upper nibble to 0. Random ROM bytes have it 0 only with
    #       1/16 probability. Require ≥95% of color-RAM bytes to
    #       have upper-nibble == 0 - this single test rejects nearly
    #       all false positives.
    #    b) Background-color byte (offset $2710 / 10000) must be
    #       a single nibble ($00-$0F).
    #    c) Bitmap must show byte-level variety (random crunched
    #       data has Shannon entropy near 8 bits/byte, but a real
    #       bitmap has significant runs of $00 / $FF in background
    #       and edge cells - entropy lower).
    #    d) Screen-RAM should have decent variety (≥10 distinct
    #       byte values in a sample of 200) - real images use many
    #       cell colour combinations; uniform bitmap data fails.
    if n >= 10001:
        for off in range(0, n - 10000, 0x100):
            color_ram = data[off + 9000:off + 10000]
            # Test (a): color-RAM upper nibble must be ~all-zero
            upper_zero = sum(1 for b in color_ram if (b >> 4) == 0)
            if upper_zero < 950:   # require 95% strictness
                continue
            # Plus: color-RAM must have variety. Empty padding
            # regions (1000x $00) pass test (a) trivially. Real
            # color RAM has at least 4 distinct nibble values.
            distinct_col = len(set(b & 0x0F for b in color_ram))
            if distinct_col < 4:
                continue
            # Test (b): background-colour byte
            bg = data[off + 10000]
            if bg > 0x0F:
                continue
            # Test (c): bitmap variety. We accept anything in the
            # range 5..250 distinct bytes per 1000-byte sample.
            # Random crunched data: ~250+. Real bitmap with realistic
            # imagery: 50-220. All-zero/all-FF: 1. Highly artificial
            # uniform fills: 2-4. The lower bound rejects degenerate
            # blocks; the upper bound rejects compressed/random data.
            bm_sample = data[off:off + 1000]
            distinct_bm = len(set(bm_sample))
            if distinct_bm > 250 or distinct_bm < 5:
                continue
            # Test (d): screen-RAM variety. Real images can have
            # large flat regions (sky, ground) that use only 2-3
            # distinct cell-color pairs, so the floor is generous;
            # the strong discriminator is test (a). This test mainly
            # rejects all-same-byte fills.
            scr = data[off + 8000:off + 9000]
            distinct_scr = len(set(scr[:200]))
            if distinct_scr < 3:
                continue
            # All four passed - confident enough to report
            note = (f"color-RAM upper nibble {upper_zero}/1000 zero, "
                    f"bg=${bg:02X}, bitmap {distinct_bm} distinct, "
                    f"screen {distinct_scr} distinct")
            found.append(EmbeddedBlob(
                chip_index=-1, bank=packet.bank,
                offset=off, kind="Koala bitmap",
                size=10001, note=note))
            break

    # 2b. Hires bitmap files - 9000 bytes = 8000 bitmap + 1000
    #    screen RAM. Detection signals:
    #
    #    a) Bitmap variety: 8000 bytes of bitmap data should not
    #       be all-zero, all-FF, or uniform. Distinct byte-count
    #       in a 1000-byte sample below ~250.
    #    b) Screen-RAM character: real screen RAM holds two-color
    #       cell-colour pairs. Distinct byte-count of screen sample
    #       between 8 and ~200 (real images use few cell colour
    #       pairs; random data uses near 256). Fewer than 8 means
    #       uniform bitmap-like data.
    #    c) Both nibble distributions of screen RAM should have
    #       low entropy compared to random data, but high enough
    #       that the screen isn't pure single-colour.
    if n >= 9000:
        for off in range(0, n - 8999, 0x100):
            bm_sample = data[off:off + 1000]
            distinct_bm = len(set(bm_sample))
            if distinct_bm > 250 or distinct_bm < 5:
                continue
            scr = data[off + 8000:off + 9000]
            distinct_scr = len(set(scr[:200]))
            if not (10 <= distinct_scr <= 220):
                continue
            distinct_lo = len(set(b & 0x0F for b in scr[:200]))
            distinct_hi = len(set((b >> 4) & 0x0F for b in scr[:200]))
            # Real images typically use 4-12 distinct palette
            # indices per cell-half. Random data uses 13-16 (close
            # to all 16). Use ≤12 as the upper bound.
            if not (3 <= distinct_lo <= 12):
                continue
            if not (3 <= distinct_hi <= 12):
                continue
            note = (f"bitmap {distinct_bm} distinct, "
                    f"screen {distinct_scr} distinct, "
                    f"nib-lo {distinct_lo}, nib-hi {distinct_hi}")
            found.append(EmbeddedBlob(
                chip_index=-1, bank=packet.bank,
                offset=off, kind="Hires bitmap",
                size=9000, note=note))
            break

    # 2c. Sprite blocks - sprites are 63 bytes and live in 64-byte
    #    aligned slots. A "sprite block" is hard to identify from
    #    bytes alone (sprites are dense bit-patterns indistinguishable
    #    from compressed code/data), but we can spot the typical
    #    cartridge convention: a long run of 64-byte chunks where
    #    the 64th byte (slot[63]) is always $00 (the unused trailing
    #    byte). Require at least 8 consecutive 64-byte slots with
    #    byte 63 == $00 to fire.
    SPRITE_RUN_MIN = 8
    if n >= SPRITE_RUN_MIN * 64:
        for off in range(0, n - SPRITE_RUN_MIN * 64 + 1, 64):
            ok = True
            for s in range(SPRITE_RUN_MIN):
                if data[off + s * 64 + 63] != 0:
                    ok = False
                    break
            if ok:
                # Determine actual run length
                run = SPRITE_RUN_MIN
                while (off + run * 64 < n
                       and data[off + run * 64 + 63] == 0):
                    run += 1
                # Sanity: each sprite shouldn't be all-zero or
                # all-FF (those would be empty / blocked sprites)
                non_empty = sum(
                    1 for s in range(run)
                    if any(b not in (0x00, 0xFF)
                           for b in data[off + s * 64:off + s * 64 + 63]))
                if non_empty >= 2:
                    found.append(EmbeddedBlob(
                        chip_index=-1, bank=packet.bank,
                        offset=off, kind=f"Sprite block ({run} sprites)",
                        size=run * 64,
                        note=f"63B+pad slots, {non_empty} non-empty"))
                    # Skip past the block
                    break  # only report first block per packet to limit output

    # 2d. Charsets (256 glyphs * 8 bytes = 2048 bytes). Scan at
    # $0100 alignment - charsets in carts are usually $0800-aligned
    # but some scene productions deliberately misalign.
    #
    # Two anti-false-positive guards:
    #   1) Skip offsets where a Koala or Hires bitmap was already
    #      detected - bitmap data with uniform rows can pass the
    #      symmetry+edge-clear charset checks otherwise.
    #   2) Require glyph-pattern diversity - a real charset has
    #      ~150-256 distinct 8-byte glyph patterns out of 256;
    #      uniform bitmap rows produce only a handful of distinct
    #      patterns (just $00, $FF, and a few others repeating).
    bitmap_offsets = set(b.offset for b in found
                          if 'koala' in b.kind.lower()
                          or 'hires' in b.kind.lower())
    charset_hits = 0
    CHARSET_HITS_LIMIT = 2
    if n >= 2048:
        for off in range(0, n - 2047, 0x100):
            if charset_hits >= CHARSET_HITS_LIMIT:
                break
            # Guard 1: skip if already detected as bitmap
            if any(abs(off - bo) < 256 for bo in bitmap_offsets):
                continue
            # Guard 2: require glyph diversity. Build the set of
            # distinct 8-byte tuples in the 2KB block. Real charsets
            # have ≥120 unique glyphs (most letters are distinct);
            # uniform bitmap data has 1-20 unique glyphs because
            # repetitive byte patterns produce few unique 8-tuples.
            block = data[off:off + 2048]
            glyphs_set = set()
            for g in range(256):
                glyphs_set.add(block[g * 8:g * 8 + 8])
                if len(glyphs_set) >= 120:
                    break  # short-circuit early once threshold met
            if len(glyphs_set) < 120:
                continue
            is_cs, score, csnote = _is_likely_charset(data, off)
            if is_cs and score >= 60:
                found.append(EmbeddedBlob(
                    chip_index=-1, bank=packet.bank,
                    offset=off, kind=f"Charset (2KB)",
                    size=2048,
                    note=f"score {score}/100, "
                          f"{len(glyphs_set)}+ unique glyphs, {csnote}"))
                charset_hits += 1

    # 3. PRG-like payloads - a 16-bit load address followed by
    #    plausible 6502 code. We're conservative here because random
    #    ROM bytes too easily look like a "load address + init code"
    #    pair. Two strategies:
    #
    #    (a) Strong signal: BASIC SYS-stub. The byte sequence
    #        01 08 NN 08 NN NN 9E (load $0801 + line link + 9E=SYS
    #        token) is essentially impossible by chance. Any match
    #        is almost certainly a packed/auto-starting PRG embedded
    #        in the cart. Common in EasyFlash compilations that use
    #        Exomizer/Pucrunch self-decrunching files (RGCD, Psytronik
    #        etc.). We allow these without limit.
    #
    #    (b) Weaker heuristic: any 16-bit load address from a known
    #        C64 location followed by 64 bytes of code-like content
    #        (>= 6 common opcodes). Limited to 4 hits per packet
    #        because random ROM bytes can satisfy this with ~1%
    #        probability.
    PLAUSIBLE_PRG_LOADS = (
        0x0801,  # BASIC start
        0x1000, 0x2000, 0x3000, 0x4000, 0x5000,
        0x6000, 0x7000, 0x8000, 0xA000, 0xC000,
    )
    COMMON_6502_OPS = {0xA9, 0xA2, 0xA0, 0x8D, 0x20, 0x60, 0x4C, 0x85, 0x86, 0x84}

    # (a) BASIC SYS-stub detector. The actual detection AND size
    # determination has moved to _scan_raw_crt_for_sys_stub_prgs()
    # in scan_all_blobs() because SYS-stub PRGs in cart compilations
    # often span multiple bank packets and proper size detection
    # needs to walk past bank boundaries to the padding marker.
    #
    # Here we just record stub offsets so the generic load-address
    # heuristic below knows to skip them (it would otherwise match
    # every PRG-stub as a "load addr $0801" hit).
    pos = 0
    sys_stub_offsets = set()
    while pos + 16 < n:
        if (data[pos] == 0x01 and data[pos+1] == 0x08
                and data[pos+3] == 0x08
                and data[pos+6] == 0x9E):
            sys_stub_offsets.add(pos)
            pos += 64
            continue
        pos += 1

    # (b) Generic load-address heuristic - DISABLED.
    # Previously this scanned for any 16-bit load-address-shaped
    # bytes followed by 6+ common 6502 opcodes in the next 64 bytes.
    # In practice this matched aggressively in graphics/charset/
    # sprite data and produced too many false positives, drowning
    # out the real PRG SYS-stub matches in the Blobs tab.
    # The cross-bank SYS-stub scan in scan_all_blobs() is much
    # more reliable for finding real PRGs.

    # 4. Decompressor stubs.
    for label, sig in _DEPACKER_STUB_PATTERNS:
        if sig in data:
            i = data.index(sig)
            found.append(EmbeddedBlob(
                chip_index=-1, bank=packet.bank,
                offset=i, kind=f"Possible {label} SFX stub",
                size=len(sig),
                note=f"matched {len(sig)}-byte pattern"))

    # 5. CBM80 autostart marker - only meaningful in bank 0 at
    # offset $0004 (i.e. C64 absolute $8004). Anywhere else it's
    # almost certainly a coincidental byte match in random ROM data
    # so we don't report it.
    if packet.bank == 0 and len(data) > 9:
        if data[4:9] == b"\xC3\xC2\xCD\x38\x30":
            found.append(EmbeddedBlob(
                chip_index=-1, bank=packet.bank,
                offset=4, kind="CBM80 autostart",
                size=5,
                note="cartridge autostart magic"))

    # Sort by offset and de-duplicate exact matches
    found.sort(key=lambda b: (b.offset, b.kind))
    seen = set()
    out = []
    for b in found:
        key = (b.offset, b.kind)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def scan_all_blobs(crt: CrtFile) -> list:
    """Run scan_embedded_blobs on every CHIP packet in the CRT and
    return a flat list with chip_index filled in. Also runs a second
    cross-bank scan that looks for blobs (Koala / Hires / Charset /
    Sprite blocks) which are too big to fit in one 8K packet, by
    concatenating consecutive ROML banks and scanning the joined
    stream. Cross-bank hits are tagged with `spans_banks` so the
    renderer / extractor knows to splice across packets."""
    all_blobs = []
    for ci, p in enumerate(crt.chips):
        for blob in scan_embedded_blobs(p):
            blob.chip_index = ci
            all_blobs.append(blob)

    # Cross-bank scan: build runs of consecutive ROML banks
    # ($8000 packets in ascending bank order) and scan the joined
    # bytes for blobs that wouldn't fit in a single packet.
    roml_chips = sorted(
        ((ci, p) for ci, p in enumerate(crt.chips)
         if p.load_addr == 0x8000 and p.chip_type != 1),
        key=lambda item: item[1].bank)
    if len(roml_chips) >= 2:
        # Group into runs of consecutive bank numbers
        runs = []
        current = [roml_chips[0]]
        for ci, p in roml_chips[1:]:
            prev_bank = current[-1][1].bank
            if p.bank == prev_bank + 1 and len(p.data) == len(current[-1][1].data):
                current.append((ci, p))
            else:
                runs.append(current)
                current = [(ci, p)]
        runs.append(current)

        for run in runs:
            if len(run) < 2:
                continue   # single-bank, already covered by per-packet scan
            joined = b''.join(p.data for _, p in run)
            bank_size = len(run[0][1].data)
            cross_blobs = _scan_joined_stream(joined, bank_size, run)
            all_blobs.extend(cross_blobs)

    # Per-packet scan: charsets fit in 2KB so they can live inside
    # an 8K packet, but we want to scan even small packets for
    # them. The per-packet scanner already handles that case.

    # De-duplicate: if the same blob (same kind, same bank, same
    # offset) is reported by both the single-bank scan and a
    # cross-bank run with spans_banks of just one chip, prefer
    # the canonical (non-spanning) one.
    seen = {}
    for b in all_blobs:
        key = (b.kind, b.bank, b.offset)
        if key not in seen:
            seen[key] = b
        else:
            # Keep the entry that isn't a degenerate single-element span
            existing = seen[key]
            if not b.spans_banks and existing.spans_banks:
                seen[key] = b
    out = list(seen.values())

    # Cross-bank Exomizer SFX scan in the raw CRT bytes. SYS-stub
    # PRGs in cart compilations can be much larger than a single
    # 8K bank, but cart-builders typically lay them out
    # contiguously across consecutive bank packets in the .crt
    # file. By scanning the raw .crt byte stream (skipping over
    # CHIP packet headers) we can locate each PRG, find its
    # decruncher entry-point JMP, and walk to the padding boundary
    # to determine the exact size.
    try:
        raw = open(crt.path, 'rb').read() if crt.path else None
    except Exception:
        raw = None
    if raw:
        cross_bank_blobs = _scan_raw_crt_for_sys_stub_prgs(crt, raw, out)
        out.extend(cross_bank_blobs)

    return out


def _scan_raw_crt_for_sys_stub_prgs(crt, raw_crt: bytes,
                                       existing_blobs: list) -> list:
    """Scan the raw .crt byte stream for BASIC SYS-stub PRGs and
    determine their exact size by following the copy-loop JMP
    target and finding the padding boundary ($EA / $FF runs).

    Returns a list of EmbeddedBlobs with size = exact PRG size
    in the cart (from SYS-stub byte to last byte before padding).
    Annotates spans_banks if the PRG crosses bank boundaries.

    Skips PRGs whose SYS-stub start position is already covered
    by an existing per-packet detection (those will be merged
    with the cross-bank size info via the de-dup logic).
    """
    found = []
    if not raw_crt:
        return found
    n = len(raw_crt)

    # Build a map of CRT-file offset -> (chip_index, bank, addr,
    # in-packet-offset) for translating stub positions back to
    # bank coordinates.
    # We do this by walking the CHIP packet table.
    chip_ranges = []   # list of (file_start, file_end_excl, chip_index, packet)
    for ci, p in enumerate(crt.chips):
        file_start = p.file_offset + 16  # +16 for CHIP header
        file_end = file_start + len(p.data)
        chip_ranges.append((file_start, file_end, ci, p))

    def file_off_to_chip(off):
        for fs, fe, ci, p in chip_ranges:
            if fs <= off < fe:
                return ci, p, off - fs
        return None, None, None

    # Find all "01 08 NN 08 NN NN 9E" SYS-stub patterns in raw_crt
    pos = 0
    seen_stub_positions = set()
    while pos < n - 16:
        if not (raw_crt[pos] == 0x01 and raw_crt[pos+1] == 0x08
                and raw_crt[pos+3] == 0x08
                and raw_crt[pos+6] == 0x9E):
            pos += 1
            continue
        # Parse SYS arg
        sa = pos + 7
        if sa < n and raw_crt[sa] == 0x20:
            sa += 1
        digits = bytearray()
        while sa < n and 0x30 <= raw_crt[sa] <= 0x39:
            digits.append(raw_crt[sa])
            sa += 1
        sys_arg = int(digits) if digits else 0

        # Find JMP target after BASIC stub by stepping through 6502
        # instructions. The first $4C byte we see may actually be an
        # OPERAND of a preceding instruction (e.g. LDA $4CD6,X has
        # $4C as its high-address byte) - so a naive byte-scan finds
        # phantom JMPs. We step through instructions properly: for
        # each opcode we know its length (1/2/3 bytes) and skip
        # past it, only treating it as a JMP if it really is opcode
        # $4C at instruction-aligned position.
        #
        # Two flavours of decruncher entry:
        #   (a) Main-memory decruncher: JMP target $0820..$FFFE
        #   (b) Zero-page / stack-page decruncher: JMP target
        #       $0100..$081F (decruncher is copied to zp first)
        # We accept either - the first JMP we hit is the target.
        jmp_target = None
        jmp_in_zp = False
        # Skip the BASIC stub: find the SYS arg digits and the
        # null-terminator that ends the BASIC line.
        ip = pos + 7
        if ip < n and raw_crt[ip] == 0x20:
            ip += 1
        while ip < n and 0x30 <= raw_crt[ip] <= 0x39:
            ip += 1
        # Skip up to 4 zero bytes (BASIC line terminator + null link)
        nulls = 0
        while ip < n and raw_crt[ip] == 0x00 and nulls < 4:
            ip += 1
            nulls += 1
        # Now step opcodes for up to 100 bytes
        step_end = min(ip + 100, n - 3)
        # 6502 opcode -> instruction length lookup. 1-byte: implied/
        # accumulator/stack ops, plus illegal opcodes we treat as 1.
        # 2-byte: immediate, zero-page, zp,X/Y, indirect-X/Y, branches.
        # 3-byte: absolute, absolute,X/Y, indirect.
        OPLEN_2 = {
            0x09,0x29,0x49,0x69,0x89,0xA9,0xC9,0xE9,    # imm
            0x05,0x25,0x45,0x65,0x85,0xA5,0xC5,0xE5,    # zp
            0x06,0x26,0x46,0x66,0x86,0xA6,0xC6,0xE6,
            0x24,0x84,0xA4,0xC4,0xE4,
            0x15,0x35,0x55,0x75,0x95,0xB5,0xD5,0xF5,    # zp,X
            0x16,0x36,0x56,0x76,0x96,0xB6,0xD6,0xF6,
            0x94,0xB4,
            0x01,0x21,0x41,0x61,0x81,0xA1,0xC1,0xE1,    # (zp,X)
            0x11,0x31,0x51,0x71,0x91,0xB1,0xD1,0xF1,    # (zp),Y
            0xA0,0xA2,                                    # immediate Y/X
            0x10,0x30,0x50,0x70,0x90,0xB0,0xD0,0xF0,    # branches
        }
        OPLEN_3 = {
            0x0D,0x2D,0x4D,0x6D,0x8D,0xAD,0xCD,0xED,    # abs
            0x0E,0x2E,0x4E,0x6E,0x8E,0xAE,0xCE,0xEE,
            0x2C,0x8C,0xAC,0xCC,0xEC,
            0x1D,0x3D,0x5D,0x7D,0x9D,0xBD,0xDD,0xFD,    # abs,X
            0x19,0x39,0x59,0x79,0x99,0xB9,0xD9,0xF9,    # abs,Y
            0x1E,0x3E,0x5E,0x7E,0xBE,0xDE,0xFE,
            0xBC,
            0x20,0x4C,                                    # JSR, JMP abs
            0x6C,                                          # JMP indirect
        }
        while ip < step_end:
            op = raw_crt[ip]
            if op == 0x4C:
                # JMP abs - read target
                if ip + 2 < n:
                    target = raw_crt[ip+1] | (raw_crt[ip+2] << 8)
                    if 0x0100 <= target <= 0xFFFE:
                        jmp_target = target
                        jmp_in_zp = (target < 0x0820)
                        break
                ip += 3
                continue
            if op in OPLEN_3:
                ip += 3
            elif op in OPLEN_2:
                ip += 2
            else:
                ip += 1
        if jmp_target is None:
            pos += 1
            continue

        # Estimate end position. For "main memory" decrunchers the
        # decruncher routine sits at JMP target and is ~250 bytes,
        # so file ends ~(JMP - $0801 + 250) bytes after stub. For
        # zero-page decrunchers the JMP target tells us nothing
        # about file size; use a moderate initial guess that the
        # padding-boundary scan can refine.
        if jmp_in_zp:
            rough_size = 8192   # ~8 KiB initial guess
        else:
            rough_size = (jmp_target + 250) - 0x0801

        # Find the next SYS-stub in the raw CRT (if any) - that's
        # an absolute upper bound on this PRG's size, since two
        # PRGs can't overlap.
        next_stub_pos = n
        scan_p = pos + 64
        while scan_p < n - 16:
            if (raw_crt[scan_p] == 0x01 and raw_crt[scan_p+1] == 0x08
                    and raw_crt[scan_p+3] == 0x08
                    and raw_crt[scan_p+6] == 0x9E):
                next_stub_pos = scan_p
                break
            scan_p += 1

        # Now scan forward from a bit before that estimate for the
        # first run of >= 32 consecutive $EA or $FF bytes (= padding).
        # Stop the scan at the next SYS-stub - the real PRG can't
        # extend past it. Cap the scan at 64 KiB total.
        search_start = pos + max(rough_size - 64, 256)
        search_end = min(pos + 65536, next_stub_pos, n - 32)
        padding_pos = -1
        for k in range(search_start, search_end):
            chunk = raw_crt[k:k+32]
            if len(chunk) < 32:
                break
            if all(b == 0xEA for b in chunk) or all(b == 0xFF for b in chunk):
                padding_pos = k
                break
        if padding_pos > 0:
            exact_size = padding_pos - pos
            size_source = (f"JMP ${jmp_target:04X} + padding "
                            f"boundary at CRT off ${padding_pos:06X}")
        elif next_stub_pos < n:
            # No padding found, but there's another SYS-stub ahead -
            # use that as the upper bound. The real PRG ends just
            # before the next one.
            exact_size = next_stub_pos - pos
            size_source = (f"JMP ${jmp_target:04X}, bounded by next "
                            f"SYS-stub at CRT ${next_stub_pos:06X}")
        else:
            # No padding and no next stub - fall back to rough estimate
            exact_size = rough_size
            size_source = (f"JMP ${jmp_target:04X} + ~250 (no padding)")

        # Translate CRT file offset to bank/in-packet offset
        ci, packet, in_packet = file_off_to_chip(pos)
        if ci is None:
            pos += 1
            continue
        # Determine span: how many CHIP packets this PRG covers
        end_off = pos + exact_size
        last_ci, _, _ = file_off_to_chip(end_off - 1)
        if last_ci is None or last_ci < ci:
            last_ci = ci
        spans = list(range(ci, last_ci + 1)) if last_ci > ci else []

        # Build human-readable bank list for the note
        if spans:
            bank_list = [crt.chips[i].bank for i in spans]
            bank_list_str = ",".join(f"{b:02X}" for b in bank_list)
            note = (f"BASIC SYS {sys_arg} stub, {exact_size}b "
                     f"(banks ${bank_list_str}; {size_source})")
        else:
            note = (f"BASIC SYS {sys_arg} stub, {exact_size}b "
                     f"({size_source})")

        found.append(EmbeddedBlob(
            chip_index=ci, bank=packet.bank,
            offset=in_packet,
            kind="PRG payload (multi-bank)" if spans else "PRG payload",
            size=exact_size,
            note=note,
            spans_banks=spans))
        # Skip past this PRG to avoid re-detecting nested SYS-stubs
        # in the packed data itself
        pos += max(64, exact_size)

    return found


def _scan_joined_stream(joined: bytes, bank_size: int,
                          run: list) -> list:
    """Scan a concatenated ROML-bank stream for blobs that wouldn't
    fit in a single packet. Returns EmbeddedBlobs with spans_banks
    set to the chip indices the blob crosses.

    `run` is a list of (chip_index, packet) tuples in ascending
    bank order; `joined` is the concatenation of their data.
    `bank_size` is the per-bank size (typically $2000)."""
    found = []
    n = len(joined)

    def make_blob(off_in_run, kind, size, note):
        # Translate offset-in-run -> (first_chip, offset_in_first_chip)
        bank_idx = off_in_run // bank_size
        off_in_chip = off_in_run % bank_size
        last_idx = (off_in_run + size - 1) // bank_size
        last_idx = min(last_idx, len(run) - 1)
        spans = [run[i][0] for i in range(bank_idx, last_idx + 1)]
        # Build a human-readable banks-list for the note
        bank_nums = [run[i][1].bank for i in range(bank_idx, last_idx + 1)]
        bank_list_str = ",".join(str(b) for b in bank_nums)
        full_note = (f"{note}; banks {bank_list_str}"
                       if note else f"banks {bank_list_str}")
        return EmbeddedBlob(
            chip_index=run[bank_idx][0],
            bank=run[bank_idx][1].bank,
            offset=off_in_chip,
            kind=kind, size=size, note=full_note,
            spans_banks=spans if len(spans) > 1 else [],
        )

    # Cross-bank Koala (10001 bytes) - same strict tests as
    # per-packet detection (color-RAM upper nibble, bitmap variety
    # cap, screen variety floor) but applied to the joined stream.
    KOALA_TOTAL = 10001
    if n >= KOALA_TOTAL:
        for off in range(0, n - KOALA_TOTAL + 1, 0x100):
            if (off // bank_size) == ((off + KOALA_TOTAL - 1) // bank_size):
                continue
            color_ram = joined[off + 9000:off + 10000]
            upper_zero = sum(1 for b in color_ram if (b >> 4) == 0)
            if upper_zero < 950:
                continue
            distinct_col = len(set(b & 0x0F for b in color_ram))
            if distinct_col < 4:
                continue
            bg = joined[off + 10000]
            if bg > 0x0F:
                continue
            bm_sample = joined[off:off + 1000]
            distinct_bm = len(set(bm_sample))
            if distinct_bm > 250:
                continue
            scr = joined[off + 8000:off + 9000]
            distinct_scr = len(set(scr[:200]))
            if distinct_scr < 8:
                continue
            spans_count = (((off + KOALA_TOTAL - 1) // bank_size)
                            - (off // bank_size) + 1)
            note = (f"crosses {spans_count} banks; color-RAM "
                    f"upper-nibble {upper_zero}/1000 zero, "
                    f"bg=${bg:02X}, bitmap {distinct_bm} distinct, "
                    f"screen {distinct_scr} distinct")
            found.append(make_blob(
                off, "Koala bitmap (multi-bank)",
                KOALA_TOTAL, note))
            break

    # Cross-bank Hires (9000 bytes)
    HIRES_TOTAL = 9000
    if n >= HIRES_TOTAL:
        for off in range(0, n - HIRES_TOTAL + 1, 0x100):
            if (off // bank_size) == ((off + HIRES_TOTAL - 1) // bank_size):
                continue
            bm_sample = joined[off:off + 1000]
            distinct_bm = len(set(bm_sample))
            if distinct_bm > 250 or distinct_bm < 5:
                continue
            scr = joined[off + 8000:off + 9000]
            distinct_scr = len(set(scr[:200]))
            if not (10 <= distinct_scr <= 220):
                continue
            distinct_lo = len(set(b & 0x0F for b in scr[:200]))
            distinct_hi = len(set((b >> 4) & 0x0F for b in scr[:200]))
            if not (3 <= distinct_lo <= 12):
                continue
            if not (3 <= distinct_hi <= 12):
                continue
            spans_count = (((off + HIRES_TOTAL - 1) // bank_size)
                            - (off // bank_size) + 1)
            note = (f"crosses {spans_count} banks; bitmap "
                    f"{distinct_bm} distinct, screen "
                    f"{distinct_scr} distinct, "
                    f"nib-lo {distinct_lo}, nib-hi {distinct_hi}")
            found.append(make_blob(
                off, "Hires bitmap (multi-bank)",
                HIRES_TOTAL, note))
            break

    # Cross-bank charsets at 2KB don't actually cross banks unless
    # they happen to start near a bank end. We don't separately
    # scan those - the per-packet scanner picks them up.

    return found


# =====================================================================
# Charset detector
# =====================================================================
# A C64 charset is 256 glyphs * 8 bytes = exactly 2048 bytes. Each
# glyph is an 8x8 bitmap (1 bit per pixel for hires, or 2 bits per
# pixel for multicolor charsets - the byte layout is the same; the
# screen mode decides interpretation).
#
# Distinctive patterns of a real charset:
#   - Total size is a multiple of $0800 (2 KiB)
#   - Each glyph has a non-trivial pixel count (alphabet glyphs have
#     5-25 set bits typically; pure $00 / $FF glyphs are rare)
#   - Standard C64 charsets begin with the @-character (glyph 0) which
#     in the KERNAL chargen looks like a hollow square with a stem
#   - Many charsets include an obvious "@ A B C ... Z [" run starting
#     at glyph index 1 - a sequence of glyphs whose set-bit counts
#     are similar (alphabet letters all have ~10-15 set bits)
#
# Detection strategy: scan for 2 KiB-aligned regions where bytes have
# a "good distribution" - significant non-trivial variety, not too
# many pure $00 or $FF bytes, and the per-glyph set-bit counts
# cluster in the alphabet range (5-30 bits per 64-bit glyph).

def _is_likely_charset(data: bytes, offset: int) -> tuple:
    """Test whether the 2 KiB block at `offset` looks like a C64
    charset. Returns (is_charset, score, note) where score is 0..100
    and note is a human-readable explanation. Higher scores mean
    higher confidence.

    The detector combines four heuristics, each contributing 0-30
    points to the score (final score is clamped to 100):

      A. Glyph density distribution
         A real charset has 5-40 set bits per 64-bit glyph for the
         vast majority of glyphs (letters / digits / punctuation
         cluster around 10-25 set bits; reverse-video pairs go
         higher; pure $00/$FF glyphs are rare).
      B. Vertical correlation within glyphs
         The 8 rows of a real glyph are not independent random bytes
         - a letter has vertical strokes whose adjacent rows share
         many bits. We measure mean Hamming-distance between adjacent
         rows. Real charsets: ~1.5-3 bits flipped per row pair;
         random data: ~3.7 (uniform random).
      C. Edge bits clearing
         The leftmost and rightmost columns of a real charset glyph
         are usually clear (letters don't fill the cell horizontally),
         though graphics chars can fill these. We measure how many
         glyphs have their leftmost bit (0x80) clear in all 8 rows;
         random data: 50% of glyphs; real charsets: 30-70% depending
         on graphics weight.
      D. Symmetry hint
         Many ASCII letters are vertically symmetric (A, H, I, M, O,
         T, U, V, W, X, Y) or horizontally (B, C, D, E, H, I, K,
         O, X). We count glyphs with vertical symmetry (row N == row
         7-N for N=0..3); a real charset has 10-30 such glyphs out of
         256, random data has ~3-5.
    """
    if len(data) - offset < 2048:
        return False, 0, "too small"
    block = data[offset:offset + 2048]

    # Quick reject: too many all-zero or all-FF glyphs (real charsets
    # have at most ~10 trivial glyphs out of 256)
    zero_glyphs = 0
    ff_glyphs = 0
    real_glyphs = []
    bit_counts = []
    for g in range(256):
        glyph = block[g * 8:g * 8 + 8]
        if glyph == b'\x00' * 8:
            zero_glyphs += 1
            continue
        if glyph == b'\xFF' * 8:
            ff_glyphs += 1
            continue
        real_glyphs.append(glyph)
        bit_counts.append(sum(bin(b).count("1") for b in glyph))

    n_real = len(real_glyphs)
    if n_real < 200:
        return False, 0, (f"only {n_real} real glyphs "
                           f"({zero_glyphs} zero, {ff_glyphs} FF)")

    # === A. Density distribution (0-30 points) ===
    in_range = sum(1 for c in bit_counts if 5 <= c <= 40)
    in_range_ratio = in_range / max(1, n_real)
    sorted_bc = sorted(bit_counts)
    median = sorted_bc[len(sorted_bc) // 2]
    score_a = 0
    if in_range_ratio >= 0.6 and 5 <= median <= 40:
        # Charsets with reverse-video halves can have median up
        # to ~35; the 5-40 window is generous on purpose.
        score_a = int(30 * min(1.0, in_range_ratio))
    elif in_range_ratio >= 0.4:
        score_a = int(15 * in_range_ratio)

    # === B. Vertical correlation (0-30 points) ===
    # Hamming distance between adjacent rows of each glyph.
    # Real charset: low (rows are correlated). Random: ~3.7-4.0
    # Code data:    ~3.5
    total_hamming = 0
    total_pairs = 0
    for glyph in real_glyphs:
        for r in range(7):
            x = glyph[r] ^ glyph[r + 1]
            total_hamming += bin(x).count("1")
            total_pairs += 1
    avg_hamming = total_hamming / max(1, total_pairs)
    # Real charsets: 1.0-3.0 bits per row pair
    # Random data:   ~3.7
    # 3.2 cutoff: linear ramp to 30 at avg=0.5
    score_b = 0
    if avg_hamming <= 3.2:
        score_b = int(max(0, min(30, 30 * (3.2 - avg_hamming) / 2.7)))

    # === C. Edge-bit clearing (0-20 points) ===
    edge_clear_count = 0
    for glyph in real_glyphs:
        left_clear  = all((b & 0x80) == 0 for b in glyph)
        right_clear = all((b & 0x01) == 0 for b in glyph)
        if left_clear or right_clear:
            edge_clear_count += 1
    edge_ratio = edge_clear_count / max(1, n_real)
    # Real charset: 0.30-0.70 of glyphs have at least one edge clear
    # (graphics chars and reverse video reduce the ratio)
    # Random:      ~0.0078 (vanishingly small)
    score_c = 0
    if edge_ratio >= 0.10:
        # Linear: 0.10 -> 5pts, 0.50 -> 20pts
        score_c = int(min(20, 5 + 37.5 * (edge_ratio - 0.10)))

    # === D. Symmetry (0-20 points) ===
    # Vertical-axis symmetry within each row (mirror-image), counted
    # over all rows of all glyphs. A vertically-symmetric letter
    # like 'A' or 'M' has every row palindromic in its bit pattern.
    sym_glyphs = 0
    for glyph in real_glyphs:
        # Count rows that are bit-palindromes (b == reverse-bits(b))
        sym_rows = 0
        for b in glyph:
            rev = 0
            for i in range(8):
                if (b >> i) & 1:
                    rev |= (1 << (7 - i))
            if b == rev:
                sym_rows += 1
        # A symmetric glyph has many palindromic rows
        if sym_rows >= 6:
            sym_glyphs += 1
    # Real charsets: typically 8-30 glyphs out of 256 (alphabet
    # symmetric letters + many graphics chars). Random data: <2.
    score_d = 0
    if sym_glyphs >= 5:
        score_d = int(min(20, sym_glyphs * 0.8))

    score = min(100, score_a + score_b + score_c + score_d)

    note = (f"density={score_a}/30 (med {median} bits, {in_range_ratio:.0%} in range), "
            f"row-corr={score_b}/30 (avg ham {avg_hamming:.2f}), "
            f"edge={score_c}/20 ({edge_ratio:.0%}), "
            f"sym={score_d}/20 ({sym_glyphs} glyphs)")
    return score >= 50, score, note


def render_charset(data: bytes, offset: int = 0,
                     fg: int = 1, bg: int = 6,
                     cols: int = 32):
    """Render a 2 KiB charset to a sheet QImage. Each glyph is
    drawn as a hires 8x8 cell with `fg`/`bg` palette indices.
    `cols` controls how many glyphs per row (32 cols * 8 rows
    fits all 256 glyphs in 256x64 pixels)."""
    if len(data) - offset < 2048:
        return None
    rows = (256 + cols - 1) // cols
    W = cols * 8
    H = rows * 8
    pixels = [bg] * (W * H)
    for g in range(256):
        cx = g % cols
        cy = g // cols
        for row in range(8):
            b = data[offset + g * 8 + row]
            for pxi in range(8):
                bit = (b >> (7 - pxi)) & 0x01
                px = cx * 8 + pxi
                py = cy * 8 + row
                pixels[py * W + px] = fg if bit else bg
    return _make_qimage_from_pixels(pixels, W, H)


def get_blob_bytes(crt: CrtFile, blob: EmbeddedBlob,
                     edits: dict = None) -> bytes:
    """Return the bytes of an embedded blob, splicing across
    multiple banks for multi-bank blobs. `edits` is an optional
    {chip_index: edited_data} dict so callers can honour pending
    Hex-tab edits."""
    edits = edits or {}

    def chip_data(ci):
        if ci in edits:
            return edits[ci]
        return crt.chips[ci].data

    if not blob.spans_banks:
        # Single-bank blob - just slice
        d = chip_data(blob.chip_index)
        end = min(blob.offset + blob.size, len(d))
        return d[blob.offset:end]

    # Multi-bank blob: walk spans_banks list, slicing the first
    # bank from the offset onwards, the middle banks fully, and
    # the last bank up to whatever bytes remain.
    out = bytearray()
    remaining = blob.size
    first = True
    for ci in blob.spans_banks:
        d = chip_data(ci)
        if first:
            avail = len(d) - blob.offset
            take = min(avail, remaining)
            out.extend(d[blob.offset:blob.offset + take])
            first = False
        else:
            take = min(len(d), remaining)
            out.extend(d[:take])
        remaining -= take
        if remaining <= 0:
            break
    return bytes(out)


def extract_embedded_blob(crt: CrtFile, blob: EmbeddedBlob,
                            out_path) -> Path:
    """Write the bytes of an embedded blob to disk.

    Uses get_blob_bytes() under the hood, so multi-bank blobs are
    correctly stitched across packet boundaries before writing.
    For PRG payloads we don't currently re-add the 2-byte load
    header - the user can use the Hex tab's Edit field to prepend
    one if needed."""
    payload = get_blob_bytes(crt, blob)
    target = Path(out_path)
    target.write_bytes(payload)
    return target


# =====================================================================
# C64 image renderers
# =====================================================================
# Renderers that turn raw byte buffers into Qt QImages so the user
# can visually inspect Koala bitmaps, hires bitmaps and sprite blocks
# pulled out of cart banks. All renderers return None on error rather
# than raising - the caller decides how to surface failures.
#
# Reference for the layouts:
#   Koala (multicolor): https://www.c64-wiki.com/wiki/Koala
#   Hires:              https://www.c64-wiki.com/wiki/High_resolution_graphics
#   Sprites:            https://www.c64-wiki.com/wiki/Sprite

# Standard C64 16-color VIC-II palette in approximate sRGB.
# Values from VICE's Pepto palette, rounded to 8-bit.
_C64_PALETTE_RGB = [
    (0x00, 0x00, 0x00),   # 0  black
    (0xFF, 0xFF, 0xFF),   # 1  white
    (0x88, 0x39, 0x32),   # 2  red
    (0x67, 0xB6, 0xBD),   # 3  cyan
    (0x8B, 0x3F, 0x96),   # 4  purple
    (0x55, 0xA0, 0x49),   # 5  green
    (0x40, 0x31, 0x8D),   # 6  blue
    (0xBF, 0xCE, 0x72),   # 7  yellow
    (0x8B, 0x54, 0x29),   # 8  orange
    (0x57, 0x42, 0x00),   # 9  brown
    (0xB8, 0x69, 0x62),   # 10 pink
    (0x50, 0x50, 0x50),   # 11 dark grey
    (0x78, 0x78, 0x78),   # 12 mid grey
    (0x94, 0xE0, 0x89),   # 13 light green
    (0x78, 0x69, 0xC4),   # 14 light blue
    (0x9F, 0x9F, 0x9F),   # 15 light grey
]


def _make_qimage_from_pixels(pixels, width, height):
    """Build a Qt QImage from a flat pixel-index list. `pixels` is
    a sequence of palette indices (0-15) of length width*height.
    Each pixel is rendered as ARGB32. Returns the QImage, or None
    on import failure (Qt not available)."""
    try:
        from PyQt6.QtGui import QImage
    except ImportError:
        return None
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    for y in range(height):
        row = y * width
        for x in range(width):
            idx = pixels[row + x] & 0xF
            r, g, b = _C64_PALETTE_RGB[idx]
            img.setPixel(x, y, (0xFF << 24) | (r << 16) | (g << 8) | b)
    return img


def render_koala_bitmap(data: bytes, offset: int = 0,
                          has_load_addr: bool = True):
    """Render a multicolor Koala bitmap to a 160x200 QImage. The
    image is doubled in width (to 320x200) so multicolor pixels
    look correct with square pixels.

    Layout (Koala-paint format):
      [optional 2-byte load address $6000]
      $0000-$1F3F  bitmap data       (8000 bytes)
      $1F40-$2327  screen RAM        (1000 bytes, two colors per cell)
      $2328-$270F  color RAM         (1000 bytes, 1 nibble per cell)
      $2710        background colour (1 byte)
                                    -----
                                    9002 bytes

    With the optional 2-byte load address $6000 prepended (typical
    for Koala-paint .kla files saved as PRG), total = 10003 bytes.
    Newer "Koala painter Magazine" save format is 10001 bytes."""
    if has_load_addr:
        offset += 2
    if len(data) - offset < 9001:
        return None
    bitmap = data[offset:offset + 8000]
    screen = data[offset + 8000:offset + 9000]
    color  = data[offset + 9000:offset + 10000]
    if len(color) < 1000:
        # Some saved formats truncate before color RAM; fall back to
        # a black color RAM so we at least get a partial render.
        color = bytes(1000)
    # Background colour: byte after color RAM (or default to black)
    bg = data[offset + 10000] & 0x0F if len(data) - offset >= 10001 else 0

    pixels = [0] * (160 * 200)
    # Multicolor: every 8x8 cell uses 4 colors, encoded by 2-bit
    # pairs. The pair-to-colour mapping per cell is:
    #   00 -> background (global)
    #   01 -> upper nibble of screen byte
    #   10 -> lower nibble of screen byte
    #   11 -> color RAM nibble
    # Pixel doubling on x: 4 wide-pixels per byte, but we render
    # at 160 native (each pair is one wide pixel).
    for cy in range(25):
        for cx in range(40):
            cell_idx = cy * 40 + cx
            scr_byte = screen[cell_idx]
            c01 = (scr_byte >> 4) & 0x0F
            c10 = scr_byte & 0x0F
            c11 = color[cell_idx] & 0x0F
            # Each cell occupies 8 bytes vertically in the bitmap,
            # in linear order: cell_offset = 320*cy + 8*cx
            base = (cy * 40 + cx) * 8
            for row in range(8):
                b = bitmap[base + row]
                # 4 pixel pairs in this byte, MSB first
                py = cy * 8 + row
                for pxi in range(4):
                    bits = (b >> (6 - 2 * pxi)) & 0x03
                    if   bits == 0: c = bg
                    elif bits == 1: c = c01
                    elif bits == 2: c = c10
                    else:           c = c11
                    pixels[py * 160 + cx * 4 + pxi] = c
    img = _make_qimage_from_pixels(pixels, 160, 200)
    if img is None:
        return None
    # Double horizontally to give correct 4:3 aspect on screen
    return img.scaled(320, 200)


def render_hires_bitmap(data: bytes, offset: int = 0,
                          has_load_addr: bool = True):
    """Render a high-res (320x200) bitmap to a QImage.

    Layout: 8000 bytes bitmap + 1000 bytes screen RAM.
    Each 8x8 cell uses 2 colors:
      bit set   -> upper nibble of screen-RAM byte for this cell
      bit clear -> lower nibble of same screen byte

    Returns None if the buffer is too short."""
    if has_load_addr:
        offset += 2
    if len(data) - offset < 9000:
        return None
    bitmap = data[offset:offset + 8000]
    screen = data[offset + 8000:offset + 9000]

    pixels = [0] * (320 * 200)
    for cy in range(25):
        for cx in range(40):
            cell_idx = cy * 40 + cx
            scr_byte = screen[cell_idx]
            c1 = (scr_byte >> 4) & 0x0F   # foreground
            c0 = scr_byte & 0x0F          # background
            base = (cy * 40 + cx) * 8
            for row in range(8):
                b = bitmap[base + row]
                py = cy * 8 + row
                for pxi in range(8):
                    bit = (b >> (7 - pxi)) & 0x01
                    pixels[py * 320 + cx * 8 + pxi] = c1 if bit else c0
    return _make_qimage_from_pixels(pixels, 320, 200)


def render_sprite(data: bytes, offset: int = 0,
                    multicolor: bool = False,
                    fg_color: int = 1, bg_color: int = 0,
                    mc1_color: int = 5, mc2_color: int = 7):
    """Render a single C64 sprite to a 24x21 QImage (multicolor:
    12x21 native, doubled to 24x21).

    A sprite is 63 bytes: 21 rows of 3 bytes each. The 64th byte
    of an aligned sprite block is conventionally unused (sometimes
    color in $D800 derivatives).

    For multicolor sprites:
      00 -> bg_color
      01 -> mc1_color (shared MC1)
      10 -> fg_color  (per-sprite color)
      11 -> mc2_color (shared MC2)
    """
    if len(data) - offset < 63:
        return None
    pixels = [0] * (24 * 21)
    for row in range(21):
        for byte_x in range(3):
            b = data[offset + row * 3 + byte_x]
            if multicolor:
                # 4 wide-pixels per byte
                for pxi in range(4):
                    bits = (b >> (6 - 2 * pxi)) & 0x03
                    if   bits == 0: c = bg_color
                    elif bits == 1: c = mc1_color
                    elif bits == 2: c = fg_color
                    else:           c = mc2_color
                    # Wide pixel = 2 native pixels
                    px = byte_x * 8 + pxi * 2
                    pixels[row * 24 + px]     = c
                    pixels[row * 24 + px + 1] = c
            else:
                for pxi in range(8):
                    bit = (b >> (7 - pxi)) & 0x01
                    pixels[row * 24 + byte_x * 8 + pxi] = (
                        fg_color if bit else bg_color)
    return _make_qimage_from_pixels(pixels, 24, 21)


def render_sprite_sheet(data: bytes, offset: int = 0,
                          count: int = 16,
                          multicolor: bool = False,
                          cols: int = 8,
                          fg_color: int = 1, bg_color: int = 11,
                          mc1_color: int = 5, mc2_color: int = 7,
                          gap: int = 2):
    """Render up to `count` sprites into a single grid sheet QImage.
    Each sprite is 63 bytes; we step through `data` 64 bytes at a
    time (the standard alignment used in cart sprite blocks).
    Returns None if no sprites fit or Qt isn't available."""
    try:
        from PyQt6.QtGui import QImage, QPainter
        from PyQt6.QtCore import Qt as _Qt
    except ImportError:
        return None
    if len(data) - offset < 63:
        return None
    # Cap count to what actually fits
    avail = (len(data) - offset) // 64
    n = min(count, avail)
    if n <= 0:
        return None
    rows = (n + cols - 1) // cols
    sw, sh = 24, 21
    W = cols * (sw + gap) + gap
    H = rows * (sh + gap) + gap
    sheet = QImage(W, H, QImage.Format.Format_ARGB32)
    # Fill background with the bg color
    r, g, b = _C64_PALETTE_RGB[bg_color]
    sheet.fill((0xFF << 24) | (r << 16) | (g << 8) | b)
    p = QPainter(sheet)
    try:
        for i in range(n):
            spr = render_sprite(
                data, offset + i * 64,
                multicolor=multicolor,
                fg_color=fg_color, bg_color=bg_color,
                mc1_color=mc1_color, mc2_color=mc2_color)
            if spr is None:
                continue
            cx = i % cols
            cy = i // cols
            p.drawImage(gap + cx * (sw + gap),
                          gap + cy * (sh + gap), spr)
    finally:
        p.end()
    return sheet


# =====================================================================
# GMod2 EEPROM helpers
# =====================================================================
# A GMod2 cartridge (hardware ID 60) has a 2 KiB serial EEPROM
# (M93C86) for save-game data alongside its 512 KiB flash ROM.
# When the EEPROM contents are bundled inside the CRT, they appear
# as their own CHIP packet - typically chip_type=2 (Flash) with
# rom_size=$800. By VICE convention this packet is the last one
# in the file and has a different bank/load_addr than the flash
# bank packets. We identify it heuristically: any 2048-byte CHIP
# packet in a GMod2 CRT that isn't part of the 64-bank flash
# layout (banks 0-63 at $8000) is taken to be the EEPROM.

def find_gmod2_eeprom_packet(crt: CrtFile) -> Optional[CrtChipPacket]:
    """Return the CHIP packet that holds the GMod2 EEPROM data,
    or None if no EEPROM packet is present in the CRT.

    Heuristics in order:
      1. Hardware ID must be 60 (GMod2). Other carts can't have
         a GMod2 EEPROM.
      2. Look for a packet with size exactly 0x800 (2 KiB) - this
         is the M93C86 chip's full capacity.
      3. Among multiple matches (rare), prefer the LAST one in
         the file - VICE's cartconv writes the EEPROM after all
         flash banks.
    """
    if crt.hardware_id != 60:
        return None
    # Walk packets in reverse so we naturally pick the last 2 KiB
    # one if there are multiple candidates.
    for p in reversed(crt.chips):
        if p.rom_size == 0x800:
            return p
    return None


def extract_gmod2_eeprom(crt: CrtFile, out_path) -> Path:
    """Write the GMod2 EEPROM contents to a .bin file.

    Raises ValueError if the cart isn't a GMod2 or doesn't contain
    an EEPROM packet. The output file is exactly 2048 bytes.
    """
    if crt.hardware_id != 60:
        raise ValueError(
            f"This isn't a GMod2 cartridge (hardware ID {crt.hardware_id}, "
            "expected 60). Only GMod2 carts have an M93C86 EEPROM.")
    p = find_gmod2_eeprom_packet(crt)
    if p is None:
        raise ValueError(
            "This GMod2 CRT has no embedded EEPROM data. The 1541U2 / "
            "Ultimate-64 cartridge documentation notes that EEPROM "
            "contents are only saved with the CRT if they were already "
            "part of the original CRT.")
    target = Path(out_path)
    target.write_bytes(p.data)
    return target


def replace_gmod2_eeprom(crt_path,
                           eeprom_path,
                           out_path=None) -> Path:
    """Read the CRT at `crt_path`, replace its GMod2 EEPROM contents
    with the bytes from `eeprom_path`, and write the result back.

    If `out_path` is None, overwrites the source file in place.
    The replacement EEPROM file must be exactly 2048 bytes long.

    If the source CRT doesn't currently have an EEPROM packet
    (some GMod2 dumps don't include it), a new one is appended at
    the end of the file with chip_type=2 (Flash) and bank=0,
    load_addr=$DE00, rom_size=$800 - matching VICE's cartconv
    convention.

    Returns the path of the written file.
    """
    src = Path(crt_path)
    eeprom_data = Path(eeprom_path).read_bytes()
    if len(eeprom_data) != 0x800:
        raise ValueError(
            f"EEPROM image must be exactly 2048 bytes "
            f"(M93C86 capacity); got {len(eeprom_data)}.")

    # Parse the CRT to verify it's a GMod2 and find the EEPROM
    # packet position.
    crt = parse_crt(src)
    if crt.hardware_id != 60:
        raise ValueError(
            f"This isn't a GMod2 cartridge (hardware ID {crt.hardware_id}, "
            "expected 60). Cannot replace EEPROM.")

    raw = bytearray(src.read_bytes())
    eeprom_pkt = find_gmod2_eeprom_packet(crt)

    if eeprom_pkt is not None:
        # Existing EEPROM packet - overwrite its data section. The
        # 16-byte CHIP header sits at file_offset, then 2048 bytes
        # of data.
        data_start = eeprom_pkt.file_offset + 16
        data_end = data_start + 0x800
        if data_end > len(raw):
            raise ValueError(
                "CRT file appears truncated - EEPROM packet data "
                "extends past end of file.")
        raw[data_start:data_end] = eeprom_data
    else:
        # No EEPROM packet present: append one. Use VICE's convention
        # for GMod2 EEPROM: chip_type=2 (Flash), bank=0, load_addr
        # set to 0 (it's not memory-mapped), size 0x800.
        # NOTE: cartconv actually writes load_addr = some fixed marker
        # value; we use 0 which is the common case in dumps observed
        # in the wild. Tools should treat any 2KB packet on a GMod2
        # cart as the EEPROM regardless of the load_addr field.
        chip_hdr = bytearray(16)
        chip_hdr[0:4] = b"CHIP"
        chip_hdr[4:8] = struct.pack(">I", 16 + 0x800)  # total length
        chip_hdr[8:10] = struct.pack(">H", 2)           # chip_type=Flash
        chip_hdr[10:12] = struct.pack(">H", 0)          # bank=0
        chip_hdr[12:14] = struct.pack(">H", 0)          # load_addr=0
        chip_hdr[14:16] = struct.pack(">H", 0x800)     # rom_size=2KB
        raw.extend(chip_hdr)
        raw.extend(eeprom_data)

    target = Path(out_path) if out_path is not None else src
    target.write_bytes(bytes(raw))
    return target


# =====================================================================
# CRT Toolkit Dialog (Qt)
# =====================================================================
# A non-modal viewer that opens a .crt file and lets the user inspect:
#   - Header info + hardware-type description (Info tab)
#   - List of CHIP packets with bank / address / size (Banks tab)
#   - Hex dump of the selected bank (Hex tab)
#   - 6502 disassembly of the selected bank (Disasm tab)
#   - Extract one bank or all banks to .bin / .prg files


class CrtToolkitDialog:
    """Wrapper around the QDialog so we can defer Qt imports until
    the dialog is actually opened. Lets non-GUI scripts import
    crt_toolkit purely for parsing without dragging PyQt6 in."""

    def __new__(cls, crt_or_path, parent=None):
        from PyQt6.QtWidgets import QDialog
        # Resolve path-or-CrtFile
        if isinstance(crt_or_path, CrtFile):
            crt = crt_or_path
        else:
            crt = parse_crt(crt_or_path)
        dlg = _CrtToolkitDialog(crt, parent=parent)
        return dlg


def _make_crt_dialog():
    """Lazy class-builder so the file imports cleanly without PyQt."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
        QPushButton, QListWidget, QListWidgetItem, QPlainTextEdit,
        QTabWidget, QFileDialog, QMessageBox, QWidget,
        QCheckBox, QGroupBox,
    )
    from .palette import (
        C, button_qss, SCROLLBAR_QSS, get_topaz_font,
    )

    class _CrtToolkitDialog(QDialog):
        def __init__(self, crt: CrtFile, parent=None):
            super().__init__(parent)
            self.crt = crt
            self.setWindowTitle(
                f"CRT Toolkit: {crt.path.name}  "
                f"[{crt.hardware_short} - {crt.machine}]")
            self.resize(1100, 720)
            self.setModal(False)
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

            # Main layout: top toolbar, then a horizontal splitter
            # with the bank list on the left and a tabbed view on
            # the right.
            v = QVBoxLayout(self)
            v.setContentsMargins(4, 4, 4, 4)
            v.setSpacing(3)

            # ---- Toolbar ----
            bar = QHBoxLayout()
            bar.setSpacing(4)
            bar.setContentsMargins(0, 0, 0, 0)

            b_ext_one = QPushButton("Extract Bank (.bin)")
            b_ext_one.setStyleSheet(button_qss("blue"))
            b_ext_one.setToolTip(
                "Save the currently-selected ROM bank (CHIP packet)\n"
                "as a raw .bin file (no PRG load-address header).\n\n"
                "If this cart has an EasyFS or Yeti file system,\n"
                "Claude will offer to extract proper FILES instead.")
            b_ext_one.clicked.connect(self._extract_selected)
            bar.addWidget(b_ext_one)

            b_ext_one_prg = QPushButton("Extract Bank (.prg)")
            b_ext_one_prg.setStyleSheet(button_qss("blue"))
            b_ext_one_prg.setToolTip(
                "Save the currently-selected ROM bank (CHIP packet)\n"
                "as a .prg file with a 2-byte load-address header.\n\n"
                "If this cart has an EasyFS or Yeti file system,\n"
                "you'll be offered to extract proper FILES instead.")
            b_ext_one_prg.clicked.connect(
                lambda: self._extract_selected(as_prg=True))
            bar.addWidget(b_ext_one_prg)

            b_ext_all = QPushButton("Extract All...")
            b_ext_all.setStyleSheet(button_qss("orange"))
            b_ext_all.setToolTip(
                "Extract every ROM bank as raw chip dumps.\n\n"
                "If this cart has an EasyFS or Yeti file system,\n"
                "you'll be offered to extract proper FILES instead.")
            b_ext_all.clicked.connect(self._extract_all)
            bar.addWidget(b_ext_all)

            # GMod2-specific EEPROM buttons. Only visible if this is
            # actually a GMod2 cart (hardware ID 60). They sit between
            # the Extract All button and the illegal-opcode checkbox.
            self._b_eeprom_export = QPushButton("Export GMod2 EEPROM...")
            self._b_eeprom_export.setStyleSheet(button_qss("green"))
            self._b_eeprom_export.setToolTip(
                "Save the GMod2 cartridge's 2 KiB M93C86 EEPROM "
                "contents to a .bin file (e.g. for backup of high\n"
                "scores / save games before re-flashing the cart).")
            self._b_eeprom_export.clicked.connect(self._export_gmod2_eeprom)
            bar.addWidget(self._b_eeprom_export)

            self._b_eeprom_import = QPushButton("Replace GMod2 EEPROM...")
            self._b_eeprom_import.setStyleSheet(button_qss("yellow"))
            self._b_eeprom_import.setToolTip(
                "Replace the GMod2 EEPROM contents inside the CRT "
                "with bytes from a .bin file. Must be exactly 2048\n"
                "bytes. The CRT is rewritten in place by default; "
                "you can choose 'Save As' to write to a new file.")
            self._b_eeprom_import.clicked.connect(self._import_gmod2_eeprom)
            bar.addWidget(self._b_eeprom_import)

            # Hide unless this cart is a GMod2.
            is_gmod2 = (crt.hardware_id == 60)
            self._b_eeprom_export.setVisible(is_gmod2)
            self._b_eeprom_import.setVisible(is_gmod2)

            bar.addSpacing(20)

            # View-size selector. Lets the user view a bank as the
            # raw $2000 (8K) it is in the file, or combined with its
            # ROML/ROMH sibling at bank N to form a contiguous 16K
            # block ($2000 + $2000). The latter is useful for
            # disassembling code that crosses the $A000 boundary on
            # EasyFlash carts (very common - JMP $Axxx targets in
            # ROML code resolve to actual code in ROMH).
            from PyQt6.QtWidgets import QLabel as _QLabel, QComboBox
            bar.addWidget(_QLabel("View:"))
            self._cb_view_size = QComboBox()
            self._cb_view_size.addItem("Single packet (as in CRT)", "single")
            self._cb_view_size.addItem(
                "ROML+ROMH paired ($8000-$BFFF, 16K)", "pair_16k")
            self._cb_view_size.addItem(
                "Force 16K ($4000) - first 16K of bank", "force_16k")
            self._cb_view_size.setToolTip(
                "Single packet: shows just the selected CHIP packet's data.\n"
                "ROML+ROMH paired: combines the $8000 packet with the\n"
                "matching $A000 packet of the same bank into one 16K view\n"
                "(the way the C64 sees it in 16K cartridge mode). For\n"
                "EasyFlash, Final Cartridge III, etc.\n"
                "Force 16K: takes whatever bytes follow the packet to fill\n"
                "16K - useful for raw .bin dumps that aren't bank-split.")
            self._cb_view_size.currentIndexChanged.connect(
                self._on_view_size_changed)
            bar.addWidget(self._cb_view_size)

            bar.addSpacing(10)

            self._cb_illegal = QCheckBox("Show illegal opcodes")
            self._cb_illegal.setToolTip(
                "Decode undocumented 6502 opcodes (LAX, SAX, SLO, ...)\n"
                "as proper mnemonics instead of .byte data lines.\n"
                "Affects the Disasm tab; toggle re-renders.")
            self._cb_illegal.toggled.connect(self._refresh_disasm)
            bar.addWidget(self._cb_illegal)

            bar.addStretch(1)

            b_close = QPushButton("Close (Esc)")
            b_close.setStyleSheet(button_qss("red"))
            b_close.clicked.connect(self.accept)
            bar.addWidget(b_close)
            v.addLayout(bar)

            # ---- Splitter ----
            split = QSplitter(Qt.Orientation.Horizontal)
            self._split = split  # for _save_state / _restore_state

            # Left: bank list
            left_box = QWidget()
            lv = QVBoxLayout(left_box)
            lv.setContentsMargins(0, 0, 0, 0)
            lv.setSpacing(2)
            lv.addWidget(QLabel(f"<b>{len(crt.chips)} CHIP packets</b>"))
            self._list = QListWidget()
            self._list.setStyleSheet(SCROLLBAR_QSS)
            mono = get_topaz_font(11)
            self._list.setFont(mono)
            self._list.currentRowChanged.connect(self._on_bank_changed)
            lv.addWidget(self._list, 1)
            split.addWidget(left_box)
            # Defer the actual list population to _rebuild_bank_list()
            # so it can re-run when the view-size combo changes.
            # We can't call it here yet because _cb_view_size is built
            # later in __init__; we run it at the end of __init__.

            # Right: tabs (Info / Hex / Disasm)
            self._tabs = QTabWidget()

            # Info tab — full text summary
            self._info_pane = QPlainTextEdit()
            self._info_pane.setReadOnly(True)
            self._info_pane.setFont(mono)
            self._info_pane.setStyleSheet(SCROLLBAR_QSS)
            self._info_pane.setPlainText(format_crt_summary(crt))
            self._tabs.addTab(self._info_pane, "Info")

            # ---- Hex tab: compound widget with toolbar ----
            # Search field, Find Next button, Replace field+button,
            # Edit-mode toggle + Save button, then the hex pane.
            from PyQt6.QtWidgets import QLineEdit, QToolButton
            hex_outer = QWidget()
            hex_layout = QVBoxLayout(hex_outer)
            hex_layout.setContentsMargins(0, 0, 0, 0)
            hex_layout.setSpacing(2)

            hex_bar = QHBoxLayout()
            hex_bar.setSpacing(3)
            hex_bar.setContentsMargins(2, 2, 2, 0)

            hex_bar.addWidget(_QLabel("Find:"))
            self._hex_find = QLineEdit()
            self._hex_find.setMaximumWidth(180)
            self._hex_find.setPlaceholderText("hex bytes or 'text'")
            self._hex_find.setToolTip(
                "Search the bank's data. Two input modes:\n"
                "  - Hex bytes: 'A9 01 8D 20 D0' (spaces optional)\n"
                "  - ASCII text in single quotes: 'CBM80'\n"
                "Mix is allowed: 'A9 01 \"CBM\" FF'\n"
                "Press Enter or click 'Next' to search; F3 also works.")
            self._hex_find.returnPressed.connect(self._hex_find_next)
            hex_bar.addWidget(self._hex_find)

            b_hex_next = QPushButton("Next (F3)")
            b_hex_next.setStyleSheet(button_qss("blue"))
            b_hex_next.clicked.connect(self._hex_find_next)
            hex_bar.addWidget(b_hex_next)

            b_hex_prev = QPushButton("Prev")
            b_hex_prev.setStyleSheet(button_qss("blue"))
            b_hex_prev.clicked.connect(self._hex_find_prev)
            hex_bar.addWidget(b_hex_prev)

            hex_bar.addSpacing(8)
            hex_bar.addWidget(_QLabel("Replace:"))
            self._hex_replace = QLineEdit()
            self._hex_replace.setMaximumWidth(180)
            self._hex_replace.setPlaceholderText("hex bytes or 'text'")
            self._hex_replace.setToolTip(
                "Replacement bytes. Same syntax as Find. Must produce\n"
                "the same number of bytes as the Find pattern - the\n"
                "edit is in-place to keep all addresses valid.")
            hex_bar.addWidget(self._hex_replace)

            b_hex_rep = QPushButton("Replace")
            b_hex_rep.setStyleSheet(button_qss("yellow"))
            b_hex_rep.clicked.connect(self._hex_replace_one)
            hex_bar.addWidget(b_hex_rep)

            b_hex_rep_all = QPushButton("All")
            b_hex_rep_all.setStyleSheet(button_qss("yellow"))
            b_hex_rep_all.clicked.connect(self._hex_replace_all)
            hex_bar.addWidget(b_hex_rep_all)

            hex_bar.addStretch(1)

            # Safe byte-level edit. Instead of letting the user type
            # directly into the hex pane (which trivially breaks the
            # layout) we provide a small two-field editor:
            #   Edit @ $XXXX = [bytes]  [Apply]
            # Address can be either a C64 absolute address ($8042)
            # or a packet-relative offset (42, $042, 0x42). Bytes
            # use the same hex/quoted-string syntax as Find/Replace.
            hex_bar.addWidget(_QLabel("Edit @"))
            self._hex_edit_addr = QLineEdit()
            self._hex_edit_addr.setMaximumWidth(80)
            self._hex_edit_addr.setPlaceholderText("$8042")
            self._hex_edit_addr.setToolTip(
                "Address or offset to patch. Either:\n"
                "  - C64 absolute address inside this bank: $8042\n"
                "  - packet offset (decimal or hex): 42, $42, 0x42\n"
                "Outside the bank or out of range = error.")
            hex_bar.addWidget(self._hex_edit_addr)

            hex_bar.addWidget(_QLabel("="))
            self._hex_edit_bytes = QLineEdit()
            self._hex_edit_bytes.setMaximumWidth(160)
            self._hex_edit_bytes.setPlaceholderText("FF EA EA")
            self._hex_edit_bytes.setToolTip(
                "Replacement bytes. Same syntax as Find/Replace:\n"
                "  - Hex: 'FF EA EA' (spaces optional)\n"
                "  - Quoted ASCII: 'CBM80'\n"
                "Length is whatever you give; cannot extend past\n"
                "the bank's end.")
            self._hex_edit_bytes.returnPressed.connect(self._hex_apply_edit)
            hex_bar.addWidget(self._hex_edit_bytes)

            b_hex_apply = QPushButton("Apply")
            b_hex_apply.setStyleSheet(button_qss("yellow"))
            b_hex_apply.setToolTip(
                "Apply the patch in-place to the current bank's edit\n"
                "buffer. Click 'Save to CRT' afterwards to write back\n"
                "to disk.")
            b_hex_apply.clicked.connect(self._hex_apply_edit)
            hex_bar.addWidget(b_hex_apply)

            b_hex_revert = QPushButton("Revert")
            b_hex_revert.setStyleSheet(button_qss("blue"))
            b_hex_revert.setToolTip(
                "Discard all unsaved edits in the current bank and\n"
                "restore the original bytes from the CRT file.")
            b_hex_revert.clicked.connect(self._hex_revert)
            hex_bar.addWidget(b_hex_revert)

            self._b_hex_save = QPushButton("Save to CRT")
            self._b_hex_save.setStyleSheet(button_qss("red"))
            self._b_hex_save.setEnabled(False)
            self._b_hex_save.setToolTip(
                "Write all pending bank edits back to the .crt file\n"
                "on disk. A one-shot .bak backup is created the first\n"
                "time you save in this session.")
            self._b_hex_save.clicked.connect(self._hex_save_to_crt)
            hex_bar.addWidget(self._b_hex_save)

            hex_layout.addLayout(hex_bar)

            self._hex_pane = QPlainTextEdit()
            self._hex_pane.setReadOnly(True)
            self._hex_pane.setFont(mono)
            self._hex_pane.setStyleSheet(SCROLLBAR_QSS)
            hex_layout.addWidget(self._hex_pane, 1)

            self._tabs.addTab(hex_outer, "Hex")

            # ---- Disasm tab: compound widget with toolbar ----
            asm_outer = QWidget()
            asm_layout = QVBoxLayout(asm_outer)
            asm_layout.setContentsMargins(0, 0, 0, 0)
            asm_layout.setSpacing(2)

            asm_bar = QHBoxLayout()
            asm_bar.setSpacing(3)
            asm_bar.setContentsMargins(2, 2, 2, 0)

            self._cb_labels = QCheckBox("Show as labels")
            self._cb_labels.setToolTip(
                "Replace in-bank branch / jump / JSR target addresses\n"
                "with synthetic labels (Lxxxx). Out-of-bank targets like\n"
                "$FFD2 (CHROUT) keep their $xxxx form so it's clear they\n"
                "exit the cartridge.\n\n"
                "Useful for copying disassembly into an assembler -\n"
                "the result is closer to a re-assemblable source.")
            self._cb_labels.toggled.connect(self._refresh_disasm)
            asm_bar.addWidget(self._cb_labels)

            asm_bar.addSpacing(10)
            b_asm_copy = QPushButton("Copy Disasm")
            b_asm_copy.setStyleSheet(button_qss("green"))
            b_asm_copy.setToolTip(
                "Copy the entire disassembly to the clipboard.\n"
                "Honours the current label / illegal-opcode settings.")
            b_asm_copy.clicked.connect(self._copy_disasm)
            asm_bar.addWidget(b_asm_copy)

            b_asm_save = QPushButton("Save .asm...")
            b_asm_save.setStyleSheet(button_qss("blue"))
            b_asm_save.setToolTip(
                "Save the entire disassembly to a .asm file. Honours\n"
                "the current label / illegal-opcode settings.")
            b_asm_save.clicked.connect(self._save_disasm)
            asm_bar.addWidget(b_asm_save)

            asm_bar.addStretch(1)

            asm_layout.addLayout(asm_bar)

            self._asm_pane = QPlainTextEdit()
            self._asm_pane.setReadOnly(True)
            self._asm_pane.setFont(mono)
            self._asm_pane.setStyleSheet(SCROLLBAR_QSS)
            asm_layout.addWidget(self._asm_pane, 1)

            self._tabs.addTab(asm_outer, "Disasm")

            # Bytes tab — raw byte view (large hex grid only, no ASCII)
            self._bytes_pane = QPlainTextEdit()
            self._bytes_pane.setReadOnly(True)
            self._bytes_pane.setFont(mono)
            self._bytes_pane.setStyleSheet(SCROLLBAR_QSS)
            self._tabs.addTab(self._bytes_pane, "Bytes")

            # ---- Compare tab: bank A vs bank B side-by-side diff ----
            cmp_outer = QWidget()
            cmp_layout = QVBoxLayout(cmp_outer)
            cmp_layout.setContentsMargins(0, 0, 0, 0)
            cmp_layout.setSpacing(2)

            cmp_bar = QHBoxLayout()
            cmp_bar.setSpacing(3)
            cmp_bar.setContentsMargins(2, 2, 2, 0)
            cmp_bar.addWidget(_QLabel("Bank A:"))
            self._cmp_a = QComboBox()
            self._cmp_a.setToolTip("Pick the first bank/packet to compare.")
            cmp_bar.addWidget(self._cmp_a)
            cmp_bar.addSpacing(6)
            cmp_bar.addWidget(_QLabel("vs Bank B:"))
            self._cmp_b = QComboBox()
            self._cmp_b.setToolTip("Pick the second bank/packet to compare.")
            cmp_bar.addWidget(self._cmp_b)
            cmp_bar.addSpacing(8)
            b_cmp_run = QPushButton("Compare")
            b_cmp_run.setStyleSheet(button_qss("orange"))
            b_cmp_run.clicked.connect(self._run_compare)
            cmp_bar.addWidget(b_cmp_run)
            cmp_bar.addStretch(1)
            cmp_layout.addLayout(cmp_bar)

            # Populate bank dropdowns from the current chip list
            for i, p in enumerate(crt.chips):
                lbl = (f"#{i:>3} bank{p.bank:>3} "
                        f"{p.chip_type_label:<5} "
                        f"{p.addr_range_str}")
                self._cmp_a.addItem(lbl, i)
                self._cmp_b.addItem(lbl, i)
            if len(crt.chips) >= 2:
                self._cmp_b.setCurrentIndex(1)

            self._cmp_pane = QPlainTextEdit()
            self._cmp_pane.setReadOnly(True)
            self._cmp_pane.setFont(mono)
            self._cmp_pane.setStyleSheet(SCROLLBAR_QSS)
            self._cmp_pane.setPlainText(
                "Pick two banks above and click 'Compare' to see a\n"
                "side-by-side hex diff. Differing bytes are wrapped\n"
                "in [brackets] so they stand out in plain text.")
            cmp_layout.addWidget(self._cmp_pane, 1)
            self._tabs.addTab(cmp_outer, "Compare")

            # ---- PETSCII tab: render bytes as C64 PETSCII glyphs ----
            # Mirrors the look of CBM toolboxes that show "memory as
            # PETSCII" - useful for spotting embedded scene marker
            # strings, EAPI signatures, FILE-ID-DIZ data, etc., that
            # don't show up nicely in a plain hex+ASCII dump because
            # the C64 character set uses different glyphs for $00-$1F
            # and $80-$FF than ASCII does. Inspired by the "Toolbox
            # For Cartridges" Hexpad layout (charset Lo/Hi toggle,
            # font-size +/-).
            from PyQt6.QtWidgets import QScrollArea
            pet_outer = QWidget()
            pet_layout = QVBoxLayout(pet_outer)
            pet_layout.setContentsMargins(0, 0, 0, 0)
            pet_layout.setSpacing(2)

            pet_bar = QHBoxLayout()
            pet_bar.setSpacing(3)
            pet_bar.setContentsMargins(2, 2, 2, 0)

            pet_bar.addWidget(_QLabel("Charset:"))
            self._cb_pet_charset = QComboBox()
            self._cb_pet_charset.addItem("Lo (mixed case)", "lower")
            self._cb_pet_charset.addItem("Hi (UPPER + graphics)", "upper")
            self._cb_pet_charset.setToolTip(
                "C64 has two character sets:\n"
                "  Lo: lowercase a-z + uppercase A-Z (via SHIFT)\n"
                "      + a smaller graphics range. Cracker scene\n"
                "      output / mixed-case filenames look correct here.\n"
                "  Hi: full UPPER A-Z + extensive PETSCII graphics.\n"
                "      Boot default - what KERNAL prints on power-on.\n"
                "Press CMDR+Shift on a real C64 to swap charsets.")
            self._cb_pet_charset.currentIndexChanged.connect(
                self._refresh_petscii)
            pet_bar.addWidget(self._cb_pet_charset)

            pet_bar.addSpacing(8)
            pet_bar.addWidget(_QLabel("Cell:"))
            b_pet_smaller = QPushButton("-")
            b_pet_smaller.setMaximumWidth(28)
            b_pet_smaller.setStyleSheet(button_qss("blue"))
            b_pet_smaller.clicked.connect(
                lambda: self._adjust_petscii_size(-2))
            pet_bar.addWidget(b_pet_smaller)
            self._lbl_pet_size = _QLabel("16 px")
            pet_bar.addWidget(self._lbl_pet_size)
            b_pet_bigger = QPushButton("+")
            b_pet_bigger.setMaximumWidth(28)
            b_pet_bigger.setStyleSheet(button_qss("blue"))
            b_pet_bigger.clicked.connect(
                lambda: self._adjust_petscii_size(+2))
            pet_bar.addWidget(b_pet_bigger)

            pet_bar.addSpacing(8)
            pet_bar.addWidget(_QLabel("Width:"))
            self._cb_pet_cols = QComboBox()
            for c in (16, 32, 40, 48, 64, 80):
                self._cb_pet_cols.addItem(f"{c} cols", c)
            self._cb_pet_cols.setCurrentIndex(4)   # 64 cols
            self._cb_pet_cols.setToolTip(
                "How many bytes per row in the PETSCII grid.\n"
                "16/32/64 are powers of two for clean address alignment.\n"
                "40 matches a real C64 screen line; 80 matches a VDC.")
            self._cb_pet_cols.currentIndexChanged.connect(
                self._refresh_petscii)
            pet_bar.addWidget(self._cb_pet_cols)

            pet_bar.addStretch(1)

            b_pet_save = QPushButton("Save PNG...")
            b_pet_save.setStyleSheet(button_qss("green"))
            b_pet_save.setToolTip(
                "Save the current PETSCII rendering as a PNG image.")
            b_pet_save.clicked.connect(self._save_petscii_png)
            pet_bar.addWidget(b_pet_save)

            pet_layout.addLayout(pet_bar)

            # The pixmap goes inside a scroll area because the bank
            # rendered at 16 px/cell is ~1024x2048 for a typical 8 KB
            # bank with 64 cols.
            self._pet_scroll = QScrollArea()
            self._pet_scroll.setStyleSheet(SCROLLBAR_QSS)
            self._pet_scroll.setWidgetResizable(False)
            from PyQt6.QtWidgets import QLabel as _QL2
            self._pet_label = _QL2()
            self._pet_label.setStyleSheet("background-color: #3F3FD7;")
            self._pet_scroll.setWidget(self._pet_label)
            pet_layout.addWidget(self._pet_scroll, 1)
            # Persist cell size in instance so we can adjust it.
            self._pet_cell_size = 16
            self._tabs.addTab(pet_outer, "PETSCII")

            # ---- Files tab: EasyFS / loader-table entries ----
            # If the cart has a detected file system (currently EasyFS;
            # the framework can be extended for other directory
            # formats later), this tab lists every entry with bank,
            # offset, size and lets the user extract files singly or
            # in bulk. Disabled (greyed out content) when the cart has
            # no detected file system.
            from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem
            from PyQt6.QtWidgets import QHeaderView, QAbstractItemView
            files_outer = QWidget()
            files_layout = QVBoxLayout(files_outer)
            files_layout.setContentsMargins(0, 0, 0, 0)
            files_layout.setSpacing(2)

            files_bar = QHBoxLayout()
            files_bar.setSpacing(3)
            files_bar.setContentsMargins(2, 2, 2, 0)

            self._files_status = _QLabel("")
            files_bar.addWidget(self._files_status)
            files_bar.addStretch(1)

            b_files_jump = QPushButton("Jump to bank")
            b_files_jump.setStyleSheet(button_qss("blue"))
            b_files_jump.setToolTip(
                "Select the file's starting bank in the left bank list\n"
                "(handy for inspecting the file in the Hex / Disasm /\n"
                "PETSCII tabs).")
            b_files_jump.clicked.connect(self._files_jump_to_bank)
            files_bar.addWidget(b_files_jump)

            b_files_one = QPushButton("Extract Selected")
            b_files_one.setStyleSheet(button_qss("blue"))
            b_files_one.setToolTip(
                "Save the currently-selected file(s) from the table\n"
                "to disk as raw binaries. Names are sanitised for\n"
                "the host filesystem; PETSCII names with spaces, +,\n"
                "(), [] etc. are kept; only Windows-forbidden chars\n"
                "(< > : \" / \\ | ? *) get replaced with _.")
            b_files_one.clicked.connect(self._files_extract_selected)
            files_bar.addWidget(b_files_one)

            b_files_run = QPushButton("Run in Emulator")
            b_files_run.setStyleSheet(button_qss("green"))
            b_files_run.setToolTip(
                "Extract the selected file to a temporary folder and\n"
                "launch the configured C64 emulator on it.\n\n"
                "The emulator path is shared with the disasm tool's\n"
                "Run-in-emulator config (config.json key 'c64_emulator').\n"
                "If not configured yet, you'll be prompted for the path.")
            b_files_run.clicked.connect(self._files_run_in_emulator)
            files_bar.addWidget(b_files_run)

            b_files_all = QPushButton("Extract All")
            b_files_all.setStyleSheet(button_qss("orange"))
            b_files_all.setToolTip(
                "Extract every file in the directory table to a chosen\n"
                "directory. Each file gets its name from the EasyFS entry.")
            b_files_all.clicked.connect(self._files_extract_all)
            files_bar.addWidget(b_files_all)

            b_files_save_list = QPushButton("Save list (.txt)")
            b_files_save_list.setStyleSheet(button_qss("green"))
            b_files_save_list.setToolTip(
                "Save the file table as a plain-text listing (one\n"
                "row per entry, columns: name, type, bank, offset,\n"
                "size).")
            b_files_save_list.clicked.connect(self._files_save_list)
            files_bar.addWidget(b_files_save_list)

            files_layout.addLayout(files_bar)

            self._files_table = QTableWidget()
            self._files_table.setColumnCount(6)
            self._files_table.setHorizontalHeaderLabels(
                ["#", "Name", "Type", "Bank", "Offset", "Size"])
            self._files_table.setAlternatingRowColors(True)
            self._files_table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows)
            self._files_table.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection)
            self._files_table.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers)
            self._files_table.verticalHeader().setVisible(False)
            self._files_table.setStyleSheet(SCROLLBAR_QSS)
            self._files_table.setFont(mono)
            hdr = self._files_table.horizontalHeader()
            # All columns user-resizable. Name column gets a generous
            # default; other columns sized to content. The user can
            # adjust freely and the widths are persisted in
            # crt_toolkit.files_cols.
            hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            # Set sensible default column widths upfront. These are
            # used for carts without EasyFS entries (where _refresh
            # returns early) and as starting widths before the user
            # adjusts. Persisted widths in config override these.
            for col, w in enumerate([40, 200, 80, 50, 70, 90]):
                self._files_table.setColumnWidth(col, w)
            self._files_table.itemDoubleClicked.connect(
                lambda _it: self._files_jump_to_bank())
            files_layout.addWidget(self._files_table, 1)

            self._files_tab = files_outer
            self._tabs.addTab(files_outer, "Files")

            # Populate the files table from EasyFS entries (if any)
            self._refresh_files_table()

            # ---- Blobs tab: embedded data-blob scanner ----
            # Scans every CHIP packet for SID files, Koala bitmaps,
            # PRG payloads, decompressor stubs, CBM80 magic etc.
            # Useful for ripping cart contents that aren't reachable
            # through EasyFS - e.g. SID tunes embedded in a level
            # bank, or compressed payloads that the cart unpacks at
            # runtime.
            blobs_outer = QWidget()
            blobs_layout = QVBoxLayout(blobs_outer)
            blobs_layout.setContentsMargins(0, 0, 0, 0)
            blobs_layout.setSpacing(2)

            blobs_bar = QHBoxLayout()
            blobs_bar.setSpacing(3)
            blobs_bar.setContentsMargins(2, 2, 2, 0)

            self._blobs_status = _QLabel("")
            blobs_bar.addWidget(self._blobs_status)
            blobs_bar.addStretch(1)

            b_blobs_rescan = QPushButton("Rescan")
            b_blobs_rescan.setStyleSheet(button_qss("blue"))
            b_blobs_rescan.setToolTip(
                "Re-run the blob scanner across all banks. Useful\n"
                "after Hex-tab edits so newly-patched byte sequences\n"
                "are picked up.")
            b_blobs_rescan.clicked.connect(self._refresh_blobs_table)
            blobs_bar.addWidget(b_blobs_rescan)

            b_blobs_jump = QPushButton("Jump to bank")
            b_blobs_jump.setStyleSheet(button_qss("blue"))
            b_blobs_jump.setToolTip(
                "Select the blob's bank in the left list and place\n"
                "the Hex-tab cursor on the blob's start offset.")
            b_blobs_jump.clicked.connect(self._blobs_jump_to_bank)
            blobs_bar.addWidget(b_blobs_jump)

            b_blobs_extract = QPushButton("Extract Selected")
            b_blobs_extract.setStyleSheet(button_qss("orange"))
            b_blobs_extract.setToolTip(
                "Save the highlighted blob(s) to disk. The output\n"
                "extension matches the blob kind (.sid, .prg, .koa,\n"
                "or .bin for the rest). For 'Possible ... SFX stub'\n"
                "blobs, the entire bank-tail from the stub onwards\n"
                "is saved so a desktop unpacker can read it.")
            b_blobs_extract.clicked.connect(self._blobs_extract_selected)
            blobs_bar.addWidget(b_blobs_extract)

            b_blobs_save_list = QPushButton("Save list (.txt)")
            b_blobs_save_list.setStyleSheet(button_qss("green"))
            b_blobs_save_list.setToolTip(
                "Save the blob inventory as a plain-text listing.")
            b_blobs_save_list.clicked.connect(self._blobs_save_list)
            blobs_bar.addWidget(b_blobs_save_list)

            blobs_layout.addLayout(blobs_bar)

            self._blobs_table = QTableWidget()
            self._blobs_table.setColumnCount(6)
            self._blobs_table.setHorizontalHeaderLabels(
                ["#", "Kind", "Bank", "Offset", "Size", "Note"])
            self._blobs_table.setAlternatingRowColors(True)
            self._blobs_table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows)
            self._blobs_table.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection)
            self._blobs_table.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers)
            self._blobs_table.verticalHeader().setVisible(False)
            self._blobs_table.setStyleSheet(SCROLLBAR_QSS)
            self._blobs_table.setFont(mono)
            bhdr = self._blobs_table.horizontalHeader()
            # All columns user-resizable; widths persisted in
            # crt_toolkit.blobs_cols.
            bhdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            # Sensible defaults: Note column gets the most space
            # since it carries the longest text.
            for col, w in enumerate([40, 220, 50, 70, 80, 350]):
                self._blobs_table.setColumnWidth(col, w)
            self._blobs_table.itemDoubleClicked.connect(
                lambda _it: self._blobs_jump_to_bank())
            self._blobs_table.itemSelectionChanged.connect(
                self._blobs_update_preview)
            blobs_layout.addWidget(self._blobs_table, 2)

            # Preview pane - renders Koala / Hires / Sprite blobs
            # below the table when one is selected. For non-image
            # blob types it shows a small text label.
            preview_box = QGroupBox("Preview (Koala / Hires / Sprite)")
            preview_layout = QVBoxLayout(preview_box)
            preview_layout.setContentsMargins(6, 6, 6, 6)

            preview_bar = QHBoxLayout()
            preview_bar.setSpacing(4)
            preview_bar.addWidget(_QLabel("Mode:"))
            self._cb_blob_render_mode = QComboBox()
            self._cb_blob_render_mode.addItem("Auto (by detected kind)", "auto")
            self._cb_blob_render_mode.addItem("Force Koala (multicolor)", "koala")
            self._cb_blob_render_mode.addItem("Force Hires", "hires")
            self._cb_blob_render_mode.addItem("Force Sprites (hires)", "sprite")
            self._cb_blob_render_mode.addItem(
                "Force Sprites (multicolor)", "sprite_mc")
            self._cb_blob_render_mode.addItem("Force Charset", "charset")
            self._cb_blob_render_mode.setToolTip(
                "Override the auto-detected kind to force a specific\n"
                "render. Useful when the heuristic guessed wrong, or\n"
                "for arbitrary blocks of bitmap data the scanner did\n"
                "not flag.")
            self._cb_blob_render_mode.currentIndexChanged.connect(
                self._blobs_update_preview)
            preview_bar.addWidget(self._cb_blob_render_mode)

            preview_bar.addSpacing(8)
            preview_bar.addWidget(_QLabel("Zoom:"))
            self._cb_blob_zoom = QComboBox()
            for z in (1, 2, 3, 4):
                self._cb_blob_zoom.addItem(f"{z}x", z)
            self._cb_blob_zoom.setCurrentIndex(1)  # 2x default
            self._cb_blob_zoom.currentIndexChanged.connect(
                self._blobs_update_preview)
            preview_bar.addWidget(self._cb_blob_zoom)

            preview_bar.addSpacing(8)
            preview_bar.addWidget(_QLabel("Has $XXXX header:"))
            self._cb_blob_skip_addr = QComboBox()
            self._cb_blob_skip_addr.addItem("Auto", "auto")
            self._cb_blob_skip_addr.addItem("Yes (skip 2 bytes)", "yes")
            self._cb_blob_skip_addr.addItem("No (raw)", "no")
            self._cb_blob_skip_addr.setToolTip(
                "Whether the bytes start with a 2-byte PRG load\n"
                "address. 'Auto' is a heuristic; if the preview is\n"
                "shifted by 2 pixels, override here.")
            self._cb_blob_skip_addr.currentIndexChanged.connect(
                self._blobs_update_preview)
            preview_bar.addWidget(self._cb_blob_skip_addr)

            preview_bar.addStretch(1)

            b_preview_save = QPushButton("Save preview...")
            b_preview_save.setStyleSheet(button_qss("green"))
            b_preview_save.setToolTip(
                "Save the rendered preview as a PNG file.")
            b_preview_save.clicked.connect(self._blobs_save_preview)
            preview_bar.addWidget(b_preview_save)

            preview_layout.addLayout(preview_bar)

            from PyQt6.QtWidgets import QScrollArea, QLabel as _QL
            self._blob_preview_scroll = QScrollArea()
            self._blob_preview_scroll.setStyleSheet(SCROLLBAR_QSS)
            self._blob_preview_scroll.setWidgetResizable(False)
            self._blob_preview_label = _QL()
            self._blob_preview_label.setStyleSheet(
                "background-color: #1e1e1e; padding: 8px;")
            self._blob_preview_label.setText(
                "Select a blob in the table above to preview it. "
                "Koala / Hires / Sprite blobs will render here.")
            self._blob_preview_label.setMinimumHeight(220)
            self._blob_preview_scroll.setWidget(self._blob_preview_label)
            preview_layout.addWidget(self._blob_preview_scroll, 1)

            blobs_layout.addWidget(preview_box, 1)

            self._tabs.addTab(blobs_outer, "Blobs")
            # Cache scanned blobs and populate
            self._scanned_blobs = []
            self._refresh_blobs_table()

            split.addWidget(self._tabs)
            split.setStretchFactor(0, 0)
            split.setStretchFactor(1, 1)
            split.setSizes([320, 780])

            v.addWidget(split, 1)

            # Build the initial bank list (deferred until now because
            # _cb_view_size has to exist first), then auto-select
            # first row.
            self._rebuild_bank_list()
            if crt.chips and self._list.count() > 0:
                self._list.setCurrentRow(0)

            # Restore persisted geometry / column widths / active
            # tab. We do an immediate restoreGeometry (covers the
            # window position+size before the OS decides where to
            # place us) plus a deferred restore for splitter/columns
            # via a 0-ms singleShot - Qt needs the widget to have
            # been laid out at least once before splitter sizes and
            # column widths "stick", otherwise the values get clipped
            # against the not-yet-final window width.
            from PyQt6.QtCore import QTimer
            self._restore_state_geometry_only()
            QTimer.singleShot(0, self._restore_state_late)

        # ----- bank selection -----
        def _on_view_size_changed(self, _idx):
            """Re-render Hex / Disasm / Bytes when the View combo
            switches between single-packet and paired-16K modes.
            Also rebuilds the left bank list so paired pairs collapse
            into single rows in pair-16K mode."""
            # Remember which packet was selected so we can re-select
            # its row after the rebuild (which may collapse 2 rows
            # into 1).
            cur_row = self._list.currentRow()
            cur_chip_idx = -1
            if 0 <= cur_row < self._list.count():
                cur_chip_idx = self._list.item(cur_row).data(
                    Qt.ItemDataRole.UserRole)
            self._rebuild_bank_list()
            # Try to re-select something matching the previously
            # selected chip index (handles the case where pair_16k
            # collapsed the row, by picking the row that contains
            # that chip index in its data).
            if cur_chip_idx >= 0:
                for i in range(self._list.count()):
                    if self._list.item(i).data(
                            Qt.ItemDataRole.UserRole) == cur_chip_idx:
                        self._list.setCurrentRow(i)
                        break
                else:
                    self._list.setCurrentRow(0)
            else:
                self._list.setCurrentRow(0)

        def _rebuild_bank_list(self):
            """(Re)populate the left bank-list widget. The format
            depends on the current view-size mode:

              - single / force_16k: one row per CHIP packet
              - pair_16k: ROML/ROMH packets of the same bank are
                merged into one row showing $8000-$BFFF, while
                packets without a sibling stay as their own row

            If the cart has detected EasyFS entries, banks that hold
            file data get a `[Fn]` (or `[Fn,m]`) suffix listing the
            indices of files that start in or pass through that bank.

            The UserRole on each list item holds the index of the
            chip that should be selected when this row is clicked
            (for pair_16k rows, the lower of the two chip indices,
            i.e. the ROML packet, since selection drives the
            view-resolution code).
            """
            # Pre-compute file markers per bank: which EasyFS file
            # indices touch which bank? A file at (bank=5, offset=$1000,
            # size=$3000) touches banks 5 and 6 (rolls into bank 6
            # because $1000 + $3000 > $2000).
            bank_files = {}     # bank_no -> list[file_idx]
            if self.crt.has_easyfs:
                for fi, e in enumerate(self.crt.easyfs_entries):
                    if e.size == 0:
                        continue
                    n_banks = max(1,
                        (e.offset + e.size + 0x1FFF) // 0x2000)
                    for bn in range(e.bank, e.bank + n_banks):
                        bank_files.setdefault(bn, []).append(fi)

            def file_marker(bank_no):
                files = bank_files.get(bank_no, [])
                if not files:
                    return ""
                # Compact "F0", "F0,1,5" - cap at 4 indices for room
                if len(files) <= 4:
                    return "  [F" + ",".join(str(f) for f in files) + "]"
                return f"  [F{files[0]}..F{files[-1]} ({len(files)})]"

            self._list.blockSignals(True)
            try:
                self._list.clear()
                mode = self._cb_view_size.currentData() if hasattr(
                    self, '_cb_view_size') else "single"
                if mode == "pair_16k":
                    # Build a set of chip indices already covered by
                    # a paired row so we don't list them twice.
                    covered = set()
                    for i, p in enumerate(self.crt.chips):
                        if i in covered:
                            continue
                        if p.chip_type == 1:
                            lbl = (f"#{i:>3} bank{p.bank:>3} "
                                    f"{p.chip_type_label:<5} "
                                    f"{p.addr_range_str}  "
                                    f"${p.rom_size:04X}"
                                    + file_marker(p.bank))
                            from PyQt6.QtWidgets import QListWidgetItem as _LW
                            it = _LW(lbl)
                            it.setData(Qt.ItemDataRole.UserRole, i)
                            self._list.addItem(it)
                            continue
                        # Find sibling
                        sibling_idx = -1
                        if p.load_addr == 0x8000:
                            for j, q in enumerate(self.crt.chips):
                                if (j != i and j not in covered
                                        and q.bank == p.bank
                                        and q.load_addr == 0xA000
                                        and q.chip_type != 1):
                                    sibling_idx = j
                                    break
                        elif p.load_addr in (0xA000, 0xE000):
                            for j, q in enumerate(self.crt.chips):
                                if (j != i and j not in covered
                                        and q.bank == p.bank
                                        and q.load_addr == 0x8000
                                        and q.chip_type != 1):
                                    sibling_idx = j
                                    break
                        if sibling_idx >= 0:
                            sib = self.crt.chips[sibling_idx]
                            primary = min(i, sibling_idx)
                            lbl = (f"#{i:>2}+#{sibling_idx:<2} "
                                    f"bank{p.bank:>3} "
                                    f"{p.chip_type_label:<5} "
                                    f"$8000-$BFFF  $4000"
                                    + file_marker(p.bank))
                            from PyQt6.QtWidgets import QListWidgetItem as _LW
                            it = _LW(lbl)
                            it.setData(Qt.ItemDataRole.UserRole, primary)
                            self._list.addItem(it)
                            covered.add(i)
                            covered.add(sibling_idx)
                        else:
                            lbl = (f"#{i:>3} bank{p.bank:>3} "
                                    f"{p.chip_type_label:<5} "
                                    f"{p.addr_range_str}  "
                                    f"${p.rom_size:04X}"
                                    + file_marker(p.bank))
                            from PyQt6.QtWidgets import QListWidgetItem as _LW
                            it = _LW(lbl)
                            it.setData(Qt.ItemDataRole.UserRole, i)
                            self._list.addItem(it)
                            covered.add(i)
                else:
                    # single / force_16k: plain one-row-per-packet
                    for i, p in enumerate(self.crt.chips):
                        lbl = (f"#{i:>3} bank{p.bank:>3} "
                                f"{p.chip_type_label:<5} "
                                f"{p.addr_range_str}  "
                                f"${p.rom_size:04X}"
                                + file_marker(p.bank))
                        from PyQt6.QtWidgets import QListWidgetItem as _LW
                        it = _LW(lbl)
                        it.setData(Qt.ItemDataRole.UserRole, i)
                        self._list.addItem(it)
            finally:
                self._list.blockSignals(False)

        def _get_view_packet(self, packet):
            """Return a (possibly synthetic) CrtChipPacket reflecting
            the current View-size selection.

            For "single" mode: returns the input packet unchanged.
            For "pair_16k": if the selected packet is at $8000 and
              there's a sibling at $A000 with the same bank, returns
              a synthetic packet starting at $8000 with both banks'
              data concatenated. If selected is at $A000, finds the
              $8000 sibling instead. If no sibling found, returns
              the original packet (degraded to single-packet view).
            For "force_16k": pads or extends the packet's data to a
              full $4000 bytes (16 KiB), filling with $FF if short.

            The returned packet keeps the original `bank`, `chip_type`,
            and `file_offset` of the LO-ROM (or original) so labels
            still make sense.
            """
            mode = self._cb_view_size.currentData() if hasattr(
                self, '_cb_view_size') else "single"
            if mode == "single" or packet is None:
                return packet
            if packet.chip_type == 1:
                # RAM bank - nothing to combine
                return packet

            if mode == "pair_16k":
                # Find a sibling packet at the matching address with
                # the same bank.
                lo = hi = None
                if packet.load_addr == 0x8000:
                    lo = packet
                    # Find ROMH sibling
                    for q in self.crt.chips:
                        if (q.bank == packet.bank
                                and q.load_addr == 0xA000
                                and q.chip_type != 1):
                            hi = q
                            break
                elif packet.load_addr in (0xA000, 0xE000):
                    hi = packet
                    for q in self.crt.chips:
                        if (q.bank == packet.bank
                                and q.load_addr == 0x8000
                                and q.chip_type != 1):
                            lo = q
                            break
                if lo is not None and hi is not None:
                    # Synthesise a 16K packet at $8000 with lo+hi data
                    return CrtChipPacket(
                        file_offset=lo.file_offset,
                        packet_length=lo.packet_length + hi.packet_length,
                        chip_type=lo.chip_type,
                        bank=lo.bank,
                        load_addr=0x8000,
                        rom_size=len(lo.data) + len(hi.data),
                        data=lo.data + hi.data,
                    )
                # No sibling found - fall through to single-packet
                return packet

            if mode == "force_16k":
                want = 0x4000
                if len(packet.data) >= want:
                    return packet
                padded = packet.data + b'\xFF' * (want - len(packet.data))
                return CrtChipPacket(
                    file_offset=packet.file_offset,
                    packet_length=packet.packet_length,
                    chip_type=packet.chip_type,
                    bank=packet.bank,
                    load_addr=packet.load_addr,
                    rom_size=want,
                    data=padded,
                )

            return packet

        def _on_bank_changed(self, row: int):
            # row is the list-widget row, NOT necessarily a chip index
            # (in pair_16k mode multiple chips collapse into one row).
            # Resolve via the helper.
            idx = self._current_chip_index()
            if idx < 0:
                return
            raw_pkt = self.crt.chips[idx]

            # If this bank has pending edits, show them - construct a
            # synthetic packet so view-size combinations still work.
            edits = getattr(self, '_hex_edits', {})
            if idx in edits:
                edited = CrtChipPacket(
                    file_offset=raw_pkt.file_offset,
                    packet_length=raw_pkt.packet_length,
                    chip_type=raw_pkt.chip_type,
                    bank=raw_pkt.bank,
                    load_addr=raw_pkt.load_addr,
                    rom_size=len(edits[idx]),
                    data=edits[idx],
                )
                # Use the edited bytes as the source for the view-size
                # logic. We swap raw_pkt locally so _get_view_packet
                # finds it in the chips list (it iterates self.crt.chips
                # for siblings, so paired views still pick up the
                # *original* sibling - that's intentional, edits to
                # one bank shouldn't silently change the view of
                # another).
                raw_pkt = edited
            p = self._get_view_packet(raw_pkt)
            # Reset search position when switching banks
            self._hex_search_pos = 0

            # Show a one-line header above the hex/bytes panes that
            # makes the current view-mode clear.
            mode = self._cb_view_size.currentData() if hasattr(
                self, '_cb_view_size') else "single"
            edited_marker = (" [EDITED, unsaved]"
                              if idx in edits else "")
            if (mode == "pair_16k" and p is not raw_pkt
                    and p.rom_size > raw_pkt.rom_size):
                hdr = (f"[Paired 16K view — bank {p.bank}, "
                        f"{p.addr_range_str}, {p.rom_size:,} bytes "
                        f"= ROML+ROMH combined{edited_marker}]\n")
            elif mode == "force_16k" and p is not raw_pkt:
                hdr = (f"[Forced 16K view — bank {p.bank}, "
                        f"{p.addr_range_str}, {p.rom_size:,} bytes "
                        f"= packet padded with $FF{edited_marker}]\n")
            elif edited_marker:
                hdr = f"[Bank {raw_pkt.bank}{edited_marker}]\n"
            else:
                hdr = ""

            # Hex
            if p.chip_type == 1:
                self._hex_pane.setPlainText(
                    "RAM bank (no ROM data in CRT)\n"
                    f"Size: ${p.rom_size:04X} bytes\n"
                    f"Address: {p.addr_range_str}")
            else:
                self._hex_pane.setPlainText(hdr + hex_dump_bank(p))

            # Disasm
            self._refresh_disasm()

            # Bytes — pure 32-byte-wide hex grid, no ASCII gutter,
            # no per-line address. Useful for diff'ing or copy-pasting
            # into an editor. For large banks we paginate softly.
            if p.chip_type == 1 or not p.data:
                self._bytes_pane.setPlainText("(no data)")
            else:
                lines = [hdr] if hdr else []
                for i in range(0, len(p.data), 32):
                    chunk = p.data[i:i + 32]
                    lines.append(' '.join(f"{b:02X}" for b in chunk))
                self._bytes_pane.setPlainText('\n'.join(lines))

            # PETSCII view
            if hasattr(self, '_pet_label'):
                self._refresh_petscii()

        def _refresh_disasm(self):
            idx = self._current_chip_index()
            if idx < 0:
                return
            raw_pkt = self.crt.chips[idx]
            # Honor pending edits like _on_bank_changed does
            edits = getattr(self, '_hex_edits', {})
            if idx in edits:
                raw_pkt = CrtChipPacket(
                    file_offset=raw_pkt.file_offset,
                    packet_length=raw_pkt.packet_length,
                    chip_type=raw_pkt.chip_type,
                    bank=raw_pkt.bank,
                    load_addr=raw_pkt.load_addr,
                    rom_size=len(edits[idx]),
                    data=edits[idx],
                )
            p = self._get_view_packet(raw_pkt)
            if p.chip_type == 1 or not p.data:
                self._asm_pane.setPlainText(
                    "RAM bank or empty packet — nothing to disassemble.")
                return
            mode = self._cb_view_size.currentData() if hasattr(
                self, '_cb_view_size') else "single"
            if (mode == "pair_16k" and p is not raw_pkt
                    and p.rom_size > raw_pkt.rom_size):
                hdr = (f"; Paired 16K view — bank {p.bank}, "
                        f"{p.addr_range_str}, ROML+ROMH combined\n"
                        f"; Code crossing $A000 boundary will resolve\n"
                        f"; correctly here (unlike single-packet view).\n\n")
            elif mode == "force_16k" and p is not raw_pkt:
                hdr = (f"; Forced 16K view — packet padded to $4000\n\n")
            else:
                hdr = ""
            label_mode = (hasattr(self, '_cb_labels')
                           and self._cb_labels.isChecked())
            self._asm_pane.setPlainText(
                hdr + disasm_bank(p,
                                    show_illegal=self._cb_illegal.isChecked(),
                                    label_mode=label_mode))

        # ----- extract -----
        def _extract_selected(self, as_prg: bool = False):
            # If a file system is detected, the top "Extract Selected"
            # button is ambiguous: did the user mean the bank in the
            # left list, or a file from the file system? Offer to
            # switch to the Files tab and use the per-file extraction
            # there instead. The reasoning is that the top buttons
            # operate on raw CHIP packets (bank dumps), which is a
            # power-user feature; most people who see "30 entries in
            # EasyFS directory" want the files, not the banks.
            if self.crt.has_easyfs or self.crt.has_yeti_filetable:
                fs_label = ("EasyFS" if self.crt.has_easyfs
                              else "Yeti loader")
                fs_count = (len(self.crt.easyfs_entries)
                              if self.crt.has_easyfs
                              else len(self.crt.yeti_entries))
                btn = QMessageBox.question(self,
                    "Extract: bank or file?",
                    f"This cartridge contains {fs_label} with "
                    f"{fs_count} files.\n\n"
                    "<b>Yes</b> = Extract the selected file(s) from "
                    "the directory (uses the proper file names and "
                    "respects each file's exact size).\n\n"
                    "<b>No</b> = Extract the selected ROM bank as a "
                    f"raw 8 KiB .{('prg' if as_prg else 'bin')} chip "
                    "dump.\n\n"
                    "Cancel aborts.",
                    QMessageBox.StandardButton.Yes
                      | QMessageBox.StandardButton.No
                      | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes)
                if btn == QMessageBox.StandardButton.Cancel:
                    return
                if btn == QMessageBox.StandardButton.Yes:
                    # Switch to Files tab and trigger the file
                    # extraction; if the user hasn't selected
                    # anything there, prompt them.
                    self._tabs.setCurrentWidget(self._files_tab)
                    sel = self._files_table.selectionModel().selectedRows()
                    if not sel:
                        # Pre-select all so the call doesn't no-op
                        self._files_table.selectAll()
                    self._files_extract_selected()
                    return
                # else: fall through to bank extraction
            idx = self._current_chip_index()
            if idx < 0:
                QMessageBox.information(self, "Extract",
                    "Select a bank from the list first.")
                return
            p = self.crt.chips[idx]
            ext = "prg" if as_prg else "bin"
            default_name = (f"{self.crt.path.stem}_chip{idx:03d}_"
                             f"b{p.bank:02d}_{p.load_addr:04x}.{ext}")
            target, _ = QFileDialog.getSaveFileName(
                self, f"Save bank #{idx} as .{ext}",
                str(self.crt.path.parent / default_name),
                f"*.{ext}")
            if not target:
                return
            try:
                if as_prg:
                    extract_bank_to_prg(p, target)
                else:
                    extract_bank_to_bin(p, target)
                QMessageBox.information(self, "Extract",
                    f"Wrote {len(p.data):,} bytes to:\n{target}")
            except Exception as e:
                QMessageBox.warning(self, "Extract", str(e))

        def _extract_all(self):
            # Same idea as _extract_selected: when a file system is
            # present, the user almost always wants files, not raw
            # bank dumps. Offer the choice.
            if self.crt.has_easyfs or self.crt.has_yeti_filetable:
                fs_label = ("EasyFS" if self.crt.has_easyfs
                              else "Yeti loader")
                fs_count = (len(self.crt.easyfs_entries)
                              if self.crt.has_easyfs
                              else len(self.crt.yeti_entries))
                btn = QMessageBox.question(self,
                    "Extract All: banks or files?",
                    f"This cartridge contains {fs_label} with "
                    f"{fs_count} files.\n\n"
                    "<b>Yes</b> = Extract all <b>files</b> from the "
                    "directory (uses proper names + sizes).\n\n"
                    "<b>No</b> = Extract all <b>ROM banks</b> as raw "
                    f"chip dumps ({len(self.crt.chips)} files).\n\n"
                    "Cancel aborts.",
                    QMessageBox.StandardButton.Yes
                      | QMessageBox.StandardButton.No
                      | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes)
                if btn == QMessageBox.StandardButton.Cancel:
                    return
                if btn == QMessageBox.StandardButton.Yes:
                    self._tabs.setCurrentWidget(self._files_tab)
                    self._files_table.selectAll()
                    self._files_extract_selected()
                    return
            target_dir = QFileDialog.getExistingDirectory(
                self, "Extract all banks to directory",
                str(self.crt.path.parent))
            if not target_dir:
                return
            try:
                paths = extract_all_banks(self.crt, target_dir)
                QMessageBox.information(self, "Extract All",
                    f"Wrote {len(paths)} banks to:\n{target_dir}")
            except Exception as e:
                QMessageBox.warning(self, "Extract All", str(e))

        # ----- GMod2 EEPROM -----
        def _export_gmod2_eeprom(self):
            """Save the GMod2 EEPROM packet to a .bin file."""
            pkt = find_gmod2_eeprom_packet(self.crt)
            if pkt is None:
                QMessageBox.information(self, "GMod2 EEPROM",
                    "This GMod2 CRT has no embedded EEPROM data.\n\n"
                    "EEPROMs are only saved into the CRT if they were "
                    "part of the original file. The cartridge save data "
                    "is initialised with $FF on first run.")
                return
            default = (f"{self.crt.path.stem}_eeprom.bin")
            target, _ = QFileDialog.getSaveFileName(
                self, "Save GMod2 EEPROM as .bin",
                str(self.crt.path.parent / default),
                "*.bin")
            if not target:
                return
            try:
                p = extract_gmod2_eeprom(self.crt, target)
                QMessageBox.information(self, "GMod2 EEPROM",
                    f"Wrote {len(pkt.data):,} bytes to:\n{p}")
            except Exception as e:
                QMessageBox.warning(self, "GMod2 EEPROM", str(e))

        def _import_gmod2_eeprom(self):
            """Replace the GMod2 EEPROM contents in the CRT with
            bytes from a chosen .bin file. The user picks where to
            write the result - either overwriting the source CRT
            or saving to a new path."""
            src, _ = QFileDialog.getOpenFileName(
                self, "Pick GMod2 EEPROM .bin (must be 2048 bytes)",
                str(self.crt.path.parent),
                "*.bin")
            if not src:
                return
            # Decide where to write the result. Default suggestion:
            # same path as the CRT (overwrite). The user can still
            # pick a different path in the save dialog.
            default = self.crt.path.name
            target, _ = QFileDialog.getSaveFileName(
                self, "Save modified CRT as",
                str(self.crt.path.parent / default),
                "*.crt")
            if not target:
                return
            try:
                replace_gmod2_eeprom(self.crt.path, src, out_path=target)
                # If they overwrote the source, re-parse and refresh
                # so the dialog shows the new EEPROM bytes.
                if Path(target) == self.crt.path:
                    self.crt = parse_crt(self.crt.path)
                    self._info_pane.setPlainText(format_crt_summary(self.crt))
                    # Rebuild list to show the (possibly new) EEPROM
                    # packet.
                    self._rebuild_bank_list()
                    if self.crt.chips and self._list.count() > 0:
                        self._list.setCurrentRow(0)
                QMessageBox.information(self, "GMod2 EEPROM",
                    f"EEPROM replaced. New CRT written to:\n{target}")
            except Exception as e:
                QMessageBox.warning(self, "GMod2 EEPROM", str(e))

        # ----- Hex search/replace/edit -----
        def _parse_hex_pattern(self, text):
            """Parse a Find/Replace expression into bytes.

            Accepts mixed input:
              - Hex pairs: "A9 01 8D 20 D0" or "A9018D20D0"
              - Quoted text: 'CBM80' (single quotes)
              - Mix: 'A9 01 "CBM" FF' (single OR double quotes for text)

            Returns (bytes_pattern, error_message). On error, the
            bytes are b'' and the message describes the problem.
            """
            text = (text or "").strip()
            if not text:
                return b'', "empty pattern"
            out = bytearray()
            i = 0
            while i < len(text):
                c = text[i]
                if c.isspace():
                    i += 1
                    continue
                if c in ("'", '"'):
                    # Find matching closing quote
                    end = text.find(c, i + 1)
                    if end < 0:
                        return b'', f"unterminated string starting at col {i}"
                    out.extend(text[i + 1:end].encode('ascii', 'replace'))
                    i = end + 1
                    continue
                # Try a hex pair
                if i + 1 < len(text):
                    pair = text[i:i + 2]
                    try:
                        out.append(int(pair, 16))
                        i += 2
                        continue
                    except ValueError:
                        pass
                # Single hex digit (allow odd-length hex)
                try:
                    out.append(int(c, 16))
                    i += 1
                except ValueError:
                    return b'', f"unexpected char {c!r} at col {i}"
            return bytes(out), ""

        def _current_chip_index(self):
            """Return the actual chips[] index for the currently
            selected list row, accounting for pair_16k mode where
            list rows and chip indices may not match 1:1."""
            row = self._list.currentRow()
            if row < 0 or row >= self._list.count():
                return -1
            data = self._list.item(row).data(Qt.ItemDataRole.UserRole)
            if isinstance(data, int) and 0 <= data < len(self.crt.chips):
                return data
            return -1

        def _current_packet_data(self):
            """Return the current bank's bytes (the editable copy if
            edit mode has been used, else the packet's data)."""
            idx = self._current_chip_index()
            if idx < 0:
                return None, None
            p = self.crt.chips[idx]
            # If we have a per-bank pending edit buffer, use that.
            if not hasattr(self, '_hex_edits'):
                self._hex_edits = {}
            if idx in self._hex_edits:
                return p, self._hex_edits[idx]
            return p, p.data

        def _set_packet_data(self, row, new_data):
            """Mark the given bank as edited and stash a copy of its
            new bytes. The CHIP packet's `.data` is left alone until
            the user clicks Save."""
            if not hasattr(self, '_hex_edits'):
                self._hex_edits = {}
            self._hex_edits[row] = bytes(new_data)
            # Indicate unsaved-changes state on the Save button
            self._b_hex_save.setEnabled(True)
            self._b_hex_save.setText("Save to CRT *")

        def _hex_find_next(self):
            self._hex_find_step(forward=True)

        def _hex_find_prev(self):
            self._hex_find_step(forward=False)

        def _hex_find_step(self, forward=True):
            pat, err = self._parse_hex_pattern(self._hex_find.text())
            if err:
                QMessageBox.warning(self, "Find", f"Bad pattern: {err}")
                return
            if not pat:
                return
            p, data = self._current_packet_data()
            if data is None or not data:
                return
            # Track the current cursor offset so successive Next /
            # Prev clicks advance through hits.
            cursor_attr = "_hex_search_pos"
            cur = getattr(self, cursor_attr, 0)
            if forward:
                idx = data.find(pat, cur + 1)
                if idx < 0:
                    # Wrap around
                    idx = data.find(pat)
                    if idx < 0:
                        QMessageBox.information(self, "Find",
                            f"No match for {len(pat)}-byte pattern.")
                        return
                    self._hex_status(f"Found at offset ${idx:04X} (wrapped)")
                else:
                    self._hex_status(f"Found at offset ${idx:04X}")
            else:
                idx = data.rfind(pat, 0, cur)
                if idx < 0:
                    idx = data.rfind(pat)
                    if idx < 0:
                        QMessageBox.information(self, "Find",
                            "No match.")
                        return
                    self._hex_status(f"Found at offset ${idx:04X} (wrapped)")
                else:
                    self._hex_status(f"Found at offset ${idx:04X}")
            setattr(self, cursor_attr, idx)
            # Scroll the hex pane to the line containing the match.
            # Each hex line is 16 bytes; each line of hex_dump_bank
            # output is one line in the pane.
            line_no = idx // 16
            cursor = self._hex_pane.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            for _ in range(line_no):
                cursor.movePosition(cursor.MoveOperation.Down)
            self._hex_pane.setTextCursor(cursor)
            self._hex_pane.ensureCursorVisible()
            # Highlight the matched line by selecting it
            cursor.select(cursor.SelectionType.LineUnderCursor)
            self._hex_pane.setTextCursor(cursor)

        def _hex_status(self, msg):
            """Show a transient status message at the bottom of the
            hex pane via window title."""
            base = self.windowTitle()
            if " | " in base:
                base = base.split(" | ", 1)[0]
            self.setWindowTitle(f"{base} | {msg}")

        def _hex_replace_one(self):
            pat, err = self._parse_hex_pattern(self._hex_find.text())
            if err or not pat:
                QMessageBox.warning(self, "Replace",
                    f"Bad Find pattern: {err or 'empty'}")
                return
            rep, err = self._parse_hex_pattern(self._hex_replace.text())
            if err:
                QMessageBox.warning(self, "Replace",
                    f"Bad Replace pattern: {err}")
                return
            if len(pat) != len(rep):
                QMessageBox.warning(self, "Replace",
                    f"Replace pattern is {len(rep)} byte(s) but Find is "
                    f"{len(pat)} - they must match (in-place edit).")
                return
            row = self._current_chip_index()
            if row < 0:
                return
            p, data = self._current_packet_data()
            if not data:
                return
            cur = getattr(self, "_hex_search_pos", 0)
            idx = data.find(pat, cur)
            if idx < 0:
                idx = data.find(pat)
                if idx < 0:
                    QMessageBox.information(self, "Replace",
                        "No match to replace.")
                    return
            new_data = data[:idx] + rep + data[idx + len(rep):]
            self._set_packet_data(row, new_data)
            self._hex_search_pos = idx
            self._refresh_hex_only()
            self._hex_status(
                f"Replaced 1 at offset ${idx:04X}")

        def _hex_replace_all(self):
            pat, err = self._parse_hex_pattern(self._hex_find.text())
            if err or not pat:
                QMessageBox.warning(self, "Replace All",
                    f"Bad Find pattern: {err or 'empty'}")
                return
            rep, err = self._parse_hex_pattern(self._hex_replace.text())
            if err:
                QMessageBox.warning(self, "Replace All",
                    f"Bad Replace pattern: {err}")
                return
            if len(pat) != len(rep):
                QMessageBox.warning(self, "Replace All",
                    f"Replace is {len(rep)} byte(s) but Find is "
                    f"{len(pat)} - they must match.")
                return
            row = self._current_chip_index()
            if row < 0:
                return
            p, data = self._current_packet_data()
            if not data:
                return
            count = 0
            new_data = data
            search_from = 0
            while True:
                idx = new_data.find(pat, search_from)
                if idx < 0:
                    break
                new_data = new_data[:idx] + rep + new_data[idx + len(rep):]
                count += 1
                search_from = idx + len(rep)
            if count == 0:
                QMessageBox.information(self, "Replace All",
                    "No matches found.")
                return
            self._set_packet_data(row, new_data)
            self._refresh_hex_only()
            self._hex_status(f"Replaced {count}")

        def _hex_apply_edit(self):
            """Apply a single byte-level patch to the current bank's
            edit buffer. Reads the address and bytes from the toolbar
            fields; never touches the hex pane's text directly so
            the layout can't be broken by stray keystrokes."""
            row = self._current_chip_index()
            if row < 0:
                return
            addr_text = self._hex_edit_addr.text().strip()
            bytes_text = self._hex_edit_bytes.text().strip()
            if not addr_text or not bytes_text:
                QMessageBox.information(self, "Apply edit",
                    "Fill in both Address and Bytes fields, then click Apply.")
                return

            p, data = self._current_packet_data()
            if data is None:
                return

            # Parse address: $xxxx absolute, or 0xNN / NNh / decimal offset
            try:
                offset = self._parse_address_or_offset(addr_text, p)
            except ValueError as e:
                QMessageBox.warning(self, "Apply edit",
                    f"Bad address: {e}")
                return

            # Parse bytes
            new_bytes, err = self._parse_hex_pattern(bytes_text)
            if err:
                QMessageBox.warning(self, "Apply edit",
                    f"Bad bytes: {err}")
                return
            if not new_bytes:
                return

            # Range check
            if offset < 0 or offset >= len(data):
                QMessageBox.warning(self, "Apply edit",
                    f"Offset ${offset:04X} is outside this bank "
                    f"($0000-${len(data) - 1:04X}).")
                return
            if offset + len(new_bytes) > len(data):
                QMessageBox.warning(self, "Apply edit",
                    f"Patch would extend past end of bank "
                    f"(offset ${offset:04X} + {len(new_bytes)} bytes "
                    f"= ${offset + len(new_bytes):04X} > "
                    f"${len(data):04X}).")
                return

            # Apply
            new_data = data[:offset] + new_bytes + data[offset + len(new_bytes):]
            self._set_packet_data(row, new_data)
            self._refresh_hex_only()
            self._hex_status(
                f"Patched {len(new_bytes)} byte(s) at offset ${offset:04X} "
                f"(addr ${p.load_addr + offset:04X})")
            # Auto-advance address by len(new_bytes) so consecutive
            # edits at sequential addresses are easy.
            new_addr = p.load_addr + offset + len(new_bytes)
            self._hex_edit_addr.setText(f"${new_addr:04X}")
            self._hex_edit_bytes.clear()
            self._hex_edit_bytes.setFocus()
            # Re-render disasm too in case the patch touched code
            self._refresh_disasm()

        def _parse_address_or_offset(self, text, packet):
            """Parse an address-or-offset expression. Returns the
            offset within `packet.data`. Raises ValueError on
            unparseable input or on out-of-bank addresses."""
            t = text.strip()
            if not t:
                raise ValueError("empty")
            # $xxxx or 0xNN: hex
            if t.startswith('$'):
                n = int(t[1:], 16)
            elif t.lower().startswith('0x'):
                n = int(t[2:], 16)
            elif t.lower().endswith('h'):
                n = int(t[:-1], 16)
            else:
                # Plain digits: decimal
                try:
                    n = int(t, 10)
                except ValueError:
                    # Last attempt: bare hex
                    try:
                        n = int(t, 16)
                    except ValueError:
                        raise ValueError(
                            f"can't parse {t!r} as address or offset")
            # If the value looks like a C64 absolute address inside
            # this bank, convert to offset; otherwise assume it's
            # already an offset.
            base = packet.load_addr
            end = base + len(packet.data)
            if base <= n < end:
                return n - base
            if 0 <= n < len(packet.data):
                return n
            raise ValueError(
                f"${n:04X} is outside this bank "
                f"({packet.addr_range_str}, "
                f"or offset $0-${len(packet.data) - 1:04X})")

        def _hex_revert(self):
            """Drop unsaved edits in the current bank."""
            row = self._current_chip_index()
            if row < 0:
                return
            edits = getattr(self, '_hex_edits', None)
            if not edits or row not in edits:
                self._hex_status("No unsaved edits in this bank.")
                return
            del edits[row]
            if not edits:
                self._b_hex_save.setEnabled(False)
                self._b_hex_save.setText("Save to CRT")
            # Pass the list-widget row to _on_bank_changed since that's
            # what the QListWidget signal expects.
            self._on_bank_changed(self._list.currentRow())
            self._hex_status(f"Reverted unsaved edits in bank {row}")

        def _refresh_hex_only(self):
            """Re-render the Hex pane from the current edit buffer
            without reloading other tabs (avoid losing scroll pos)."""
            row = self._current_chip_index()
            if row < 0:
                return
            p, data = self._current_packet_data()
            if not data:
                return
            # Build a synthetic packet with the edited bytes so the
            # hex_dump_bank() formatting is consistent.
            view = CrtChipPacket(
                file_offset=p.file_offset,
                packet_length=p.packet_length,
                chip_type=p.chip_type,
                bank=p.bank,
                load_addr=p.load_addr,
                rom_size=len(data),
                data=data,
            )
            self._hex_pane.setPlainText(hex_dump_bank(view))

        def _hex_save_to_crt(self):
            """Write all pending edits back to the CRT file. Creates
            a one-shot .bak backup the first time we save in this
            session."""
            if not getattr(self, '_hex_edits', None):
                QMessageBox.information(self, "Save",
                    "No pending changes.")
                return
            crt_path = self.crt.path
            if not crt_path or not crt_path.exists():
                QMessageBox.warning(self, "Save",
                    "No source file - cart was loaded from bytes, not from disk.")
                return

            # One-shot .bak backup
            if not getattr(self, '_made_backup', False):
                bak = crt_path.with_suffix(crt_path.suffix + ".bak")
                if not bak.exists():
                    bak.write_bytes(crt_path.read_bytes())
                self._made_backup = True

            raw = bytearray(crt_path.read_bytes())
            # Apply each pending edit
            for row, new_data in self._hex_edits.items():
                p = self.crt.chips[row]
                data_start = p.file_offset + 16  # past CHIP header
                data_end = data_start + p.rom_size
                if data_end > len(raw):
                    QMessageBox.warning(self, "Save",
                        f"Bank {row} extends past EOF in CRT - aborting.")
                    return
                if len(new_data) != p.rom_size:
                    QMessageBox.warning(self, "Save",
                        f"Bank {row} size mismatch - "
                        f"expected {p.rom_size}, got {len(new_data)}.")
                    return
                raw[data_start:data_end] = new_data
            crt_path.write_bytes(bytes(raw))
            # Re-parse the cart so packets reflect the new state
            self.crt = parse_crt(crt_path)
            self._hex_edits = {}
            self._b_hex_save.setEnabled(False)
            self._b_hex_save.setText("Save to CRT")
            self._info_pane.setPlainText(format_crt_summary(self.crt))
            self._hex_status(f"Saved to {crt_path.name}")
            QMessageBox.information(self, "Save",
                f"Wrote modified bytes to:\n{crt_path}\n"
                f"Backup: {crt_path.name}.bak")

        # ----- Disasm copy/save -----
        def _copy_disasm(self):
            """Copy current Disasm pane to clipboard."""
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(self._asm_pane.toPlainText())
            self._hex_status("Disasm copied to clipboard")

        def _save_disasm(self):
            """Save current Disasm pane to a .asm file."""
            idx = self._current_chip_index()
            if idx < 0:
                return
            p = self.crt.chips[idx]
            default = (f"{self.crt.path.stem}_chip{idx:03d}_"
                       f"b{p.bank:02d}_{p.load_addr:04x}.asm")
            target, _ = QFileDialog.getSaveFileName(
                self, "Save disassembly",
                str(self.crt.path.parent / default), "*.asm")
            if not target:
                return
            try:
                Path(target).write_text(
                    self._asm_pane.toPlainText(), encoding='utf-8')
                self._hex_status(f"Saved disasm to {Path(target).name}")
            except Exception as e:
                QMessageBox.warning(self, "Save .asm", str(e))

        # ----- Compare two banks -----
        def _run_compare(self):
            """Render a side-by-side hex diff of the two selected banks."""
            ai = self._cmp_a.currentData()
            bi = self._cmp_b.currentData()
            if ai is None or bi is None:
                return
            if not (0 <= ai < len(self.crt.chips)) or \
               not (0 <= bi < len(self.crt.chips)):
                return
            pa = self.crt.chips[ai]
            pb = self.crt.chips[bi]
            da = pa.data
            db = pb.data
            # Pad the shorter side with $FF so the diff still lines up
            n = max(len(da), len(db))
            if len(da) < n:
                da = da + b'\xFF' * (n - len(da))
            if len(db) < n:
                db = db + b'\xFF' * (n - len(db))

            # Sweep stats first
            equal_bytes = sum(1 for x, y in zip(da, db) if x == y)
            differ_bytes = n - equal_bytes
            pct_same = (100.0 * equal_bytes / n) if n else 0
            header = (
                f"Compare: bank #{ai} ({pa.addr_range_str}) "
                f"vs bank #{bi} ({pb.addr_range_str})\n"
                f"Total bytes: {n:,}  identical: {equal_bytes:,}  "
                f"differ: {differ_bytes:,}  ({pct_same:.1f}% same)\n\n"
                f"Address  | bank A bytes (16)                                      "
                f"| bank B bytes\n"
                f"{'-' * 130}\n"
            )

            BPL = 16
            lines = [header]
            # Show lines that differ + a couple of context lines either side.
            # If carts are identical we show the full hex anyway so the
            # user can scroll through.
            for off in range(0, n, BPL):
                ca = da[off:off + BPL]
                cb = db[off:off + BPL]
                hex_a = []
                hex_b = []
                for x, y in zip(ca, cb):
                    if x == y:
                        hex_a.append(f"{x:02X}")
                        hex_b.append(f"{y:02X}")
                    else:
                        hex_a.append(f"[{x:02X}]")
                        hex_b.append(f"[{y:02X}]")
                # Pad if one bank is shorter than this row
                while len(hex_a) < BPL:
                    hex_a.append("--")
                while len(hex_b) < BPL:
                    hex_b.append("--")
                # Mark line with '*' if any differ
                marker = " " if ca == cb else "*"
                lines.append(
                    f"{marker}${off:04X}  | {' '.join(hex_a)} "
                    f"| {' '.join(hex_b)}")
            self._cmp_pane.setPlainText('\n'.join(lines))

        # ----- PETSCII rendering -----
        def _refresh_petscii(self):
            """Render the current bank's bytes as PETSCII glyphs in
            the layout selected by the toolbar (charset / cell size /
            cols). Honours the View-size mode so 'paired 16K' shows
            the combined ROML+ROMH bytes."""
            idx = self._current_chip_index()
            if idx < 0:
                self._pet_label.setText("(no bank selected)")
                return
            raw_pkt = self.crt.chips[idx]
            edits = getattr(self, '_hex_edits', {})
            if idx in edits:
                raw_pkt = CrtChipPacket(
                    file_offset=raw_pkt.file_offset,
                    packet_length=raw_pkt.packet_length,
                    chip_type=raw_pkt.chip_type,
                    bank=raw_pkt.bank,
                    load_addr=raw_pkt.load_addr,
                    rom_size=len(edits[idx]),
                    data=edits[idx],
                )
            p = self._get_view_packet(raw_pkt)
            data = p.data
            if not data:
                self._pet_label.setText("(no bytes to render)")
                return

            cols = self._cb_pet_cols.currentData() or 64
            cell = max(8, min(48, self._pet_cell_size))
            charset = self._cb_pet_charset.currentData() or "lower"

            # Slice the bank into cols-byte rows.
            lines = []
            for off in range(0, len(data), cols):
                lines.append(bytes(data[off:off + cols]))

            # Reuse the cbmfiles renderer. It takes "directory lines"
            # but works fine for arbitrary byte rows - the only
            # special-cased line is row 0 which gets a partial
            # reverse-video for the disk header. We pad row 0 with a
            # leading blank-blank so col_idx >= 2 reverse logic
            # doesn't fire on real bytes - simpler than re-implementing
            # the renderer.
            from .cbmfiles import render_directory_to_pixmap

            # Build the rendering: prepend an empty header line so the
            # renderer's row-0 reverse-video logic never affects our
            # actual data.
            padded_lines = [b''] + lines
            pix = render_directory_to_pixmap(
                padded_lines, cell_size=cell, charset=charset)
            # Crop off the empty top row (height = cell)
            from PyQt6.QtGui import QPixmap as _QPix
            cropped = pix.copy(
                0, cell, pix.width(), pix.height() - cell)
            self._pet_label.setPixmap(cropped)
            self._pet_label.adjustSize()
            self._lbl_pet_size.setText(f"{cell} px")

        def _adjust_petscii_size(self, delta):
            self._pet_cell_size = max(8, min(48,
                self._pet_cell_size + delta))
            self._refresh_petscii()

        def _save_petscii_png(self):
            """Save the current PETSCII rendering as a PNG file."""
            pix = self._pet_label.pixmap()
            if pix is None or pix.isNull():
                QMessageBox.information(self, "Save PNG",
                    "No PETSCII rendering to save - select a bank first.")
                return
            idx = self._current_chip_index()
            if idx < 0:
                return
            p = self.crt.chips[idx]
            charset = self._cb_pet_charset.currentData() or "lower"
            default = (f"{self.crt.path.stem}_chip{idx:03d}_"
                       f"b{p.bank:02d}_{p.load_addr:04x}_"
                       f"petscii_{charset}.png")
            target, _ = QFileDialog.getSaveFileName(
                self, "Save PETSCII PNG",
                str(self.crt.path.parent / default), "*.png")
            if not target:
                return
            if not pix.save(target, "PNG"):
                QMessageBox.warning(self, "Save PNG",
                    "Failed to write PNG - check the destination path.")
            else:
                self._hex_status(f"Saved PETSCII to {Path(target).name}")

        # ----- Files tab -----
        def _refresh_files_table(self):
            """Populate the Files tab table from whichever file
            system the cart provides. Currently supports:
              - EasyFS directory (skoe's standard, 24-byte entries
                in ROMH bank 0 at $A000)
              - Yeti loader file table (8-byte entries in ROML bank
                0 at offset $0100; used by the Yeti Mountain EF
                release and possibly other Onslaught-era custom
                EF loaders)

            The two formats expose somewhat different fields (Yeti
            entries don't store names, EasyFS entries don't store
            target C64 memory locations), so we adapt the columns
            accordingly via `self._files_source`."""
            from PyQt6.QtWidgets import QTableWidgetItem
            from PyQt6.QtCore import Qt as _Qt
            t = self._files_table
            t.setRowCount(0)

            # Pick which file system to display. Prefer EasyFS
            # if both are detected (rare), since it carries names.
            # As a third fallback, if the cart has no real file
            # system but our cross-bank scanner found PRG-stubs
            # (typical for RGCD/Onslaught-style multi-bank
            # compilations that use a custom loader), surface those
            # as synthetic "PRG" entries so the user can browse and
            # extract them from the Files tab.
            self._files_source = None
            if self.crt.has_easyfs and self.crt.easyfs_entries:
                self._files_source = "easyfs"
            elif (self.crt.has_yeti_filetable
                    and self.crt.yeti_entries):
                self._files_source = "yeti"
            else:
                # Look for SYS-stub PRG blobs in the cart's blob
                # list. These are produced by the cross-bank
                # scanner in scan_all_blobs() and have exact size
                # info (padding-boundary or next-stub bound).
                blobs = getattr(self.crt, 'embedded_blobs', None)
                if blobs is None:
                    try:
                        blobs = scan_all_blobs(self.crt)
                        self.crt.embedded_blobs = blobs
                    except Exception:
                        blobs = []
                prg_blobs = [b for b in blobs
                              if 'PRG' in b.kind
                              and 'BASIC SYS' in (b.note or '')]
                if prg_blobs:
                    self._files_source = "blobs"
                    self._files_blob_entries = sorted(
                        prg_blobs,
                        key=lambda b: (b.bank, b.offset))

            if self._files_source is None:
                self._files_status.setText(
                    "No file system detected in this CRT. "
                    "(EasyFS or Yeti-style file table would appear "
                    "here.)")
                return

            if self._files_source == "easyfs":
                # Standard 6-column layout for EasyFS
                t.setColumnCount(6)
                t.setHorizontalHeaderLabels(
                    ["#", "Name", "Type", "Bank", "Offset", "Size"])
                entries = self.crt.easyfs_entries
                self._files_status.setText(
                    f"<b>{len(entries)} file(s)</b> in EasyFS directory")
                t.setRowCount(len(entries))
                for i, e in enumerate(entries):
                    cells = [
                        str(i),
                        e.name + (" (hidden)" if e.is_hidden else ""),
                        e.type_label,
                        str(e.bank),
                        f"${e.offset:04X}",
                        f"{e.size:,}",
                    ]
                    for col, val in enumerate(cells):
                        it = QTableWidgetItem(val)
                        if col in (0, 3, 5):
                            it.setTextAlignment(
                                _Qt.AlignmentFlag.AlignRight |
                                _Qt.AlignmentFlag.AlignVCenter)
                        t.setItem(i, col, it)
            elif self._files_source == "yeti":
                # Yeti layout: 7 columns including C64 Loc but no
                # name. The "Name" column is synthesised as
                # "file_NN" so something readable shows up.
                t.setColumnCount(7)
                t.setHorizontalHeaderLabels(
                    ["#", "Name", "Type", "Bank", "Start", "Size", "C64 Loc"])
                entries = self.crt.yeti_entries
                self._files_status.setText(
                    f"<b>{len(entries)} file(s)</b> in Yeti loader file table")
                t.setRowCount(len(entries))
                for i, e in enumerate(entries):
                    cells = [
                        f"{i:02X}",
                        e.name,
                        e.type_label,
                        f"${e.bank:02X}",
                        f"${e.start:04X}",
                        f"${e.size:04X}",
                        f"${e.c64_loc:04X}",
                    ]
                    for col, val in enumerate(cells):
                        it = QTableWidgetItem(val)
                        if col in (0, 3, 4, 5, 6):
                            it.setTextAlignment(
                                _Qt.AlignmentFlag.AlignRight |
                                _Qt.AlignmentFlag.AlignVCenter)
                        t.setItem(i, col, it)
            else:
                # blobs source: SYS-stub PRGs detected by the
                # cross-bank scanner. Used for cart compilations
                # without a proper file system (e.g. RGCD multi-
                # bank releases) where each game is just a
                # contiguous block of bytes starting at a BASIC
                # SYS stub. We synthesise a name like "prg_BB_OOOO"
                # from the bank+offset so the user can tell
                # entries apart.
                t.setColumnCount(6)
                t.setHorizontalHeaderLabels(
                    ["#", "Name", "Type", "Bank", "Offset", "Size"])
                entries = self._files_blob_entries
                self._files_status.setText(
                    f"<b>{len(entries)} PRG(s)</b> detected via "
                    f"BASIC SYS-stub scan (no file system in cart)")
                t.setRowCount(len(entries))
                for i, b in enumerate(entries):
                    name = f"prg_{b.bank:02X}_{b.offset:04X}"
                    span_str = (f"PRG ({len(b.spans_banks)} banks)"
                                 if b.spans_banks else "PRG")
                    cells = [
                        str(i),
                        name,
                        span_str,
                        str(b.bank),
                        f"${b.offset:04X}",
                        f"{b.size:,}",
                    ]
                    for col, val in enumerate(cells):
                        it = QTableWidgetItem(val)
                        if col in (0, 3, 5):
                            it.setTextAlignment(
                                _Qt.AlignmentFlag.AlignRight |
                                _Qt.AlignmentFlag.AlignVCenter)
                        t.setItem(i, col, it)

            # Auto-size columns only on the first build. Subsequent
            # rebuilds (e.g. after Hex-tab edits that invalidate
            # entries) keep whatever widths the user has chosen.
            if not getattr(self, '_files_table_sized', False):
                t.resizeColumnsToContents()
                if t.columnCount() > 1:
                    t.setColumnWidth(1, max(180, t.columnWidth(1)))
                self._files_table_sized = True

        def _current_files_entries(self):
            """Return the entries list backing the Files tab,
            depending on which file system was detected."""
            src = getattr(self, '_files_source', None)
            if src == "yeti":
                return self.crt.yeti_entries
            if src == "blobs":
                return getattr(self, '_files_blob_entries', [])
            return self.crt.easyfs_entries

        def _files_jump_to_bank(self):
            """Select the bank in the left bank list that contains
            the currently-highlighted file's start address."""
            t = self._files_table
            row = t.currentRow()
            entries = self._current_files_entries()
            if row < 0 or row >= len(entries):
                return
            entry = entries[row]
            # Both EasyFS and Yeti entries expose `bank`, but the
            # offset attribute is `offset` for EasyFS and `start`
            # for Yeti.
            entry_offset = getattr(entry, 'offset',
                                     getattr(entry, 'start', 0))
            entry_bank = entry.bank
            for i in range(self._list.count()):
                chip_idx = self._list.item(i).data(Qt.ItemDataRole.UserRole)
                if (isinstance(chip_idx, int)
                        and 0 <= chip_idx < len(self.crt.chips)
                        and self.crt.chips[chip_idx].bank == entry_bank):
                    self._list.setCurrentRow(i)
                    self._hex_status(
                        f"Jumped to bank {entry_bank} "
                        f"(file at offset ${entry_offset:04X})")
                    self._hex_search_pos = max(0, entry_offset - 1)
                    return
            self._hex_status(f"Bank {entry_bank} not present in CRT.")

        def _files_extract_selected(self):
            """Extract selected file(s) from the directory to a
            chosen folder. Works for both EasyFS and Yeti formats.

            For Yeti entries, the bytes are saved as a proper PRG
            file with the 2-byte little-endian load address (=
            entry.c64_loc) prefixed - this matches the layout that
            Yeti's loader applies when copying to RAM, and lets
            VICE / x64sc load the resulting .prg directly with
            LOAD"...",8,1.

            For EasyFS entries, write_easyfs_entry() handles the
            details of the EasyFS-specific extraction logic."""
            t = self._files_table
            sel = t.selectionModel().selectedRows()
            if not sel:
                QMessageBox.information(self, "Extract",
                    "Pick one or more files in the table first.")
                return
            indices = sorted(idx.row() for idx in sel)
            target_dir = QFileDialog.getExistingDirectory(
                self, "Extract files to directory",
                str(self.crt.path.parent))
            if not target_dir:
                return
            entries = self._current_files_entries()
            source = getattr(self, '_files_source', "easyfs")
            written = []
            errors = []
            for i in indices:
                if i >= len(entries):
                    continue
                e = entries[i]
                try:
                    if source == "blobs":
                        # SYS-stub PRG blob from the cross-bank
                        # scanner. The blob's bytes already start
                        # with the PRG load-address header (since
                        # the SYS stub at $0801 is itself the start
                        # of the PRG file), so we write them as-is.
                        # Filename pattern: <crt-stem>_prg_<bank>_<offset>.prg
                        fn = sanitize_easyfs_name(
                            f"{self.crt.path.stem}_"
                            f"prg_{e.bank:02X}_{e.offset:04X}", "prg")
                        target = Path(target_dir) / fn
                        if target.exists():
                            target = Path(target_dir) / sanitize_easyfs_name(
                                f"{self.crt.path.stem}_"
                                f"prg_{e.bank:02X}_{e.offset:04X}_dup", "prg")
                        extract_embedded_blob(self.crt, e, target)
                    elif source == "yeti":
                        # Save as <crt-stem>_<index>.prg with PRG
                        # load-address header prepended. Mario's
                        # filetable convention uses zero-padded
                        # 2-digit lower-case hex for the index, e.g.
                        # "00.prg", "01.prg", ... "cc.prg".
                        fn = sanitize_easyfs_name(
                            f"{self.crt.path.stem}_yeti_"
                            f"{i:02x}", "prg")
                        target = Path(target_dir) / fn
                        if target.exists():
                            target = Path(target_dir) / sanitize_easyfs_name(
                                f"{self.crt.path.stem}_yeti_"
                                f"{i:02x}_dup", "prg")
                        raw = get_yeti_file_bytes(self.crt, e)
                        load_hdr = bytes([
                            e.c64_loc & 0xFF,
                            (e.c64_loc >> 8) & 0xFF])
                        target.write_bytes(load_hdr + raw)
                    else:
                        # Pick file extension based on the EasyFS
                        # type code:
                        #   $01 / $02 / $03 = PRG (with 2-byte load
                        #     address as part of the data)
                        #   $10..$13 = cartridge ROM dump (8K/16K/
                        #     Ultimax). These are NOT loadable PRGs,
                        #     they're raw ROM images. We save them
                        #     as .bin so the user knows they need
                        #     to be wrapped into a CRT or attached
                        #     differently (cartconv -t ulti, etc.).
                        #   $14..$15 = Ocean Type 1 cart (raw banks)
                        #   $1C..$1E = xbank cart (raw banks)
                        #   anything else = .bin (unknown structure)
                        type_code = e.type_byte & 0x3F
                        if type_code in (0x01, 0x02, 0x03):
                            ext = "prg"
                        else:
                            ext = "bin"
                        fn = sanitize_easyfs_name(e.name, ext)
                        target = Path(target_dir) / fn
                        if target.exists():
                            target = Path(target_dir) / sanitize_easyfs_name(
                                f"{e.name}_{i}", ext)
                        write_easyfs_entry(self.crt, e, target)
                    written.append(target)
                except Exception as ex:
                    name = (e.name if hasattr(e, 'name') and e.name
                            else f"#{i:02X}")
                    errors.append(f"{name}: {ex}")
            msg = f"Extracted {len(written)}/{len(indices)} file(s) to:\n{target_dir}"
            if errors:
                msg += f"\n\nErrors:\n" + "\n".join(errors[:8])
            QMessageBox.information(self, "Extract", msg)
            self._hex_status(f"Extracted {len(written)} file(s)")

        def _files_extract_all(self):
            """Extract every file in the directory to a chosen folder."""
            if not self._current_files_entries():
                return
            t = self._files_table
            t.selectAll()
            self._files_extract_selected()

        def _files_run_in_emulator(self):
            """Extract the currently-selected file to a temporary
            location and launch the configured C64 emulator on it.

            Shares config with the disasm tool's Run-in-emulator
            (config['c64_emulator'] = path, config['c64_emulator_args']
            = command-line template with {file}/{name}/{dir} tokens).
            If not configured yet, prompts the user.

            For PRG files the emulator is launched with the .prg
            directly (use {file} or e.g. -autostart {file} in args).
            For cartridge-typed files (Cart8K/Cart16K/Ultimax) we
            wrap the raw .bin into a temporary .crt with cartconv
            if available, otherwise fall back to launching with the
            raw .bin (which most emulators won't auto-detect).
            """
            t = self._files_table
            sel = t.selectionModel().selectedRows()
            if not sel:
                QMessageBox.information(self, "Run in Emulator",
                    "Pick exactly one file from the table to launch.")
                return
            if len(sel) > 1:
                QMessageBox.information(self, "Run in Emulator",
                    "Pick exactly ONE file - the emulator can only "
                    "run one program at a time.")
                return
            row = sel[0].row()
            entries = self._current_files_entries()
            if row >= len(entries):
                return
            e = entries[row]
            source = getattr(self, '_files_source', "easyfs")

            # Find the parent main window to read config from
            main = self.parent()
            while main is not None and not hasattr(main, 'config'):
                main = main.parent()
            if main is None or not hasattr(main, 'config'):
                QMessageBox.warning(self, "Run in Emulator",
                    "Could not find main window config. Set the "
                    "emulator path via the disasm tool first.")
                return

            emu_path = (main.config.get('c64_emulator', '') or '').strip()
            emu_args = (main.config.get('c64_emulator_args', '{file}')
                          or '{file}').strip()
            if not emu_path or not Path(emu_path).exists():
                QMessageBox.warning(self, "Run in Emulator",
                    "No C64 emulator configured yet.\n\n"
                    "Open the disasm tool and use its Run-in-emulator "
                    "config first. The same path will then be used "
                    "here.\n\nExpected config key: 'c64_emulator' "
                    "(e.g. C:\\VICE\\x64sc.exe)")
                return

            # Decide file extension per type, like _files_extract_selected
            if source == "yeti":
                ext = "prg"  # all Yeti files are PRG-style
            elif source == "blobs":
                ext = "prg"  # SYS-stub blobs are PRG by definition
            else:
                type_code = e.type_byte & 0x3F
                ext = "prg" if type_code in (0x01, 0x02, 0x03) else "bin"

            # Extract to a temp file
            import tempfile
            try:
                # Use the cart's stem + entry name so the emulator
                # window title (some emus show the filename) is helpful
                if source == "blobs":
                    stem = (f"{self.crt.path.stem}_"
                             f"prg_{e.bank:02X}_{e.offset:04X}")
                else:
                    stem = sanitize_easyfs_name(e.name, ext).rsplit('.', 1)[0]
                tmpdir = Path(tempfile.gettempdir()) / "dopus_crt_run"
                tmpdir.mkdir(parents=True, exist_ok=True)
                target = tmpdir / f"{stem}.{ext}"
                if source == "yeti":
                    raw = get_yeti_file_bytes(self.crt, e)
                    load_hdr = bytes([
                        e.c64_loc & 0xFF,
                        (e.c64_loc >> 8) & 0xFF])
                    target.write_bytes(load_hdr + raw)
                elif source == "blobs":
                    # SYS-stub PRG bytes already include the
                    # 2-byte $0801 load header
                    extract_embedded_blob(self.crt, e, target)
                else:
                    write_easyfs_entry(self.crt, e, target)
            except Exception as ex:
                QMessageBox.warning(self, "Run in Emulator",
                    f"Could not extract file:\n{ex}")
                return

            # Build emulator command
            import shlex, subprocess, sys
            try:
                template_args = shlex.split(emu_args, posix=False)
            except Exception:
                template_args = emu_args.split()
            def _expand(s):
                return (s.replace('{file}', str(target))
                         .replace('{name}', target.name)
                         .replace('{dir}',  str(target.parent)))
            arg_list = [_expand(a) for a in template_args]
            # Strip surrounding quotes (subprocess does its own quoting)
            cleaned = []
            for a in arg_list:
                if len(a) >= 2 and a[0] == '"' and a[-1] == '"':
                    a = a[1:-1]
                cleaned.append(a)
            arg_list = cleaned
            full_cmd = [emu_path] + arg_list

            # Launch detached
            try:
                if sys.platform == 'win32':
                    DETACHED = 0x00000008
                    subprocess.Popen(
                        full_cmd,
                        creationflags=(DETACHED
                                         | subprocess.CREATE_NEW_PROCESS_GROUP),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True)
                else:
                    subprocess.Popen(
                        full_cmd,
                        start_new_session=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True)
                self._hex_status(
                    f"Launched: {Path(emu_path).name} "
                    f"{' '.join(arg_list)}")
            except Exception as ex:
                QMessageBox.warning(self, "Run in Emulator",
                    f"Could not launch emulator:\n{ex}\n\n"
                    f"Command: {full_cmd}")

        def _files_save_list(self):
            """Save the file directory listing as a plain-text file.
            Works for both EasyFS and Yeti file systems; the column
            layout adapts to whichever is current."""
            entries = self._current_files_entries()
            source = getattr(self, '_files_source', None)
            if not entries or source is None:
                QMessageBox.information(self, "Save list",
                    "No file system detected.")
                return
            default = (f"{self.crt.path.stem}_"
                        f"{'yeti' if source == 'yeti' else 'easyfs'}.txt")
            target, _ = QFileDialog.getSaveFileName(
                self, "Save directory listing",
                str(self.crt.path.parent / default), "*.txt")
            if not target:
                return
            lines = []
            if source == "yeti":
                lines.append(f"Yeti loader file table for {self.crt.path.name}")
                lines.append(f"{len(entries)} entries")
                lines.append("")
                for i, e in enumerate(entries):
                    # Match Mario's filetable.txt format
                    raw = (e.start if e.bank == 0
                            else 0x4000 + (e.bank - 1) * 0x2000 + e.start)
                    lines.append(
                        f"Filenumber : {i:02X}, Start Address: {e.start:04X}, "
                        f"EF-Bank Number: {e.bank:02X}, "
                        f"RAW Start : {raw:x}, "
                        f"File Data Length: {e.size:04X}, "
                        f"C64 Memory Location: {e.c64_loc:04X}")
            else:
                lines.append(f"EasyFS directory listing for {self.crt.path.name}")
                lines.append(f"{len(entries)} entries")
                lines.append("")
                lines.append(f"{'#':>3}  {'name':<16}  {'type':<8}  "
                             f"{'bank':>4}  {'offset':>6}  {'size':>10}  hidden")
                lines.append("-" * 70)
                for i, e in enumerate(entries):
                    lines.append(
                        f"{i:>3}  {e.name:<16}  {e.type_label:<8}  "
                        f"{e.bank:>4}  ${e.offset:04X}  "
                        f"{e.size:>10,}  {'Y' if e.is_hidden else ''}")
            try:
                Path(target).write_text(
                    "\n".join(lines), encoding='utf-8')
                self._hex_status(f"Saved file listing to {Path(target).name}")
            except Exception as e:
                QMessageBox.warning(self, "Save list", str(e))

        # ----- Blobs tab -----
        def _refresh_blobs_table(self):
            """Re-run the embedded-blob scanner and rebuild the
            Blobs-tab table from the results."""
            from PyQt6.QtWidgets import QTableWidgetItem
            from PyQt6.QtCore import Qt as _Qt
            self._scanned_blobs = scan_all_blobs(self.crt)
            t = self._blobs_table
            t.setRowCount(0)
            if not self._scanned_blobs:
                self._blobs_status.setText(
                    "No embedded blobs identified. (SID files, Koala "
                    "bitmaps, decompressor stubs, PRG payloads etc. "
                    "would appear here.)")
                return
            self._blobs_status.setText(
                f"<b>{len(self._scanned_blobs)} blob(s)</b> "
                f"identified across {len(self.crt.chips)} chip packets")
            t.setRowCount(len(self._scanned_blobs))
            for i, b in enumerate(self._scanned_blobs):
                cells = [
                    str(i),
                    b.kind,
                    str(b.bank),
                    f"${b.offset:04X}",
                    f"{b.size:,}" if b.size > 0 else "?",
                    b.note,
                ]
                for col, val in enumerate(cells):
                    it = QTableWidgetItem(val)
                    if col in (0, 2, 4):
                        it.setTextAlignment(
                            _Qt.AlignmentFlag.AlignRight |
                            _Qt.AlignmentFlag.AlignVCenter)
                    t.setItem(i, col, it)
            # Auto-size columns only on the first build (subsequent
            # Rescans keep user-chosen widths)
            if not getattr(self, '_blobs_table_sized', False):
                t.resizeColumnsToContents()
                # Generous defaults for the Kind and Note columns
                # which carry the longest text.
                if t.columnCount() > 1:
                    t.setColumnWidth(1, max(220, t.columnWidth(1)))
                if t.columnCount() > 5:
                    t.setColumnWidth(5, max(300, t.columnWidth(5)))
                self._blobs_table_sized = True

        def _blobs_jump_to_bank(self):
            t = self._blobs_table
            row = t.currentRow()
            if row < 0 or row >= len(self._scanned_blobs):
                return
            blob = self._scanned_blobs[row]
            # Find a list-widget row whose UserRole points at this
            # blob's chip_index.
            for i in range(self._list.count()):
                chip_idx = self._list.item(i).data(Qt.ItemDataRole.UserRole)
                if isinstance(chip_idx, int) and chip_idx == blob.chip_index:
                    self._list.setCurrentRow(i)
                    self._hex_search_pos = max(0, blob.offset - 1)
                    self._hex_status(
                        f"Jumped to bank {blob.bank}, offset "
                        f"${blob.offset:04X} ({blob.kind})")
                    return
            self._hex_status(f"Bank {blob.bank} not present.")

        def _blobs_extract_selected(self):
            """Save selected blob(s) to a chosen folder. Picks an
            extension based on the blob's kind."""
            t = self._blobs_table
            sel = t.selectionModel().selectedRows()
            if not sel:
                QMessageBox.information(self, "Extract",
                    "Pick one or more blobs in the table first.")
                return
            indices = sorted(idx.row() for idx in sel)
            target_dir = QFileDialog.getExistingDirectory(
                self, "Extract blobs to directory",
                str(self.crt.path.parent))
            if not target_dir:
                return

            written = []
            errors = []
            for i in indices:
                if i >= len(self._scanned_blobs):
                    continue
                blob = self._scanned_blobs[i]
                # Pick extension by kind
                k = blob.kind.lower()
                if k.startswith("psid") or k.startswith("rsid"):
                    ext = "sid"
                elif "koala" in k:
                    ext = "koa"
                elif "prg" in k:
                    ext = "prg"
                elif "stub" in k:
                    ext = "exo"   # generic compressed-data ext
                elif "cbm80" in k:
                    ext = "bin"
                else:
                    ext = "bin"
                # Generate name
                base = (f"{self.crt.path.stem}_b{blob.bank:02d}_"
                        f"o{blob.offset:04X}_{blob.kind.split()[0]}")
                base = sanitize_easyfs_name(base, ext)
                target = Path(target_dir) / base
                try:
                    # For SFX-stub blobs, save the entire bank-tail
                    # from the stub onwards (so desktop unpackers
                    # have the actual compressed data).
                    if "stub" in k:
                        p = self.crt.chips[blob.chip_index]
                        target.write_bytes(p.data[blob.offset:])
                    else:
                        extract_embedded_blob(self.crt, blob, target)
                    written.append(target)
                except Exception as ex:
                    errors.append(f"#{i} ({blob.kind}): {ex}")
            msg = f"Extracted {len(written)}/{len(indices)} blob(s) to:\n{target_dir}"
            if errors:
                msg += "\n\nErrors:\n" + "\n".join(errors[:8])
            QMessageBox.information(self, "Extract", msg)
            self._hex_status(f"Extracted {len(written)} blob(s)")

        def _blobs_save_list(self):
            if not self._scanned_blobs:
                QMessageBox.information(self, "Save list",
                    "No blobs to list.")
                return
            default = f"{self.crt.path.stem}_blobs.txt"
            target, _ = QFileDialog.getSaveFileName(
                self, "Save blob inventory",
                str(self.crt.path.parent / default), "*.txt")
            if not target:
                return
            lines = [
                f"Embedded-blob inventory for {self.crt.path.name}",
                f"{len(self._scanned_blobs)} blobs across "
                f"{len(self.crt.chips)} chip packets",
                "",
                f"{'#':>4}  {'kind':<28}  {'bank':>4}  "
                f"{'offset':>6}  {'size':>10}  note",
                "-" * 90,
            ]
            for i, b in enumerate(self._scanned_blobs):
                lines.append(
                    f"{i:>4}  {b.kind:<28}  {b.bank:>4}  "
                    f"${b.offset:04X}  "
                    f"{b.size:>10,}  {b.note}")
            try:
                Path(target).write_text(
                    "\n".join(lines), encoding='utf-8')
                self._hex_status(f"Saved blob list to {Path(target).name}")
            except Exception as e:
                QMessageBox.warning(self, "Save list", str(e))

        def _blobs_update_preview(self):
            """Render the currently-selected blob into the preview
            label below the table. The render mode comes from the
            'Mode' combo (auto / koala / hires / sprite-hires /
            sprite-mc / charset) and respects the zoom factor + the
            optional 'has 2-byte load addr' override.

            For multi-bank blobs (those with `spans_banks` set), the
            bytes are stitched together via get_blob_bytes() so
            renderers that expect a contiguous buffer work fine."""
            t = self._blobs_table
            row = t.currentRow()
            if row < 0 or row >= len(self._scanned_blobs):
                self._blob_preview_label.clear()
                self._blob_preview_label.setText(
                    "Select a blob in the table to preview it.")
                return
            blob = self._scanned_blobs[row]
            edits = getattr(self, '_hex_edits', {})

            mode = self._cb_blob_render_mode.currentData() or "auto"
            zoom = self._cb_blob_zoom.currentData() or 2
            skip = self._cb_blob_skip_addr.currentData() or "auto"

            # For multi-bank or full-blob renders, fetch the spliced
            # bytes via get_blob_bytes(). For single-bank with a
            # large size cap (charset = 2KB, etc.) this is the same
            # as direct slicing.
            if blob.spans_banks:
                blob_bytes = get_blob_bytes(self.crt, blob, edits)
                # The renderer's internal `offset` is now 0 because
                # we already sliced.
                data = blob_bytes
                local_offset = 0
            else:
                # Single bank: pass the chip's full data and let the
                # renderer slice from blob.offset. This way charset
                # detection scans within a packet and renders from
                # there directly.
                ci = blob.chip_index
                data = (edits[ci] if ci in edits
                         else self.crt.chips[ci].data)
                local_offset = blob.offset

            # Determine effective render kind
            kind_lower = blob.kind.lower()
            if mode == "auto":
                if "charset" in kind_lower:
                    eff = "charset"
                elif "koala" in kind_lower:
                    eff = "koala"
                elif "hires" in kind_lower:
                    eff = "hires"
                elif "sprite" in kind_lower:
                    eff = "sprite"
                else:
                    self._blob_preview_label.clear()
                    self._blob_preview_label.setText(
                        f"No preview for kind: {blob.kind}\n\n"
                        f"Use 'Mode' override to force-render this "
                        f"blob as Koala / Hires / Sprites / Charset\n"
                        f"if you think the auto-detection missed it.")
                    self._blob_preview_label.adjustSize()
                    return
            else:
                eff = mode

            # Determine has_load_addr
            has_addr = (skip == "yes")

            img = None
            label_kind = ""
            if eff == "koala":
                img = render_koala_bitmap(
                    data, offset=local_offset, has_load_addr=has_addr)
                label_kind = "Koala (multicolor 160x200)"
            elif eff == "hires":
                img = render_hires_bitmap(
                    data, offset=local_offset, has_load_addr=has_addr)
                label_kind = "Hires (320x200)"
            elif eff == "charset":
                img = render_charset(data, offset=local_offset,
                                       fg=1, bg=6, cols=32)
                label_kind = "Charset (256 glyphs, 32x8 grid)"
            elif eff in ("sprite", "sprite_mc"):
                multi = (eff == "sprite_mc")
                avail = (len(data) - local_offset) // 64
                count = min(64, max(1, avail))
                img = render_sprite_sheet(
                    data, offset=local_offset, count=count,
                    multicolor=multi, cols=8, fg_color=1, bg_color=11,
                    mc1_color=5, mc2_color=7)
                label_kind = (f"Sprites x{count} "
                               f"({'multicolor' if multi else 'hires'})")

            if img is None:
                self._blob_preview_label.clear()
                self._blob_preview_label.setText(
                    f"Render failed for kind: {eff}\n"
                    f"(buffer too short or Qt error).")
                self._blob_preview_label.adjustSize()
                return

            from PyQt6.QtGui import QPixmap
            from PyQt6.QtCore import Qt as _Qt
            scaled = img.scaled(img.width() * zoom,
                                 img.height() * zoom,
                                 _Qt.AspectRatioMode.KeepAspectRatio,
                                 _Qt.TransformationMode.FastTransformation)
            self._blob_preview_label.setText("")
            self._blob_preview_label.setPixmap(QPixmap.fromImage(scaled))
            self._blob_preview_label.adjustSize()
            span_str = (f" (multi-bank, spans chips {blob.spans_banks})"
                         if blob.spans_banks else "")
            self._hex_status(
                f"Preview: {label_kind} @ bank {blob.bank} "
                f"offset ${blob.offset:04X}{span_str}")

        def _blobs_save_preview(self):
            """Save the rendered preview as a PNG file."""
            pix = self._blob_preview_label.pixmap()
            if pix is None or pix.isNull():
                QMessageBox.information(self, "Save preview",
                    "Nothing to save - select a blob with a renderable "
                    "preview first.")
                return
            t = self._blobs_table
            row = t.currentRow()
            if row < 0 or row >= len(self._scanned_blobs):
                return
            blob = self._scanned_blobs[row]
            mode = self._cb_blob_render_mode.currentData() or "auto"
            default = (f"{self.crt.path.stem}_b{blob.bank:02d}_"
                       f"o{blob.offset:04X}_{mode}.png")
            target, _ = QFileDialog.getSaveFileName(
                self, "Save preview as PNG",
                str(self.crt.path.parent / default), "*.png")
            if not target:
                return
            if pix.save(target, "PNG"):
                self._hex_status(
                    f"Saved preview to {Path(target).name}")
            else:
                QMessageBox.warning(self, "Save preview",
                    "Failed to write PNG.")

        def keyPressEvent(self, event):
            from PyQt6.QtCore import Qt as _Qt
            if event.key() == _Qt.Key.Key_Escape:
                self.accept()
                return
            # F3 = Find Next, Shift+F3 = Find Prev (works anywhere
            # in the dialog, not just when the find field has focus).
            if event.key() == _Qt.Key.Key_F3:
                if event.modifiers() & _Qt.KeyboardModifier.ShiftModifier:
                    self._hex_find_prev()
                else:
                    self._hex_find_next()
                return
            super().keyPressEvent(event)

        # ----- persistent geometry / column-width state -----
        def _load_persisted_state(self):
            """Read the persisted state dict from config, returning
            an empty dict on error."""
            from .config import load_config
            try:
                cfg = load_config()
                return cfg.get("crt_toolkit", {}) or {}
            except Exception:
                return {}

        def _restore_state_geometry_only(self):
            """First-phase restore: window geometry only. Called
            synchronously from __init__ so the window appears at
            its remembered position/size as quickly as possible.

            Strategy: apply explicit resize(w,h) + move(x,y) directly
            because restoreGeometry() is unreliable when called before
            show() on some Qt platforms. We also keep restoreGeometry()
            as an alternative path because it correctly handles
            multi-monitor anchoring + fullscreen state, but apply the
            explicit size/pos AFTER so they win."""
            from PyQt6.QtCore import QByteArray
            state = self._load_persisted_state()
            self._pending_resize = None
            self._pending_move = None

            # Try restoreGeometry first - it handles multi-monitor
            # anchoring and fullscreen better than raw resize/move.
            geom_b64 = state.get("geometry", "")
            if geom_b64:
                try:
                    import base64
                    qba = QByteArray(base64.b64decode(geom_b64))
                    self.restoreGeometry(qba)
                except Exception:
                    pass

            # Now apply explicit size + pos. These OVERRIDE the
            # restoreGeometry values because we found that on Windows
            # 11 with PyQt6 6.7+, restoreGeometry sometimes silently
            # drops the size data and only restores position.
            sz = state.get("size", [])
            if (isinstance(sz, list) and len(sz) == 2
                    and all(isinstance(x, int) and x > 100 for x in sz)):
                try:
                    self.resize(sz[0], sz[1])
                    self._pending_resize = (int(sz[0]), int(sz[1]))
                except Exception:
                    pass
            ps = state.get("pos", [])
            if (isinstance(ps, list) and len(ps) == 2
                    and all(isinstance(x, int) for x in ps)):
                try:
                    self.move(ps[0], ps[1])
                    self._pending_move = (int(ps[0]), int(ps[1]))
                except Exception:
                    pass

        def _restore_state_late(self):
            """Second-phase restore: splitter + tab + column widths.
            Deferred via QTimer.singleShot(0, ...) so Qt has had a
            chance to lay out the dialog at least once - otherwise
            splitter setSizes() values get clipped against an
            initial-size window and column widths fight against
            still-running auto-sizing.

            We try-block individual fields so a partially-corrupt
            state file doesn't lose everything - any unparseable
            entry just falls back to the default."""
            state = self._load_persisted_state()
            print(f"[CRT Toolkit] loading state: "
                  f"size={state.get('size')} "
                  f"pos={state.get('pos')} "
                  f"splitter={state.get('splitter')} "
                  f"tab={state.get('active_tab')} "
                  f"files_cols={state.get('files_cols')}")

            # Apply explicit size/pos if restoreGeometry() didn't
            # take effect. This is a fallback for Qt platforms
            # (offscreen, some Wayland configurations) that ignore
            # restoreGeometry's size data.
            if self._pending_resize is not None:
                try:
                    cur = self.size()
                    want = self._pending_resize
                    if cur.width() != want[0] or cur.height() != want[1]:
                        self.resize(want[0], want[1])
                except Exception:
                    pass
            if self._pending_move is not None:
                try:
                    self.move(self._pending_move[0], self._pending_move[1])
                except Exception:
                    pass

            # Splitter sizes (left bank-list / right tabs)
            splitter = state.get("splitter", [])
            if (isinstance(splitter, list) and len(splitter) == 2
                    and all(isinstance(x, int) and x >= 0 for x in splitter)
                    and hasattr(self, '_split')):
                try:
                    self._split.setSizes(splitter)
                except Exception:
                    pass

            # Active tab index
            tab_idx = state.get("active_tab", 0)
            if (isinstance(tab_idx, int)
                    and 0 <= tab_idx < self._tabs.count()):
                self._tabs.setCurrentIndex(tab_idx)

            # Files-tab column widths
            files_cols = state.get("files_cols", [])
            if (isinstance(files_cols, list) and files_cols
                    and hasattr(self, '_files_table')):
                t = self._files_table
                for i, w in enumerate(files_cols):
                    if i < t.columnCount() and isinstance(w, int) and w > 0:
                        t.setColumnWidth(i, w)

            # Blobs-tab column widths
            blobs_cols = state.get("blobs_cols", [])
            if (isinstance(blobs_cols, list) and blobs_cols
                    and hasattr(self, '_blobs_table')):
                t = self._blobs_table
                for i, w in enumerate(blobs_cols):
                    if i < t.columnCount() and isinstance(w, int) and w > 0:
                        t.setColumnWidth(i, w)

        def _save_state(self):
            """Persist window geometry, splitter sizes, active tab
            and table column widths into config['crt_toolkit'].
            Called from closeEvent / accept / reject so user's
            adjustments carry forward to the next open."""
            from .config import load_config, save_config
            try:
                cfg = load_config()
            except Exception:
                cfg = {}

            state = {}

            # Geometry as base64-encoded QByteArray (canonical Qt
            # serialisation - covers position, size, multi-monitor
            # anchor, and fullscreen state)
            try:
                import base64
                qba = self.saveGeometry()
                state["geometry"] = base64.b64encode(
                    bytes(qba)).decode("ascii")
            except Exception:
                pass

            # Plus explicit size + pos as a fallback for Qt platforms
            # where restoreGeometry doesn't take effect (offscreen
            # plugin etc.). Cheap to store, robust to apply.
            try:
                sz = self.size()
                state["size"] = [sz.width(), sz.height()]
            except Exception:
                pass
            try:
                ps = self.pos()
                state["pos"] = [ps.x(), ps.y()]
            except Exception:
                pass

            # Splitter sizes
            try:
                if hasattr(self, '_split'):
                    state["splitter"] = list(self._split.sizes())
            except Exception:
                pass

            # Active tab
            try:
                state["active_tab"] = self._tabs.currentIndex()
            except Exception:
                pass

            # Files / Blobs column widths
            try:
                if hasattr(self, '_files_table'):
                    t = self._files_table
                    state["files_cols"] = [
                        t.columnWidth(i) for i in range(t.columnCount())]
            except Exception:
                pass
            try:
                if hasattr(self, '_blobs_table'):
                    t = self._blobs_table
                    state["blobs_cols"] = [
                        t.columnWidth(i) for i in range(t.columnCount())]
            except Exception:
                pass

            cfg["crt_toolkit"] = state
            # Also push the new state into the parent main-window's
            # in-memory config cache (if the parent has one). The
            # parent typically holds a long-lived self.config dict
            # that it loaded once at startup, and its own
            # save_config(self.config) calls (e.g. when the user
            # changes a drive setting) would otherwise overwrite our
            # crt_toolkit entry with the parent's stale copy of it.
            try:
                parent = self.parent()
                if parent is not None and hasattr(parent, 'config'):
                    parent.config["crt_toolkit"] = state
            except Exception:
                pass
            try:
                save_config(cfg)
                # Debug aid for diagnosing persistence problems:
                # print to console so the user can see whether the
                # save actually fired. Quiet in normal usage since
                # most people never look at the console.
                print(f"[CRT Toolkit] saved state: "
                      f"size={state.get('size')} "
                      f"pos={state.get('pos')} "
                      f"splitter={state.get('splitter')} "
                      f"tab={state.get('active_tab')} "
                      f"files_cols={state.get('files_cols')}")
            except Exception as e:
                print(f"CRT toolkit state save error: {e}")

        def closeEvent(self, event):
            self._save_state()
            super().closeEvent(event)

        def accept(self):
            self._save_state()
            super().accept()

        def reject(self):
            self._save_state()
            super().reject()

    return _CrtToolkitDialog


# Lazy build at first dialog open. _CrtToolkitDialog placeholder.
_CrtToolkitDialog = None


def _ensure_dialog_class():
    global _CrtToolkitDialog
    if _CrtToolkitDialog is None:
        _CrtToolkitDialog = _make_crt_dialog()
    return _CrtToolkitDialog


def open_crt_toolkit(crt_or_path, parent=None):
    """Open the CRT Toolkit non-modal dialog. Accepts either a
    Path/string to a .crt file or a pre-parsed CrtFile object.
    Returns the QDialog so callers can keep a reference.

    For path inputs, parsing the cart (CRT-header + chip walk +
    EAPI / EasyFS / Yeti / blob detection) plus building the
    QDialog itself with all tabs and tables can take a noticeable
    fraction of a second on large carts. We show a centred
    "Detecting cartridge..." notice as a top-level frameless
    splash window for the WHOLE duration: from before parse_crt()
    runs until the dialog is fully constructed and visible.
    """
    cls = _ensure_dialog_class()

    # Decide whether to show a splash. Only when we're going to do
    # the heavy work (path -> parse + dialog build). If we already
    # have a parsed CrtFile, dialog construction is the only slow
    # part - show splash anyway, since that build is non-trivial
    # for big carts.
    splash = None
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QLabel, QApplication
        splash = QLabel(" Detecting cartridge, please wait... ")
        splash.setStyleSheet(
            "QLabel { background-color: #ffcc00;"
            " color: #000000; border: 3px solid #000000;"
            f" padding: 16px 32px; font-size: {scaled_font_px(18)}px;"
            " font-weight: bold; font-family: monospace; }")
        splash.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint)
        splash.setWindowTitle("Detecting cartridge")
        splash.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose, True)
        splash.adjustSize()
        # Centre over parent or primary screen
        if parent is not None:
            pg = parent.frameGeometry()
            cx = pg.center().x() - splash.width() // 2
            cy = pg.center().y() - splash.height() // 2
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            cx = screen.center().x() - splash.width() // 2
            cy = screen.center().y() - splash.height() // 2
        splash.move(cx, cy)
        splash.show()
        splash.raise_()
        splash.activateWindow()
        # Force Qt to paint the splash before we block on heavy work
        QApplication.processEvents()
        QApplication.processEvents()
    except Exception:
        splash = None

    try:
        # Phase 1: parsing (only needed if path input)
        if isinstance(crt_or_path, CrtFile):
            crt = crt_or_path
        else:
            crt = parse_crt(crt_or_path)
            # Repaint the splash after the long parse, just in case
            # Qt collapsed it during the blocking call.
            try:
                from PyQt6.QtWidgets import QApplication
                if splash is not None:
                    splash.raise_()
                    QApplication.processEvents()
            except Exception:
                pass

        # Phase 2: dialog construction (also takes time on big carts -
        # building 100+ row tables, scanning blobs, etc.)
        dlg = cls(crt, parent=parent)
        # Phase 3: show the dialog. We tear the splash down ONLY after
        # the dialog has been shown and painted, so the user always
        # has something on screen during the transition.
        dlg.show()
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.processEvents()
        except Exception:
            pass
    finally:
        if splash is not None:
            try:
                splash.close()
            except Exception:
                pass

    return dlg
