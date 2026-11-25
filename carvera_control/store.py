"""Persistent state: indexing points (per-axis captures) and the activity log."""
from __future__ import annotations

import collections
import json
import os
import threading
import time


def _atomic_write(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class Activity:
    """Every action from the UI, the API or the MCP, newest last. Mirrored to activity.jsonl
    so Claude can read what the user did in the UI."""

    def __init__(self, path: str, keep: int = 500):
        self.path = path
        self.items: collections.deque = collections.deque(maxlen=keep)
        self.seq = 0
        self.lock = threading.Lock()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    for line in f.readlines()[-keep:]:
                        item = json.loads(line)
                        self.items.append(item)
                        self.seq = max(self.seq, item.get("seq", 0))
            except (OSError, ValueError):
                pass

    def __call__(self, source: str, action: str, detail=None) -> dict:
        with self.lock:
            self.seq += 1
            item = {"seq": self.seq, "t": round(time.time(), 3),
                    "time": time.strftime("%H:%M:%S"), "source": source, "action": action,
                    "detail": detail}
            self.items.append(item)
            try:
                with open(self.path, "a") as f:
                    f.write(json.dumps(item, default=str) + "\n")
            except OSError:
                pass
            return item

    def recent(self, limit: int = 50, since: int = 0) -> list[dict]:
        with self.lock:
            items = [i for i in self.items if i["seq"] > since]
        return items[-limit:]


class Points:
    """Named indexing points. Each axis is captured separately (as in the touch-off workflow:
    save X at one spot, Y at another) and stores machine + work coordinates, tool and TLO."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.points: dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.points = json.load(f).get("points", {})
            except (OSError, ValueError):
                self.points = {}

    def _save(self) -> None:
        _atomic_write(self.path, {"points": self.points})

    def list(self) -> list[dict]:
        with self.lock:
            return sorted(self.points.values(), key=lambda p: p.get("created", 0))

    def get(self, name: str) -> dict:
        p = self.points.get(name)
        if not p:
            raise KeyError(f"no point named {name!r}")
        return p

    def capture(self, name: str, axes: list[str], snapshot: dict, note: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("point name is required")
        axes = [a.lower() for a in axes] or ["x", "y", "z"]
        st = snapshot["status"]
        if "mpos" not in st:
            raise ValueError("no machine position yet")
        g54 = snapshot.get("offsets", {}).get("G54")
        now = time.time()
        with self.lock:
            p = self.points.setdefault(name, {"name": name, "note": "", "created": now, "axes": {}})
            for ax in axes:
                if ax not in ("x", "y", "z"):
                    raise ValueError(f"bad axis {ax!r}")
                p["axes"][ax] = {"machine": round(st["mpos"][ax], 4), "work": round(st["wpos"][ax], 4),
                                 "tool": st.get("tool"), "tlo": st.get("tlo"),
                                 "g54": round(g54[ax], 4) if g54 else None,
                                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            if note is not None:
                p["note"] = note
            p["updated"] = now
            self._save()
            return dict(p)

    def update(self, name: str, note: str | None = None, new_name: str | None = None) -> dict:
        with self.lock:
            p = self.get(name)
            if note is not None:
                p["note"] = note
            if new_name and new_name != name:
                if new_name in self.points:
                    raise ValueError(f"a point named {new_name!r} already exists")
                p["name"] = new_name
                self.points[new_name] = self.points.pop(name)
            p["updated"] = time.time()
            self._save()
            return dict(p)

    def delete(self, name: str) -> None:
        with self.lock:
            self.get(name)
            del self.points[name]
            self._save()
