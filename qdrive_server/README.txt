Quopus Drive Server
===================

Standalone Python script that lets a Quopus Commander client
running on another machine browse and transfer files on this
machine, over an authenticated, TLS-encrypted connection.

This is the thing you copy onto the REMOTE PC - the machine
whose drives you want to access from your Quopus.


Requirements
------------

  - Python 3.9 or newer (only the standard library is required
    if `openssl` is on your PATH; otherwise install the
    `cryptography` Python package for the cert generator)
  - One open TCP port (default 47823) on this machine,
    reachable from the client. If both PCs are on the same
    LAN, open the port in the local firewall. For
    connections across the internet, run it behind a tunnel
    (Tailscale, WireGuard, ZeroTier, or simply SSH port
    forwarding) - do NOT expose port 47823 directly to the
    internet.


First-time setup
----------------

On this PC (the SERVER):

    python quopus_drive_server.py setup

The wizard will:

  1. Ask which TCP port and bind address you want
  2. Generate a self-signed TLS certificate (one-time, valid
     10 years). Prints the SHA-256 fingerprint - you'll
     paste this into Quopus on the other PC.
  3. Let you pick which directories should be exposed (each
     gets a short name and a read-only flag)
  4. Authorize your first CLIENT - this requires the MAC
     address of a physical LAN card on the CLIENT machine,
     and produces a per-client secret. Print this secret
     immediately and paste into Quopus's connection profile
     on the client; it is never shown again.

The wizard saves config to:

  - Linux:    ~/.config/quopus_drive_server/
  - macOS:    ~/Library/Application Support/quopus_drive_server/
  - Windows:  %APPDATA%\quopus_drive_server\

Sensitive files (server.key, server.json) are chmod 600 where
the platform supports it.


Running the server
------------------

    python quopus_drive_server.py run

Stays in the foreground, prints every connect/deny to stdout.
Ctrl-C to stop.

For unattended operation, wrap it in a service:

  - Linux:    a systemd .service unit
  - macOS:    a launchd .plist
  - Windows:  Task Scheduler "At system startup" / nssm /
              Windows service wrapper of choice

The server doesn't fork or daemonize itself - that's
deliberate, services should run under their own supervisor
which is what every modern init system already does.


Other commands
--------------

    python quopus_drive_server.py info

  Prints current config: port, cert fingerprint, exposed
  drives, registered clients with all their authorized MACs.

    python quopus_drive_server.py listclients

  Compact one-line-per-client listing. Useful when info gets
  too verbose.

    python quopus_drive_server.py addclient

  Adds another client to an already-configured server.

    python quopus_drive_server.py delclient [name]

  Removes a client. The client's secret is wiped, so a stolen
  copy of it stops working immediately - which is the whole
  point of running this command. Asks for confirmation
  (type 'yes', not just 'y'). If the name isn't passed on
  the command line, it shows the list and prompts.

    python quopus_drive_server.py addmac [client_name] [mac]

  Adds another MAC address to an existing client. Useful when
  a laptop swaps NICs (Ethernet vs Wi-Fi) or you didn't get
  all the right MACs in during setup. Accepts MAC in any
  format (colons, hyphens, no separator); duplicates are
  silently skipped. If client_name or mac is missing on the
  command line, it prompts.

    python quopus_drive_server.py adddrive

  Adds another exposed directory to the server. Walks you
  through path, short name and read-only flag - same as
  the corresponding step in setup.

    python quopus_drive_server.py deldrive [name]

  Removes an exposed drive. Doesn't touch the actual files
  on disk - just stops exposing them to clients.

    python quopus_drive_server.py whoami

  Prints THIS machine's physical LAN MAC addresses (useful
  when authorizing this machine to act as a client on a
  different server).

NOTE: any command that changes server.json (addclient,
delclient, addmac, adddrive, deldrive) requires a server
restart to take effect. Stop the running server with Ctrl-C
and start it again with `run`.


Finding the client's MAC address
--------------------------------

On the CLIENT machine (the PC running Quopus), open a terminal
and run:

    python quopus_drive_server.py whoami

That same script works for this purpose. It enumerates physical
LAN cards (excluding virtual adapters: VirtualBox, Hyper-V,
Tailscale, Docker, WireGuard, loopback, ...) and prints their
MACs. Pick the one for the network the client uses to reach
this server. For a laptop that switches between Ethernet and
Wi-Fi, register BOTH MACs - the wizard accepts a list.


Security notes
--------------

The transport uses TLS 1.2+ with a self-signed cert that the
client pins by SHA-256 fingerprint. Auth uses HMAC-SHA256
over a server-supplied nonce + a 30-second timestamp + the
client's claimed MAC, signed with a per-client shared secret.
The server verifies the HMAC AND checks that the MAC is on
the client's allowlist - both must match.

This defeats:
  - Passive eavesdropping (TLS)
  - MitM with forged certs (pinning)
  - Replay (nonce + tight TS window)
  - Stolen-secret-on-different-PC (MAC binding)

This does NOT defeat:
  - A determined attacker on the same LAN segment who can
    sniff the client's MAC AND steal its secret. MAC
    spoofing is trivial - the MAC tie is a hardening layer,
    not a primary defense. Treat the secret like a
    password.
  - Anyone with shell access on the server machine. The
    server runs with the privileges of the user that
    started it, so the exposed drives are accessible to
    that user anyway.

If you ever suspect a secret has leaked, run

    python quopus_drive_server.py addclient

with a fresh client name and delete the compromised entry
from server.json. Restart the server; the old secret won't
work anymore.


Protocol version
----------------

Current protocol: v1. Both ends should be running the same
Quopus release. Backwards-compat is best-effort across
minor versions.
