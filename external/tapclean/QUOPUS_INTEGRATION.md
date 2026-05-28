# TAPClean integration in Quopus

This directory holds the GPL **TAPClean** source (see `README.md`
for the upstream project). Quopus Commander's TAP toolkit calls
the compiled `tapclean` binary for accurate loader identification
and PRG extraction (all ~93 loader scanners).

## Building

Quopus builds it automatically on first use if `gcc`/`cc` + `make`
are present. Manual build:

```sh
cd src && make
```

Produces `src/tapclean` (`src/tapclean.exe` on Windows/MinGW).
Compiled binary and `.o` files are not committed - built locally.
On Windows without a compiler, drop a prebuilt `tapclean.exe` into
`src/`.

## Fallback

If TAPClean can't be built/run, the toolkit falls back to the
built-in Python analyzer (`quopus_lib/tap_analyzer.py`).

TAPClean is GPL v2; Quopus invokes it as a separate process.
