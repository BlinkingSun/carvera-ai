#!/usr/bin/env python3
"""Carvera Control MCP server.

Thin FastMCP relay over the Carvera Control HTTP API (http://127.0.0.1:${CARVERA_PORT:-17916}).
The server owns the one TCP connection to the machine; the user's web UI and this MCP share it,
and every action lands in one activity log (source "ui" vs "mcp").

Run:       python3 mcp/carvera_mcp.py
Register:  claude mcp add carvera --scope user -- python3 "<abs>/mcp/carvera_mcp.py"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

PORT = int(os.environ.get("CARVERA_PORT", "17916"))
BASE = os.environ.get("CARVERA_URL", f"http://127.0.0.1:{PORT}")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

mcp = FastMCP(
    "carvera",
    instructions=(
        "Carvera Control drives the user's Makera Carvera CNC (Wi-Fi, Smoothie-based firmware). "
        "The user does fine positioning (finding edges / indexing points) in the web UI; you help plan "
        "and do the math, and act on the machine when asked. Start with machine_status and get_activity "
        "(what the user just did in the UI) and list_points (their saved indexing points). "
        "SAFETY: only move the machine when the user asked for that motion; say what you will send "
        "before you send it; prefer feed moves near the work; never start the spindle unless asked. "
        "SITE RULES (the reference setup's; the user's own are in site-rules.txt at the project root): "
        "(1) DOWELS: the tool must NEVER travel below Z0, the spoilboard top "
        "surface, within R5 of the dowels at work X82.5 Y126 and X171 Y37.5 (holes D4.1 for 4 mm dowels; "
        "the spoilboard is 335x225x6.35 PVC, its front-left corner = G54 0,0). This applies to every "
        "program, probe, grid and jog; the "
        "only exception is the spoilboard program that cuts those holes. (2) ANCHOR 1: nothing travels "
        "over the anchor block at any height and no probing there (work X-15..85 x Y-15..0 and "
        "X-15..0 x Y-15..85); never start a tool change while parked over it. Check machine_status "
        "(position, tool) before every motion step. "
        "Coordinates: 'machine' is absolute machine space (X right+, Y away+, Z up+, all <= 0); 'work' is "
        "the active G54. Points store machine X/Y (stable while homing holds) and work Z. "
        "Setting work Z (probe_z with set_zero, or set_work_zero z) resets the tool length offset: the "
        "current tool becomes the length reference and later tools are measured against it at tool change. "
        "T0 is the wireless probe; the ATC measures each tool on the setter at M6."
    ),
)


class CarveraError(Exception):
    pass


def _start_server() -> None:
    log = open(os.path.join(ROOT, "data", "server.log"), "a")
    subprocess.Popen([sys.executable, "-m", "carvera_control"], cwd=ROOT, stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        time.sleep(0.1)
        try:
            urllib.request.urlopen(BASE + "/api/status", timeout=1).read()
            return
        except Exception:  # noqa: BLE001
            continue
    raise CarveraError(f"started the Carvera Control server but it is not answering at {BASE}")


def _call(method: str, path: str, body: dict | None = None, query: dict | None = None,
          timeout: float = 900, _retry: bool = True) -> Any:
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Source": "mcp"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read() or b"{}")
        except ValueError:
            out = {}
        if e.code == 422 and "state" in out:   # a job that ran and failed: return the full record
            return out
        raise CarveraError(out.get("error") or f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if _retry and isinstance(e.reason, ConnectionRefusedError):
            os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
            _start_server()
            return _call(method, path, body, query, timeout, _retry=False)
        raise CarveraError(f"Carvera Control server unreachable at {BASE}: {e.reason}") from None


def _job(path: str, body: dict) -> dict:
    out = _call("POST", path, body, {"wait": 1})
    return {k: out.get(k) for k in ("kind", "state", "result", "error", "messages") if k in out}


def _brief(s: dict) -> dict:
    st = s.get("status") or {}
    return {
        "connected": s.get("connected"), "machine": f"{s.get('info', {}).get('name', '')} {s.get('host')}".strip(),
        "firmware": s.get("info", {}).get("version"), "state": st.get("state"),
        "machine_pos": st.get("mpos"), "work_pos": st.get("wpos"),
        "tool": s.get("tool_name"), "tlo": st.get("tlo"), "probe_battery_v": st.get("probe_v"),
        "spindle_rpm": st.get("spindle_rpm"), "vacuum": s.get("vacuum"),
        "G54": (s.get("offsets") or {}).get("G54"),
        "last_probe": (s.get("offsets") or {}).get("PRB"), "modal": s.get("modal"),
        "job": s.get("job"), "limits_machine": s.get("limits"), "safe_z_machine": s.get("safe_z_machine"),
    }


# ----------------------------------------------------------------------------- status & connection
@mcp.tool()
def machine_status() -> dict:
    """Live machine state: connection, state, machine/work position, tool + TLO, probe battery,
    spindle, vacuum (on/power/auto + probe_charger; null = unknown), G54, last probe result and
    the current/last job."""
    return _brief(_call("GET", "/api/status"))


@mcp.tool()
def connect(host: Annotated[str | None, Field(description="Machine IP; default from config")] = None,
            force: Annotated[bool, Field(description="Connect even if the machine reports another client")] = False) -> dict:
    """Open the server's connection to the Carvera. Refuses if CarveraController (or anything else)
    already holds it, unless force."""
    return _brief(_call("POST", "/api/connect", {"host": host, "force": force}))


@mcp.tool()
def disconnect() -> dict:
    """Release the machine so CarveraController can connect."""
    return _call("POST", "/api/disconnect", {})


@mcp.tool()
def discover(timeout: float = 3.0) -> dict:
    """Listen for Carvera discovery broadcasts (UDP 3333). busy=true means a controller is connected."""
    return _call("GET", "/api/discover", query={"timeout": timeout})


@mcp.tool()
def get_activity(limit: int = 30, since: int = 0) -> dict:
    """Recent actions from the UI (source 'ui'), this MCP ('mcp') and the system, oldest first.
    Read this to see what the user just did in the UI (jogs, saved points, probes)."""
    return _call("GET", "/api/activity", query={"limit": limit, "since": since})


@mcp.tool()
def get_messages(since: int = 0) -> dict:
    """Raw text lines the machine printed (ATC progress, probe results, errors)."""
    return _call("GET", "/api/messages", query={"since": since})


# ----------------------------------------------------------------------------- motion
@mcp.tool()
def jog(axis: Literal["x", "y", "z"],
        distance: Annotated[float, Field(description="mm, signed. +X right, +Y away, +Z up")],
        feed: Annotated[float | None, Field(description="mm/min (default 1000 XY / 500 Z)")] = None) -> dict:
    """Relative feed move on one axis (G91 G1 ... then G90). Waits until the move is finished."""
    return _job("/api/jog", {"axis": axis, "distance": distance, "feed": feed})


@mcp.tool()
def move_to(x: float | None = None, y: float | None = None, z: float | None = None,
            system: Literal["work", "machine"] = "work",
            feed: Annotated[float | None, Field(description="XY feed mm/min")] = None,
            z_feed: Annotated[float | None, Field(description="Z feed mm/min")] = None,
            rapid: bool = False) -> dict:
    """Absolute move. Z goes first when rising and last when descending. Targets are checked
    against machine travel."""
    return _job("/api/move", {"x": x, "y": y, "z": z, "system": system, "feed": feed,
                              "z_feed": z_feed, "rapid": rapid})


@mcp.tool()
def home() -> dict:
    """Home all axes ($H). Ends at machine -1, -1, -1. Offsets and TLO survive homing."""
    return _job("/api/home", {})


@mcp.tool()
def tool_change(tool: Annotated[int, Field(description="0 = probe, 1-6 = rack tools, -1 = empty")],
                remeasure: bool = False) -> dict:
    """ATC tool change (M6T<n>); the new tool is measured on the setter against the current
    length reference. Returns the new TLO and setter Z. When the probe (T0) is the tool being put
    away, it is stowed with a deeper plunge (probe_stow_extra_mm, default 2 mm lower than the
    firmware's drop) so it seats on its charger; the result then carries 'stow' with the in-spindle
    probe voltage to compare against the docked reading."""
    return _job("/api/tool", {"tool": tool, "remeasure": remeasure})


@mcp.tool()
def probe_z(max_distance: Annotated[float, Field(description="max downward travel, mm")] = 30.0,
            lift: Annotated[float, Field(description="lift after the touch, mm")] = 5.0,
            set_zero: Annotated[bool, Field(description="set work Z0 on the surface")] = True) -> dict:
    """Probe straight down here with the wireless probe (T0): fast G38.3 at 300 mm/min, back off 2 mm,
    slow re-touch at 60 mm/min, lift. With set_zero, work Z0 is set on the surface via G10 L20
    (verified; repeated once if the firmware's TLO reset leaves the readout off)."""
    return _job("/api/probe/z", {"max_distance": max_distance, "lift": lift, "set_zero": set_zero})


@mcp.tool()
def set_work_zero(x: float | None = None, y: float | None = None, z: float | None = None) -> dict:
    """Make the CURRENT position read these work values (G10 L20 P0). E.g. z=5 means 'the tool tip
    is 5 mm above work zero'. Setting Z resets the TLO (current tool becomes the reference)."""
    vals = {k: v for k, v in (("x", x), ("y", y), ("z", z)) if v is not None}
    return _job("/api/zero", {"values": vals})


@mcp.tool()
def send_gcode(lines: Annotated[list[str], Field(description="G-code lines, sent in order")],
               allow_spindle: bool = False) -> dict:
    """Send raw G-code (waits for motion to finish; restores G90 if G91 was used). Tool changes are
    refused (use tool_change); spindle starts are refused unless allow_spindle."""
    return _job("/api/gcode", {"lines": lines, "allow_spindle": allow_spindle})


@mcp.tool()
def vacuum(on: Annotated[bool, Field(description="true = switch the onboard vacuum on, false = off")],
           power: Annotated[int | None, Field(description="1-100 %; default from config (80)")] = None) -> dict:
    """Onboard vacuum (M801 S<power> / M802), read back from the firmware's 'diagnose' report
    (confirmed=true when the machine reports the new state). Not motion. Works while a program
    plays: the switch acts at once and the cut does not pause. Current state, plus the probe
    charger, is in machine_status under 'vacuum'. Fusion's M851/M852 switch the external output,
    not this vacuum."""
    body: dict = {"on": on}
    if power is not None:
        body["power"] = power
    return _call("POST", "/api/vacuum", body)


@mcp.tool()
def feed_hold() -> dict:
    """Pause motion (!)."""
    return _call("POST", "/api/hold", {})


@mcp.tool()
def resume() -> dict:
    """Resume after a feed hold (~)."""
    return _call("POST", "/api/resume", {})


@mcp.tool()
def stop() -> dict:
    """Feed hold + abort (Ctrl-X). Clears queued motion; the machine needs unlock afterwards.
    Software stop only — not a substitute for the E-stop."""
    return _call("POST", "/api/stop", {})


@mcp.tool()
def unlock() -> dict:
    """Clear an alarm ($X)."""
    return _job("/api/unlock", {})


# ----------------------------------------------------------------------------- points
@mcp.tool()
def list_points() -> dict:
    """Saved indexing points. Each axis is captured separately with machine + work coordinates,
    tool, TLO and the G54 value at capture."""
    return _call("GET", "/api/points")


@mcp.tool()
def save_point(name: str,
               axes: Annotated[list[Literal["x", "y", "z"]], Field(description="axes to capture from the current position")],
               note: str | None = None) -> dict:
    """Capture the current position into a named point (only the given axes; others are kept)."""
    return _call("POST", "/api/points", {"name": name, "axes": axes, "note": note})


@mcp.tool()
def goto_point(name: str, mode: Literal["lift", "direct"] = "lift", include_z: bool = False,
               feed: float | None = None) -> dict:
    """Move to a saved point. lift = rapid up to safe machine Z first; direct = XY at the current
    height. include_z also descends to the point's work Z (refused if work Z0 changed since capture)."""
    return _job(f"/api/points/{urllib.parse.quote(name, safe='')}/goto",
                {"mode": mode, "include_z": include_z, "feed": feed})


@mcp.tool()
def edit_point(name: str, note: str | None = None, new_name: str | None = None) -> dict:
    """Change a point's note or name."""
    return _call("PATCH", f"/api/points/{urllib.parse.quote(name, safe='')}", {"note": note, "new_name": new_name})


@mcp.tool()
def delete_point(name: str) -> dict:
    """Delete a saved point."""
    return _call("DELETE", f"/api/points/{urllib.parse.quote(name, safe='')}")


@mcp.tool()
def open_ui() -> dict:
    """Open the Carvera Control UI in Safari."""
    _call("GET", "/api/status")
    subprocess.run(["open", "-a", "Safari", BASE + "/"], check=False)
    return {"url": BASE + "/"}


if __name__ == "__main__":
    mcp.run()
