"""VICE binary monitor TCP client.

Implements just enough of the VICE binary monitor protocol to support
the memory grab / poke flow used by MemoryViewDialog.

Start VICE with the binary monitor enabled:

    x64sc -binarymonitor -binarymonitoraddress ip4://127.0.0.1:6502

Then `vice_readmem('127.0.0.1', 6502, 0x0800, 256)` returns the bytes.

Protocol summary
================
TCP socket, no TLS, default port 6502. Requests and responses are
prefixed with STX = 0x02. All multi-byte values are little-endian.

Request header (11 bytes):
    [0]     STX                  0x02
    [1]     API version          0x02 (current)
    [2..5]  body length (uint32) - does NOT include header
    [6..9]  request id (uint32) - we just echo a counter
    [10]    command byte

Response header (12 bytes):
    [0]     STX                  0x02
    [1]     API version
    [2..5]  body length (uint32)
    [6]     response type
    [7]     error code (0 = ok)
    [8..11] request id (matches the request, or 0xffffffff for events)

Memory get (cmd 0x01) body:
    [0]     side-effects (0 = quiet, 1 = allow side effects)
    [1..2]  start address (LE)
    [3..4]  end address (LE, inclusive)
    [5]     memspace (0 = main CPU)
    [6..7]  bank id (LE; 0 = default)

Memory get response body:
    [0..1]  payload length (LE)
    [2..]   payload bytes

Memory set (cmd 0x02) body:
    [0]     side-effects
    [1..2]  start address (LE)
    [3..4]  end address (LE, inclusive)
    [5]     memspace
    [6..7]  bank id (LE)
    [8..]   data bytes

A couple of quirks worth knowing about:

* When you connect, VICE may already be in the monitor (paused) - or
  it may be running. Memory get/set commands work either way. They
  do NOT pause execution; the read is satisfied from the current
  emulator state.

* Some commands cause VICE to emit unsolicited "stopped" (0x62) or
  "resumed" (0x63) events. We just skip them while waiting for our
  own response (matched by request id).

* The "exit" command (0xaa) tells the monitor to leave - but if we
  never entered it ourselves we don't need to send it. memget/memset
  alone leave the emulator running.

So for our purposes the flow is just:
    connect -> send memget -> read response by id -> disconnect.
"""

import socket
import struct
import itertools
import time


# -- protocol constants -----------------------------------------------

VICE_STX = 0x02
# API version that VICE expects in the request header. All real-world
# traces (C64Studio, vice-bridge-net, vice bug reports) consistently
# show 0x01 here. The VICE source treats this as a *compatibility*
# marker, not a "newest server version" - clients must send 0x01.
VICE_API_VERSION = 0x01

CMD_MEMGET = 0x01
CMD_MEMSET = 0x02
CMD_PING   = 0x81
CMD_EXIT   = 0xaa

# Response types worth distinguishing.
RESP_MEMGET   = 0x01
RESP_MEMSET   = 0x02
RESP_INVALID  = 0x00   # generic error
RESP_STOPPED  = 0x62
RESP_RESUMED  = 0x63

# Counter for request IDs. We don't need uniqueness across processes,
# just within a single connection - but a process-wide counter keeps
# things simple and is plenty unique within one VICE session.
_req_id_counter = itertools.count(1)


def _next_req_id() -> int:
    # Wrap into uint32 to be safe over very long sessions.
    return next(_req_id_counter) & 0xFFFFFFFF


def _build_request(cmd: int, body: bytes, req_id: int) -> bytes:
    """Build a fully-formed binary monitor request packet."""
    header = struct.pack(
        '<BBIIB',
        VICE_STX, VICE_API_VERSION, len(body), req_id, cmd)
    return header + body


def _read_exact(sock: socket.socket, n: int, timeout: float) -> bytes:
    """Read exactly n bytes from sock, respecting an overall deadline.

    socket.recv() can return fewer bytes than asked for; we loop until
    we have everything or the deadline expires.
    """
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"VICE read timed out after {timeout}s "
                f"(got {len(buf)}/{n} bytes)")
        sock.settimeout(remaining)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"VICE closed the connection (got {len(buf)}/{n} bytes)")
        buf.extend(chunk)
    return bytes(buf)


def _read_response(sock: socket.socket, want_req_id: int,
                     timeout: float = 5.0):
    """Read responses from VICE until one matching our request ID
    arrives, skipping unsolicited events along the way.

    Returns (response_type, error_code, body). Raises on errors.
    """
    deadline = time.monotonic() + timeout
    while True:
        # Header is 12 bytes: STX, ver, length(4), type, err, reqid(4)
        remaining = max(0.05, deadline - time.monotonic())
        header = _read_exact(sock, 12, remaining)
        if header[0] != VICE_STX:
            raise ValueError(
                f"VICE: bad STX in response: 0x{header[0]:02X}")
        # header[1] = api version, we tolerate any
        body_len = struct.unpack_from('<I', header, 2)[0]
        resp_type = header[6]
        err_code = header[7]
        resp_id = struct.unpack_from('<I', header, 8)[0]

        body = b""
        if body_len:
            remaining = max(0.05, deadline - time.monotonic())
            body = _read_exact(sock, body_len, remaining)

        if resp_id == 0xFFFFFFFF:
            # Unsolicited event (stopped, resumed, register info on
            # entering the monitor, etc) - ignore and keep waiting.
            continue
        if resp_id != want_req_id:
            # Stale response from a previous request we abandoned.
            # Discard and keep waiting.
            continue
        return resp_type, err_code, body


