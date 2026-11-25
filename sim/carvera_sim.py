"""Carvera simulator for tests: a TCP server that answers like the real machine closely enough
to exercise Carvera Control without moving anything.

Mimicked behaviours (observed on the real machine 2026-11-21):
  * "?" -> status line; one "ok" per line; motion "ok" when queued; M400/$H/G38 ok when done
  * M6T<n> streams internal lines with stray "ok"s, then "Done ATC" (and one trailing ok)
  * G10 L20/L2 with Z: offset computed with the old TLO, then TLO reset to 0 and the current tool
    becomes the length reference (so a naive single G10 L20 leaves the readout off by the old TLO)
  * G38.3 in G91 is a relative probe; only the probe (T0) triggers, at a configurable surface
  * "upload <path>": firmware-1.0.3 XMODEM-8K receive ('C' handshake, MD5 block 0, ACK/NAK, EOT),
    "md5sum <path>", "play <path>" (lines run without "ok", status "|P:lines,percent,secs",
    "Done printing file"), "abort"; "|A:" in status while a tool change runs
  * "diagnose" (shell reply, no "ok", answered in Alarm too): "{S:..|V:on,power|..|C:charger|..}";
    M801 S<p> / M802 switch the vacuum, M841 / M842 the probe charger; an abort (halt) switches
    both off (observed 2026-11-23)
  * the tool rack (fw 1.0.3 ATCHandler, 2026-11-24): M499.2 prints the slot table; M490.2 releases
    the tool (into the slot under the spindle if it is low enough, else it is lost), M490.1 clamps;
    M492.1 / M492.2 halt with "ERROR: Unexpected tool absence/presence detected" when the slot under
    the spindle is empty / filled; M493.2 T<n> sets the tool number; M120 / M121 push / pop state.
    `seated` records the Z each tool was released at.
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import re
import socket
import threading
import time

TOOL_LEN = {-1: 0.0, 0: 0.0, 1: -12.0, 2: -18.5, 3: -15.405, 4: -20.0, 5: -10.0, 6: -25.0}
SLOT_X, SLOT_Z = -3.403, -112.5          # the real machine's rack (M499.2, 2026-11-24)


def slot_y(i: int) -> float:
    return -235.143 + (210 if i == 0 else (6 - i) * 30)


class Sim:
    def __init__(self, surface_z: float = -99.8065, speed: float = 20.0):
        self.mpos = [-1.0, -1.0, -1.0]
        self.g54 = [-359.503, -235.143, -96.33]
        self.tool = 3
        self.ref_len = 0.075           # length reference -> probe TLO -0.075, like the real machine
        self.rel = False
        self.state = "Idle"
        self.surface = surface_z
        self.speed = speed
        self.rpm = 0.0
        self.rpm_target = 0.0
        self.queue: list = []
        self.hold = False
        self.lock = threading.RLock()
        self.prb = [0.0, 0.0, 0.0, 0]
        self.out = None
        self.alarm = False
        self.files: dict[str, bytes] = {}
        self.playing = None          # {"name", "lines", "total", "t0"} while a file plays
        self.player = None           # the player thread (its lines get no "ok")
        self.atc = False
        self.rack = {t for t in range(7) if t != self.tool}   # tools sitting in their slots
        self.seated: dict[int, float] = {}                    # tool -> Z it was released at
        self.loosed = False
        self.vacuum = [0, 0]         # switch state, value %
        self.charger = 1
        self.diag_count = 0
        self.nak_once = False       # test hook: NAK the first data block once
        self.upload_busy = False     # test hook: refuse uploads (planner not idle)
        threading.Thread(target=self._motion, daemon=True).start()

    @property
    def tlo(self) -> float:
        return round(TOOL_LEN.get(self.tool, 0.0) - self.ref_len, 4) if self.tool not in (-1,) else 0.0

    def wpos(self):
        return [self.mpos[0] - self.g54[0], self.mpos[1] - self.g54[1], self.mpos[2] - self.g54[2] - self.tlo]

    def status(self) -> str:
        m, w = self.mpos, self.wpos()
        st = "Alarm" if self.alarm else ("Hold" if self.hold else self.state)
        if st == "Idle" and self.rpm > 0:
            st = "Run"            # fw 1.0.3 reports Run whenever the spindle is on, even with no motion (2026-11-24)
        return (f"<{st}|MPos:{m[0]:.4f},{m[1]:.4f},{m[2]:.4f},0.0000,0.0272"
                f"|WPos:{w[0]:.4f},{w[1]:.4f},{w[2]:.4f},-90.0000,0.0272|F:0.0,1000.0,100.0"
                f"|S:{self.rpm:.1f},{self.rpm_target:.1f},100.0,1,28.8|T:{self.tool},{self.tlo:.3f}|W:3.65"
                f"|L:0, 0, 0, 0.0,100.0|O:0.660|C:1,5,0,1"
                + (f"|A:5" if self.atc else "")
                + (f"|P:{self.playing['lines']},{int(100 * self.playing['lines'] / max(1, self.playing['total']))},"
                   f"{int(time.time() - self.playing['t0'])}" if self.playing else "") + ">")

    def diagnose(self) -> str:
        return (f"{{S:{1 if self.rpm_target else 0},{int(self.rpm_target or 20000)}|L:0,0"
                f"|V:{self.vacuum[0]},{self.vacuum[1]}|F:0,0|G:1|T:0|R:0|C:{self.charger}"
                f"|E:0,0,0,0,0,0,1,0|P:0,0|A:0,0|I:0}}")

    def ok(self) -> None:
        if threading.current_thread() is not self.player:   # lines played from SD get no "ok"
            self.send("ok")

    def send(self, text: str) -> None:
        if self.out:
            try:
                self.out.sendall((text + "\n").encode())
            except OSError:
                pass

    # --- motion
    def _motion(self):
        while True:
            with self.lock:
                job = self.queue[0] if self.queue and not self.hold and not self.alarm else None
                if job is None and not self.queue and self.state == "Run":
                    self.state = "Idle"
            if job is None:
                time.sleep(0.01)
                continue
            target, feed, kind = job
            with self.lock:
                self.state = "Run"
            step = feed / 60.0 * self.speed * 0.01
            while True:
                with self.lock:
                    if self.alarm:
                        break
                    if self.hold:
                        pass
                    else:
                        d = [t - c for t, c in zip(target, self.mpos)]
                        dist = sum(x * x for x in d) ** 0.5
                        if kind == "probe" and self.tool == 0 and self.mpos[2] + TOOL_LEN[0] <= self.surface + 1e-9:
                            self.mpos[2] = self.surface
                            self.prb = [self.mpos[0], self.mpos[1], self.surface, 1]
                            break
                        if dist <= step:
                            self.mpos = list(target)
                            if kind == "probe":
                                if self.tool == 0 and target[2] <= self.surface:
                                    self.mpos[2] = self.surface
                                    self.prb = [self.mpos[0], self.mpos[1], self.surface, 1]
                                else:
                                    self.prb = [self.mpos[0], self.mpos[1], self.mpos[2], 0]
                            break
                        self.mpos = [c + x / dist * step for c, x in zip(self.mpos, d)]
                time.sleep(0.01)
            with self.lock:
                if self.queue:
                    self.queue.pop(0)

    def wait_idle(self):
        while True:
            with self.lock:
                if not self.queue or self.alarm:
                    return
            time.sleep(0.01)

    # --- command handling
    def handle(self, line: str) -> None:
        line = re.sub(r"\(.*?\)", "", line).strip()
        if not line:
            return
        up = line.upper()
        if up == "DIAGNOSE":                  # shell command: no "ok", no halt check
            self.diag_count += 1
            self.send(self.diagnose()); return
        if self.alarm and up not in ("$X", "M999", "$#", "$G", "M119", "VERSION"):
            self.send("error:Alarm lock")
            return
        if up.startswith("MD5SUM "):
            name = line.split(None, 1)[1]
            if name in self.files:
                self.send(f"{hashlib.md5(self.files[name]).hexdigest()} {name}")
            else:
                self.send(f"File not found: {name}")
            return
        if up.startswith("PLAY "):
            name = line.split(None, 1)[1]
            if self.playing:
                self.send("Currently printing, abort print first"); return
            if name not in self.files:
                self.send(f"File not found: {name}"); return
            self.send(f"Playing {name}")
            self.send(f"  File size {len(self.files[name])}")
            self.player = threading.Thread(target=self._play, args=(name,), daemon=True)
            self.playing = {"name": name, "lines": 0, "total": self.files[name].count(b"\n"), "t0": time.time()}
            self.player.start()
            return
        if up == "ABORT":
            if self.playing:
                self.playing = None
                with self.lock:
                    self.queue.clear(); self.rpm = self.rpm_target = 0.0
                self.send("Aborted playing or paused file.")
            return
        if up == "VERSION":
            self.send("version = sim-1.0"); return   # real fw: shell reply, no "ok"
        if up == "$X" or up == "M999":
            self.alarm = False; self.state = "Idle"; self.ok(); return
        if up == "$#":
            g = self.g54
            self.send(f"[G54:{g[0]:.4f},{g[1]:.4f},{g[2]:.4f},90.0000,0.0000]")
            for n in ("G55", "G56", "G57", "G58", "G59"):
                self.send(f"[{n}:0.0000,0.0000,0.0000,0.0000,0.0000]")
            self.send(f"[TL0:{self.tlo:.4f}]")
            p = self.prb
            self.send(f"[PRB:{p[0]:.4f},{p[1]:.4f},{p[2]:.4f}:{p[3]}]")
            self.ok(); return
        if up == "$G":
            self.send(f"[G0 G54 G17 G21 {'G91' if self.rel else 'G90'} G94 M0 M5 M9 T{self.tool} F1000.0000 S0.0000]")
            self.ok(); return
        if up == "M119":
            self.send("X_max:0 Y_max:0 Z_max:0 A_min:1 pins- Probe: 0"); self.ok(); return
        if up == "M400":
            self.wait_idle(); self.ok(); return
        if up == "$H":
            with self.lock:
                self.state = "Home"
            time.sleep(1.0)
            with self.lock:
                self.mpos = [-1.0, -1.0, -1.0]; self.state = "Idle"
            self.ok(); return
        m = re.match(r"M0?6\s*T(-?\d+)|T(-?\d+)M0?6$", up.replace(" ", ""))
        if m:
            new = int(m.group(1) or m.group(2))
            self.wait_idle()
            self.atc = True
            mdi = threading.current_thread() is not self.player   # played files: only "Done ATC" is printed
            if mdi:
                self.send(f"Start atc, old tool: T{self.tool}, new tool: T{new}")
            for t in ("M497.1", "G53 G0 Z-3.000", "M490.2"):
                if mdi:
                    self.send(t)
                self.ok(); time.sleep(0.05)
            if mdi:
                self.send("ATC loosed!")
            self.ok()
            time.sleep(0.2)
            with self.lock:
                if self.tool >= 0:
                    self.rack.add(self.tool); self.seated[self.tool] = SLOT_Z
                self.rack.discard(new)
                self.tool = new
                self.loosed = False                 # the pick ends with M490.1
                self.mpos = [self.mpos[0], self.mpos[1], -3.0]
                self.prb = [-3.405, -55.145, -74.32 + TOOL_LEN.get(new, 0.0), 1]
            if mdi:
                self.send("ATC clamped!")
            self.ok()
            self.atc = False
            self.send("Done ATC"); self.ok()
            return
        am = re.match(r"^M(12[01]|49[0-3]|499)(?:\.(\d))?(?:T(-?\d+))?$", up.replace(" ", ""))
        if am:
            self._atc_mcode(int(am.group(1)), int(am.group(2) or 0), am.group(3)); return
        words = dict()
        for w, v in re.findall(r"([A-Z])\s*(-?\d*\.?\d+)", up):
            words.setdefault(w, []).append(float(v))
        gs = words.get("G", [])
        if 91 in gs:
            self.rel = True
        if 90 in gs:
            self.rel = False
        if 10 in gs:
            L, P = words.get("L", [0])[0], words.get("P", [0])[0]
            with self.lock:
                for i, ax in enumerate("XYZ"):
                    if ax in words:
                        v = words[ax][0]
                        if L == 20:
                            self.g54[i] = self.mpos[i] - v - (self.tlo if ax == "Z" else 0.0)
                        elif L == 2:
                            self.g54[i] = v
                if "Z" in words:   # firmware: TLO reset, current tool becomes the reference
                    self.ref_len = TOOL_LEN.get(self.tool, 0.0)
            self.ok(); return
        if 4 in gs:
            time.sleep(words.get("S", [0])[0] / self.speed + words.get("P", [0])[0] / 1000 / self.speed)
            self.ok(); return
        if "M" in words:
            for mv in words["M"]:
                if mv in (3, 4):
                    self.rpm_target = words.get("S", [self.rpm_target or 10000])[0]; self.rpm = self.rpm_target * 0.84
                if mv == 5:
                    self.rpm = self.rpm_target = 0.0
                if mv == 801:
                    v = int(min(100, max(0, words.get("S", [100])[0])))
                    self.vacuum = [1 if v > 0 else 0, v]
                if mv == 802:
                    self.vacuum = [0, 0]
                if mv in (841, 842):
                    self.charger = 1 if mv == 841 else 0
            self.ok(); return
        probe = any(abs(g - 38.3) < 1e-6 or abs(g - 38.2) < 1e-6 for g in gs)
        axes = {ax: words[ax][0] for ax in "XYZ" if ax in words}
        if axes and (probe or not gs or any(g in (0, 1, 53) for g in gs) or True):
            g53 = 53 in gs
            with self.lock:
                base = list(self.queue[-1][0]) if self.queue else list(self.mpos)
                tgt = list(base)
                for i, ax in enumerate("XYZ"):
                    if ax in axes:
                        v = axes[ax]
                        if g53:
                            tgt[i] = v
                        elif self.rel:
                            tgt[i] = base[i] + v
                        else:
                            tgt[i] = v + self.g54[i] + (self.tlo if ax == "Z" else 0.0)
                feed = words.get("F", [3000 if 0 in gs else 1000])[0]
                self.queue.append((tgt, feed, "probe" if probe else "move"))
            if probe:
                self.wait_idle()
                p = self.prb
                self.send(f"[PRB:{p[0]:.3f},{p[1]:.3f},{p[2]:.3f}:{p[3]}]")
            self.ok(); return
        self.ok()

    def _slot_here(self):
        with self.lock:
            x, y = self.mpos[0], self.mpos[1]
        return next((i for i in range(7) if abs(x - SLOT_X) < 1 and abs(y - slot_y(i)) < 1), None)

    def _atc_mcode(self, m: int, sub: int, t) -> None:
        if m in (120, 121):
            self.ok(); return
        self.wait_idle()
        if m == 499 and sub == 2:
            self.send(f"probe -- mx:{SLOT_X:.1f} my:{-235.143 + 180:.1f} mz:{SLOT_Z - 40:.1f}")
            for i in range(7):
                self.send(f"tool{i} -- mx:{SLOT_X:.1f} my:{slot_y(i):.1f} mz:{SLOT_Z:.1f}")
        elif m == 490 and sub == 2:
            if self.loosed:
                self.send("Already loosed!")
            else:
                self.loosed = True
                with self.lock:
                    slot = self._slot_here()
                    if self.tool >= 0 and self.tool not in self.rack:
                        if slot == self.tool and self.mpos[2] <= SLOT_Z + 0.5:
                            self.rack.add(self.tool); self.seated[self.tool] = round(self.mpos[2], 3)
                        else:
                            self.seated[self.tool] = None      # fell out somewhere else
                self.send("ATC loosed!")
        elif m == 490 and sub == 1:
            self.loosed = False; self.send("ATC clamped!")
        elif m == 492 and sub in (0, 1, 2):
            slot = self._slot_here()
            present = slot is not None and slot in self.rack
            if sub in (0, 1) and not present:
                with self.lock:
                    self.alarm = True; self.queue.clear()
                self.send("ERROR: Unexpected tool absence detected, please check tool rack!")
            elif sub == 2 and present:
                with self.lock:
                    self.alarm = True; self.queue.clear()
                self.send("ERROR: Unexpected tool presence detected, please check tool rack!")
        elif m == 493 and sub == 2:
            if t is None:
                self.send("ERROR: No tool was set!")
            else:
                self.tool = int(t)
        self.ok()

    def _play(self, name: str) -> None:
        for raw in self.files[name].decode(errors="replace").splitlines():
            if not self.playing or self.alarm:
                return
            while True:                       # keep the planner a few moves ahead, like the firmware
                with self.lock:
                    if len(self.queue) < 8 or self.alarm:
                        break
                time.sleep(0.005)
            self.handle(raw)
            if self.playing:
                self.playing["lines"] += 1
        self.wait_idle()
        if self.playing and not self.alarm:
            self.playing = None
            self.send("Done printing file")

    def receive_upload(self, conn, name: str) -> None:
        """Firmware 1.0.3 Player::upload_command, host side expectations included."""
        if self.upload_busy or self.queue:
            conn.sendall(bytes([0x04])); return
        conn.settimeout(0.1)
        buf = bytearray()

        def need(n, tries=30):
            while len(buf) < n and tries > 0:
                try:
                    d = conn.recv(65536)
                    if not d:
                        raise OSError("closed")
                    buf.extend(d)
                except TimeoutError:
                    tries -= 1
                except socket.timeout:
                    tries -= 1
            if len(buf) < n:
                return None
            out = bytes(buf[:n]); del buf[:n]; return out
        for _ in range(10):                       # 'C' every ~100 ms until the first block arrives
            conn.sendall(b"C")
            first = need(1, tries=1)
            if first:
                break
        else:
            conn.settimeout(None); return
        data, md5, num, nak_done = bytearray(), None, 1, False
        try:
            while True:
                c = first[0]
                if c == 0x04:                     # EOT
                    conn.sendall(bytes([0x06])); break
                if c == 0x16:
                    self.send("Info: Upload canceled by remote!"); conn.settimeout(None); return
                if c != 0x02:
                    raise OSError(f"bad block start {c}")
                rest = need(8198)
                if rest is None:
                    raise OSError("short block")
                blk = bytes([c]) + rest
                n_, inv, ln = blk[1], blk[2], blk[3] << 8 | blk[4]
                body, crc = blk[3:3 + 8194], blk[3 + 8194] << 8 | blk[3 + 8195]
                good = n_ == 0xFF - inv and binascii.crc_hqx(body, 0) == crc
                if good and md5 is None and n_ == 0 and ln == 32:
                    md5 = blk[5:5 + 32].decode(); conn.sendall(bytes([0x06]))
                elif good and n_ == (num & 0xFF):
                    if self.nak_once and not nak_done:
                        nak_done = True; conn.sendall(bytes([0x15]))
                    else:
                        data.extend(blk[5:5 + ln]); num += 1; conn.sendall(bytes([0x06]))
                else:
                    conn.sendall(bytes([0x15]))
                first = need(1)
                if first is None:
                    raise OSError("timeout between blocks")
        except OSError as e:
            conn.settimeout(None); self.send(f"Error: upload failed: {e}"); return
        conn.settimeout(None)
        self.files[name] = bytes(data)
        time.sleep(0.1)
        self.send(f"Info: upload success: {name}.")

    def realtime(self, ch: str) -> None:
        if ch == "?":
            self.send(self.status())
        elif ch == "!":
            self.hold = True
        elif ch == "~":
            self.hold = False
        elif ch == "\x18":
            with self.lock:
                self.queue.clear(); self.hold = False; self.alarm = True; self.rpm = 0.0
                self.playing = None
                self.vacuum = [0, 0]; self.charger = 0     # switch failsafe on halt
            self.send("ALARM: Abort during cycle")


def serve(port: int = 22222, surface_z: float = -99.8065, speed: float = 20.0, ready=None) -> None:
    sim = Sim(surface_z, speed)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    if ready is not None:
        ready.append(srv.getsockname()[1])
    while True:
        conn, _ = srv.accept()
        sim.out = conn
        buf = ""
        lines: list[str] = []
        cv = threading.Condition()

        def worker():
            while True:
                with cv:
                    while not lines:
                        cv.wait()
                    line = lines.pop(0)
                if line is None:
                    return
                sim.handle(line)
        threading.Thread(target=worker, daemon=True).start()
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                for ch in data.decode(errors="replace"):
                    if ch in "?!~\x18":
                        sim.realtime(ch)
                    elif ch == "\n":
                        if buf.strip().lower().startswith("upload "):
                            sim.receive_upload(conn, buf.strip().split(None, 1)[1])   # raw bytes from here
                            buf = ""
                            break
                        with cv:
                            lines.append(buf); cv.notify()
                        buf = ""
                    elif ch != "\r":
                        buf += ch
        except OSError:
            pass
        finally:
            with cv:
                lines.append(None); cv.notify()
            sim.out = None
            conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=22222)
    ap.add_argument("--surface", type=float, default=-99.8065)
    ap.add_argument("--speed", type=float, default=20.0)
    a = ap.parse_args()
    print(f"Carvera simulator on 127.0.0.1:{a.port}", flush=True)
    serve(a.port, a.surface, a.speed)
