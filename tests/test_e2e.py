"""End-to-end: simulator <- Carvera Control server <- HTTP API. No real machine involved."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim"))

import carvera_sim  # noqa: E402
from carvera_control.server import App, build_server  # noqa: E402

SURFACE = -99.8065


def start_stack():
    ready = []
    threading.Thread(target=carvera_sim.serve, kwargs={"port": 0, "surface_z": SURFACE, "speed": 40.0,
                                                       "ready": ready}, daemon=True).start()
    while not ready:
        time.sleep(0.01)
    data = tempfile.mkdtemp(prefix="carvera-test-")
    app = App(data, "127.0.0.1", ready[0], discovery=False)
    srv = build_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return app, f"http://127.0.0.1:{srv.server_address[1]}"


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.base = start_stack()

    def call(self, method, path, body=None, wait=True, expect=200):
        url = self.base + path + ("?wait=1" if wait and method == "POST" else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Source": "test"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
        self.assertEqual(code, expect, out)
        return out

    def status(self):
        return self.call("GET", "/api/status")

    def test_01_flow(self):
        s = self.call("POST", "/api/connect", {})
        self.assertTrue(s["connected"])
        self.assertEqual(s["status"]["tool"], 3)
        self.assertAlmostEqual(s["offsets"]["G54"]["z"], -96.33)

        # jog X -1 and back
        j = self.call("POST", "/api/jog", {"axis": "x", "distance": -1, "feed": 1000})
        self.assertEqual(j["state"], "done", j)
        self.assertAlmostEqual(self.status()["status"]["mpos"]["x"], -2.0, places=3)
        # limit guard: X +5 from -2 would pass 0
        e = self.call("POST", "/api/jog", {"axis": "x", "distance": 5}, expect=422)
        self.assertIn("outside machine travel", e["error"])

        # home
        self.call("POST", "/api/home", {})
        self.assertEqual(self.status()["status"]["mpos"], {"x": -1.0, "y": -1.0, "z": -1.0, "a": 0.0})

        # tool change to the probe (stray oks + Done ATC handled)
        t = self.call("POST", "/api/tool", {"tool": 0})
        self.assertEqual(t["state"], "done", t)
        self.assertEqual(t["result"]["tool"], 0)
        self.assertAlmostEqual(t["result"]["tlo"], -0.075)
        # the command channel still works right after the stray oks
        j = self.call("POST", "/api/jog", {"axis": "z", "distance": -50, "feed": 2000})
        self.assertEqual(j["state"], "done", j)

        # probe Z and set zero: first G10 is off by the old TLO (-0.075) -> server repeats
        p = self.call("POST", "/api/probe/z", {"max_distance": 60})
        r = p["result"]
        self.assertTrue(r["hit"], r)
        self.assertAlmostEqual(r["surface_machine_z"], SURFACE, places=3)
        self.assertTrue(r["repeated_for_tlo_reset"], r)
        st = self.status()
        self.assertAlmostEqual(st["status"]["wpos"]["z"], 5.0, places=3)
        self.assertAlmostEqual(st["status"]["tlo"], 0.0)
        self.assertAlmostEqual(st["offsets"]["G54"]["z"], SURFACE, places=3)

        # tool 3: TLO measured against the probe reference
        t = self.call("POST", "/api/tool", {"tool": 3})
        self.assertAlmostEqual(t["result"]["tlo"], -15.405, places=3)

        # move to work Z 10 over the probed spot, then index points
        self.call("POST", "/api/move", {"z": 10, "system": "work", "z_feed": 1500})
        self.assertAlmostEqual(self.status()["status"]["wpos"]["z"], 10.0, places=3)
        self.call("POST", "/api/jog", {"axis": "x", "distance": -20})
        self.call("POST", "/api/points", {"name": "start", "axes": ["x"]}, wait=False)
        sx = self.status()["status"]["mpos"]["x"]
        self.call("POST", "/api/jog", {"axis": "y", "distance": -30})
        self.call("POST", "/api/points", {"name": "start", "axes": ["y"], "note": "edges"}, wait=False)
        sy = self.status()["status"]["mpos"]["y"]
        pts = self.call("GET", "/api/points")["points"]
        self.assertEqual(sorted(pts[0]["axes"]), ["x", "y"])
        self.assertAlmostEqual(pts[0]["axes"]["x"]["machine"], sx, places=3)

        # wander off, then go back (direct = at current height)
        self.call("POST", "/api/jog", {"axis": "x", "distance": -7})
        self.call("POST", "/api/jog", {"axis": "y", "distance": 4})
        g = self.call("POST", "/api/points/start/goto", {"mode": "direct"})
        self.assertEqual(g["state"], "done", g)
        mp = self.status()["status"]["mpos"]
        self.assertAlmostEqual(mp["x"], sx, places=3)
        self.assertAlmostEqual(mp["y"], sy, places=3)
        self.assertAlmostEqual(self.status()["status"]["wpos"]["z"], 10.0, places=3)
        # lift mode retracts to safe Z first
        self.call("POST", "/api/jog", {"axis": "x", "distance": -3})
        self.call("POST", "/api/points/start/goto", {"mode": "lift"})
        self.assertAlmostEqual(self.status()["status"]["mpos"]["z"], -3.0, places=3)

        # gcode guard
        e = self.call("POST", "/api/gcode", {"lines": ["S20000 M3"]}, expect=422)
        self.assertIn("spindle", e["error"])
        gc = self.call("POST", "/api/gcode", {"lines": ["G91 G1 X-1 F600"]})
        self.assertEqual(gc["state"], "done", gc)
        self.assertIn("G90", self.status()["modal"] or "G90")

        # busy guard: start a long move without waiting, then try to jog
        self.call("POST", "/api/move", {"x": -300, "system": "machine", "feed": 300}, wait=False)
        time.sleep(0.2)
        self.call("POST", "/api/jog", {"axis": "y", "distance": -1}, wait=False, expect=409)
        # stop aborts; unlock recovers
        self.call("POST", "/api/stop", {}, wait=False)
        time.sleep(0.6)
        self.assertEqual(self.status()["status"]["state"], "Alarm")
        u = self.call("POST", "/api/unlock", {})
        self.assertEqual(u["state"], "done", u)
        self.assertEqual(self.status()["status"]["state"], "Idle")

        # activity log saw the UI-style sources
        acts = self.call("GET", "/api/activity?limit=200")["items"]
        self.assertTrue(any(a["action"] == "save_point" for a in acts))
        self.assertTrue(all(a["source"] in ("test", "system") for a in acts))

        self.call("POST", "/api/disconnect", {}, wait=False)
        self.assertFalse(self.status()["connected"])

    def test_02_vacuum(self):
        if not self.status()["connected"]:
            self.call("POST", "/api/connect", {}, wait=False)
        # the poller reads 'diagnose' about once a second
        deadline = time.time() + 4
        while self.status()["vacuum"]["on"] is None and time.time() < deadline:
            time.sleep(0.1)
        self.assertIs(self.status()["vacuum"]["probe_charger"], False)   # test_01's STOP halted it
        self.call("POST", "/api/gcode", {"lines": ["M841"]})              # charger back on, as on the machine
        deadline = time.time() + 4
        while self.status()["vacuum"]["probe_charger"] is not True and time.time() < deadline:
            time.sleep(0.1)
        v = self.status()["vacuum"]
        self.assertIs(v["on"], False, v)
        self.assertIs(v["probe_charger"], True, v)
        self.assertLess(v["age"], 3.0)
        # diagnose replies are state, not messages
        msgs = [m["text"] for m in self.call("GET", "/api/messages")["messages"]]
        self.assertFalse(any(t.startswith("{") for t in msgs), msgs[-5:])

        r = self.call("POST", "/api/vacuum", {"on": True, "power": 60}, wait=False)
        self.assertEqual(r["command"], "M801 S60")
        self.assertTrue(r["confirmed"], r)
        self.assertEqual(r["vacuum"], {"on": True, "power": 60})
        self.assertEqual(self.status()["vacuum"]["power"], 60)
        r = self.call("POST", "/api/vacuum", {"on": True}, wait=False)       # default power from config
        self.assertEqual(r["vacuum"], {"on": True, "power": 80}, r)
        e = self.call("POST", "/api/vacuum", {"on": True, "power": 150}, wait=False, expect=400)
        self.assertIn("1-100", e["error"])
        e = self.call("POST", "/api/vacuum", {}, wait=False, expect=400)
        self.assertIn("on", e["error"])
        r = self.call("POST", "/api/vacuum", {"on": False}, wait=False)
        self.assertTrue(r["confirmed"], r)
        self.assertEqual(r["vacuum"], {"on": False, "power": 0})
        self.assertEqual(self.call("GET", "/api/vacuum")["default_power"], 80)

        # M400 holds the line during a move: refused as busy rather than queued behind it
        self.call("POST", "/api/vacuum", {"on": True, "power": 70}, wait=False)
        self.call("POST", "/api/move", {"x": -300, "system": "machine", "feed": 10}, wait=False)  # ~45 s in the sim
        time.sleep(0.3)
        e = self.call("POST", "/api/vacuum", {"on": False}, wait=False, expect=409)
        self.assertIn("busy", e["error"])
        # a halt switches the vacuum and the probe charger off (real firmware failsafe), and the
        # vacuum command is refused in Alarm
        self.call("POST", "/api/stop", {}, wait=False)
        time.sleep(0.6)
        e = self.call("POST", "/api/vacuum", {"on": True}, wait=False, expect=400)
        self.assertIn("Alarm", e["error"])
        deadline = time.time() + 4
        while self.status()["vacuum"]["on"] is not False and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(1.2)          # one more poll, answered while in Alarm
        v = self.status()["vacuum"]
        self.assertIs(v["on"], False, v)
        self.assertIs(v["probe_charger"], False, v)
        self.call("POST", "/api/unlock", {})
        acts = self.call("GET", "/api/activity?limit=50")["items"]
        self.assertTrue(any(a["action"] == "vacuum" and a["detail"]["confirmed"] for a in acts))
        self.assertTrue(any(a["action"] == "vacuum:failed" for a in acts))
        self.call("POST", "/api/disconnect", {}, wait=False)
        self.assertIsNone(self.status()["vacuum"]["on"])

    @staticmethod
    def sim_obj():
        import gc
        return next(o for o in gc.get_objects() if isinstance(o, carvera_sim.Sim))

    def test_03_probe_stow(self):
        """USER 2026-11-24: stowing the probe plunges 2 mm below the firmware's drop so it seats on
        its charger. Other tools keep the firmware's M6."""
        if not self.status()["connected"]:
            self.call("POST", "/api/connect", {}, wait=False)
        sim, m = self.sim_obj(), self.app.machine
        if self.status()["status"]["state"] == "Alarm":
            self.call("POST", "/api/unlock", {})
        self.assertEqual(self.call("POST", "/api/tool", {"tool": 0})["result"]["tool"], 0)
        self.call("POST", "/api/move", {"x": -150, "y": -120, "system": "machine"})
        sim.charger = 0
        r = self.call("POST", "/api/tool", {"tool": -1})["result"]
        st = r["stow"]
        self.assertEqual(st["release_z"], -114.5, r)
        self.assertEqual(st["extra_mm"], 2.0)
        self.assertEqual(sim.seated[0], -114.5)           # released 2 mm below the slot Z
        self.assertIn(0, sim.rack)
        self.assertEqual(sim.tool, -1)
        self.assertEqual(sim.charger, 1)                   # M841 after the stow
        mp = self.status()["status"]["mpos"]               # back over the start X/Y at clearance, like M6
        self.assertEqual((round(mp["x"], 3), round(mp["y"], 3), round(mp["z"], 3)), (-150.0, -120.0, -3.0))

        # T0 -> T3: deep stow, then the firmware only picks and measures T3
        self.call("POST", "/api/tool", {"tool": 0})
        sim.seated.clear()
        r = self.call("POST", "/api/tool", {"tool": 3})["result"]
        self.assertEqual(r["tool"], 3, r)
        self.assertIn("stow", r)
        self.assertEqual(sim.seated[0], -114.5)
        # any other tool keeps the firmware's drop
        r = self.call("POST", "/api/tool", {"tool": 4})["result"]
        self.assertNotIn("stow", r)
        self.assertEqual(sim.seated[3], -112.5)

        # slot already filled: M492.2 halts before the plunge
        self.call("POST", "/api/tool", {"tool": 0})
        sim.rack.add(0)
        e = self.call("POST", "/api/tool", {"tool": -1}, expect=422)
        self.assertIn("M492.2", e["error"])
        self.assertIn("presence", e["error"])
        deadline = time.time() + 3
        while self.status()["status"]["state"] != "Alarm" and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.status()["status"]["state"], "Alarm")
        self.assertGreater(sim.mpos[2], -10)               # never went down
        sim.rack.discard(0)
        self.call("POST", "/api/unlock", {})

        # the firmware's slot moved: refused before any motion
        before = self.status()["status"]["mpos"]
        saved = dict(m.probe_slot)
        m.probe_slot["y"] = -30.0
        try:
            e = self.call("POST", "/api/tool", {"tool": -1}, expect=422)
            self.assertIn("mismatch", e["error"])
            self.assertEqual(self.status()["status"]["mpos"], before)
        finally:
            m.probe_slot = saved
        # deeper than 3 mm is refused
        m.probe_stow_extra_mm = 5.0
        try:
            e = self.call("POST", "/api/tool", {"tool": -1}, expect=422)
            self.assertIn("up to 3", e["error"])
        finally:
            m.probe_stow_extra_mm = 2.0
        # 0 turns it off: the firmware's own drop
        m.probe_stow_extra_mm = 0.0
        try:
            r = self.call("POST", "/api/tool", {"tool": -1})["result"]
            self.assertNotIn("stow", r)
            self.assertEqual(sim.seated[0], -112.5)
        finally:
            m.probe_stow_extra_mm = 2.0

        # raw G-code that moves offsets refreshes them (was stale after M495, 2026-11-24)
        g = self.call("POST", "/api/gcode", {"lines": ["G10 L2 P1 Z-90"]})["result"]
        self.assertAlmostEqual(g["offsets"]["G54"]["z"], -90.0)
        self.assertAlmostEqual(self.status()["offsets"]["G54"]["z"], -90.0)
        self.call("POST", "/api/disconnect", {}, wait=False)

    def test_00_default_config(self):
        """A fresh install has no machine IP: connect explains how to set one instead of failing obscurely."""
        from carvera_control.server import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG["host"], "")
        for k in ("port", "safe_z_machine", "feeds", "vacuum_power", "spindle_rpm", "probe_stow_extra_mm"):
            self.assertIn(k, DEFAULT_CONFIG)
        app = App(tempfile.mkdtemp(prefix="carvera-fresh-"), None, None, discovery=False)
        self.assertEqual(app.machine.host, "")
        with self.assertRaises(Exception) as cm:
            app.machine.connect()
        self.assertIn("no Carvera found", str(cm.exception))

    def test_04_spindle(self):
        """Web UI spindle control (2026-11-24): only with a cutting tool, confirmed from 'diagnose'."""
        if not self.status()["connected"]:
            self.call("POST", "/api/connect", {}, wait=False)
        if self.status()["status"]["state"] == "Alarm":
            self.call("POST", "/api/unlock", {})
        # never with the wireless probe in the spindle
        self.call("POST", "/api/tool", {"tool": 0})
        e = self.call("POST", "/api/spindle", {"on": True, "rpm": 12000}, wait=False, expect=400)
        self.assertIn("cutting tool", e["error"])
        deadline = time.time() + 4
        while self.call("GET", "/api/spindle")["on"] is None and time.time() < deadline:
            time.sleep(0.1)
        sv = self.call("GET", "/api/spindle")
        self.assertFalse(sv["can_start"], sv)
        self.assertIn("probe", sv["why_not"].lower())
        self.assertEqual(sv["default_rpm"], 10000)

        self.call("POST", "/api/tool", {"tool": 3})
        deadline = time.time() + 2
        while not self.call("GET", "/api/spindle")["can_start"] and time.time() < deadline:
            time.sleep(0.1)
        r = self.call("POST", "/api/spindle", {"on": True, "rpm": 12000}, wait=False)
        self.assertEqual(r["command"], "M3 S12000")
        self.assertTrue(r["confirmed"], r)
        self.assertEqual(r["spindle"], {"on": True, "target": 12000})
        time.sleep(0.4)
        self.assertGreater(self.status()["status"]["spindle_rpm"], 1000)
        self.assertEqual(self.status()["spindle"]["on"], True)
        # the tool change refuses while it turns
        e = self.call("POST", "/api/tool", {"tool": 4}, expect=422)
        self.assertIn("spindle", e["error"])
        r = self.call("POST", "/api/spindle", {"on": False}, wait=False)
        self.assertEqual(r["command"], "M5")
        self.assertTrue(r["confirmed"], r)
        self.assertIs(r["spindle"]["on"], False)
        # bad input
        e = self.call("POST", "/api/spindle", {"on": True, "rpm": 25000}, wait=False, expect=400)
        self.assertIn("1000-20000", e["error"])
        e = self.call("POST", "/api/spindle", {}, wait=False, expect=400)
        self.assertIn("on", e["error"])
        # default speed from config, then off
        r = self.call("POST", "/api/spindle", {"on": True}, wait=False)
        self.assertEqual(r["spindle"], {"on": True, "target": 10000}, r)
        # a halt stops it and the command is refused in Alarm
        self.call("POST", "/api/stop", {}, wait=False)
        time.sleep(0.6)
        e = self.call("POST", "/api/spindle", {"on": True, "rpm": 12000}, wait=False, expect=400)
        self.assertIn("Alarm", e["error"])
        self.assertLess(self.status()["status"]["spindle_rpm"], 1)
        self.call("POST", "/api/unlock", {})
        acts = self.call("GET", "/api/activity?limit=60")["items"]
        self.assertTrue(any(a["action"] == "spindle" and a["detail"]["confirmed"] for a in acts))
        self.assertTrue(any(a["action"] == "spindle:failed" for a in acts))
        self.call("POST", "/api/disconnect", {}, wait=False)
        self.assertIsNone(self.status()["spindle"]["on"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
