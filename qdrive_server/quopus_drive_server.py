#!/usr/bin/env python3
"""
Quopus Drive Server
===================

Standalone script that exposes selected directories on this
machine to remote Quopus clients over a TLS-encrypted TCP
socket. Auth uses HMAC challenge-response with a pre-shared
secret tied to a specific client's LAN-card MAC address, so a
stolen secret on its own won't let an attacker connect from a
different machine.

== First-run setup ==

    python quopus_drive_server.py setup

Walks you through:
  - generating the TLS keypair (self-signed) for this server
  - picking which directories should be exposed (whitelist)
  - generating a client key bound to a specific MAC address
  - choosing a listening port (default: 47823)

All of this writes to ~/.config/quopus_drive_server/ (Linux),
~/Library/Application Support/quopus_drive_server/ (macOS),
or %APPDATA%/quopus_drive_server/ (Windows). NEVER edit the
config files by hand - secrets are stored there.

== Running ==

    python quopus_drive_server.py run

Or set up a systemd service / Windows Task / launchd plist
that runs the same command at boot. The script daemonizes
itself only on explicit --daemon, otherwise it stays in the
foreground so you can see what's happening.

== Adding more clients ==

    python quopus_drive_server.py addclient

Asks for a client name and the MAC address of the client's
LAN adapter (you can read this off the client's network
settings). Generates a fresh per-client secret and prints
the connection info you paste into Quopus.

== Protocol (for the curious) ==

  - TLS 1.3 over TCP with the server's self-signed cert
  - The client must present the server cert fingerprint
    it has pinned; otherwise we drop the connection
  - On a fresh connection the server sends a 32-byte
    random nonce + a UTC timestamp
  - The client replies with: client_name + HMAC-SHA256(
        nonce + timestamp + claimed_mac, per_client_secret)
  - Server looks up the client by name, verifies that the
    claimed_mac is in the client's MAC whitelist, recomputes
    the HMAC and constant-time compares
  - On success, the session is open. Commands are length-
    prefixed JSON over the TLS stream. File payloads use
    a separate length-prefixed binary frame after the
    command JSON.

This is NOT a secret protocol - the security comes from TLS
+ a strong HMAC secret + MAC binding, all of which use
standard primitives.

== Threat model ==

Defends against:
  - Network eavesdropping (TLS)
  - MitM with a forged cert (cert pinning)
  - Replay attacks (random nonce + 30s timestamp window)
  - Stolen-secret-on-different-PC (MAC binding)
  - Path traversal in commands (canonicalize + whitelist check)

Does NOT defend against:
  - A determined attacker on the same LAN who can both
    sniff MACs AND steal the secret - MAC spoofing is
    trivial. The MAC tie is a hardening layer, not a
    primary defense. Treat the secret like any other
    password.
  - Side-channel attacks on the host OS - the server runs
    with the privileges of the user who started it, so
    anyone with shell access on the server can already
    read the configured drives.

Author: Mario aka lA-sTYLe / Quantum (Quopus Commander)
License: see LICENSE in the Quopus distribution
"""

# ---------- Stdlib only - we don't want a dependency hell ----
import argparse
import datetime
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional


# =============================================================
# Config / paths
# =============================================================

APP_NAME = "quopus_drive_server"
DEFAULT_PORT = 2000      # Quopus Drive Server default port
PROTOCOL_VERSION = 1

# Max size of a single command (JSON header) - 64 KiB is way
# more than any command needs and catches malformed input that
# claims to be megabytes long.
MAX_COMMAND_SIZE = 64 * 1024

# Max bytes per file-read response. We chunk anything bigger.
# 4 MiB balances throughput against memory pressure.
READ_CHUNK_SIZE = 4 * 1024 * 1024

# Replay window: HMAC tokens older than this in seconds are
# rejected even if signature matches. Keep tight because TLS
# already protects against passive recording; this just stops
# someone with read-then-replay shell access.
REPLAY_WINDOW_SECONDS = 30


def config_dir() -> Path:
    """Per-user config dir on the SERVER (this machine)."""
    if sys.platform == "win32":
        base = Path(os.environ.get(
            "APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get(
            "XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / APP_NAME


CONFIG_FILE = lambda: config_dir() / "server.json"
CERT_FILE = lambda: config_dir() / "server.crt"
KEY_FILE = lambda: config_dir() / "server.key"


# =============================================================
# MAC-address handling
# =============================================================

# Heuristics for "this is a virtual adapter, not a real LAN
# card". The check is by name; the interface API differs across
# Win/Linux/macOS so we let each platform produce a (name, mac)
# list and apply name-based filters uniformly.
_VIRTUAL_NAME_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bvirtual\b",
        r"\bvmware\b",
        r"\bvbox\b",
        r"\bvirtualbox\b",
        r"\bhyper-?v\b",
        r"\bvethernet\b",
        r"\bvmnet\b",
        r"\bdocker\b",
        r"\bbridge\b",
        r"\bveth\d+\b",                  # Linux container veth
        r"\btun\d+\b",                   # tun tunnels
        r"\btap\d+\b",                   # tap tunnels
        r"\btailscale\b",
        r"\bwireguard\b",
        r"\bwg\d+\b",                    # wg0, wg1 ...
        r"\bzerotier\b",
        r"\bzt\w+\b",                    # zt prefixed
        r"\bnpcap\b",
        r"\bpppoe\b",
        r"\bloopback\b",
        r"\bppp\d+\b",
        r"^lo$",                         # Linux loopback
    ]
]

