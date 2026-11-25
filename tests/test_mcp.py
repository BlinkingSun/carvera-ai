"""MCP over stdio -> server (--sim) -> simulator. Usage: test_mcp.py <server_port>"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = sys.argv[1]


def payload(res):
    return json.loads(res.content[0].text) if res.content else None


async def main():
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "mcp", "carvera_mcp.py")],
                                   env={**os.environ, "CARVERA_PORT": PORT})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = sorted(t.name for t in (await s.list_tools()).tools)
            print("tools:", len(names), names)
            st = payload(await s.call_tool("connect", {}))
            assert st["connected"], st
            j = payload(await s.call_tool("jog", {"axis": "x", "distance": -2.5}))
            assert j["state"] == "done", j
            p = payload(await s.call_tool("save_point", {"name": "mcp-pt", "axes": ["x", "y"], "note": "from mcp"}))
            assert abs(p["axes"]["x"]["machine"] - (-3.5)) < 1e-3, p
            bad = payload(await s.call_tool("jog", {"axis": "y", "distance": 10}))
            assert bad["state"] == "failed" and "outside" in bad["error"], bad
            g = payload(await s.call_tool("send_gcode", {"lines": ["M3 S10000"]}))
            assert g["state"] == "failed" and "spindle" in g["error"], g
            acts = payload(await s.call_tool("get_activity", {"limit": 10}))["items"]
            assert any(a["source"] == "mcp" and a["action"] == "save_point" for a in acts), acts
            vac = payload(await s.call_tool("vacuum", {"on": True, "power": 50}))
            assert vac["confirmed"] and vac["vacuum"] == {"on": True, "power": 50}, vac
            vac = payload(await s.call_tool("vacuum", {"on": False}))
            assert vac["confirmed"] and vac["vacuum"]["on"] is False, vac
            st = payload(await s.call_tool("machine_status", {}))
            assert st["vacuum"]["on"] is False and st["vacuum"]["probe_charger"] is True, st["vacuum"]
            print("status:", st["state"], st["machine_pos"], st["tool"], "vacuum", st["vacuum"])
            payload(await s.call_tool("delete_point", {"name": "mcp-pt"}))
            print("MCP OK")


asyncio.run(main())
