# Drop fonts here

Place `.ttf` or `.otf` files in this folder; they load automatically on startup.

## Recommended

1. **C64 Pro Mono** — https://style64.org/c64-truetype
   For pixel-perfect PETSCII. Covers all 256 screencodes in Unicode PUA:
   - U+E100-U+E1FF = uppercase/graphics charset
   - U+E200-U+E2FF = lowercase/uppercase charset

2. **Topaz** (Amiga font) — search aminet for "topaz ttf" or
   github.com/rewtnull/amigafonts

Without these, the UI uses Courier New and PETSCII falls back to
Unicode box-drawing approximation.