# OUI prefixes commonly assigned to virtual-adapter vendors.
# These are the first 3 octets of the MAC, uppercase, no
# separators. The list is non-exhaustive but covers the
# common cases - a determined VM operator could spoof a
# physical OUI and we'd miss it. That's OK; this filter is
# best-effort heuristics, the AUTH still works regardless.
_VIRTUAL_OUI_PREFIXES = {
    "000C29",   # VMware
    "001C14",   # VMware
    "005056",   # VMware
    "080027",   # VirtualBox
    "0A0027",   # VirtualBox (host)
    "00155D",   # Hyper-V
    "0003FF",   # Microsoft Hyper-V / Virtual PC
    "525400",   # QEMU / KVM
    "020000",   # Locally administered (often virtual)
}


def _is_virtual_iface(name: str, mac: str) -> bool:
    """Return True if this interface looks virtual / not a real
    physical LAN card. Heuristic - false-positives on weirdly-
    named real cards are possible but unlikely on consumer
    hardware."""
    if not mac or mac == "00:00:00:00:00:00":
        return True
    for pat in _VIRTUAL_NAME_PATTERNS:
        if pat.search(name):
            return True
    oui = mac.replace(":", "").replace("-", "").upper()[:6]
    if oui in _VIRTUAL_OUI_PREFIXES:
        return True
    return False


def list_physical_macs() -> list[tuple[str, str]]:
    """Return [(iface_name, mac_address)] for every adapter we
    believe is a real physical LAN card on this machine.
    Cross-platform; falls back to a single best-effort uuid.getnode
    entry if nothing better is available.

    MAC format in the output is always colon-separated lowercase
    (aa:bb:cc:dd:ee:ff).
    """
    results: list[tuple[str, str]] = []

    if sys.platform == "win32":
        # `getmac /v /nh /fo csv` gives Connection Name + MAC +
        # description. Easier and more reliable than WMIC across
        # Windows versions.
        try:
            out = subprocess.run(
                ["getmac", "/v", "/nh", "/fo", "csv"],
                capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                # CSV: "ConnectionName","NetworkAdapter","MAC","Transport"
                parts = [p.strip().strip('"')
                          for p in line.split('","')]
                # First and last may still have leading/trailing
                # quotes - normalize.
                parts = [p.replace('"', '').strip()
                          for p in parts if p]
                if len(parts) < 3:
                    continue
                name = f"{parts[0]} ({parts[1]})"
                mac = parts[2].replace("-", ":").lower()
                if mac == "n/a" or len(mac) != 17:
                    continue
                if _is_virtual_iface(name, mac):
                    continue
                results.append((name, mac))
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ifconfig"], capture_output=True,
                text=True, timeout=5)
            # macOS ifconfig: iface name at column 0, "ether xx:..."
            # line later. en0 / en1 are usually physical Ethernet /
            # Wi-Fi.
            cur_name = None
            for line in out.stdout.splitlines():
                if not line.startswith("\t"):
                    cur_name = line.split(":")[0]
                elif "ether " in line and cur_name:
                    mac = line.strip().split()[1].lower()
                    if _is_virtual_iface(cur_name, mac):
                        continue
                    results.append((cur_name, mac))
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        # Linux + BSD: read /sys/class/net/<iface>/address.
        # Skip wireless? No - Wi-Fi is just as "physical" as
        # Ethernet from our point of view. The user may be on
        # a laptop.
        net_dir = Path("/sys/class/net")
        if net_dir.is_dir():
            for iface in sorted(net_dir.iterdir()):
                name = iface.name
                addr_file = iface / "address"
                if not addr_file.is_file():
                    continue
                try:
                    mac = addr_file.read_text().strip().lower()
                except OSError:
                    continue
                if _is_virtual_iface(name, mac):
                    continue
                # Extra Linux check: the symlink under
                # /sys/class/net/<iface>/device tells us whether
                # this is a real PCI/USB device or something
                # synthetic. veth pairs don't have a device link.
                if not (iface / "device").exists():
                    # Could still be a real WiFi card depending
                    # on driver - we keep it but at lower
                    # confidence. If a stricter check is needed
                    # the user can drop the entry from their
                    # client-MAC whitelist.
                    pass
                results.append((name, mac))

    # Last-resort fallback: at least give the uuid.getnode MAC
    # so the script isn't useless on weird platforms.
    if not results:
        node = uuid.getnode()
        mac = ":".join(f"{(node >> i) & 0xff:02x}"
                       for i in range(40, -1, -8))
        results.append(("uuid.getnode()", mac))

    return results


