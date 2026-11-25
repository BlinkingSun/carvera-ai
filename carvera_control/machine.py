"""Carvera connection: one TCP owner, live status, and guarded machine operations.

Protocol facts (verified on a Carvera C1, firmware 1.0.3, 2026-11-21):
  * plain-text TCP on port 2222; discovery broadcast on UDP 3333: "name,ip,port,busy"
  * "?" (no newline) -> "<State|MPos:x,y,z,a,b|WPos:...|F:..|S:rpm,target,..|T:tool,tlo|W:probe_v|...>"
  * one "ok" per line; $H, G38.x and M400 hold their "ok" until the motion has finished
  * M6T<n> prints a stream of internal commands and many "ok"s, ending with "Done ATC"
  * T0 = wireless probe, T1-T6 = rack, T-1 = empty; M119 shows "Probe: 0/1"
  * Setting a work Z (G10 L2/L20 with Z) resets the active TLO to 0 and makes the current
    tool the length reference; later tools are measured against it.
  * The "?" status has no vacuum field on fw 1.0.3. The "diagnose" shell command (no "ok", works in
    Alarm) answers "{S:..|L:..|V:on,power|F:..|G:light|T:..|R:air|C:probecharger|E:..|P:..|A:..|I:estop}"
    (verified 2026-11-23). Onboard vacuum: M801 S<1-100> on, M802 off; the switch acts at once, even
    while a file plays. A halt turns the vacuum and the probe charger off.
"""
from __future__ import annotations

import collections
import os
import re
import socket
import threading
import time
import uuid

from . import program as prog

LIMITS_DEFAULT = {"x": (-371.0, 0.0), "y": (-250.0, 0.0), "z": (-135.0, 0.0)}

# The firmware's own drop script for the probe (T0), exactly as fw 1.0.3 printed it on 2026-11-24
# (ATCHandler::fill_drop_scripts; M499.2 reports the same slot to 0.1 mm):
#   G53 G0 Z-3 / G53 G0 X-3.403 Y-25.143 / M492.2 / G53 G1 Z-97.5 F1000 / G53 G1 Z-112.5 F200 /
#   M490.2 / G53 G0 Z-50 / M493.2 T-1 / M492.1
# It releases the probe at the slot Z, which often leaves it sitting off the charging coil (2026-11-23,
# 2026-11-24). The deep stow runs the same script but plunges probe_stow_extra_mm lower, slowly,
# before releasing.
PROBE_SLOT_DEFAULT = {"x": -3.403, "y": -25.143, "z": -112.5, "approach": 15.0,
                      "fast_feed": 1000.0, "slow_feed": 200.0, "lift_z": -50.0, "clearance_z": -3.0}
STOW_EXTRA_MAX = 3.0        # mm; refuse anything deeper
STOW_EXTRA_FEED = 100.0     # mm/min for the extra plunge
SPINDLE_RPM = (1000, 20000)  # UI spindle range (S20000 reads about 16-17k rpm on this spindle)
# Jobs allowed while the spindle turns (manual control, USER 2026-11-24: "allow for jogging while the spindle
# runs"). Home, tool change, probing, upload and program runs still need the spindle stopped.
SPINDLE_OK_JOBS = {"jog", "move", "goto_point", "set_work_zero", "gcode"}
MAX_FEED = {"x": 3000.0, "y": 3000.0, "z": 2000.0}
AXES = ("x", "y", "z")
TOOL_NAMES = {0: "Probe", -1: "Empty", 8888: "Laser"}
# Kernel.h HALT_REASON (fw 1.0.3). 1-15 clear with Unlock; 21-26 need a reset; 41 needs a power cycle.
HALT_REASONS = {1: "manual (stop/abort)", 2: "home failed", 3: "probe failed", 4: "calibrate failed",
                5: "ATC home failed", 6: "ATC tool invalid", 7: "ATC no tool", 8: "ATC has tool",
                9: "spindle overheated", 10: "soft limit", 11: "cover open", 12: "probe invalid",
                13: "E-stop", 14: "power overheated", 15: "not homed", 21: "hard limit",
                22: "motor error X", 23: "motor error Y", 24: "motor error Z", 25: "spindle stall",
                26: "SD error", 41: "spindle alarm"}

_ERR_RE = re.compile(r"^(error|alarm|!!|halt)", re.I)
_PRB_RE = re.compile(r"\[PRB:([-\d.]+),([-\d.]+),([-\d.]+):(\d)\]")


class MachineError(Exception):
    pass


class Busy(MachineError):
    pass


def tool_name(n: int | None) -> str:
    if n is None:
        return "?"
    return TOOL_NAMES.get(n, f"T{n}")


def parse_status(line: str) -> dict | None:
    m = re.match(r"<([^|>]+)\|(.*)>$", line.strip())
    if not m:
        return None
    fields = {}
    for part in m.group(2).split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            k = k.strip()
            fields[k + "#2" if k in fields else k] = v   # fw 1.0.3 sends F twice: feed, then spindle fan

    def nums(s):
        out = []
        for x in s.split(","):
            x = x.strip()
            try:
                out.append(float(x))
            except ValueError:
                pass
        return out

    st: dict = {"state": m.group(1).split(":")[0], "raw": line.strip()}
    for key, name in (("MPos", "mpos"), ("WPos", "wpos")):
        if key in fields:
            v = nums(fields[key])
            st[name] = {"x": v[0], "y": v[1], "z": v[2], "a": v[3] if len(v) > 3 else None}
    if "T" in fields:
        v = fields["T"].split(",")
        try:
            st["tool"] = int(float(v[0]))
            st["tlo"] = float(v[1]) if len(v) > 1 else None
        except ValueError:
            pass
    if "S" in fields:
        v = nums(fields["S"])
        st["spindle_rpm"] = v[0] if v else None
        st["spindle_target"] = v[1] if len(v) > 1 else None
        if len(v) > 3:
            st["vacuum_mode"] = bool(v[3])          # M331/M332: built-in vacuum follows the spindle
    if "F" in fields:
        v = nums(fields["F"])
        st["feed"] = v[0] if v else None
    if "W" in fields:
        v = nums(fields["W"])
        st["probe_v"] = v[0] if v else None
    # CarveraController reads these: A = atc_state, O = auto-leveling max delta (its "max:" readout;
    # non-zero while leveling data is held, cleared by M370).
    if "A" in fields:
        v = nums(fields["A"])
        st["atc_state"] = v[0] if v else None
    if "O" in fields:
        v = nums(fields["O"])
        st["leveling_max"] = v[0] if v else None
    if "V" in fields:   # built-in vacuum switch (ATC machines): state, value (0-100)
        v = nums(fields["V"])
        if len(v) >= 2:
            st["vacuum"] = {"on": bool(v[0]), "value": int(v[1])}
    if "F#2" in fields:  # spindle fan switch: state, value
        v = nums(fields["F#2"])
        if len(v) >= 2:
            st["spindle_fan"] = {"on": bool(v[0]), "value": int(v[1])}
    if "H" in fields:   # present only while halted
        v = nums(fields["H"])
        if v:
            st["halt_reason"] = int(v[0])
            st["halt_text"] = HALT_REASONS.get(int(v[0]), "unknown")
    if "P" in fields:   # playing a file: lines played, percent, elapsed seconds
        v = nums(fields["P"])
        if len(v) >= 3:
            st["progress"] = {"lines": int(v[0]), "percent": int(v[1]), "secs": int(v[2])}
    return st


