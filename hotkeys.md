# Quopus Commander — Hotkey & Key Binding Reference

Complete keyboard and mouse reference. Most key bindings follow Total Commander / Norton Commander conventions so existing muscle memory carries over.

For per-button custom hotkeys (your own `Ctrl+Shift+P` style bindings), see the **Custom hotkeys via action buttons** section at the bottom.

---

## Table of contents
- [Conventions](#conventions)
- [Function keys (F1–F10)](#function-keys-f1f10)
- [Shift + F-keys](#shift--f-keys)
- [Alt + F-keys](#alt--f-keys)
- [Alt + letter combos](#alt--letter-combos)
- [Ctrl + letter combos](#ctrl--letter-combos)
- [Ctrl + F-keys (sort / view modes)](#ctrl--f-keys-sort--view-modes)
- [Ctrl + special keys](#ctrl--special-keys)
- [Numpad — Norton-style tagging](#numpad--norton-style-tagging)
- [Lister-local keys](#lister-local-keys)
- [Mouse actions — lister](#mouse-actions--lister)
- [Mouse actions — drive bar](#mouse-actions--drive-bar)
- [Action button modifier layers](#action-button-modifier-layers)
- [Module Player (MOD/XM/S3M/IT)](#module-player-modxms3mit)
- [SID Player](#sid-player)
- [Telnet / Raw TCP / SSH terminal](#telnet--raw-tcp--ssh-terminal)
- [Telegram client](#telegram-client)
- [IRC client](#irc-client)
- [U64 Streamer (Ultimate 64 viewer)](#u64-streamer-ultimate-64-viewer)
- [Memory Viewer / Cheat Engine](#memory-viewer--cheat-engine)
- [Custom hotkeys via action buttons](#custom-hotkeys-via-action-buttons)
- [Mac users](#mac-users)

---

## Conventions
- `Ctrl` on Mac is shown as-is but Quopus maps it to `Cmd` automatically. Hold the printed key combo — it just works.
- `Alt+F4` is the OS quit shortcut on Windows/Linux; on Mac use `Cmd+Q`.
- `Backspace` and `Del` are distinct: `Backspace` navigates to parent directory; `Del` deletes the selection.
- Combos with a `/` separator mean "either works" (e.g. `Ctrl+Right / N`).
- Wherever Quopus says "active panel", that's whichever lister has the highlighted (red/yellow) title bar. Toggle with `Tab`.

---

## Function keys (F1–F10)

Classic Norton / Total Commander row.

| Key | Action | Notes |
|-----|--------|-------|
| `F1` | Help / README | Opens the README in the internal text viewer |
| `F2` | Refresh both listers | Same as `Ctrl+R` |
| `F3` | **View** | Auto-detects type: text, hex, image, archive, AmigaGuide, etc. |
| `F4` | **Edit** | Opens via configured editor (Tools → Config → Editor) |
| `F5` | **Copy** | Selection → opposite panel |
| `F6` | **Move** | Selection → opposite panel |
| `F7` | **Makedir** | Create directory in active panel |
| `F8` | **Delete** | Recycle bin where supported |
| `Del` | Delete | Alias for F8 |
| `F9` | Hex view | Force hex viewer regardless of file type |
| `F10` | **Config menu** | Tools / Settings / Buttons / etc. |

---

## Shift + F-keys

| Key | Action |
|-----|--------|
| `Shift+F4` | Create new empty text file + open in editor (prompts for name) |
| `Shift+F5` | Copy with new name in **same** directory |
| `Shift+F6` | **Inline rename** of current row (in-place LineEdit overlay) |
| `Shift+F10` | Context menu of current row (same as right-click) |
| `Shift+Del` | **Permanent delete** — no recycle bin |

---

## Alt + F-keys

| Key | Action |
|-----|--------|
| `Alt+F1` | Drive picker for **left** lister |
| `Alt+F2` | Drive picker for **right** lister |
| `Alt+F3` | Open with system default app (Windows shell `start`, Linux `xdg-open`, macOS `open`) |
| `Alt+F4` | **Exit** Quopus |
| `Alt+F5` | **Pack** (create archive, threaded) |
| `Alt+F7` | **Find Files** dialog — name/text/hex search, recursive, threaded |
| `Alt+F9` | **Unpack** (extract archive) |
| `Alt+F10` | Archive sub-menu (pack/extract chooser) |
| `Alt+F11` | **Compare** two files (text/hex side-by-side diff) |
| `Alt+Enter` | Info / properties dialog |

---

## Alt + letter combos

| Key | Action |
|-----|--------|
| `Alt+F` | **FILE_ID.DIZ preview** toggle |
| `Alt+U` | **U64 Streamer** — Ultimate 64 live VIC video + audio over LAN |

---

## Ctrl + Alt + letter

| Key | Action |
|-----|--------|
| `Ctrl+Alt+<letter/digit>` | **Quick-Filter** (Total Commander style) — when the file list has focus, start an inline filter showing only entries whose name begins with the typed character; keep typing to narrow, `Backspace` edits / closes, `Esc` cancels, `Enter` opens the highlighted entry. Any directory change resets the filter. On a German keyboard `AltGr+<letter>` is the same chord and works too. |

---

## Ctrl + letter combos

Alphabetical.

| Key | Action |
|-----|--------|
| `Ctrl+A` | Select all entries in active panel |
| `Ctrl+B` | **Branch view** — flat list of all files in subtrees of current dir. `Ctrl+B` again to exit |
| `Ctrl+C` | **Clipboard copy** — real file URLs, paste in Explorer / Files works |
| `Ctrl+D` | **Directory hotlist** — bookmarks dialog |
| `Ctrl+F` | **FTP connect** |
| `Ctrl+H` | **Hunt** (Find) — opens find files dialog |
| `Ctrl+I` | **Invert tags** in active panel |
| `Ctrl+L` | **Get sizes** — compute folder sizes recursively |
| `Ctrl+M` | **Multi-rename tool** — batch rename with regex/templates |
| `Ctrl+N` | **New FTP connection** |
| `Ctrl+Q` | **Quick view** — show preview in opposite panel without opening a viewer dialog |
| `Ctrl+R` | Refresh both listers |
| `Ctrl+S` | **Search filter** — tag entries matching a wildcard |
| `Ctrl+T` | **Cycle action button layer** — main → Shift → Shift+Alt → main |
| `Ctrl+U` | **Swap** sides — left↔right panel contents and paths |
| `Ctrl+V` | Clipboard paste — copy or move depending on cut flag |
| `Ctrl+X` | Clipboard cut |
| `Ctrl+Z` | Edit file `.comment` sidecar |

---

## Ctrl + F-keys (sort / view modes)

| Key | Action |
|-----|--------|
| `Ctrl+F1` | Brief view (multiple columns of names) |
| `Ctrl+F2` | Details view (one row per file, all columns) |
| `Ctrl+F3` | Sort by **name** |
| `Ctrl+F4` | Sort by **extension** |
| `Ctrl+F5` | Sort by **date** |
| `Ctrl+F6` | Sort by **size** |

Each sort key click toggles ascending/descending. The sort indicator (↑/↓) appears in the column header.

---

## Ctrl + special keys

| Key | Action |
|-----|--------|
| `Ctrl+Space` | Toggle tag on current row |
| `Ctrl+\\` | Go to **root** of current drive |
| `Ctrl+PgUp` | Parent directory (alias for `Backspace`) |
| `Ctrl+Enter` | Copy filename (basename only) to clipboard |
| `Ctrl+Shift+Enter` | Copy **full path** to clipboard |
| `Ctrl+Shift+F` | FTP **disconnect** |
| `Ctrl+Left` | Send current path to **left** lister |
| `Ctrl+Right` | Send current path to **right** lister |
| `Backspace` | Parent directory |

---

## Numpad — Norton-style tagging

These are the classic tag-by-wildcard shortcuts from Norton Commander / Total Commander.

| Key | Action |
|-----|--------|
| `Num +` | **Tag** files by wildcard (e.g. `*.lha`, `*.[ch]`) |
| `Num -` | **Untag** files by wildcard |
| `Num *` | **Invert** all tags |

Tag highlighting (orange background) stays visible on both panels even when they lose focus. Tags are per-panel and per-session — not persisted across restarts.

---

## Lister-local keys

Active only when a lister has keyboard focus.

| Key | Action |
|-----|--------|
| `Tab` | Switch to other lister (toggle active panel) |
| `Space` | Toggle tag on current row, **move down one** |
| `Enter` | Enter directory / open file via configured viewer |
| `Backspace` | Parent directory (same as `Ctrl+PgUp`) |
| `Esc` | Disconnect remote FTP lister (when on an FTP path) |
| `Home` / `End` | Jump to first / last entry |
| `PgUp` / `PgDn` | Page navigation |
| Type-to-find | Start typing letters → cursor jumps to entry starting with those letters |

---

## Mouse actions — lister

| Action | Function |
|--------|----------|
| **Left-click** | Select / move cursor |
| **Shift+Click** | Range-select (extend selection) |
| **Ctrl+Click** | Toggle item in selection |
| **Left-drag** | Drag & Drop. Default = Copy, hold `Shift` = Move |
| **Right-click** | Context menu (incl. Size column bytes/blocks toggle) |
| **Middle-click** | **Jump to parent directory** — anywhere in lister body |
| **Double-click** | Open file / enter directory |
| **Shift+Double-click** | Open file's `.comment` sidecar (offer to create one if missing) |
| **Right-click column header** | Column-specific menu (sort / reverse / display mode) |
| **Drag column boundary** | Resize column (width persists) |
| **Click column header** | Sort by column (click again to reverse) |

---

## Mouse actions — drive bar

The drive button column on the left side of each lister.

| Action | Function |
|--------|----------|
| **Click** | Navigate **active panel** (default — configurable per-bookmark to active/both/left/right) |
| **Shift+Click** | Navigate **BOTH** panels at once |
| **Middle-click** | Navigate **right** panel only |
| **Right-click** | Drive button context menu (Edit / Remove / Properties) |

---

## Action button modifier layers

The 6×6 action button grid below the listers has **three layers** that swap based on which modifier you're holding:

| Modifier state | Layer shown |
|---|---|
| _(none)_ | **Main** layer — 36 actions |
| Hold `Shift` | **Shift** layer — 36 different actions |
| Hold `Shift+Alt` | **Shift+Alt** layer — 36 more actions |

Effective capacity: **108 actions** across the three layers.

A global event filter watches modifier state. Auto-repeat key events are filtered out so the layer stays steady while keys are held.

| Key | Action |
|-----|--------|
| `Ctrl+T` | **Cycle layer persistently** — `main → shift → shift_alt → main`. Layer stays where you cycled it after releasing modifiers, until next `Ctrl+T`. The status bar shows the active layer name when not on main. |

---

## Module Player (MOD/XM/S3M/IT)

When the Module Player dialog has focus.

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `Esc` | Close (stops audio cleanly) |
| `Left` | Skip back 5 seconds |
| `Right` | Skip forward 5 seconds |
| `Ctrl+Right` / `N` | Next track (shuffle mode) |
| `Ctrl+Left` / `P` | Previous track (shuffle mode) |

---

## SID Player

When the SID Player dialog has focus.

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `Esc` | Close (stops audio cleanly) |
| `Right` / `N` / `+` | Next subsong (clamps at max) |
| `Left` / `P` / `-` | Previous subsong (clamps at 1) |
| `Ctrl+Right` / `Ctrl+N` | Next track (shuffle mode) |
| `Ctrl+Left` / `Ctrl+P` | Previous track (shuffle mode) |

---

## Telnet / Raw TCP / SSH terminal

When the terminal widget has focus.

### General

| Key | Action |
|-----|--------|
| Standard typing | Forwarded to the connection |
| `Ctrl+C`, `Ctrl+D`, `Ctrl+Z` | Forwarded to the remote, **not** local Qt shortcuts |
| `Ctrl+Shift+C` | **Local copy** — copies marked text without sending `^C` |
| `Ctrl+Shift+V` | **Local paste** — pastes clipboard text into the connection |
| `Esc` | **Sent to remote** — does **not** close the dialog |

To disconnect, click the toolbar Close button or use the window's close button (X).

### PETSCII cursor codes

When the screen is in PETSCII mode, arrow keys are translated to native C64 single-byte codes rather than ANSI CSI sequences. BBSes that read cursor traffic react correctly.

| Key | C64 byte | Function |
|-----|----------|----------|
| `Up` | `$91` | Cursor up |
| `Down` | `$11` | Cursor down |
| `Right` | `$1D` | Cursor right |
| `Left` | `$9D` | Cursor left |
| `Home` | `$13` | HOME (top-left, no clear) |
| `Shift+Home` | `$93` | CLR (clear screen + home) |
| `Backspace` / `Del` | `$14` (configurable) | INST/DEL |

### PETSCII case-swap on send

In PETSCII mode, characters typed on a PC keyboard are translated through `ascii_to_petscii(..., mode="mixed")`:

| You type | Sent byte | Shows on C64 as |
|----------|-----------|----------------|
| `a` | `$41` | lowercase `a` |
| `A` (Shift+a) | `$C1` | uppercase `A` |

Without this translation the BBS sees raw ASCII and renders the case inverted. The translation happens transparently — you type normally.

### Resizing

The Telnet dialog is freely resizable in both directions. As you drag the window edges, the terminal cell metrics rescale so the character grid fills the available area. PETSCII cells stretch with arbitrary per-pixel scaling; ANSI scales the font size to fit. Status bar always stays visible at the bottom.

---

## Telegram client

When the Telegram dialog has focus.

| Action | Function |
|--------|----------|
| Click chat in left pane | Open chat (instant if cached, else fetches latest batch from server) |
| Type in compose box, `Enter` | Send message to active chat |
| `Shift+Enter` in compose box | Insert newline without sending |
| "Colors..." button | Open bubble color editor (own messages vs incoming) |
| Archive toggle | Show / hide archived chats |
| Click on photo / document thumbnail | Download attachment |

The disk-persistent chat cache means the message list opens instantly from local JSON on the second visit and after restarts. No re-fetching on tab switch unless the worker thread receives new messages from Telegram itself.

---

## IRC client

When the IRC client dialog has focus.

| Action | Function |
|--------|----------|
| Click server / channel tab | Switch active buffer |
| Type message, `Enter` | Send to active channel / DM |
| `/join #channel` | Join a channel |
| `/me action` | CTCP ACTION (the classic `* nick action` line) |
| `/msg nick text` | Send private message |
| `/quit reason` | Disconnect from current server |
| `/raw COMMAND args` | Send a raw IRC line |
| `/part [reason]` | Leave the current channel |
| `/topic [new topic]` | View or change channel topic |
| `/kick nick [reason]` | Kick user (if op) |
| `/notice nick text` | Send a notice |
| `/ctcp nick COMMAND` | Send CTCP query |

Color/formatting bytes in incoming messages render automatically (mIRC `^C`, `^B`, `^U`, `^I`). Outgoing color is via the format-button picker.

---

## U64 Streamer (Ultimate 64 viewer)

When the U64 viewer window has focus, **keyboard input is forwarded to the C64**.

| Key | C64 function |
|-----|--------------|
| `A`–`Z`, `0`–`9`, punctuation | Plain key |
| `Enter` | RETURN |
| `Backspace` | INST/DEL |
| `Space` | SPACE |
| Arrow keys | Cursor keys |
| `Esc` | RUN/STOP |
| `Tab` | C= (Commodore key) |
| `Home` | CLR/HOME |
| `Insert` | INST (Shift+INST/DEL) |
| `F1` ... `F8` | C64 function keys F1-F8 (F2/F4/F6/F8 via Shift+F-key) |

To regain focus elsewhere in Quopus, click outside the U64 video pane. The toolbar buttons (Reset, Reboot, Pause, Rec) capture clicks but **not** key input.

---

## Memory Viewer / Cheat Engine

When the Memory Viewer window has focus.

| Key | Action |
|-----|--------|
| `Ctrl+F` | Focus the search bar |
| `Enter` in search bar | Run search step |
| `Esc` | Close the viewer |
| Click hex cell | Edit value in-place |
| Right-click candidate value | Remove from list |
| Click HEX/ASM toggle | Switch between hex dump and disassembly |
| Click cell + "Find refs" | Find what reads/writes this address |

---

## Custom hotkeys via action buttons

Beyond the built-in keys above, **any action button** can have its own hotkey. Right-click a button → set hotkey via either:

1. **Dropdown of 61 built-in combos** — pick from a list of unbound or unused Quopus shortcuts. The dropdown shows `<combo>  (<action>)` so you can see what each does. Picking e.g. `Alt+U` auto-fills Action = `u64view`.
2. **Free-form combo** — type any modifier+key combination Quopus doesn't already use (e.g. `Ctrl+Shift+P`, `F12`, `Alt+0`). Saved as a global `QShortcut` and rebuilt on every button-grid change.

Custom-bound buttons are guaranteed to do the **same thing** as a key press: button clicks and the assigned hotkey both fire through the same dispatcher. No logic duplication.

The hotkey survives:
- Switching action button layers (the binding is grid-position-aware)
- Editing other cells
- Restarting Quopus (persisted in `quopus.cfg`)

Removing a custom hotkey: clear the hotkey field in the button edit dialog (right-click → Edit).

---

## Mac users

Quopus runs natively on macOS. Keyboard mapping:

- `Cmd` is treated as the Quopus `Ctrl` modifier (Qt does this automatically). So `Ctrl+C` in the README means `Cmd+C` on Mac.
- `Cmd+Q` quits the app — `Alt+F4` doesn't exist on Mac.
- `Cmd+,` is the standard "preferences" combo and is unused in Quopus, so it's free for custom bindings.
- Function keys `F1`-`F12` work the same as on Windows / Linux. On laptops you may need to press `Fn` simultaneously depending on your macOS keyboard settings.

---

*Last updated: 2026-06-04*

*If a hotkey isn't behaving as documented, check whether you have an action button on the same combo — custom button hotkeys override the built-in dispatch.*