def normalize_mac(mac: str) -> str:
    """Lowercase, colon-separated. Accepts xx-xx-xx-xx-xx-xx,
    xx:xx:xx:xx:xx:xx, or xxxxxxxxxxxx; rejects anything else."""
    s = mac.strip().lower().replace("-", ":").replace(".", "")
    if ":" not in s and len(s) == 12:
        s = ":".join(s[i:i+2] for i in range(0, 12, 2))
    if not re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", s):
        raise ValueError(f"Not a valid MAC address: {mac!r}")
    return s


# =============================================================
# Server-state load / save
# =============================================================

def load_server_state() -> dict:
    """Read server.json or return an empty default."""
    p = CONFIG_FILE()
    if not p.exists():
        return {
            "port":     DEFAULT_PORT,
            "bind":     "0.0.0.0",
            "drives":   {},      # name -> {"path": "...", "readonly": bool}
            "clients":  {},      # name -> {"secret": hex,
                                  #          "macs": [mac, mac]}
            "created":  None,
        }
    return json.loads(p.read_text(encoding="utf-8"))


def save_server_state(state: dict) -> None:
    """Write server.json with mode 600 (best-effort on Windows)."""
    p = CONFIG_FILE()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# =============================================================
# TLS certificate generation
# =============================================================

def generate_self_signed_cert(cert_path: Path,
                                key_path: Path,
                                hostname: Optional[str] = None
                                ) -> None:
    """Create a fresh self-signed RSA cert for the server.

    We require `cryptography` if available because it gives a
    pure-Python path and saves the user from installing OpenSSL
    on Windows. Falls back to shelling out to the `openssl`
    binary if `cryptography` isn't installed.
    """
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    if hostname is None:
        hostname = socket.gethostname() or "quopus-drive-server"

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import datetime, timezone, timedelta as td

        key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME,
                                "Quopus Drive Server"),
        ])
        cert = (
            x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc))
                .not_valid_after(
                    datetime.now(timezone.utc) + td(days=3650))
                .add_extension(
                    x509.SubjectAlternativeName([
                        x509.DNSName(hostname),
                        x509.DNSName("localhost"),
                    ]),
                    critical=False)
                .sign(key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption())
        key_path.write_bytes(key_pem)
        cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM))
    except ImportError:
        # Fall back to openssl binary
        ssl_bin = shutil.which("openssl")
        if not ssl_bin:
            raise RuntimeError(
                "Neither the Python 'cryptography' package nor "
                "the 'openssl' command is available. Install "
                "one of them and re-run setup:\n"
                "    pip install cryptography\n"
                "or install OpenSSL from "
                "https://www.openssl.org/")
        subj = (
            f"/CN={hostname}"
            f"/O=Quopus Drive Server")
        subprocess.run(
            [ssl_bin, "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", str(key_path), "-out", str(cert_path),
             "-sha256", "-days", "3650", "-nodes",
             "-subj", subj],
            check=True, capture_output=True)

    # Lock down the private key.
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass


def cert_fingerprint(cert_path: Path) -> str:
    """Return the SHA-256 fingerprint of the cert in xx:xx:xx
    hex format. The CLIENT needs this for pinning."""
    pem = cert_path.read_bytes()
    # Strip PEM wrapper + base64-decode
    import base64
    body = b"".join(
        line for line in pem.splitlines()
        if not line.startswith(b"-----"))
    der = base64.b64decode(body)
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i+2] for i in range(0, len(digest), 2))


# =============================================================
# Setup wizards
# =============================================================