def parse_diagnose(line: str) -> dict | None:
    """Reply to the 'diagnose' shell command (fw 1.0.3 SimpleShell::diagnose_command)."""
    m = re.match(r"^\{(.*)\}$", line.strip())
    if not m or "V:" not in m.group(1):
        return None
    f: dict[str, list[int]] = {}
    for part in m.group(1).split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            vals = []
            for x in v.split(","):
                try:
                    vals.append(int(float(x)))
                except ValueError:
                    pass
            f[k.strip()] = vals
    d: dict = {}
    if len(f.get("V", [])) >= 2:      # built-in vacuum switch (ATC machines): state, value %
        d["vacuum"] = {"on": bool(f["V"][0]), "power": f["V"][1]}
    if len(f.get("F", [])) >= 2:
        d["spindle_fan"] = {"on": bool(f["F"][0]), "power": f["F"][1]}
    for key, name in (("G", "light"), ("R", "air"), ("C", "probe_charger"), ("T", "tool_sensor"), ("I", "estop")):
        if f.get(key):
            d[name] = bool(f[key][0])
    if f.get("S"):
        d["spindle"] = {"on": bool(f["S"][0]), "target": f["S"][1] if len(f["S"]) > 1 else None}
    return d


def discover(timeout: float = 3.0) -> list[dict]:
    """Listen for Carvera discovery broadcasts on UDP 3333."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", 3333))
    s.settimeout(0.5)
    found: dict[str, dict] = {}
    end = time.time() + timeout
    try:
        while time.time() < end:
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                continue
            parts = data.decode(errors="replace").strip().split(",")
            if len(parts) >= 4:
                found[parts[1]] = {"name": parts[0], "ip": parts[1], "port": int(parts[2]),
                                   "busy": parts[3].strip() == "1"}
    finally:
        s.close()
    return list(found.values())


class Machine:
    def __init__(self, host: str, port: int = 2222, *, limits=None, safe_z_machine: float = -3.0,
                 activity=None, discovery: bool = True, probe_stow_extra_mm: float = 2.0,
                 probe_slot: dict | None = None):
        self.host, self.port = host, port
        self.limits = dict(limits or LIMITS_DEFAULT)
        self.safe_z_machine = safe_z_machine
        self.probe_stow_extra_mm = float(probe_stow_extra_mm or 0.0)   # 0 = the firmware's own M6 drop
        self.probe_slot = dict(PROBE_SLOT_DEFAULT, **(probe_slot or {}))
        self.activity = activity or (lambda *a, **k: None)
        self.discovery = discovery
        self.sock: socket.socket | None = None
        self.connected = False
        self.info: dict = {}
        self.status: dict = {}
        self.status_time = 0.0
        self.status_seq = 0
        self.offsets: dict = {}
        self.modal = ""
        self.messages: collections.deque = collections.deque(maxlen=300)
        self.msg_seq = 0
        self.job: dict | None = None
        self.jobs: collections.OrderedDict = collections.OrderedDict()
        self._wlock = threading.Lock()        # socket writes
        self._cmd_lock = threading.RLock()    # one command/ok exchange at a time
        self._op_lock = threading.Lock()      # one machine operation at a time
        self._cv = threading.Condition()
        self._ok_count = 0
        self._collect: list[str] = []
        self._error: str | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._buf = ""
        self._quiet = False   # suppress routine $#/$G replies from the message feed
        self._raw: bytearray | None = None   # upload: reader collects raw bytes instead of lines
        self._raw_pos = 0
        self._raw_rest = b""                 # bytes left over after an upload, fed to the reader in order
        self._poll_pause = threading.Event()  # upload: no "?" may reach the machine
        self.diag: dict = {}                 # last 'diagnose' reply (vacuum, probe charger, light, air ...)
        self.diag_time = 0.0
        self.diag_seq = 0
        self._diag_sent = 0.0                # time of the outstanding 'diagnose' request (0 = none)

    # ------------------------------------------------------------------ connection
    def connect(self, host: str | None = None, force: bool = False) -> dict:
        if self.connected:
            return self.snapshot()
        if host:
            self.host = host
        if not self.host:
            # no machine configured: use the Carvera that announces itself on UDP 3333
            found = discover(3.0) if self.discovery else []
            if not found:
                raise MachineError('no Carvera found on the network (UDP 3333 broadcast). Set "host" in '
                                   'data/config.json, start the server with --machine <ip>, or connect with '
                                   '{"host": "<ip>"}')
            pick = ([m for m in found if not m["busy"]] or found)[0]
            self.host, self.port = pick["ip"], pick["port"]
        if self.discovery and not force:
            seen = {m["ip"]: m for m in discover(2.0)}
            m = seen.get(self.host)
            if m and m["busy"]:
                raise MachineError(
                    f"{m['name']} at {self.host} reports another controller is connected "
                    "(close CarveraController first, or connect with force)")
            if m:
                self.info["name"] = m["name"]
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(0.5)
        self.sock = sock
        self.connected = True
        self._stop.clear()
        self._buf = ""
        self.diag, self.diag_time, self._diag_sent = {}, 0.0, 0.0
        for fn in (self._reader, self._poller):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        try:
            self._wait_fresh_status(2, timeout=5)
            ver = self._query("version", r"^version\s*=", timeout=3)
            if ver:
                self.info["version"] = ver.split("=", 1)[-1].strip()
            self.refresh_offsets()
        except Exception:
            self.disconnect()
            raise
        self.activity("system", "connect", {"host": self.host, **self.info})
        return self.snapshot()

    def disconnect(self) -> None:
        self._stop.set()
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.diag, self.diag_time = {}, 0.0
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()
        with self._cv:
            self._cv.notify_all()
        self.activity("system", "disconnect", {"host": self.host})

    def _write(self, data: bytes) -> None:
        if not self.sock or not self.connected:
            raise MachineError("not connected")
        with self._wlock:
            self.sock.sendall(data)

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.sock.recv(8192)
                if not data:
                    raise OSError("socket closed by machine")
            except socket.timeout:
                data = b""
            except OSError as e:
                if not self._stop.is_set():
                    self.connected = False
                    self._note(f"connection lost: {e}")
                    with self._cv:
                        self._error = f"connection lost: {e}"
                        self._cv.notify_all()
                return
            with self._cv:
                if self._raw is not None:
                    self._raw.extend(data)
                    self._cv.notify_all()
                    continue
                if self._raw_rest:
                    data, self._raw_rest = self._raw_rest + data, b""
            if not data:
                continue
            self._buf += data.decode(errors="replace")
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._on_line(line.strip())

    def _on_line(self, line: str) -> None:
        if not line:
            return
        if line.startswith("{") and line.endswith("}"):
            d = parse_diagnose(line)
            if d is not None:      # routine poll reply: state only, not a message
                with self._cv:
                    self.diag, self.diag_time = d, time.time()
                    self.diag_seq += 1
                    self._diag_sent = 0.0
                    self._cv.notify_all()
                return
        if line.startswith("<"):
            st = parse_status(line)
            if st:
                with self._cv:
                    self.status = st
                    self.status_time = time.time()
                    self.status_seq += 1
                    self._cv.notify_all()
            return
        with self._cv:
            if line == "ok":
                self._ok_count += 1
            else:
                self._collect.append(line)
                if _ERR_RE.match(line):
                    self._error = line
            self._cv.notify_all()
        if line != "ok" and not self._quiet:
            self._note(line)

    def _note(self, text: str) -> None:
        self.msg_seq += 1
        item = {"seq": self.msg_seq, "t": time.time(), "text": text}
        self.messages.append(item)
        if self.job and self.job.get("state") == "running":
            self.job["messages"].append(text)

    DIAG_EVERY = 1.0       # s between 'diagnose' polls
    DIAG_RESEND = 10.0     # s before an unanswered 'diagnose' is sent again (the firmware queues
                           # shell lines while a blocking command such as $H or md5sum runs)
    DIAG_FRESH = 6.0       # s a 'diagnose' reply counts as current

    def _poller(self) -> None:
        while not self._stop.is_set():
            if not self._poll_pause.is_set():
                try:
                    self._write(b"?")
                    now = time.time()
                    if (now - self.diag_time >= self.DIAG_EVERY
                            and (not self._diag_sent or now - self._diag_sent > self.DIAG_RESEND)):
                        self._diag_sent = now
                        self._write(b"diagnose\n")
                except Exception:
                    return
            self._stop.wait(0.25)

    def _fresh_diag(self, after_seq: int, timeout: float = 3.0) -> dict | None:
        """Ask for a 'diagnose' and wait for a reply newer than after_seq."""
        self._diag_sent = time.time()
        self._write(b"diagnose\n")
        deadline = time.time() + timeout
        with self._cv:
            while self.diag_seq <= after_seq:
                rem = deadline - time.time()
                if rem <= 0 or not self.connected:
                    return None
                self._cv.wait(min(rem, 0.2))
            return dict(self.diag)

    # ------------------------------------------------------------------ low level
    def _cmd(self, line: str, timeout: float = 10.0, alarm_ok: bool = False) -> list[str]:
        """Send one line and wait for its ok. Returns the non-status lines received meanwhile.
        Fails fast if the machine drops into Alarm (e.g. after Stop), unless alarm_ok."""
        with self._cmd_lock:
            with self._cv:
                base = self._ok_count
                self._collect = []
                self._error = None
            self._write((line + "\n").encode())
            deadline = time.time() + timeout
            with self._cv:
                while self._ok_count <= base and self._error is None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise MachineError(f"timed out waiting for ok after {line!r}")
                    if not self.connected:
                        raise MachineError("connection lost")
                    if not alarm_ok and str(self.status.get("state", "")).startswith("Alarm"):
                        raise MachineError(f"machine in Alarm while waiting on {line!r}")
                    self._cv.wait(min(remaining, 0.5))
                lines, err = list(self._collect), self._error
            if err:
                raise MachineError(f"{line!r}: {err}")
            return lines

    def _query(self, line: str, pattern: str, timeout: float = 3.0) -> str | None:
        """Shell commands such as 'version' answer with a line but no 'ok' (verified on fw 1.0.3).
        Send, then wait for a reply line matching pattern."""
        rx = re.compile(pattern)
        with self._cmd_lock:
            with self._cv:
                self._collect = []
                self._error = None
            self._write((line + "\n").encode())
            deadline = time.time() + timeout
            with self._cv:
                while time.time() < deadline:
                    hit = next((x for x in self._collect if rx.search(x)), None)
                    if hit:
                        return hit
                    self._cv.wait(0.2)
        return None

    def realtime(self, ch: bytes) -> None:
        self._write(ch)

    def _wait_fresh_status(self, n: int = 2, timeout: float = 5.0) -> dict:
        with self._cv:
            target = self.status_seq + n
            deadline = time.time() + timeout
            while self.status_seq < target:
                rem = deadline - time.time()
                if rem <= 0 or not self.connected:
                    raise MachineError("no status from machine")
                self._cv.wait(min(rem, 0.5))
            return dict(self.status)

    def _spindle_run(self, st: dict) -> bool:
        """fw 1.0.3 reports state Run whenever the spindle is on, even with nothing moving (seen 2026-11-24:
        every jog after M3 failed 'machine is Run, not Idle'). True when that is the only reason for Run:
        the spindle is on, no program is playing and no tool change is running."""
        if st.get("state") != "Run" or st.get("progress") or st.get("atc_state") is not None:
            return False
        fresh = self.diag_time and time.time() - self.diag_time < self.DIAG_FRESH
        sp = self.diag.get("spindle") if fresh else None
        return (st.get("spindle_rpm") or 0) > 50 or bool(sp and sp.get("on"))

    def _wait_motion_done(self, timeout: float = 120.0) -> dict:
        """M400 holds its ok until the planner queue is empty; then confirm Idle (or Run only because the
        spindle is on: manual moves with the spindle running)."""
        self._cmd("M400", timeout=timeout)
        st = self._wait_fresh_status(2)
        deadline = time.time() + 10
        while st.get("state") != "Idle" and not self._spindle_run(st) and time.time() < deadline:
            if st.get("state", "").startswith("Alarm"):
                raise MachineError(f"machine in {st['state']}")
            st = self._wait_fresh_status(1)
        return st

    def refresh_offsets(self) -> dict:
        self._quiet = True
        try:
            # read-only queries: allowed in Alarm, so we can connect after an E-stop or halt
            lines = self._cmd("$#", timeout=5, alarm_ok=True)
            g = self._cmd("$G", timeout=5, alarm_ok=True)
        finally:
            self._quiet = False
        offs: dict = {}
        for line in lines:
            m = re.match(r"\[(G5[4-9](?:\.\d)?|G28|G30|G92):([-\d.,]+)\]", line)
            if m:
                v = [float(x) for x in m.group(2).split(",")]
                offs[m.group(1)] = {"x": v[0], "y": v[1], "z": v[2]}
                if len(v) > 3:
                    offs[m.group(1)]["a"] = v[3]
            m = re.match(r"\[TL[O0]:([-\d.]+)\]", line)
            if m:
                offs["TLO"] = float(m.group(1))
            m = _PRB_RE.search(line)
            if m:
                offs["PRB"] = {"x": float(m.group(1)), "y": float(m.group(2)),
                               "z": float(m.group(3)), "hit": m.group(4) == "1"}
        self.offsets = offs
        self.modal = next((x.strip("[]") for x in g if x.startswith("[")), "")
        return offs

    # ------------------------------------------------------------------ jobs
    def start_job(self, kind: str, fn, params: dict, source: str = "api") -> dict:
        if not self.connected:
            raise MachineError("not connected")
        if not self._op_lock.acquire(blocking=False):
            cur = self.job["kind"] if self.job else "operation"
            raise Busy(f"machine busy ({cur} running)")
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "params": params, "source": source,
               "state": "running", "started": time.time(), "finished": None,
               "messages": [], "result": None, "error": None}
        self.job = job
        self.jobs[job["id"]] = job
        while len(self.jobs) > 50:
            self.jobs.popitem(last=False)
        self.activity(source, kind, {"params": params, "job": job["id"]})

        def run():
            try:
                st = self._wait_fresh_status(1)
                if kind not in ("unlock",) and st.get("state") != "Idle":
                    if not self._spindle_run(st):
                        raise MachineError(f"machine is {st.get('state')}, not Idle")
                    if kind not in SPINDLE_OK_JOBS:      # manual moves only while the spindle turns
                        raise MachineError(f"the spindle is running; stop it before {kind.replace('_', ' ')}")
                job["result"] = fn(job, **params)
                job["state"] = "done"
            except Exception as e:  # noqa: BLE001 - report every failure to the caller
                job["state"] = "failed"
                job["error"] = str(e)
            finally:
                job["finished"] = time.time()
                self._op_lock.release()
                self.activity(source, f"{kind}:{job['state']}",
                              {"job": job["id"], "result": job["result"], "error": job["error"]})
        threading.Thread(target=run, daemon=True).start()
        return job

    def wait_job(self, job: dict, timeout: float = 600) -> dict:
        end = time.time() + timeout
        while job["state"] == "running" and time.time() < end:
            time.sleep(0.05)
        return job

    # ------------------------------------------------------------------ guards
    def _check_limits(self, target: dict) -> None:
        for ax, v in target.items():
            lo, hi = self.limits[ax]
            if not (lo <= v <= hi):
                raise MachineError(f"{ax.upper()} {v:.3f} is outside machine travel [{lo}, {hi}]")

    def _feed(self, ax: str, feed: float | None, default: float) -> float:
        f = float(feed or default)
        if f <= 0:
            raise MachineError("feed must be positive")
        return min(f, MAX_FEED[ax])

    def _mpos(self) -> dict:
        return dict(self.status.get("mpos") or {})

    def work_to_machine(self, ax: str, value: float) -> float:
        st = self.status
        return value + (st["mpos"][ax] - st["wpos"][ax])

    # ------------------------------------------------------------------ operations
    def op_jog(self, job, axis: str, distance: float, feed: float | None = None) -> dict:
        ax = axis.lower()
        if ax not in AXES:
            raise MachineError(f"unknown axis {axis!r}")
        distance = float(distance)
        if distance == 0:
            raise MachineError("distance is 0")
        start = self._mpos()
        target = start[ax] + distance
        self._check_limits({ax: target})
        f = self._feed(ax, feed, 500 if ax == "z" else 1000)
        self._cmd("G91")
        try:
            self._cmd(f"G1 {ax.upper()}{distance:.4f} F{f:.0f}")
        finally:
            self._cmd("G90")
        st = self._wait_motion_done(timeout=abs(distance) / f * 60 * 1.5 + 15)
        return {"axis": ax, "distance": distance, "feed": f, "from": start[ax],
                "mpos": st.get("mpos"), "wpos": st.get("wpos")}

    def op_move(self, job, x=None, y=None, z=None, system: str = "machine",
                feed: float | None = None, z_feed: float | None = None, rapid: bool = False) -> dict:
        if system not in ("machine", "work"):
            raise MachineError("system must be 'machine' or 'work'")
        req = {k: float(v) for k, v in (("x", x), ("y", y), ("z", z)) if v is not None}
        if not req:
            raise MachineError("no target axes given")
        tgt = {ax: (self.work_to_machine(ax, v) if system == "work" else v) for ax, v in req.items()}
        self._check_limits(tgt)
        cur = self._mpos()
        fxy = self._feed("x", feed, 1000)
        fz = self._feed("z", z_feed, 500)
        g = "G0" if rapid else "G1"

        def xy():
            words = " ".join(f"{a.upper()}{tgt[a]:.4f}" for a in ("x", "y") if a in tgt)
            if words:
                self._cmd(f"G53 {g} {words}" + ("" if rapid else f" F{fxy:.0f}"))

        def zz():
            if "z" in tgt:
                self._cmd(f"G53 {g} Z{tgt['z']:.4f}" + ("" if rapid else f" F{fz:.0f}"))
        # going up: Z first; going down: XY first
        if "z" in tgt and tgt["z"] > cur["z"]:
            zz(); xy()
        else:
            xy(); zz()
        dist = sum((tgt[a] - cur[a]) ** 2 for a in tgt) ** 0.5
        st = self._wait_motion_done(timeout=dist / min(fxy, fz) * 60 * 1.5 + 15)
        return {"target_machine": tgt, "system": system, "mpos": st.get("mpos"), "wpos": st.get("wpos")}

    def op_home(self, job) -> dict:
        self._cmd("$H", timeout=180)
        st = self._wait_motion_done(timeout=30)
        self.refresh_offsets()
        return {"mpos": st.get("mpos")}

    def op_tool_change(self, job, tool: int, remeasure: bool = False) -> dict:
        tool = int(tool)
        if tool not in (-1, 0, 1, 2, 3, 4, 5, 6):
            raise MachineError("tool must be -1 (empty), 0 (probe) or 1-6")
        st = self.status
        if (st.get("spindle_rpm") or 0) > 50:
            raise MachineError("spindle is turning; stop it before a tool change")
        if st.get("tool") == tool and not remeasure:
            return {"tool": tool, "tlo": st.get("tlo"), "note": f"{tool_name(tool)} already loaded"}
        old = st.get("tool")
        stow = None
        if old == 0 and tool != 0 and self.probe_stow_extra_mm > 0:
            stow = self._stow_probe_deep()
            if tool == -1:
                st = self._wait_fresh_status(3)
                self.refresh_offsets()
                return {"from": 0, "tool": st.get("tool"), "tlo": st.get("tlo"), "mpos": st.get("mpos"),
                        "stow": stow}
            # the spindle is empty now: the M6 below only picks and measures the new tool
        start_seq = self.msg_seq
        with self._cmd_lock:
            with self._cv:
                self._error = None
            self._write(f"M6T{tool}\n".encode())
            deadline = time.time() + 300
            done = False
            while time.time() < deadline and not done:
                with self._cv:
                    self._cv.wait(0.5)
                    if self._error:
                        raise MachineError(f"tool change failed: {self._error}")
                if not self.connected:
                    raise MachineError("connection lost during tool change")
                if str(self.status.get("state", "")).startswith("Alarm"):
                    raise MachineError("machine in Alarm during tool change")
                done = any(m["seq"] > start_seq and "Done ATC" in m["text"] for m in list(self.messages))
            if not done:
                raise MachineError("tool change did not report 'Done ATC' within 300 s")
            time.sleep(1.0)  # let any trailing "ok" land before the next command
        st = self._wait_fresh_status(3)
        self.refresh_offsets()
        prb = self.offsets.get("PRB")
        out = {"from": old, "tool": st.get("tool"), "tlo": st.get("tlo"),
               "setter_z": prb["z"] if prb else None, "mpos": st.get("mpos")}
        if stow:
            out["stow"] = stow
        return out

    def _stow_probe_deep(self) -> dict:
        """Put the probe back in its slot like the firmware's M6 drop, but plunge probe_stow_extra_mm
        lower (slowly) before releasing, so it seats on the charging coil (USER 2026-11-24: "add 2mm of
        plunge when stowing"). Same safety checks as the firmware: M492.2 halts if the slot is not
        empty, M492.1 halts if the probe did not land in it. M120/M121 save and restore the modal state
        the way M6 does, and the move back ends at the starting X/Y at clearance Z, like M6."""
        s = self.probe_slot
        extra = self.probe_stow_extra_mm
        if not 0 < extra <= STOW_EXTRA_MAX:
            raise MachineError(f"probe_stow_extra_mm must be 0 (off) or up to {STOW_EXTRA_MAX} mm, not {extra}")
        deep = s["z"] - extra
        if deep < self.limits["z"][0]:
            raise MachineError(f"deep stow Z {deep} is past the Z soft limit")
        # the configured slot must still be where the firmware keeps it (M499.2 prints 1 decimal)
        lines = self._cmd("M499.2", timeout=5)
        m = next((m for m in (re.search(r"tool0 -- mx:(-?[\d.]+) my:(-?[\d.]+) mz:(-?[\d.]+)", x) for x in lines)
                  if m), None)
        if not m:
            raise MachineError("M499.2 did not report the probe slot (tool0); not stowing")
        fw = tuple(float(v) for v in m.groups())
        if any(abs(a - b) > 0.06 for a, b in zip(fw, (s["x"], s["y"], s["z"]))):
            raise MachineError(f"probe slot mismatch: firmware has X{fw[0]} Y{fw[1]} Z{fw[2]}, Carvera Control "
                               f"has X{s['x']} Y{s['y']} Z{s['z']}; update probe_slot in data/config.json")
        st = self._wait_fresh_status(1)
        v_before = st.get("probe_v")
        start = self._mpos()
        seq = [
            "M120",
            f"G53 G0 Z{s['clearance_z']:.3f}",
            f"G53 G0 X{s['x']:.3f} Y{s['y']:.3f}",
            "M492.2",                                                  # halt if the slot is not empty
            f"G53 G1 Z{s['z'] + s['approach']:.3f} F{s['fast_feed']:.0f}",
            f"G53 G1 Z{s['z']:.3f} F{s['slow_feed']:.0f}",
            f"G53 G1 Z{deep:.3f} F{STOW_EXTRA_FEED:.0f}",              # the extra plunge
            "M400",
            "M490.2",                                                  # release
            f"G53 G0 Z{s['lift_z']:.3f}",
            "M493.2 T-1",
            "M492.1",                                                  # halt if the probe is not in the slot
            f"G53 G0 Z{s['clearance_z']:.3f}",
            f"G53 G0 X{start['x']:.3f} Y{start['y']:.3f}",
            "M400",
            "M121",
            "M841",                                                    # charger on
        ]
        done = []
        with self._cmd_lock:
            for line in seq:
                try:
                    self._cmd(line, timeout=120)
                except MachineError as e:
                    raise MachineError(f"deep probe stow stopped at {line!r} (after {len(done)} of {len(seq)} "
                                       f"steps): {e}") from None
                done.append(line)
        st = self._wait_fresh_status(3)
        if st.get("tool") not in (None, -1):
            raise MachineError(f"deep probe stow finished but the machine reports {tool_name(st.get('tool'))}")
        return {"extra_mm": extra, "release_z": deep, "slot": {"x": s["x"], "y": s["y"], "z": s["z"]},
                "probe_v_in_spindle": v_before,
                "note": "check charging: the docked W: reading should jump about +0.07 V within ~60 s"}

    def op_probe_z(self, job, max_distance: float = 30.0, fast_feed: float = 300.0,
                   slow_feed: float = 60.0, retract: float = 2.0, lift: float = 5.0,
                   set_zero: bool = True, allow_non_probe: bool = False) -> dict:
        st = self._wait_fresh_status(1)
        if st.get("tool") != 0 and not allow_non_probe:
            raise MachineError(f"{tool_name(st.get('tool'))} is loaded; load the probe (T0) first")
        if st.get("tool") == 0 and (st.get("probe_v") or 0) < 3.5:
            raise MachineError(f"probe battery {st.get('probe_v')} V is too low (< 3.5 V)")
        m119 = " ".join(self._cmd("M119"))
        if "Probe: 1" in m119:
            raise MachineError("probe already reads triggered")
        start = self._mpos()
        if start["z"] - max_distance < self.limits["z"][0]:
            raise MachineError("probe travel would pass the Z soft limit")

        def probe(dist, feed):
            lines = self._cmd(f"G38.3 Z-{dist:.4f} F{feed:.0f}", timeout=dist / feed * 60 + 20)
            for line in lines:
                m = _PRB_RE.search(line)
                if m:
                    return float(m.group(3)), m.group(4) == "1"
            raise MachineError("no [PRB] result from probe move")

        result: dict = {"start_mpos": start}
        self._cmd("G91")
        try:
            z1, hit1 = probe(max_distance, fast_feed)
            result["fast_z"] = z1
            if not hit1:
                result["hit"] = False
                result["note"] = f"no surface within {max_distance} mm; nothing changed"
                return result
            self._cmd(f"G0 Z{retract:.4f}")
            z2, hit2 = probe(retract + 1.0, slow_feed)
            if not hit2:
                raise MachineError("slow re-probe did not trigger")
            # the G38 reply has 3 decimals; $# carries the same trigger point to 4
            prb = self.refresh_offsets().get("PRB")
            if prb and prb["hit"] and abs(prb["z"] - z2) < 0.002:
                z2 = prb["z"]
            result.update(hit=True, surface_machine_z=z2, fast_minus_slow=round(z1 - z2, 4))
            self._cmd(f"G0 Z{lift:.4f}")
        finally:
            self._cmd("G90")
        st = self._wait_motion_done(timeout=30)
        if set_zero:
            before = dict(self.offsets.get("G54") or {})
            height = st["mpos"]["z"] - z2
            zero = self._set_work(job, {"z": height})
            result.update(set_zero=True, g54_z_before=before.get("z"), **zero)
        result["mpos"] = self._mpos()
        result["wpos"] = dict(self.status.get("wpos") or {})
        return result

    def _set_work(self, job, values: dict) -> dict:
        """G10 L20 P0 (current WCS). Verifies the readout and repeats once if the
        firmware's TLO reset left it off (setting Z resets TLO to 0)."""
        tlo_before = self.status.get("tlo")
        words = " ".join(f"{a.upper()}{float(v):.4f}" for a, v in values.items())
        self._cmd(f"G10 L20 P0 {words}")
        st = self._wait_fresh_status(2)
        retried = False
        bad = {a: v for a, v in values.items() if abs(st["wpos"][a] - float(v)) > 0.002}
        if bad:
            retried = True
            self._cmd("G10 L20 P0 " + " ".join(f"{a.upper()}{float(v):.4f}" for a, v in bad.items()))
            st = self._wait_fresh_status(2)
            still = {a: st["wpos"][a] for a in bad if abs(st["wpos"][a] - float(bad[a])) > 0.002}
            if still:
                raise MachineError(f"work offset did not take: readout {still}, wanted {bad}")
        self.refresh_offsets()
        return {"wcs_after": self.offsets.get("G54"), "tlo_before": tlo_before,
                "tlo_after": st.get("tlo"), "repeated_for_tlo_reset": retried,
                "wpos": st.get("wpos")}

    def op_set_work(self, job, values: dict) -> dict:
        vals = {k.lower(): float(v) for k, v in values.items() if k.lower() in AXES}
        if not vals:
            raise MachineError("give at least one of x, y, z")
        before = dict(self.offsets.get("G54") or {})
        out = self._set_work(job, vals)
        out["wcs_before"] = before
        return out

    def op_gcode(self, job, lines: list[str], allow_spindle: bool = False) -> dict:
        clean = []
        for raw in lines:
            line = re.sub(r"\(.*?\)|;.*$", "", raw).strip()
            if not line:
                continue
            up = line.upper().replace(" ", "")
            if re.search(r"M0?6|T-?\d+M6", up):
                raise MachineError("use tool_change for tool changes")
            if not allow_spindle and re.search(r"M0?[34](?!\d)", up):
                raise MachineError("spindle start refused (allow_spindle is off)")
            clean.append(line)
        if not clean:
            raise MachineError("no G-code to send")
        replies = []
        try:
            for line in clean:
                replies.append({"line": line, "reply": self._cmd(line, timeout=120)})
        finally:
            if any("G91" in l.upper() for l in clean):
                self._cmd("G90")
        st = self._wait_motion_done(timeout=600)
        out = {"sent": len(clean), "replies": replies, "mpos": st.get("mpos"), "wpos": st.get("wpos")}
        # lines that can move G54/TLO/PRB: re-read $# so the UI and status don't show stale offsets
        # (2026-11-24: after a raw M495 grid the display kept the previous sheet's G54 Z until a tool change)
        if any(re.search(r"G10|G92|G38|M495|M49[13]", l.upper().replace(" ", "")) for l in clean):
            out["offsets"] = self.refresh_offsets()
        return out


    # ------------------------------------------------------------------ programs
    def _raw_byte(self, timeout: float, skip: bytes = b"") -> int:
        deadline = time.time() + timeout
        with self._cv:
            while True:
                while self._raw is not None and self._raw_pos < len(self._raw):
                    b = self._raw[self._raw_pos]
                    self._raw_pos += 1
                    if b not in skip:
                        return b
                rem = deadline - time.time()
                if rem <= 0:
                    return -1
                if not self.connected:
                    raise MachineError("connection lost during upload")
                self._cv.wait(min(rem, 0.2))

    def _raw_text(self) -> str:
        with self._cv:
            tail = bytes(self._raw[self._raw_pos:]) if self._raw is not None else b""
        return tail.decode(errors="replace").strip()

    def op_upload(self, job, path: str, name: str) -> dict:
        """Upload a local file to /sd/gcodes/<name> (XMODEM-8K, firmware 1.0.3), then check the
        machine's md5sum of the stored file against the local one."""
        remote = prog.remote_path(name)
        with open(path, "rb") as f:
            data = f.read()
        md5 = prog.file_md5(path)
        blocks = [data[i:i + prog.BLOCK] for i in range(0, len(data), prog.BLOCK)] or [b""]
        t0 = time.time()
        start_seq = self.msg_seq
        job["progress"] = {"phase": "upload", "blocks": len(blocks), "sent": 0}
        with self._cmd_lock:
            self._poll_pause.set()
            time.sleep(0.7)                     # in-flight status replies land as text first
            with self._cv:
                self._raw, self._raw_pos = bytearray(), 0
            ok = False
            in_upload = False     # only after the handshake may CAN be sent (else it would prefix the next command)
            try:
                self._write(f"upload {remote}\n".encode())
                # The status line and the 'diagnose' reply both have a "C:" field, so a late reply must
                # not count: wait for two consecutive bare 'C' bytes outside any <...> or {...} frame.
                in_frame, run, deadline = False, 0, time.time() + 5.0
                while run < 2:
                    c = self._raw_byte(max(0.05, deadline - time.time()))
                    if c < 0:
                        raise MachineError(f"no upload handshake from the machine {self._raw_text()[:120]!r}")
                    if c in (ord("<"), ord("{")):
                        in_frame, run = True, 0
                    elif c in (ord(">"), ord("}")):
                        in_frame, run = False, 0
                    elif in_frame:
                        continue
                    elif c == prog.EOT:
                        raise MachineError("machine refused the upload (busy - planner not idle)")
                    elif c == prog.NAK:
                        raise MachineError("machine fell back to checksum mode; upload aborted")
                    else:
                        run = run + 1 if c == ord("C") else 0
                in_upload = True

                def send(num: int, payload: bytes) -> None:
                    pkt = prog.xmodem_block(num, payload)
                    for attempt in range(10):
                        self._write(pkt)
                        r = self._raw_byte(15.0, skip=b"C")
                        if r == prog.ACK:
                            return
                        if r == prog.NAK:
                            continue
                        raise MachineError(f"block {num}: unexpected reply {r!r} {self._raw_text()[:120]}")
                    raise MachineError(f"block {num}: NAK 10 times")

                send(0, md5.encode())
                for i, blk in enumerate(blocks, 1):
                    send(i, blk)
                    job["progress"]["sent"] = i
                self._write(bytes([prog.EOT]))
                r = self._raw_byte(10.0, skip=b"C")
                if r != prog.ACK:
                    raise MachineError(f"no ACK after EOT (got {r!r})")
                ok = True
            finally:
                if not ok and in_upload:
                    try:
                        self._write(bytes([prog.CAN, prog.CAN]))
                    except Exception:
                        pass
                    time.sleep(1.5)       # firmware: cancel, flush, then ignores input for 1 s
                with self._cv:
                    rest = bytes(self._raw[self._raw_pos:]) if self._raw is not None else b""
                    self._raw = None
                    self._raw_rest = rest.lstrip(b"C")   # stray handshake bytes are not text
                time.sleep(0.5)                          # machine flushes input, then leaves upload mode
                self._poll_pause.clear()
        secs = round(time.time() - t0, 1)
        deadline = time.time() + 15
        reply = None
        while time.time() < deadline and reply is None:
            reply = next((m["text"] for m in list(self.messages)
                          if m["seq"] > start_seq and ("upload success" in m["text"] or m["text"].startswith(("Error", "Info:")))), None)
            time.sleep(0.2)
        if not reply or "upload success" not in reply:
            raise MachineError(f"upload not confirmed by the machine: {reply!r}")
        job["progress"] = {"phase": "verify"}
        line = self._query(f"md5sum {remote}", r"^[0-9a-f]{32}\s|File not found", timeout=180)
        got = line.split()[0] if line and not line.startswith("File") else None
        if got != md5:
            raise MachineError(f"md5 mismatch after upload: machine {got!r}, local {md5}")
        return {"remote": remote, "bytes": len(data), "blocks": len(blocks), "md5": md5, "verified": True,
                "upload_secs": secs, "reply": reply}

    def op_run_program(self, job, path: str, name: str, margin_xy: float = 3.0, margin_z: float = 0.5,
                       check_below_z: float = 3.0) -> dict:
        """Play /sd/gcodes/<name> on the machine (the firmware runs it, including tool changes,
        spindle and vacuum) and watch it. The remote file must match the local one (md5).
        Kill switch (feed hold + abort) when, outside a tool change, the tool is below
        check_below_z (work) and outside the program's XY extents + margin_xy, or below its
        lowest Z - margin_z."""
        info = prog.analyze(path)
        remote = prog.remote_path(name)
        md5 = prog.file_md5(path)
        job["progress"] = {"phase": "verify"}
        line = self._query(f"md5sum {remote}", r"^[0-9a-f]{32}\s|File not found", timeout=180)
        got = line.split()[0] if line and not line.startswith("File") else None
        if got != md5:
            raise MachineError(f"{remote} on the machine does not match {os.path.basename(path)} "
                               f"(machine md5 {got!r}); upload it first")
        if self.status.get("progress"):
            raise MachineError("the machine is already playing a file")
        env = {"x": (info["x"][0] - margin_xy, info["x"][1] + margin_xy),
               "y": (info["y"][0] - margin_xy, info["y"][1] + margin_xy),
               "z_floor": info["z"][0] - margin_z}
        start_seq = self.msg_seq
        reply = self._query(f"play {remote}", r"^(Playing|File not found|Currently printing)", timeout=10)
        if not reply or not reply.startswith("Playing"):
            raise MachineError(f"play refused: {reply!r}")
        t0 = time.time()
        job["progress"] = {"phase": "running", "lines": 0, "total_lines": info["lines"], "percent": 0}
        idle_since = None
        seen_progress = False
        last_seq = self.status_seq
        breaches = 0
        atc_until = 0.0
        while True:
            with self._cv:
                self._cv.wait(0.3)
            if not self.connected:
                raise MachineError("connection lost while the program runs - the machine keeps playing "
                                   "from its SD card; use the machine's controls or reconnect")
            st = dict(self.status)
            new = [m for m in list(self.messages) if m["seq"] > start_seq]
            if any("Done printing file" in m["text"] for m in new):
                break
            bad = next((m["text"] for m in new if re.search(r"Abort|ALARM|HALT|^error", m["text"], re.I)), None)
            if bad:
                raise MachineError(f"program stopped: {bad}")
            state = str(st.get("state", ""))
            if state.startswith("Alarm"):
                raise MachineError("machine in Alarm while running the program")
            pr = st.get("progress")
            if pr:
                seen_progress = True
                job["progress"].update(lines=pr["lines"], percent=pr["percent"], machine_secs=pr["secs"])
            job["progress"].update(state=state, tool=st.get("tool"), elapsed=round(time.time() - t0),
                                   wpos=st.get("wpos"), spindle_rpm=st.get("spindle_rpm"))
            w = st.get("wpos") or {}
            # Tool change: status carries |A:<atc_state> for the whole ATC script (fw 1.0.3 Kernel query
            # string); "Start atc" is not printed for played files, only "Done ATC". The head visits the
            # rack and the setter outside the work envelope, so hold the checks off until 1 s after.
            if st.get("atc_state") is not None or state == "Tool":
                atc_until = time.time() + 1.0
            fresh = self.status_seq != last_seq
            last_seq = self.status_seq
            cutting = (st.get("spindle_rpm") or 0) > 1000 and time.time() > atc_until
            why = None
            if fresh and cutting and w and w.get("z") is not None and w["z"] < check_below_z:
                if not (env["x"][0] <= w["x"] <= env["x"][1] and env["y"][0] <= w["y"] <= env["y"][1]):
                    why = f"XY {w['x']:.3f},{w['y']:.3f} outside the program envelope {env['x']} {env['y']}"
                elif w["z"] < env["z_floor"]:
                    why = f"Z {w['z']:.3f} below the floor {env['z_floor']:.3f}"
            if fresh:
                breaches = breaches + 1 if why else 0
            if breaches >= 2:
                self.stop()
                self._note(f"KILL SWITCH: {why}")
                try:
                    self._wait_fresh_status(3)
                except MachineError:
                    pass
                raise MachineError(f"kill switch: {why} (feed hold + abort sent; unlock before moving)")
            if seen_progress and not pr and state == "Idle":
                idle_since = idle_since or time.time()
                if time.time() - idle_since > 5:
                    break
            else:
                idle_since = None
        st = self._wait_fresh_status(2)
        return {"remote": remote, "secs": round(time.time() - t0), "envelope": env,
                "lines": job["progress"].get("lines"), "tool": st.get("tool")}

    # ------------------------------------------------------------------ switches (not motion, not a job)
    def set_vacuum(self, on: bool, power: float | None = None) -> dict:
        """Onboard vacuum: M801 S<power %> / M802, then read the switch back with 'diagnose'.
        Not a machine operation: allowed while a program plays (the firmware switches at once and the
        cut does not pause) and between the commands of other jobs. Refused while one long exchange
        (upload, tool change, homing) holds the line."""
        if not self.connected:
            raise MachineError("not connected")
        if on:
            p = int(round(float(80 if power is None else power)))
            if not 1 <= p <= 100:
                raise MachineError("vacuum power must be 1-100 %")
            line = f"M801 S{p}"
        else:
            p, line = 0, "M802"
        if str(self.status.get("state", "")).startswith("Alarm"):
            raise MachineError("machine is in Alarm; Unlock first (a halt already switches the vacuum off)")
        if not self._cmd_lock.acquire(timeout=3.0):
            cur = self.job["kind"] if self.job and self.job.get("state") == "running" else "another command"
            raise Busy(f"machine busy ({cur}); try the vacuum again in a moment")
        try:
            self._cmd(line, timeout=5)
            with self._cv:
                seq = self.diag_seq     # replies after the ok reflect the new switch state
            d = self._fresh_diag(seq)
        finally:
            self._cmd_lock.release()
        vac = (d or {}).get("vacuum")
        confirmed = bool(vac) and vac["on"] == on and (not on or vac["power"] == p)
        return {"command": line, "vacuum": vac, "confirmed": confirmed,
                "auto": self.status.get("vacuum_mode"),
                "note": None if confirmed else "the machine did not report the new vacuum state"}

    def _program_running(self) -> bool:
        return bool(self.status.get("progress")) or bool(
            self.job and self.job.get("state") == "running" and self.job.get("kind") == "run_program")

    def set_spindle(self, on: bool, rpm: float | None = None) -> dict:
        """Spindle on (M3 S<rpm>) / off (M5) for the web UI, then read it back from 'diagnose'.
        Interlocks: connected, not in Alarm, no program playing (the program owns the spindle; use Hold
        or STOP), and only a cutting tool (T1-T6) may be started: never the wireless probe or an empty
        spindle. Like the vacuum it is not a machine job: it waits at most 3 s for the command line."""
        if not self.connected:
            raise MachineError("not connected")
        st = self.status
        if str(st.get("state", "")).startswith("Alarm"):
            raise MachineError("machine is in Alarm; Unlock first (a halt already stops the spindle)")
        if self._program_running():
            raise MachineError("a program is running and controls the spindle; use Hold or STOP")
        if on:
            tool = st.get("tool")
            if tool is None or tool < 1:
                raise MachineError(f"{tool_name(tool)} is in the spindle; load a cutting tool (T1-T6) first")
            r = int(round(float(10000 if rpm is None else rpm)))
            if not SPINDLE_RPM[0] <= r <= SPINDLE_RPM[1]:
                raise MachineError(f"spindle speed must be {SPINDLE_RPM[0]}-{SPINDLE_RPM[1]} rpm")
            line = f"M3 S{r}"
        else:
            r, line = 0, "M5"
        if not self._cmd_lock.acquire(timeout=3.0):
            cur = self.job["kind"] if self.job and self.job.get("state") == "running" else "another command"
            raise Busy(f"machine busy ({cur}); try again in a moment" +
                       ("" if on else ", or press STOP to stop the spindle at once"))
        try:
            self._cmd(line, timeout=5)
            with self._cv:
                seq = self.diag_seq
            d = self._fresh_diag(seq)
        finally:
            self._cmd_lock.release()
        sp = (d or {}).get("spindle")
        confirmed = bool(sp) and sp["on"] == on and (not on or sp.get("target") == r)
        return {"command": line, "spindle": sp, "confirmed": confirmed, "rpm": self.status.get("spindle_rpm"),
                "note": None if confirmed else "the machine did not report the new spindle state"}

    def spindle_view(self) -> dict:
        """Spindle switch/target from 'diagnose' (None = unknown or stale), actual rpm from the status line,
        and whether the UI may start it (with the reason when not)."""
        fresh = self.connected and self.diag_time and time.time() - self.diag_time < self.DIAG_FRESH
        sp = self.diag.get("spindle") if fresh else None
        tool = self.status.get("tool") if self.connected else None
        why = None
        if not self.connected:
            why = "not connected"
        elif str(self.status.get("state", "")).startswith("Alarm"):
            why = "machine in Alarm"
        elif self._program_running():
            why = "a program is running"
        elif tool is None or tool < 1:
            why = f"{tool_name(tool)} in the spindle"
        return {"on": sp["on"] if sp else None, "target": sp.get("target") if sp else None,
                "rpm": self.status.get("spindle_rpm") if self.connected else None,
                "can_start": why is None, "why_not": why, "range": list(SPINDLE_RPM)}

    def vacuum_view(self) -> dict:
        """Vacuum + probe charger as last reported by 'diagnose' (None = unknown or stale)."""
        fresh = self.connected and self.diag_time and time.time() - self.diag_time < self.DIAG_FRESH
        vac = self.diag.get("vacuum") if fresh else None
        return {"on": vac["on"] if vac else None, "power": vac["power"] if vac else None,
                "auto": self.status.get("vacuum_mode") if self.connected else None,
                "probe_charger": self.diag.get("probe_charger") if fresh else None,
                "age": round(time.time() - self.diag_time, 2) if self.diag_time else None}

    def op_unlock(self, job) -> dict:
        self._cmd("$X", timeout=5, alarm_ok=True)
        return {"state": self._wait_fresh_status(2).get("state")}

    # ------------------------------------------------------------------ immediate
    def feed_hold(self) -> None:
        self.realtime(b"!")

    def resume(self) -> None:
        self.realtime(b"~")

    def stop(self) -> None:
        """Feed hold, then abort (Ctrl-X). Clears the queue; the machine needs Unlock after."""
        self.realtime(b"!")
        time.sleep(0.2)
        self.realtime(b"\x18")

    # ------------------------------------------------------------------ view
    def snapshot(self) -> dict:
        st = dict(self.status)
        st.pop("raw", None)
        return {
            "connected": self.connected, "host": self.host, "port": self.port, "info": self.info,
            "status": st, "status_age": round(time.time() - self.status_time, 2) if self.status_time else None,
            "tool_name": tool_name(st.get("tool")), "offsets": self.offsets, "modal": self.modal,
            "limits": self.limits, "safe_z_machine": self.safe_z_machine,
            "job": _job_view(self.job), "msg_seq": self.msg_seq,
            "vacuum": self.vacuum_view(), "spindle": self.spindle_view(), "diag": self.diag,
        }


def _job_view(job: dict | None) -> dict | None:
    if not job:
        return None
    return {k: job[k] for k in ("id", "kind", "state", "started", "finished", "result", "error", "source")} | {
        "messages": job["messages"][-12:], "progress": job.get("progress")}
