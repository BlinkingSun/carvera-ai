"""Programs end to end against the simulator: analyze, XMODEM upload (with a NAK resend and a busy
refusal), md5 verification, play with tool changes, the kill switch, and the md5 guard."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_e2e import start_stack  # noqa: E402


def program(path: str, extra_lines: int = 1500) -> None:
    """Two-tool job shaped like a Fusion Carvera post: T3 pocket passes, T4 pass, back to T3."""
    L = ["(test job)", "G90 G94", "G17", "G21", "M370", "(op1)", "T3M6", "(5)", "M400", "M851 S100",
         "S20000 M3", "G54", "G0 X10. Y10.", "Z15.", "G1 Z2. F800", "Z-1. F120"]
    for i in range(extra_lines):
        L.append(f"G1 X{10 + (i % 50) * 1.2:.3f} Y{10 + (i // 50) * 0.8:.3f} Z-1. F800")
    L += ["G2 X20. Y30. I5. J0.", "G3 X30. Y30. I5. J0.", "G1 Z15.", "M5", "M400", "M852",
          "(op2)", "T4M6", "(6)", "M400", "M851 S100", "S20000 M3", "G54", "G0 X40. Y40.", "Z15.",
          "G1 Z-2. F800", "X60. Y40.", "X60. Y50.", "Z15.", "M5", "M400", "M852",
          "(op3)", "T3M6", "(5)", "M400", "M851 S100", "S20000 M3", "G54", "G0 X12. Y12.", "Z15.",
          "G1 Z-0.5 F800", "X70. Y12.", "Z15.", "M5", "M400", "M852", "G28", "M30"]
    with open(path, "wb") as f:
        f.write(("\r\n".join(L) + "\r\n").encode())


class Programs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.base = start_stack()
        cls.sim = None
        cls.tmp = tempfile.mkdtemp(prefix="carvera-prog-")
        cls.nc = os.path.join(cls.tmp, "job.cnc")
        program(cls.nc)

    def call(self, method, path, body=None, wait=True, expect=200, timeout=300):
        url = self.base + path + ("?wait=1&timeout=600" if wait and method == "POST" else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Source": "test"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
        self.assertEqual(code, expect, out)
        return out

    @staticmethod
    def sim_obj():
        import gc
        import carvera_sim
        return next(o for o in gc.get_objects() if isinstance(o, carvera_sim.Sim))

    def test_01_connect_and_analyze(self):
        self.assertTrue(self.call("POST", "/api/connect", {})["connected"])
        a = self.call("GET", "/api/program/analyze?path=" + urllib.request.quote(self.nc))
        self.assertEqual(a["tools"], [3, 4, 3])
        self.assertEqual(a["z"][0], -2.0)
        self.assertTrue(a["clears_leveling"])
        self.assertLessEqual(a["x"][0], 10.0)
        self.assertGreaterEqual(a["x"][1], 70.0)
        self.assertGreater(a["bytes"], 3 * 8192)   # several XMODEM blocks

    def test_02_upload_busy_refused(self):
        sim = self.sim_obj()
        sim.upload_busy = True
        try:
            out = self.call("POST", "/api/program/upload", {"path": self.nc, "name": "job.cnc"}, expect=422)
            self.assertIn("refused", out["error"])
        finally:
            sim.upload_busy = False
        st = self.call("GET", "/api/status")
        self.assertTrue(st["connected"])
        time.sleep(0.6)
        self.assertLess(self.call("GET", "/api/status")["status_age"], 1.0)   # polling resumed

    def test_03_upload_with_nak(self):
        sim = self.sim_obj()
        sim.nak_once = True
        out = self.call("POST", "/api/program/upload", {"path": self.nc, "name": "job.cnc"})
        sim.nak_once = False
        r = out["result"]
        self.assertTrue(r["verified"])
        self.assertEqual(r["remote"], "/sd/gcodes/job.cnc")
        self.assertGreaterEqual(r["blocks"], 4)
        with open(self.nc, "rb") as f:
            self.assertEqual(sim.files["/sd/gcodes/job.cnc"], f.read())
        time.sleep(0.6)
        self.assertLess(self.call("GET", "/api/status")["status_age"], 1.0)

    def test_04_run(self):
        out = self.call("POST", "/api/program/run", {"path": self.nc, "name": "job.cnc"})
        self.assertEqual(out["state"], "done", out)
        st = self.call("GET", "/api/status")
        self.assertEqual(st["status"]["tool"], 3)          # T3 -> T4 -> T3 ran inside the program
        self.assertIsNone(st["status"].get("progress"))     # no longer playing
        msgs = [m["text"] for m in self.call("GET", "/api/messages")["messages"]]
        self.assertTrue(any("Done printing file" in t for t in msgs))
        self.assertGreaterEqual(sum("Done ATC" in t for t in msgs), 2)

    def test_05_kill_switch(self):
        # shrink the envelope inside the program: the first cut beyond it must trip the kill switch
        try:
            out = self.call("POST", "/api/program/run", {"path": self.nc, "name": "job.cnc", "margin_xy": -20},
                            expect=422)
            self.assertIn("kill switch", out["error"])
            st = self.call("GET", "/api/status")
            self.assertEqual(st["status"]["state"], "Alarm")
            self.assertEqual(self.sim_obj().playing, None)
            self.assertEqual(self.sim_obj().queue, [])
        finally:
            self.call("POST", "/api/unlock", {})

    def test_06_md5_guard(self):
        other = os.path.join(self.tmp, "other.cnc")
        program(other, extra_lines=10)
        out = self.call("POST", "/api/program/run", {"path": other, "name": "job.cnc"}, expect=422)
        self.assertIn("does not match", out["error"])

    def test_08_vacuum_during_run(self):
        """The 2026-11-23 case: the program never switched the onboard vacuum on, so it is switched on
        while the file plays. The run must keep going."""
        job = self.call("POST", "/api/program/run", {"path": self.nc, "name": "job.cnc"}, wait=False)
        jid = job["id"]
        deadline = time.time() + 30
        j = job
        while time.time() < deadline:
            j = self.call("GET", f"/api/job/{jid}")
            if (j.get("progress") or {}).get("lines") or j["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(j["state"], "running", j)
        v = self.call("POST", "/api/vacuum", {"on": True, "power": 80}, wait=False)
        self.assertTrue(v["confirmed"], v)
        self.assertEqual(self.sim_obj().vacuum, [1, 80])
        # the program owns the spindle: the UI spindle control refuses both ways while it plays
        for body in ({"on": False}, {"on": True, "rpm": 12000}):
            e = self.call("POST", "/api/spindle", body, wait=False, expect=400)
            self.assertIn("program is running", e["error"])
        self.assertFalse(self.call("GET", "/api/spindle")["can_start"])
        self.assertEqual(self.call("GET", f"/api/job/{jid}")["state"], "running")
        while j["state"] == "running" and time.time() < deadline + 60:
            time.sleep(0.2)
            j = self.call("GET", f"/api/job/{jid}")
        self.assertEqual(j["state"], "done", j)
        self.assertEqual(self.call("GET", "/api/status")["vacuum"]["on"], True)   # stays on after the job
        self.assertEqual(self.call("POST", "/api/vacuum", {"on": False}, wait=False)["vacuum"]["on"], False)

    def test_07_bad_name(self):
        out = self.call("POST", "/api/program/upload", {"path": self.nc, "name": "has space.cnc"}, wait=False,
                        expect=500 if False else 400)
        self.assertIn("name", out.get("error", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
