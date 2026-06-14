# date_time: 2026-06-14 09:05
"""
Network Scanner / Mapper  (Quopus premium module #9)
====================================================

Host discovery + TCP port scan + banner/service detection for a
subnet or host list, with CSV/JSON export. Discovered hosts can be
saved straight into the Secrets vault as connection entries.

No root needed: discovery uses a TCP "ping" (connect to a few common
ports) instead of ICMP, and the port scan is a threaded connect-scan.

Engine is Qt-free and unit-testable; the dialog is a thin shell.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import socket
import time
from concurrent.futures import (ThreadPoolExecutor, as_completed, wait,
                                FIRST_COMPLETED)
from dataclasses import dataclass, field, asdict
from typing import Optional

# Common ports probed for "is this host alive" TCP-ping
_PING_PORTS = (80, 443, 22, 445, 139, 3389, 8080)

PORT_PRESETS = {
    "Top 20": [21, 22, 23, 25, 53, 80, 110, 139, 143, 443, 445,
               993, 995, 1723, 3306, 3389, 5900, 8080, 8443, 53],
    "Web": [80, 81, 443, 8000, 8008, 8080, 8443, 8888],
    "Remote/Admin": [22, 23, 3389, 5900, 5901, 445, 139, 161, 623],
    "Databases": [1433, 1521, 3306, 5432, 6379, 9200, 11211, 27017],
    "Common 1-1024": list(range(1, 1025)),
    "Registered 1-49151": list(range(1, 49152)),
    "Full 1-65535": list(range(1, 65536)),
}

_SERVICES = {
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns",
    80: "http", 110: "pop3", 139: "netbios", 143: "imap", 161: "snmp",
    389: "ldap", 443: "https", 445: "smb", 587: "smtp", 993: "imaps",
    995: "pop3s", 1433: "mssql", 1521: "oracle", 3306: "mysql",
    3389: "rdp", 5432: "postgres", 5900: "vnc", 6379: "redis",
    8080: "http-alt", 8443: "https-alt", 9200: "elastic",
    11211: "memcached", 27017: "mongodb",
}


def service_name(port: int) -> str:
    if port in _SERVICES:
        return _SERVICES[port]
    try:
        return socket.getservbyport(port)
    except Exception:
        return ""


def parse_targets(spec: str) -> list:
    """Expand a target spec into IP strings.

    Accepts: single host/IP, CIDR (192.168.0.0/24), dashed range
    (192.168.0.10-50), comma/space separated combinations, and
    hostnames (resolved)."""
    out: list = []
    seen = set()

    def add(ip):
        if ip not in seen:
            seen.add(ip)
            out.append(ip)

    for tok in spec.replace(",", " ").split():
        tok = tok.strip()
        if not tok:
            continue
        try:
            if "/" in tok:                      # CIDR
                net = ipaddress.ip_network(tok, strict=False)
                hosts = list(net.hosts()) or [net.network_address]
                for h in hosts:
                    add(str(h))
                continue
            if "-" in tok and tok.count(".") == 3:   # a.b.c.d-e range
                base, _, last = tok.rpartition("-")
                if last.isdigit() and base.count(".") == 3:
                    prefix = base.rsplit(".", 1)[0]
                    start = int(base.rsplit(".", 1)[1])
                    for n in range(start, int(last) + 1):
                        add(f"{prefix}.{n}")
                    continue
            # plain IP?
            try:
                ipaddress.ip_address(tok)
                add(tok)
                continue
            except ValueError:
                pass
            # hostname -> resolve
            add(socket.gethostbyname(tok))
        except Exception:
            continue
    return out


def _connect(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def is_alive(ip: str, timeout: float = 0.5) -> bool:
    """TCP-ping: alive if any probe port accepts a connection."""
    for p in _PING_PORTS:
        if _connect(ip, p, timeout):
            return True
    return False


def grab_banner(ip: str, port: int, timeout: float = 1.0) -> str:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            # nudge text protocols
            if port in (80, 8080, 8000, 8443):
                s.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
            data = s.recv(160)
            return data.decode("latin-1", "replace").strip()
    except Exception:
        return ""


@dataclass
class OpenPort:
    ip: str
    port: int
    service: str = ""
    banner: str = ""


@dataclass
class HostResult:
    ip: str
    alive: bool = False
    hostname: str = ""
    open_ports: list = field(default_factory=list)   # list[OpenPort]


def scan(targets: list, ports: list, timeout: float = 0.6,
         want_banner: bool = True, max_workers: int = 200,
         progress=None, stop=None) -> list:
    """Scan targets x ports. Returns list[HostResult]. `progress(done,
    total)` and `stop()->bool` are optional callbacks for the UI.

    Submission is bounded: at most a sliding window of futures is kept
    in flight, so a full 1-65535 sweep across many hosts (millions of
    probes) runs in constant memory instead of materialising every
    task up front."""
    results: dict = {ip: HostResult(ip=ip) for ip in targets}
    total = len(targets) * len(ports)
    done = 0

    def work(ip, port):
        if stop and stop():
            return None
        if _connect(ip, port, timeout):
            op = OpenPort(ip=ip, port=port, service=service_name(port))
            if want_banner:
                op.banner = grab_banner(ip, port, min(1.0, timeout + 0.4))
            return op
        return None

    task_iter = ((ip, p) for ip in targets for p in ports)
    window = max(max_workers * 8, 2048)
    fut_arg: dict = {}

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        # prime the window
        for _ in range(window):
            try:
                ip, p = next(task_iter)
            except StopIteration:
                break
            fut_arg[ex.submit(work, ip, p)] = (ip, p)

        exhausted = False
        while fut_arg:
            ready, _ = wait(set(fut_arg), return_when=FIRST_COMPLETED)
            for fut in ready:
                fut_arg.pop(fut, None)
                done += 1
                if progress:
                    progress(done, total)
                op = fut.result()
                if op:
                    hr = results[op.ip]
                    hr.alive = True
                    hr.open_ports.append(op)
                # top up the window unless stopping/exhausted
                if not exhausted and not (stop and stop()):
                    try:
                        ip, p = next(task_iter)
                        fut_arg[ex.submit(work, ip, p)] = (ip, p)
                    except StopIteration:
                        exhausted = True
            if stop and stop():
                break

    # resolve hostnames for hosts with hits
    final = []
    for hr in results.values():
        if hr.open_ports:
            hr.open_ports.sort(key=lambda o: o.port)
            try:
                hr.hostname = socket.gethostbyaddr(hr.ip)[0]
            except Exception:
                hr.hostname = ""
            final.append(hr)
    final.sort(key=lambda h: tuple(int(x) for x in h.ip.split(".")
                                   if x.isdigit()) or (0,))
    return final


def to_rows(results: list) -> list:
    rows = []
    for hr in results:
        for op in hr.open_ports:
            rows.append({"ip": hr.ip, "hostname": hr.hostname,
                         "port": op.port, "service": op.service,
                         "banner": op.banner})
    return rows


def export_csv(results: list) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["ip", "hostname", "port",
                                        "service", "banner"])
    w.writeheader()
    for r in to_rows(results):
        w.writerow(r)
    return buf.getvalue()


def export_json(results: list) -> str:
    return json.dumps([asdict(hr) for hr in results], indent=2)


# ------------------------------------------------------------------- UI
def open_scanner_dialog(parent=None, config=None):
    dlg = NetworkScannerDialog(parent=parent, config=config)
    dlg.show()
    return dlg


try:
    from PyQt6.QtCore import Qt, QThread, pyqtSignal
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox,
        QPushButton, QTableWidget, QTableWidgetItem, QLabel,
        QProgressBar, QFileDialog, QHeaderView, QMessageBox)
    _HAVE_QT = True
except Exception:
    _HAVE_QT = False


if _HAVE_QT:

    class _ScanThread(QThread):
        progress = pyqtSignal(int, int)
        done = pyqtSignal(list)

        def __init__(self, targets, ports, want_banner,
                     max_workers=200, timeout=0.6):
            super().__init__()
            self.targets = targets
            self.ports = ports
            self.want_banner = want_banner
            self.max_workers = max_workers
            self.timeout = timeout
            self._stop = False

        def stop(self):
            self._stop = True

        def run(self):
            res = scan(self.targets, self.ports,
                       timeout=self.timeout,
                       want_banner=self.want_banner,
                       max_workers=self.max_workers,
                       progress=lambda d, t: self.progress.emit(d, t),
                       stop=lambda: self._stop)
            self.done.emit(res)

    class NetworkScannerDialog(QDialog):
        def __init__(self, parent=None, config=None):
            super().__init__(parent)
            self.setWindowTitle("Network Scanner / Mapper")
            self.resize(820, 560)
            self.config = config or {}
            self._thread = None
            self._results = []
            self._build()

        def _build(self):
            v = QVBoxLayout(self)
            row = QHBoxLayout()
            self.ed_target = QLineEdit()
            self.ed_target.setPlaceholderText(
                "192.168.0.0/24  or  10.0.0.1-50  or  host.lan")
            self.cmb_ports = QComboBox()
            for name in PORT_PRESETS:
                self.cmb_ports.addItem(name)
            self.btn_scan = QPushButton("Scan")
            self.btn_scan.clicked.connect(self._scan)
            self.btn_stop = QPushButton("Stop")
            self.btn_stop.clicked.connect(self._stop)
            self.btn_stop.setEnabled(False)
            for w in (QLabel("Targets:"), self.ed_target,
                      QLabel("Ports:"), self.cmb_ports,
                      self.btn_scan, self.btn_stop):
                row.addWidget(w)
            v.addLayout(row)

            self.bar = QProgressBar()
            v.addWidget(self.bar)

            self.tbl = QTableWidget(0, 5)
            self.tbl.setHorizontalHeaderLabels(
                ["IP", "Hostname", "Port", "Service", "Banner"])
            self.tbl.horizontalHeader().setSectionResizeMode(
                4, QHeaderView.ResizeMode.Stretch)
            v.addWidget(self.tbl, 1)

            bot = QHBoxLayout()
            self.lbl_status = QLabel("Ready.")
            btn_csv = QPushButton("Export CSV")
            btn_csv.clicked.connect(lambda: self._export("csv"))
            btn_json = QPushButton("Export JSON")
            btn_json.clicked.connect(lambda: self._export("json"))
            btn_vault = QPushButton("Save host -> Vault")
            btn_vault.clicked.connect(self._to_vault)
            bot.addWidget(self.lbl_status, 1)
            for w in (btn_csv, btn_json, btn_vault):
                bot.addWidget(w)
            v.addLayout(bot)

        def _scan(self):
            targets = parse_targets(self.ed_target.text())
            if not targets:
                QMessageBox.information(self, "Scan", "No valid targets.")
                return
            ports = PORT_PRESETS[self.cmb_ports.currentText()]
            n = len(targets) * len(ports)
            # scale concurrency to the job; ease timeout for huge sweeps
            if n >= 200_000:
                workers, timeout, banner = 1000, 0.4, False
            elif n >= 20_000:
                workers, timeout, banner = 600, 0.5, True
            else:
                workers, timeout, banner = 200, 0.6, True
            self.tbl.setRowCount(0)
            note = "" if n < 100_000 else "  (large sweep - may take a while; Stop anytime)"
            self.lbl_status.setText(
                f"Scanning {len(targets)} host(s) x {len(ports)} "
                f"port(s) = {n:,} probes...{note}")
            self.btn_scan.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self._thread = _ScanThread(targets, ports, banner,
                                       max_workers=workers, timeout=timeout)
            self._thread.progress.connect(self._progress)
            self._thread.done.connect(self._finished)
            self._thread.start()

        def _stop(self):
            if self._thread:
                self._thread.stop()
            self.lbl_status.setText("Stopping...")

        def _progress(self, done, total):
            self.bar.setMaximum(total)
            self.bar.setValue(done)

        def _finished(self, results):
            self._results = results
            self.tbl.setRowCount(0)
            for r in to_rows(results):
                row = self.tbl.rowCount()
                self.tbl.insertRow(row)
                for c, key in enumerate(
                        ["ip", "hostname", "port", "service", "banner"]):
                    self.tbl.setItem(
                        row, c, QTableWidgetItem(str(r[key])))
            n_hosts = len(results)
            n_ports = sum(len(h.open_ports) for h in results)
            self.lbl_status.setText(
                f"Done. {n_hosts} host(s), {n_ports} open port(s).")
            self.btn_scan.setEnabled(True)
            self.btn_stop.setEnabled(False)

        def _export(self, kind):
            if not self._results:
                return
            txt = (export_csv if kind == "csv" else export_json)(
                self._results)
            fn, _ = QFileDialog.getSaveFileName(
                self, "Export", f"scan.{kind}",
                f"{kind.upper()} (*.{kind})")
            if fn:
                try:
                    with open(fn, "w", encoding="utf-8") as f:
                        f.write(txt)
                    self.lbl_status.setText(f"Exported to {fn}")
                except Exception as e:
                    QMessageBox.warning(self, "Export", str(e))

        def _to_vault(self):
            row = self.tbl.currentRow()
            if row < 0:
                return
            ip = self.tbl.item(row, 0).text()
            port = self.tbl.item(row, 2).text()
            svc = self.tbl.item(row, 3).text()
            try:
                from .secrets_vault import (get_shared_vault, Entry,
                                            open_vault_dialog)
                v = get_shared_vault(self.config)
                if not v.is_unlocked:
                    open_vault_dialog(self, self.config)
                if not v.is_unlocked:
                    return
                kind = {"ssh": "ssh_key", "smb": "smb"}.get(svc, "login")
                v.add(Entry(kind=kind, title=f"{ip}:{port} ({svc})",
                            host=ip, port=port, tags=["scanned"]))
                v.save()
                self.lbl_status.setText(
                    f"Saved {ip}:{port} to vault.")
            except Exception as e:
                QMessageBox.warning(self, "Vault", str(e))

else:
    class NetworkScannerDialog:                # pragma: no cover
        def __init__(self, *a, **k):
            raise RuntimeError("PyQt6 not available")
        def show(self):
            pass