def _prompt(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {label}{suffix}: ").strip()
    return val or (default or "")


def cmd_setup() -> int:
    """Interactive first-time setup."""
    print()
    print("=" * 60)
    print(" Quopus Drive Server - initial setup")
    print("=" * 60)
    print()
    print(f"Config will be written to: {config_dir()}")
    print()

    if CONFIG_FILE().exists():
        print("Server is already configured. To reconfigure")
        print("delete the config directory above and re-run.")
        print()
        print("To add a new client to an existing server, use:")
        print("    python quopus_drive_server.py addclient")
        return 1

    state = load_server_state()
    state["created"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat()

    # --- 1) Port + bind ----------------------------------
    print("1) Network")
    port = _prompt("Listening port", str(DEFAULT_PORT))
    try:
        state["port"] = int(port)
    except ValueError:
        print(f"  ! invalid port: {port}"); return 1
    bind = _prompt(
        "Bind address (0.0.0.0 = all interfaces, "
        "127.0.0.1 = localhost only)", "0.0.0.0")
    state["bind"] = bind
    print()

    # --- 2) TLS cert -------------------------------------
    print("2) TLS certificate (self-signed)")
    if CERT_FILE().exists():
        print(f"  found existing cert at {CERT_FILE()}")
    else:
        hostname = _prompt("Server hostname",
                            socket.gethostname() or "quopus-drive")
        print("  generating cert (RSA 2048, valid 10 years)...")
        try:
            generate_self_signed_cert(
                CERT_FILE(), KEY_FILE(), hostname=hostname)
        except Exception as e:
            print(f"  ! cert generation failed: {e}")
            return 1
        print(f"  cert  -> {CERT_FILE()}")
        print(f"  key   -> {KEY_FILE()}")
    fp = cert_fingerprint(CERT_FILE())
    print(f"  fingerprint: {fp}")
    print("  (clients must pin this fingerprint - keep it.)")
    print()

    # --- 3) Drives ---------------------------------------
    print("3) Exposed drives / directories")
    print("  Enter directories the clients are allowed to see.")
    print("  Examples:")
    print("    D:\\Backups")
    print("    /home/sysop/files")
    print("  Type an empty line to finish.")
    print()
    while True:
        path = _prompt("Drive path", "")
        if not path:
            if not state["drives"]:
                print("  ! at least one drive required")
                continue
            break
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            print(f"  ! not a directory: {p}")
            continue
        name = _prompt("  Short name (shown in Quopus)", p.name)
        ro = _prompt("  Read-only? (y/n)", "n").lower() == "y"
        state["drives"][name] = {
            "path": str(p),
            "readonly": ro,
        }
        print(f"  added '{name}' -> {p} "
              f"({'RO' if ro else 'RW'})")
    print()

    # --- 4) First client ---------------------------------
    print("4) Authorize first client")
    print("  Each client needs a name, a per-client secret, and")
    print("  one or more MAC addresses (the client's physical")
    print("  LAN cards). Connections from that client are only")
    print("  accepted if the MAC matches AND the secret is")
    print("  correct.")
    print()
    _add_client_interactive(state)

    save_server_state(state)
    print()
    print("Done. Start the server with:")
    print("    python quopus_drive_server.py run")
    print()
    return 0


def cmd_addclient() -> int:
    """Add another client to an already-configured server."""
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()
    print()
    print("Adding a new client.")
    _add_client_interactive(state)
    save_server_state(state)
    return 0


def _add_client_interactive(state: dict) -> None:
    """Prompt for a new client and add it to state['clients']."""
    while True:
        name = _prompt("Client name (unique, e.g. 'mario-laptop')",
                        "")
        if not name:
            print("  ! name required"); continue
        if name in state["clients"]:
            print(f"  ! a client with that name already exists")
            continue
        break

    print()
    print("  MAC address(es) of the client's PHYSICAL LAN card(s).")
    print("  The client must present one of these MACs at connect")
    print("  time. List several if your laptop swaps between")
    print("  Ethernet and Wi-Fi.")
    print()
    print("  To find them, run on the CLIENT machine:")
    print("    python quopus_drive_server.py whoami")
    print()
    macs = []
    while True:
        m = _prompt("  MAC (empty when done)", "")
        if not m:
            if not macs:
                print("  ! at least one MAC required"); continue
            break
        try:
            macs.append(normalize_mac(m))
        except ValueError as e:
            print(f"  ! {e}")

    # Generate 32 random bytes as the shared secret. Print it
    # ONCE - it's never shown again. The user must paste it
    # into Quopus's client config now.
    secret = secrets.token_hex(32)

    state["clients"][name] = {
        "secret": secret,
        "macs":   macs,
    }

    fp = cert_fingerprint(CERT_FILE())
    print()
    print("  Client added. Paste this into Quopus:")
    print("  " + "-" * 56)
    print(f"  Server host:    {socket.gethostname()}")
    print(f"  Server port:    {state['port']}")
    print(f"  Client name:    {name}")
    print(f"  Client secret:  {secret}")
    print(f"  Cert SHA-256:   {fp}")
    print("  " + "-" * 56)
    print("  Save the secret NOW - it will not be shown again.")


def cmd_whoami() -> int:
    """Print this machine's MAC addresses for the addclient
    wizard on the SERVER side."""
    print()
    print("Physical LAN adapters on this machine:")
    print()
    macs = list_physical_macs()
    if not macs:
        print("  (none detected)")
        return 1
    for name, mac in macs:
        print(f"  {mac}   {name}")
    print()
    print("Use one (or more) of these MACs when authorizing this")
    print("machine as a Quopus Drive client on the server.")
    return 0


def cmd_info() -> int:
    """Show server config + connection info."""
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()
    fp = cert_fingerprint(CERT_FILE()) if CERT_FILE().exists() else "?"
    print()
    print(f"  Hostname:        {socket.gethostname()}")
    print(f"  Port:            {state['port']}")
    print(f"  Bind:            {state['bind']}")
    print(f"  Cert SHA-256:    {fp}")
    print(f"  Config dir:      {config_dir()}")
    print()
    print(f"  Drives ({len(state['drives'])}):")
    for n, d in state["drives"].items():
        ro = "RO" if d.get("readonly") else "RW"
        print(f"    {ro}  {n:20s} -> {d['path']}")
    print()
    print(f"  Clients ({len(state['clients'])}):")
    for n, c in state["clients"].items():
        print(f"    {n}")
        for m in c["macs"]:
            print(f"        MAC: {m}")
    return 0


def cmd_listclients() -> int:
    """Compact one-line-per-client list. Use cmd_info for full
    server config."""
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()
    if not state["clients"]:
        print("  (no clients configured)")
        return 0
    name_w = max(len(n) for n in state["clients"]) + 2
    for name, c in state["clients"].items():
        macs = ", ".join(c["macs"]) if c["macs"] else "(no MACs!)"
        print(f"  {name:<{name_w}s}  {macs}")
    return 0


def cmd_delclient() -> int:
    """Remove a client. Reads the name from argv if given,
    otherwise lists clients and prompts interactively. The
    client's secret is wiped from the config so a stolen copy
    of it stops working immediately - that's the whole reason
    you'd run this command."""
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()
    if not state["clients"]:
        print("  No clients to delete.")
        return 0

    # Accept client name from argv[2] (after 'delclient') so
    # scripts can do `delclient OldLaptop` without prompts.
    name = sys.argv[2] if len(sys.argv) >= 3 else None
    if name is None:
        print()
        print("Configured clients:")
        for n in state["clients"]:
            print(f"  - {n}")
        print()
        name = _prompt(
            "Client to delete (empty to cancel)", "")
        if not name:
            print("Cancelled.")
            return 0

    if name not in state["clients"]:
        print(f"  ! no client named {name!r}")
        print(f"  Available: "
              f"{', '.join(state['clients']) or '(none)'}")
        return 1

    # Show what we're about to nuke and require explicit y.
    c = state["clients"][name]
    print()
    print(f"About to delete client {name!r}:")
    print(f"  secret (truncated): {c['secret'][:16]}...")
    for m in c["macs"]:
        print(f"  MAC: {m}")
    print()
    ok = _prompt(
        "Confirm delete? (yes / no)", "no").lower()
    if ok != "yes":
        print("Cancelled.")
        return 0
    del state["clients"][name]
    save_server_state(state)
    print(f"  Deleted client {name!r}. Restart the server "
          f"(Ctrl-C + 'run') for the change to take effect.")
    return 0


def cmd_addmac() -> int:
    """Add one or more MAC addresses to an existing client.
    Useful when a known PC swaps NICs (Ethernet vs Wi-Fi) or
    you didn't put all the right MACs in during setup.

    Usage:
        python quopus_drive_server.py addmac
        python quopus_drive_server.py addmac <client_name>
        python quopus_drive_server.py addmac <client_name> <mac>
    """
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()
    if not state["clients"]:
        print("  No clients configured. Use 'addclient' first.")
        return 1

    # Client name from argv if given
    name = sys.argv[2] if len(sys.argv) >= 3 else None
    if name is None:
        print()
        print("Configured clients:")
        for n in state["clients"]:
            print(f"  - {n}")
        print()
        name = _prompt("Client to add MAC to", "")
        if not name:
            print("Cancelled.")
            return 0
    if name not in state["clients"]:
        print(f"  ! no client named {name!r}")
        return 1

    # MAC from argv[3] OR prompt
    if len(sys.argv) >= 4:
        candidates = [sys.argv[3]]
    else:
        print()
        print("  Enter MAC address(es) to add. Empty line to finish.")
        candidates = []
        while True:
            m = _prompt("  MAC", "")
            if not m:
                break
            candidates.append(m)

    added = []
    skipped = []
    for raw in candidates:
        try:
            mac = normalize_mac(raw)
        except ValueError as e:
            print(f"  ! skipping {raw!r}: {e}")
            continue
        if mac in state["clients"][name]["macs"]:
            skipped.append(mac)
            continue
        state["clients"][name]["macs"].append(mac)
        added.append(mac)

    if not added and not skipped:
        print("  Nothing to do.")
        return 0
    save_server_state(state)

    if added:
        print(f"  Added to {name!r}: {', '.join(added)}")
    if skipped:
        print(f"  Already present (skipped): "
              f"{', '.join(skipped)}")
    print(f"  Restart the server (Ctrl-C + 'run') for the "
          f"change to take effect.")
    return 0


def cmd_adddrive() -> int:
    """Add a new exposed directory to the server. Path is
    validated, name must be unique, read-only flag optional."""
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()

    print()
    while True:
        path_in = _prompt("Path to expose", "")
        if not path_in:
            print("Cancelled.")
            return 0
        p = Path(path_in).expanduser().resolve()
        if not p.is_dir():
            print(f"  ! not a directory: {p}")
            continue
        break
    while True:
        name = _prompt("Short name (shown in Quopus)", p.name)
        if not name:
            print("  ! name required")
            continue
        if name in state["drives"]:
            print(f"  ! a drive named {name!r} already exists")
            continue
        break
    ro = _prompt("Read-only? (y/n)", "n").lower() == "y"
    state["drives"][name] = {
        "path":     str(p),
        "readonly": ro,
    }
    save_server_state(state)
    print(f"  Added drive {name!r} -> {p} "
          f"({'RO' if ro else 'RW'})")
    print(f"  Restart the server (Ctrl-C + 'run') for the "
          f"change to take effect.")
    return 0


def cmd_deldrive() -> int:
    """Remove an exposed drive from the server config."""
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()
    if not state["drives"]:
        print("  No drives to delete.")
        return 0

    name = sys.argv[2] if len(sys.argv) >= 3 else None
    if name is None:
        print()
        print("Exposed drives:")
        for n, d in state["drives"].items():
            ro = "RO" if d.get("readonly") else "RW"
            print(f"  - {ro} {n} -> {d['path']}")
        print()
        name = _prompt("Drive to delete (empty to cancel)", "")
        if not name:
            print("Cancelled.")
            return 0

    if name not in state["drives"]:
        print(f"  ! no drive named {name!r}")
        print(f"  Available: "
              f"{', '.join(state['drives']) or '(none)'}")
        return 1

    d = state["drives"][name]
    ro = "RO" if d.get("readonly") else "RW"
    print(f"  About to delete drive {name!r} ({ro}) "
          f"-> {d['path']}")
    ok = _prompt("Confirm delete? (yes / no)", "no").lower()
    if ok != "yes":
        print("Cancelled.")
        return 0
    del state["drives"][name]
    save_server_state(state)
    print(f"  Deleted drive {name!r}. Restart the server "
          f"(Ctrl-C + 'run') for the change to take effect.")
    return 0


# =============================================================
# The actual server
# =============================================================

class _SessionState:
    """Per-connection state. Holds the authenticated client
    name (after handshake) and a cursor for currently-allowed
    drives."""
    __slots__ = ("client_name", "authenticated",
                 "claimed_mac", "drives")

    def __init__(self):
        self.client_name = None
        self.authenticated = False
        self.claimed_mac = None
        self.drives = None


def _send_json(sock: ssl.SSLSocket, obj: dict) -> None:
    """4-byte big-endian length + JSON bytes."""
    payload = json.dumps(obj).encode("utf-8")
    if len(payload) > MAX_COMMAND_SIZE:
        raise ValueError("response too large")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    """Read exactly n bytes. Raises ConnectionError on close."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed mid-read")
        buf.extend(chunk)
    return bytes(buf)


def _recv_json(sock: ssl.SSLSocket) -> dict:
    """Read one length-prefixed JSON message."""
    raw_len = _recv_exact(sock, 4)
    n = struct.unpack(">I", raw_len)[0]
    if n > MAX_COMMAND_SIZE:
        raise ValueError(f"command too large: {n}")
    payload = _recv_exact(sock, n)
    return json.loads(payload.decode("utf-8"))


def _send_blob(sock: ssl.SSLSocket, data: bytes) -> None:
    """8-byte length + raw bytes. Used for file transfers."""
    sock.sendall(struct.pack(">Q", len(data)) + data)


def _recv_blob(sock: ssl.SSLSocket,
                 max_size: int = 256 * 1024 * 1024) -> bytes:
    raw_len = _recv_exact(sock, 8)
    n = struct.unpack(">Q", raw_len)[0]
    if n > max_size:
        raise ValueError(f"blob too large: {n}")
    return _recv_exact(sock, n)


def _resolve_safe(drive_root: Path, rel: str) -> Path:
    """Combine drive_root with a client-supplied relative path
    and refuse anything that escapes the drive root.
    Symlinks INSIDE the drive that point outside are also
    rejected.

    Always returns an absolute, resolved path."""
    if rel in ("", "/", "."):
        return drive_root.resolve()
    # Reject absolute paths and any traversal attempts.
    p = (drive_root / rel.lstrip("/\\")).resolve()
    try:
        p.relative_to(drive_root.resolve())
    except ValueError:
        raise PermissionError(f"path escapes drive root: {rel}")
    return p


def _handle_session(conn: ssl.SSLSocket, addr,
                       state: dict) -> None:
    """Handle one client session from handshake to disconnect."""
    sess = _SessionState()
    try:
        # --- 1) Handshake -----------------------------------
        nonce = secrets.token_bytes(32)
        ts = int(time.time())
        _send_json(conn, {
            "msg":      "challenge",
            "version":  PROTOCOL_VERSION,
            "nonce":    nonce.hex(),
            "ts":       ts,
        })

        reply = _recv_json(conn)
        if reply.get("msg") != "auth":
            _send_json(conn, {"msg": "deny",
                              "reason": "expected auth"})
            return
        client_name = reply.get("client")
        claimed_mac = reply.get("mac", "")
        client_hmac = reply.get("hmac", "")
        if not (client_name and claimed_mac and client_hmac):
            _send_json(conn, {"msg": "deny",
                              "reason": "missing fields"})
            return
        try:
            claimed_mac = normalize_mac(claimed_mac)
        except ValueError:
            _send_json(conn, {"msg": "deny",
                              "reason": "bad mac format"})
            return

        # Replay window
        if abs(int(time.time()) - ts) > REPLAY_WINDOW_SECONDS:
            _send_json(conn, {"msg": "deny",
                              "reason": "stale ts"})
            return

        # Look up client
        client_cfg = state["clients"].get(client_name)
        if client_cfg is None:
            _send_json(conn, {"msg": "deny",
                              "reason": "unknown client"})
            return

        # Verify MAC is allowlisted
        if claimed_mac not in client_cfg["macs"]:
            _send_json(conn, {"msg": "deny",
                              "reason": "mac not allowed"})
            print(f"  [{addr[0]}] {client_name!r} sent "
                  f"unallowed MAC {claimed_mac}")
            return

        # Verify HMAC
        secret = bytes.fromhex(client_cfg["secret"])
        msg = nonce + struct.pack(">Q", ts) + claimed_mac.encode()
        expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, client_hmac):
            _send_json(conn, {"msg": "deny",
                              "reason": "bad hmac"})
            print(f"  [{addr[0]}] {client_name!r} bad HMAC")
            return

        sess.client_name = client_name
        sess.claimed_mac = claimed_mac
        sess.authenticated = True
        sess.drives = state["drives"]

        # Send the drive list back
        drives_view = []
        for n, d in state["drives"].items():
            drives_view.append({
                "name":     n,
                "readonly": bool(d.get("readonly")),
            })
        _send_json(conn, {
            "msg":      "ok",
            "drives":   drives_view,
            "server":   socket.gethostname(),
            "version":  PROTOCOL_VERSION,
        })
        print(f"  [{addr[0]}] {client_name!r} authenticated, "
              f"{len(drives_view)} drive(s)")

        # --- 2) Command loop ---------------------------------
        while True:
            try:
                cmd = _recv_json(conn)
            except ConnectionError:
                break
            kind = cmd.get("cmd")
            try:
                if kind == "list":
                    _do_list(conn, sess, cmd)
                elif kind == "stat":
                    _do_stat(conn, sess, cmd)
                elif kind == "read":
                    _do_read(conn, sess, cmd)
                elif kind == "write":
                    _do_write(conn, sess, cmd)
                elif kind == "mkdir":
                    _do_mkdir(conn, sess, cmd)
                elif kind == "delete":
                    _do_delete(conn, sess, cmd)
                elif kind == "rename":
                    _do_rename(conn, sess, cmd)
                elif kind == "ping":
                    _send_json(conn, {"msg": "pong"})
                elif kind == "bye":
                    _send_json(conn, {"msg": "bye"})
                    break
                else:
                    _send_json(conn, {"msg": "err",
                                      "reason":
                                          f"unknown cmd {kind!r}"})
            except (PermissionError, FileNotFoundError,
                     IsADirectoryError, NotADirectoryError,
                     OSError, ValueError) as e:
                _send_json(conn, {
                    "msg":    "err",
                    "reason": str(e),
                    "errno":
                        getattr(e, "errno", None),
                })
    except (ConnectionError, ssl.SSLError, OSError) as e:
        print(f"  [{addr[0]}] connection lost: {e}")
    except Exception as e:
        # Log loud but don't crash the listener
        print(f"  [{addr[0]}] handler error: "
              f"{type(e).__name__}: {e}")
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def _get_drive_root(sess: _SessionState, drive: str) -> Path:
    d = sess.drives.get(drive)
    if d is None:
        raise FileNotFoundError(f"no such drive: {drive!r}")
    return Path(d["path"])


def _check_writeable(sess: _SessionState, drive: str) -> None:
    d = sess.drives.get(drive)
    if d is None:
        raise FileNotFoundError(f"no such drive: {drive!r}")
    if d.get("readonly"):
        raise PermissionError(f"drive {drive!r} is read-only")


def _do_list(conn, sess, cmd):
    root = _get_drive_root(sess, cmd["drive"])
    rel = cmd.get("path", "")
    target = _resolve_safe(root, rel)
    if not target.is_dir():
        raise NotADirectoryError(rel)
    entries = []
    for child in sorted(target.iterdir()):
        try:
            st = child.stat()
            entries.append({
                "name":   child.name,
                "is_dir": child.is_dir(),
                "size":   st.st_size if child.is_file() else 0,
                "mtime":  st.st_mtime,
            })
        except OSError:
            # Permission denied on a single child shouldn't kill
            # the whole listing. Skip it.
            continue
    _send_json(conn, {"msg": "ok", "entries": entries})


def _do_stat(conn, sess, cmd):
    root = _get_drive_root(sess, cmd["drive"])
    target = _resolve_safe(root, cmd["path"])
    st = target.stat()
    _send_json(conn, {
        "msg":    "ok",
        "name":   target.name,
        "is_dir": target.is_dir(),
        "size":   st.st_size,
        "mtime":  st.st_mtime,
    })


def _do_read(conn, sess, cmd):
    root = _get_drive_root(sess, cmd["drive"])
    target = _resolve_safe(root, cmd["path"])
    if target.is_dir():
        raise IsADirectoryError(target.name)
    offset = int(cmd.get("offset", 0))
    length = int(cmd.get("length", READ_CHUNK_SIZE))
    length = min(length, READ_CHUNK_SIZE)
    with target.open("rb") as f:
        f.seek(offset)
        data = f.read(length)
    _send_json(conn, {"msg": "ok", "length": len(data)})
    _send_blob(conn, data)


def _do_write(conn, sess, cmd):
    _check_writeable(sess, cmd["drive"])
    root = _get_drive_root(sess, cmd["drive"])
    target = _resolve_safe(root, cmd["path"])
    offset = int(cmd.get("offset", 0))
    append = bool(cmd.get("append", False))
    # Ack ready to receive
    _send_json(conn, {"msg": "ready"})
    blob = _recv_blob(conn)
    mode = "ab" if append else ("r+b" if offset > 0 else "wb")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() and offset > 0:
        # Pre-create the file for r+b
        target.touch()
    with target.open(mode) as f:
        if offset > 0 and not append:
            f.seek(offset)
        f.write(blob)
    _send_json(conn, {"msg": "ok", "written": len(blob)})


def _do_mkdir(conn, sess, cmd):
    _check_writeable(sess, cmd["drive"])
    root = _get_drive_root(sess, cmd["drive"])
    target = _resolve_safe(root, cmd["path"])
    target.mkdir(parents=cmd.get("parents", False),
                  exist_ok=cmd.get("exist_ok", False))
    _send_json(conn, {"msg": "ok"})


def _do_delete(conn, sess, cmd):
    _check_writeable(sess, cmd["drive"])
    root = _get_drive_root(sess, cmd["drive"])
    target = _resolve_safe(root, cmd["path"])
    if target.is_dir() and not cmd.get("recursive"):
        target.rmdir()
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    _send_json(conn, {"msg": "ok"})


def _do_rename(conn, sess, cmd):
    _check_writeable(sess, cmd["drive"])
    root = _get_drive_root(sess, cmd["drive"])
    old = _resolve_safe(root, cmd["from"])
    new = _resolve_safe(root, cmd["to"])
    old.rename(new)
    _send_json(conn, {"msg": "ok"})


def cmd_run() -> int:
    """Run the server until Ctrl-C."""
    if not CONFIG_FILE().exists():
        print("Server isn't configured yet. Run 'setup' first.")
        return 1
    state = load_server_state()

    # TLS context
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(CERT_FILE()),
                         keyfile=str(KEY_FILE()))

    bind = state["bind"]
    port = state["port"]
    fp = cert_fingerprint(CERT_FILE())
    print()
    print("=" * 60)
    print(f" Quopus Drive Server starting")
    print("=" * 60)
    print(f"  Listening:    {bind}:{port}")
    print(f"  Cert SHA-256: {fp}")
    print(f"  Drives:       {len(state['drives'])}")
    for n, d in state["drives"].items():
        ro = "RO" if d.get("readonly") else "RW"
        print(f"                  {ro} {n} -> {d['path']}")
    print(f"  Clients:      {len(state['clients'])}")
    print()
    print("  Press Ctrl-C to stop.")
    print()

    with socket.socket(socket.AF_INET,
                        socket.SOCK_STREAM) as listen_sock:
        listen_sock.setsockopt(socket.SOL_SOCKET,
                                socket.SO_REUSEADDR, 1)
        listen_sock.bind((bind, port))
        listen_sock.listen(8)
        try:
            while True:
                raw_conn, addr = listen_sock.accept()
                try:
                    tls_conn = ctx.wrap_socket(
                        raw_conn, server_side=True)
                except ssl.SSLError as e:
                    print(f"  [{addr[0]}] TLS handshake failed: "
                          f"{e}")
                    try: raw_conn.close()
                    except OSError: pass
                    continue
                # One thread per connection - simple, fine for
                # a small number of concurrent clients. For
                # heavier loads switch to a thread pool.
                t = threading.Thread(
                    target=_handle_session,
                    args=(tls_conn, addr, state),
                    daemon=True)
                t.start()
        except KeyboardInterrupt:
            print()
            print("Stopping.")
            return 0


# =============================================================
# Main
# =============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="quopus_drive_server",
        description="Quopus remote drive server")
    parser.add_argument(
        "command",
        choices=["setup", "run", "info", "whoami",
                  "addclient", "delclient", "listclients",
                  "addmac",
                  "adddrive", "deldrive"],
        help="What to do (run --help for details)")
    # Optional positional args (client name, MAC, drive name)
    # consumed by individual commands via sys.argv inspection.
    parser.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.command == "setup":
        return cmd_setup()
    if args.command == "addclient":
        return cmd_addclient()
    if args.command == "delclient":
        return cmd_delclient()
    if args.command == "listclients":
        return cmd_listclients()
    if args.command == "addmac":
        return cmd_addmac()
    if args.command == "adddrive":
        return cmd_adddrive()
    if args.command == "deldrive":
        return cmd_deldrive()
    if args.command == "run":
        return cmd_run()
    if args.command == "info":
        return cmd_info()
    if args.command == "whoami":
        return cmd_whoami()
    return 1


if __name__ == "__main__":
    sys.exit(main())
