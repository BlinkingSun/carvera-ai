"""Phone and tablet access: a second listener on the local network that only paired devices can use.

The main listener stays on 127.0.0.1 (this computer's own browser, agents, scripts). When phone access is
on, a second listener binds 0.0.0.0:<remote_port> and serves the same web UI. A device pairs once
with the 6-digit code shown on the computer and then keeps a cookie; the server stores only the
cookie's SHA-256. Pairing codes change after every pairing and after MAX_TRIES wrong guesses.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

COOKIE = "cc_device"
COOKIE_MAX_AGE = 365 * 24 * 3600
MAX_TRIES = 5          # wrong codes before the code changes
DEFAULT_REMOTE_PORT = 17917

# What a phone or tablet may call: the web UI's own controls. Raw G-code, programs (they take
# file paths on the computer), discovery and phone-access settings stay on the computer.
REMOTE_GET = {"/api/status", "/api/points", "/api/activity", "/api/messages", "/api/vacuum", "/api/spindle"}
REMOTE_POST = {"/api/connect", "/api/disconnect", "/api/hold", "/api/resume", "/api/stop", "/api/unlock",
               "/api/jog", "/api/move", "/api/home", "/api/tool", "/api/probe/z", "/api/zero",
               "/api/vacuum", "/api/spindle", "/api/points"}


def remote_allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in REMOTE_GET or path.startswith("/api/job/")
    if method == "POST":
        return path in REMOTE_POST or (path.startswith("/api/points/") and path.endswith("/goto"))
    if method in ("PATCH", "DELETE"):
        return path.startswith("/api/points/")
    return False


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lan_addresses(probe_host: str | None = None) -> list[str]:
    """This computer's address on the network that reaches the machine (no packets are sent)."""
    ips = []
    for target in [probe_host, "10.255.255.255", "192.168.255.255"]:
        if not target:
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 9))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            continue
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.append(ip)
    return ips


def bonjour_name() -> str | None:
    """The .local name phones resolve over Bonjour (stays valid when DHCP changes the IP)."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2)
            name = out.stdout.strip()
            if name:
                return name + ".local"
        except (OSError, subprocess.SubprocessError):
            pass
    name = socket.gethostname()
    return name if name.endswith(".local") else None


class Remote:
    def __init__(self, data_dir: str, port: int, activity, probe_host=None):
        self.path = os.path.join(data_dir, "devices.json")
        self.port = port
        self.activity = activity
        self.probe_host = probe_host          # callable -> machine IP, to pick the right interface
        self.lock = threading.Lock()
        self.devices: list[dict] = self._load()
        self.code: str | None = None
        self.tries = 0
        self.server: ThreadingHTTPServer | None = None
        self.error: str | None = None
        self._seen_saved = 0.0

    # ---- persistence
    def _load(self) -> list[dict]:
        try:
            with open(self.path) as f:
                return [d for d in json.load(f).get("devices", []) if d.get("hash")]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"devices": self.devices}, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ---- listener
    @property
    def on(self) -> bool:
        return self.server is not None

    def _new_code(self) -> None:
        self.code = f"{secrets.randbelow(10 ** 6):06d}"
        self.tries = 0

    def start(self, handler_cls) -> None:
        with self.lock:
            if self.server:
                return
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", self.port), handler_cls)
            except OSError as e:
                self.error = f"cannot listen on port {self.port}: {e.strerror or e}"
                raise
            srv.daemon_threads = True
            self.server = srv
            self.error = None
            self._new_code()
            threading.Thread(target=srv.serve_forever, name="remote-listener", daemon=True).start()

    def stop(self) -> None:
        with self.lock:
            srv, self.server, self.code = self.server, None, None
        if srv:
            srv.shutdown()
            srv.server_close()

    def bound_port(self) -> int:
        return self.server.server_address[1] if self.server else self.port

    # ---- pairing
    def pair(self, code, name: str, ip: str):
        """Returns (token, device) on success, None on a wrong or missing code."""
        with self.lock:
            if not self.code:
                return None
            given = "".join(ch for ch in str(code or "") if ch.isdigit())
            if not secrets.compare_digest(given, self.code):
                self.tries += 1
                if self.tries >= MAX_TRIES:
                    self._new_code()
                return None
            token = secrets.token_urlsafe(32)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            dev = {"id": secrets.token_hex(4), "name": (str(name or "").strip() or "device")[:40],
                   "hash": _hash(token), "paired": now, "last_seen": now, "ip": ip}
            self.devices.append(dev)
            self._save()
            self._new_code()
            return token, dev

    def check(self, token: str | None) -> dict | None:
        if not token:
            return None
        h = _hash(token)
        for d in self.devices:
            if secrets.compare_digest(d["hash"], h):
                d["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
                if time.time() - self._seen_saved > 60:
                    self._seen_saved = time.time()
                    with self.lock:
                        self._save()
                return d
        return None

    def forget(self, dev_id: str | None = None) -> int:
        with self.lock:
            before = len(self.devices)
            self.devices = [d for d in self.devices if dev_id is not None and d["id"] != dev_id]
            self._save()
            return before - len(self.devices)

    # ---- view
    def urls(self) -> list[str]:
        port = self.bound_port()
        host = self.probe_host() if callable(self.probe_host) else self.probe_host
        names = ([bonjour_name()] if bonjour_name() else []) + lan_addresses(host)
        return [f"http://{n}:{port}/" for n in names if n]

    def view(self, with_code: bool) -> dict:
        v = {"on": self.on, "port": self.bound_port(), "error": self.error,
             "urls": self.urls() if self.on else [],
             "devices": [{k: d.get(k) for k in ("id", "name", "paired", "last_seen", "ip")} for d in self.devices]}
        if with_code:
            v["code"] = self.code
            v["tries_left"] = MAX_TRIES - self.tries if self.code else None
        return v