def _connect(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    """Open a TCP connection to VICE's binary monitor."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except Exception:
        sock.close()
        raise
    return sock


# -- public API: matches the u64_readmem / u64_writemem signatures ----

def vice_readmem(host: str, port: int, address: int, length: int,
                   timeout: float = 10.0):
    """Read `length` bytes of C64 memory starting at `address` from a
    running VICE instance. Signature is parallel to u64_readmem so the
    MemoryViewDialog backend can swap them transparently.

    Returns (True, bytes) on success or (False, error_message).

    The VICE memget response uses a uint16 length field, so a single
    call can return at most 65535 bytes. We split larger reads into
    chunks; for a full 64K dump this means 2 round-trips. Each chunk
    opens a fresh TCP connection to keep error recovery simple - VICE
    handles back-to-back connects fine.
    """
    if length <= 0:
        return False, "length must be >= 1"
    if address < 0 or address > 0xFFFF:
        return False, "address must be in $0000..$FFFF"
    if address + length > 0x10000:
        return False, "read would wrap past $FFFF"

    # Chunk size: stay well below the 65535-byte protocol cap. 32KB
    # gives us 2 chunks for full 64K reads, which is still fast.
    CHUNK = 0x8000
    out = bytearray()
    remaining = length
    cur = address
    while remaining > 0:
        n = min(CHUNK, remaining)
        ok, data = _vice_readmem_chunk(
            host, port, cur, n, timeout=timeout)
        if not ok:
            return False, data
        out.extend(data)
        cur += n
        remaining -= n
    return True, bytes(out)


def _vice_readmem_chunk(host: str, port: int, address: int, length: int,
                          timeout: float):
    """Single memget call - never larger than 65535 bytes."""
    end_addr = address + length - 1
    body = struct.pack('<BHHBH',
                          0,          # side_effects = 0 (quiet read)
                          address,
                          end_addr,
                          0,          # memspace = main CPU
                          0)          # bank id = default
    req_id = _next_req_id()
    packet = _build_request(CMD_MEMGET, body, req_id)

    try:
        sock = _connect(host, port, timeout=timeout)
    except OSError as e:
        return False, f"Cannot reach VICE at {host}:{port}: {e}"
    try:
        sock.sendall(packet)
        try:
            resp_type, err, resp_body = _read_response(
                sock, req_id, timeout=timeout)
        except Exception as e:
            return False, f"VICE protocol error: {e}"
    finally:
        try:
            sock.close()
        except Exception:
            pass

    if err != 0:
        return False, f"VICE memget error code 0x{err:02X}"
    if resp_type != RESP_MEMGET:
        return False, (f"VICE: unexpected response type 0x{resp_type:02X}")
    if len(resp_body) < 2:
        return False, "VICE: short memget response"
    payload_len = struct.unpack_from('<H', resp_body, 0)[0]
    payload = resp_body[2:2 + payload_len]
    if len(payload) != length:
        return False, (f"VICE memget returned {len(payload)} bytes, "
                          f"expected {length}")
    return True, payload


def vice_writemem(host: str, port: int, address: int, data: bytes,
                    timeout: float = 5.0):
    """Write `data` to C64 memory at `address` in a running VICE.
    Returns (True, "") on success or (False, error_message).
    """
    if not data:
        return False, "no data to write"
    if address < 0 or address > 0xFFFF:
        return False, "address must be in $0000..$FFFF"
    if address + len(data) > 0x10000:
        return False, "write would wrap past $FFFF"

    end_addr = address + len(data) - 1
    header_body = struct.pack('<BHHBH',
                                 0,    # side_effects
                                 address,
                                 end_addr,
                                 0,    # memspace
                                 0)    # bank
    body = header_body + bytes(data)
    req_id = _next_req_id()
    packet = _build_request(CMD_MEMSET, body, req_id)

    try:
        sock = _connect(host, port, timeout=timeout)
    except OSError as e:
        return False, f"Cannot reach VICE at {host}:{port}: {e}"
    try:
        sock.sendall(packet)
        try:
            resp_type, err, _body = _read_response(
                sock, req_id, timeout=timeout)
        except Exception as e:
            return False, f"VICE protocol error: {e}"
    finally:
        try:
            sock.close()
        except Exception:
            pass

    if err != 0:
        return False, f"VICE memset error code 0x{err:02X}"
    if resp_type != RESP_MEMSET:
        return False, (f"VICE: unexpected response type 0x{resp_type:02X}")
    return True, ""


def vice_ping(host: str, port: int, timeout: float = 3.0):
    """Cheap connectivity check. Returns (ok, msg)."""
    req_id = _next_req_id()
    packet = _build_request(CMD_PING, b"", req_id)
    try:
        sock = _connect(host, port, timeout=timeout)
    except OSError as e:
        return False, f"Cannot reach VICE at {host}:{port}: {e}"
    try:
        sock.sendall(packet)
        try:
            _resp_type, err, _body = _read_response(
                sock, req_id, timeout=timeout)
        except Exception as e:
            return False, f"VICE protocol error: {e}"
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if err != 0:
        return False, f"VICE ping error 0x{err:02X}"
    return True, ""
