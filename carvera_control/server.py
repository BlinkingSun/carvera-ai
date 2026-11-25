"""Carvera Control HTTP server: JSON API + the web UI. Owns the single machine connection,
so the UI, scripts and the MCP all share it."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import program as prog
from .machine import AXES, Busy, Machine, MachineError, discover
from .remote import COOKIE, COOKIE_MAX_AGE, DEFAULT_REMOTE_PORT, Remote, remote_allowed
from .store import Activity, Points

mimetypes.add_type("application/manifest+json", ".webmanifest")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DEFAULT_PORT = 17916
# host "" = use the Carvera found by its UDP 3333 broadcast
DEFAULT_CONFIG = {"host": "", "port": 2222, "safe_z_machine": -3.0,
                  "feeds": {"xy": 1000, "z": 500}, "vacuum_power": 80, "spindle_rpm": 10000,
                  "probe_stow_extra_mm": 2.0,   # 0 = firmware M6 drop; probe_slot overrides PROBE_SLOT_DEFAULT
                  "remote": False, "remote_port": DEFAULT_REMOTE_PORT}   # phone/tablet access (remote.py)


class App:
    def __init__(self, data_dir: str, host: str, port: int, discovery: bool = True):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        cfg_path = os.path.join(data_dir, "config.json")
        self.config = dict(DEFAULT_CONFIG)
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                self.config.update(json.load(f))
        else:
            with open(cfg_path, "w") as f:
                json.dump(self.config, f, indent=2)
        self.activity = Activity(os.path.join(data_dir, "activity.jsonl"))
        self.points = Points(os.path.join(data_dir, "points.json"))
        self.machine = Machine(host or self.config["host"], port or self.config["port"],
                               safe_z_machine=self.config["safe_z_machine"],
                               activity=self.activity, discovery=discovery,
                               probe_stow_extra_mm=self.config.get("probe_stow_extra_mm", 2.0),
                               probe_slot=self.config.get("probe_slot"))
        self.remote = Remote(data_dir, int(self.config.get("remote_port", DEFAULT_REMOTE_PORT)),
                             self.activity, probe_host=lambda: self.machine.host)

    # ---- phone and tablet access
    def start_remote(self) -> None:
        self.remote.start(make_handler(self, remote=True))

    def set_remote(self, on: bool, source: str) -> dict:
        if on:
            self.start_remote()
        else:
            self.remote.stop()
        self._persist("remote", bool(on))
        self.activity(source, "phone_access", {"on": bool(on), "port": self.remote.bound_port()})
        return self.remote.view(with_code=True)

    def _persist(self, key: str, value) -> None:
        """Change one key in data/config.json, keeping the rest of the file as the user wrote it."""
        self.config[key] = value
        path = os.path.join(self.data_dir, "config.json")
        try:
            with open(path) as f:
                cur = json.load(f)
        except (OSError, ValueError):
            cur = {}
        cur[key] = value
        with open(path + ".tmp", "w") as f:
            json.dump(cur, f, indent=2)
        os.replace(path + ".tmp", path)

    # ---- composite operation: go to a saved point
    def op_goto(self, job, name: str, mode: str = "lift", include_z: bool = False,
                feed: float | None = None, z_feed: float | None = None) -> dict:
        m = self.machine
        p = self.points.get(name)
        axes = p["axes"]
        xy = {a: axes[a]["machine"] for a in ("x", "y") if a in axes}
        ztarget = None
        if include_z:
            if "z" not in axes:
                raise MachineError(f"point {name!r} has no Z")
            cap = axes["z"]
            g54 = (m.offsets.get("G54") or {}).get("z")
            if cap.get("g54") is not None and g54 is not None and abs(cap["g54"] - g54) > 0.001:
                raise MachineError(
                    f"work Z zero changed since {name!r} was captured (G54 Z {cap['g54']} -> {g54}); "
                    "its Z target is ambiguous")
            ztarget = cap["work"]
        if not xy and ztarget is None:
            raise MachineError(f"point {name!r} has nothing to move to (only Z captured; use include_z)")
        steps = []
        if mode == "lift" and xy:
            steps.append(m.op_move(job, z=m.safe_z_machine, system="machine", rapid=True))
        elif mode not in ("lift", "direct"):
            raise MachineError("mode must be 'lift' or 'direct'")
        if xy:
            steps.append(m.op_move(job, system="machine", feed=feed, **xy))
        if ztarget is not None:
            steps.append(m.op_move(job, z=ztarget, system="work", z_feed=z_feed))
        return {"point": name, "mode": mode, "include_z": include_z,
                "mpos": m.status.get("mpos"), "wpos": m.status.get("wpos")}

    def state(self) -> dict:
        snap = self.machine.snapshot()
        snap["points_count"] = len(self.points.points)
        snap["config"] = self.config
        snap["activity_seq"] = self.activity.seq
        snap["remote"] = {"on": self.remote.on, "port": self.remote.bound_port(),
                          "devices": len(self.remote.devices)}
        return snap


def make_handler(app: App, remote: bool = False):
    """remote=False: the 127.0.0.1 listener (trusted). remote=True: the LAN listener for paired
    phones and tablets (see remote.py)."""
    m = app.machine

    class H(BaseHTTPRequestHandler):
        server_version = "CarveraControl/0.1"

        def log_message(self, *a):  # quiet
            pass

        # -- helpers
        def _send(self, code: int, obj, headers: dict | None = None) -> None:
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")

        def _source(self) -> str:
            if remote:
                return "remote"
            return (self.headers.get("X-Source") or "api")[:16]

        def _cookie(self) -> str | None:
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except CookieError:
                return None
            v = c.get(COOKIE)
            return v.value if v else None

        def _guard(self, method: str, path: str):
            """Returns (code, body) to refuse the request, or None to go on."""
            host = self.headers.get("Host") or ""
            if not remote and host:
                name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
                if name not in LOCAL_HOSTS:   # DNS rebinding: a web page must not reach this port by name
                    return 403, {"error": "this port answers only on 127.0.0.1; phones and tablets use phone access"}
            origin = self.headers.get("Origin")
            if method != "GET" and origin is not None and urllib.parse.urlparse(origin).netloc != host:
                return 403, {"error": "cross-site request refused"}
            if remote and path.startswith("/api/"):
                if method == "POST" and path == "/api/pair":
                    return None
                if not remote_allowed(method, path):
                    return 403, {"error": "not available from a phone or tablet; do this on the computer running Carvera Control"}
                if not app.remote.check(self._cookie()):
                    return 401, {"error": "pair this device first", "pair": True}
            return None

        def _pair(self, b: dict):
            ip = self.client_address[0]
            got = app.remote.pair(b.get("code"), b.get("name", ""), ip)
            if not got:
                app.activity("remote", "pair:failed", {"ip": ip})
                return self._send(401, {"error": "Wrong code. Use the code shown in Carvera Control on the computer; it changes after "
                                                 "5 wrong tries and after every pairing.", "pair": True})
            token, dev = got
            app.activity("remote", "pair", {"device": dev["name"], "ip": ip})
            cookie = f"{COOKIE}={token}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly; SameSite=Strict"
            return self._send(200, {"paired": True, "device": dev["name"]}, {"Set-Cookie": cookie})

        def _job(self, kind, fn, params, q):
            job = m.start_job(kind, fn, params, source=self._source())
            if q.get("wait", ["0"])[0] in ("1", "true"):
                m.wait_job(job, timeout=float(q.get("timeout", ["900"])[0]))
            from .machine import _job_view
            v = _job_view(job)
            v["messages"] = job["messages"][-60:]
            return self._send(200 if job["state"] != "failed" else 422, v)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(WEB, rel))
            if not full.startswith(WEB) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            data = open(full, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str) -> None:
            u = urllib.parse.urlparse(self.path)
            path, q = u.path, urllib.parse.parse_qs(u.query)
            try:
                refused = self._guard(method, path)
                if refused:
                    return self._send(*refused)
                if not path.startswith("/api/"):
                    if method == "GET":
                        return self._static(path)
                    return self._send(404, {"error": "not found"})
                b = self._body() if method in ("POST", "PATCH") else {}
                if remote and path == "/api/pair":
                    return self._pair(b)
                return self._api(method, path, q, b)
            except Busy as e:
                return self._send(409, {"error": str(e)})
            except (MachineError, ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e).strip("'\"")})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def _api(self, method, path, q, b):
            src = self._source()
            if method == "GET":
                if path == "/api/status":
                    return self._send(200, app.state() | {"client": "remote" if remote else "local"})
                if path == "/api/remote":
                    return self._send(200, app.remote.view(with_code=True))
                if path == "/api/points":
                    return self._send(200, {"points": app.points.list()})
                if path == "/api/activity":
                    return self._send(200, {"items": app.activity.recent(
                        int(q.get("limit", ["50"])[0]), int(q.get("since", ["0"])[0]))})
                if path == "/api/messages":
                    since = int(q.get("since", ["0"])[0])
                    return self._send(200, {"messages": [x for x in list(m.messages) if x["seq"] > since][-200:]})
                if path == "/api/discover":
                    return self._send(200, {"machines": discover(float(q.get("timeout", ["3"])[0]))})
                if path.startswith("/api/job/"):
                    from .machine import _job_view
                    job = m.jobs.get(path.rsplit("/", 1)[-1])
                    if not job:
                        return self._send(404, {"error": "no such job"})
                    v = _job_view(job)
                    v["messages"] = job["messages"][-60:]
                    return self._send(200, v)
                if path == "/api/offsets":
                    return self._send(200, {"offsets": m.offsets, "modal": m.modal})
                if path == "/api/vacuum":
                    return self._send(200, m.vacuum_view() | {"default_power": app.config.get("vacuum_power", 80)})
                if path == "/api/spindle":
                    return self._send(200, m.spindle_view() | {"default_rpm": app.config.get("spindle_rpm", 10000)})
                if path == "/api/program/analyze":
                    return self._send(200, prog.analyze(q["path"][0]))
                return self._send(404, {"error": "unknown endpoint"})

            if method == "POST":
                if path == "/api/connect":
                    return self._send(200, m.connect(b.get("host"), bool(b.get("force"))))
                if path == "/api/disconnect":
                    m.disconnect()
                    return self._send(200, {"connected": False})
                if path == "/api/hold":
                    m.feed_hold(); app.activity(src, "feed_hold")
                    return self._send(200, {"ok": True})
                if path == "/api/resume":
                    m.resume(); app.activity(src, "resume")
                    return self._send(200, {"ok": True})
                if path == "/api/stop":
                    m.stop(); app.activity(src, "STOP")
                    return self._send(200, {"ok": True, "note": "aborted; use unlock before moving again"})
                if path == "/api/vacuum":
                    if "on" not in b:
                        raise MachineError("give on: true or false")
                    on = b["on"] if isinstance(b["on"], bool) else str(b["on"]).lower() in ("1", "true", "on")
                    power = b.get("power")
                    if on and power is None:
                        power = app.config.get("vacuum_power", 80)
                    try:
                        out = m.set_vacuum(on, power)
                    except MachineError as e:
                        app.activity(src, "vacuum:failed", {"on": on, "power": power, "error": str(e)})
                        raise
                    app.activity(src, "vacuum", {"on": on, "power": power if on else None,
                                                 "confirmed": out["confirmed"], "command": out["command"]})
                    return self._send(200, out)
                if path == "/api/spindle":
                    if "on" not in b:
                        raise MachineError("give on: true or false")
                    on = b["on"] if isinstance(b["on"], bool) else str(b["on"]).lower() in ("1", "true", "on")
                    rpm = b.get("rpm")
                    if on and rpm is None:
                        rpm = app.config.get("spindle_rpm", 10000)
                    try:
                        out = m.set_spindle(on, rpm)
                    except MachineError as e:
                        app.activity(src, "spindle:failed", {"on": on, "rpm": rpm if on else None, "error": str(e)})
                        raise
                    app.activity(src, "spindle", {"on": on, "rpm": rpm if on else None,
                                                  "confirmed": out["confirmed"], "command": out["command"]})
                    return self._send(200, out)
                if path == "/api/remote":
                    if "on" not in b:
                        raise MachineError("give on: true or false")
                    try:
                        return self._send(200, app.set_remote(bool(b["on"]), src))
                    except OSError:
                        return self._send(409, app.remote.view(with_code=True) | {"error": app.remote.error})
                if path == "/api/remote/forget":
                    n = app.remote.forget(b.get("id"))
                    app.activity(src, "phone_forget", {"id": b.get("id") or "all", "removed": n})
                    return self._send(200, app.remote.view(with_code=True))
                if path == "/api/offsets/refresh":
                    if m._op_lock.locked():
                        raise Busy("machine busy")
                    return self._send(200, {"offsets": m.refresh_offsets(), "modal": m.modal})
                if path == "/api/unlock":
                    return self._job("unlock", m.op_unlock, {}, q)
                if path == "/api/jog":
                    return self._job("jog", m.op_jog, {"axis": b["axis"], "distance": float(b["distance"]),
                                                       "feed": b.get("feed")}, q)
                if path == "/api/move":
                    p = {k: b.get(k) for k in ("x", "y", "z", "feed", "z_feed")}
                    p.update(system=b.get("system", "machine"), rapid=bool(b.get("rapid", False)))
                    return self._job("move", m.op_move, p, q)
                if path == "/api/home":
                    return self._job("home", m.op_home, {}, q)
                if path == "/api/tool":
                    return self._job("tool_change", m.op_tool_change,
                                     {"tool": int(b["tool"]), "remeasure": bool(b.get("remeasure", False))}, q)
                if path == "/api/probe/z":
                    keys = ("max_distance", "fast_feed", "slow_feed", "retract", "lift", "set_zero", "allow_non_probe")
                    return self._job("probe_z", m.op_probe_z, {k: b[k] for k in keys if k in b}, q)
                if path == "/api/zero":
                    vals = b.get("values") or {a: 0.0 for a in b.get("axes", [])}
                    return self._job("set_work_zero", m.op_set_work, {"values": vals}, q)
                if path == "/api/program/upload":
                    fp = os.path.expanduser(b["path"])
                    name = b.get("name") or re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(fp))
                    prog.remote_path(name)
                    return self._job("upload", m.op_upload, {"path": fp, "name": name}, q)
                if path == "/api/program/run":
                    fp = os.path.expanduser(b["path"])
                    name = b.get("name") or re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(fp))
                    p = {"path": fp, "name": name}
                    for k in ("margin_xy", "margin_z", "check_below_z"):
                        if k in b:
                            p[k] = float(b[k])
                    return self._job("run_program", m.op_run_program, p, q)
                if path == "/api/gcode":
                    lines = b.get("lines") or str(b.get("gcode", "")).splitlines()
                    return self._job("gcode", m.op_gcode, {"lines": lines,
                                                            "allow_spindle": bool(b.get("allow_spindle", False))}, q)
                if path == "/api/points":
                    axes = b.get("axes") or ["x", "y", "z"]
                    if not m.connected:
                        raise MachineError("not connected")
                    pt = app.points.capture(b.get("name", ""), axes, m.snapshot(), b.get("note"))
                    app.activity(src, "save_point", {"name": pt["name"], "axes": axes,
                                                     "values": {a: pt["axes"][a] for a in axes}})
                    return self._send(200, pt)
                mm = re.match(r"^/api/points/(.+)/goto$", path)
                if mm:
                    name = urllib.parse.unquote(mm.group(1))
                    p = {"name": name, "mode": b.get("mode", "lift"), "include_z": bool(b.get("include_z", False)),
                         "feed": b.get("feed"), "z_feed": b.get("z_feed")}
                    return self._job("goto_point", app.op_goto, p, q)
                return self._send(404, {"error": "unknown endpoint"})

            if method == "PATCH":
                mm = re.match(r"^/api/points/(.+)$", path)
                if mm:
                    name = urllib.parse.unquote(mm.group(1))
                    pt = app.points.update(name, b.get("note"), b.get("new_name"))
                    app.activity(src, "edit_point", {"name": name, "changes": b})
                    return self._send(200, pt)
            if method == "DELETE":
                mm = re.match(r"^/api/points/(.+)$", path)
                if mm:
                    name = urllib.parse.unquote(mm.group(1))
                    app.points.delete(name)
                    app.activity(src, "delete_point", {"name": name})
                    return self._send(200, {"deleted": name})
            return self._send(404, {"error": "unknown endpoint"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

    return H


def build_server(app: App, port: int, bind: str = "127.0.0.1") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), make_handler(app))
    srv.daemon_threads = True
    return srv


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Carvera Control server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CARVERA_PORT", DEFAULT_PORT)))
    ap.add_argument("--machine", help="machine IP (default from data/config.json)")
    ap.add_argument("--machine-port", type=int, default=None)
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--sim", action="store_true", help="connect to the simulator at 127.0.0.1:22222")
    ap.add_argument("--connect", action="store_true", help="connect to the machine at startup")
    ap.add_argument("--remote", action="store_true", help="turn on phone/tablet access (LAN, pairing code)")
    a = ap.parse_args(argv)
    if a.sim:
        app = App(os.path.join(a.data, "sim"), "127.0.0.1", a.machine_port or 22222, discovery=False)
    else:
        app = App(a.data, a.machine, a.machine_port)
    srv = build_server(app, a.port)
    where = f"{app.machine.host}:{app.machine.port}" if app.machine.host else "auto-discover on UDP 3333"
    print(f"Carvera Control on http://127.0.0.1:{a.port}  (machine {where})", flush=True)
    if a.remote or app.config.get("remote"):
        try:
            app.start_remote()
            v = app.remote.view(with_code=True)
            print(f"Phone/tablet access on {', '.join(v['urls']) or 'port ' + str(v['port'])}  "
                  f"(pairing code {v['code']})", flush=True)
        except OSError:
            print(f"Phone/tablet access failed: {app.remote.error}", flush=True)
    if a.connect:
        threading.Thread(target=lambda: _try_connect(app), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.remote.stop()
        if app.machine.connected:
            app.machine.disconnect()


def _try_connect(app: App) -> None:
    try:
        app.machine.connect()
    except Exception as e:  # noqa: BLE001
        app.activity("system", "connect_failed", {"error": str(e)})


if __name__ == "__main__":
    main()
