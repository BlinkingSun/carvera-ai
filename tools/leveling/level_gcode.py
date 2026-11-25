"""Offline auto-leveling for a Carvera (G17, G90, mm) program.

Applies a probed 5x5 grid to every move's Z: bilinear inside the grid, linear extrapolation
from the edge cells outside it (the Carvera firmware instead applies ZERO outside its grid).
Lines are split into <= MAX_SEG mm pieces and G2/G3 arcs into <= MAX_SEG mm helical sub-arcs about
the original centre, so Z follows the surface and arcs stay arcs. A line with no Z while Z is still unknown (file start, after M6 / G28)
is passed through unchanged. M370 is inserted at the top so a grid held in the firmware is not
applied a second time.
usage: level_gcode.py IN OUT GRID.json
"""
import math, re, sys

import json
G = None   # set in main(): {"xs": [...], "ys": [...], "z": [[row at ys[0] ...], ...], "date": ..., "origin": [x, y]}
MAX_SEG = 3.0


def make_comp():
    XS, YS, z = G["xs"], G["ys"], G["z"]                              # z[j][i] = value at (XS[i], YS[j])

    def cell(v, axis):
        i = 0
        while i < len(axis) - 2 and v > axis[i + 1]:
            i += 1
        return i, (v - axis[i]) / (axis[i + 1] - axis[i])          # ratio unclamped -> linear extrapolation

    def comp(x, y):
        i, rx = cell(x, XS)
        j, ry = cell(y, YS)
        left = (1 - ry) * z[j][i] + ry * z[j + 1][i]
        right = (1 - ry) * z[j][i + 1] + ry * z[j + 1][i + 1]
        return (1 - rx) * left + rx * right
    return comp


def fmt(v):
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def level(lines, comp):
    out, x, y, z, mode = [], None, None, None, 0
    stats = dict(lines=0, arcs=0, segs=0)
    for raw in lines:
        s = raw.strip()
        code = re.sub(r"\(.*?\)", "", s).strip().upper()
        if not code:
            out.append(raw); continue
        if re.search(r"M0?6(?!\d)|T-?\d+M6|G28|G53", code.replace(" ", "")):
            out.append(raw); x = y = z = None; continue               # tool change / clearance: position unknown
        words = dict((k, float(v)) for k, v in re.findall(r"([A-Z])(-?\d*\.?\d+)", code))
        gs = [int(float(g)) for g in re.findall(r"G(\d+\.?\d*)", code)]
        assert not set(gs) & {91, 18, 19, 20, 2.5}, f"unsupported modal in {s!r}"
        for g in gs:
            if g in (0, 1, 2, 3):
                mode = g
        if not ({"X", "Y", "Z"} & words.keys()):
            out.append(raw); continue                                  # G17/G21/G54/G90 G94, M/S lines
        assert not ({"M", "T", "S"} & words.keys()), f"mixed motion line {s!r}"
        tx, ty, tz = words.get("X", x), words.get("Y", y), words.get("Z", z)
        f = words.get("F")
        fpart = (" F" + fmt(f)) if f is not None else ""
        stats["lines"] += 1
        if tz is None or x is None or y is None or mode == 0:
            # endpoint only: rapids, or start of a move after a tool change
            parts = [f"G{mode}"] + [f"{a}{fmt(v)}" for a, v in (("X", words.get("X")), ("Y", words.get("Y"))) if v is not None]
            if tz is not None:
                parts.append("Z" + fmt(tz + comp(tx, ty)))
            assert mode in (0, 1), f"arc with unknown start {s!r}"
            out.append(" ".join(parts) + fpart)
            stats["segs"] += 1
        elif mode == 1:
            n = max(1, math.ceil(math.hypot(tx - x, ty - y) / MAX_SEG))
            for k in range(1, n + 1):
                t = k / n
                px, py, pz = x + (tx - x) * t, y + (ty - y) * t, z + (tz - z) * t
                if k == n:
                    px, py, pz = tx, ty, tz
                out.append(f"G1 X{fmt(px)} Y{fmt(py)} Z{fmt(pz + comp(px, py))}" + (fpart if k == 1 else ""))
            stats["segs"] += n
        else:
            cx, cy = x + words.get("I", 0.0), y + words.get("J", 0.0)
            r0, r1 = math.hypot(x - cx, y - cy), math.hypot(tx - cx, ty - cy)
            assert abs(r0 - r1) < 0.01, f"arc radius mismatch {r0} {r1} in {s!r}"
            a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ty - cy, tx - cx)
            sweep = (a0 - a1) if mode == 2 else (a1 - a0)
            sweep %= 2 * math.pi
            if sweep < 1e-9:
                sweep = 2 * math.pi                                    # start == end: full circle
            # keep arcs as arcs: split into sub-arcs of <= MAX_SEG length (>= 2 for a full circle), each a
            # helical G2/G3 about the ORIGINAL centre, ending at the compensated Z of its end point
            n = max(2 if sweep > math.pi else 1, math.ceil(sweep * max(r0, r1) / MAX_SEG))
            sign = -1 if mode == 2 else 1
            sx, sy = x, y
            for k in range(1, n + 1):
                t = k / n
                if k == n:
                    px, py, pz = tx, ty, tz
                else:
                    a, rr = a0 + sign * sweep * t, r0 + (r1 - r0) * t
                    px, py, pz = cx + rr * math.cos(a), cy + rr * math.sin(a), z + (tz - z) * t
                    px, py = round(px, 4), round(py, 4)
                out.append(f"G{mode} X{fmt(px)} Y{fmt(py)} Z{fmt(pz + comp(px, py))} I{fmt(cx - sx)} J{fmt(cy - sy)}"
                           + (fpart if k == 1 else ""))
                sx, sy = px, py
            stats["arcs"] += 1; stats["segs"] += n
        x, y, z = tx, ty, tz
    return out, stats


def main():
    global G
    src, dst, gpath = sys.argv[1], sys.argv[2], sys.argv[3]
    G = json.load(open(gpath))
    comp = make_comp()
    lines = open(src, "rb").read().decode("ascii").split("\r\n")[:-1]
    out, stats = level(lines, comp)
    i = out.index("G21") + 1
    xs, ys = G["xs"], G["ys"]
    note = [f"(OFFLINE AUTO-LEVELED {G['date']} - run with Auto Leveling OFF and Auto Z Probe OFF)",
            f"(grid {len(xs)}x{len(ys)} X{xs[0]:g}-{xs[-1]:g} Y{ys[0]:g}-{ys[-1]:g}, Z0 = probe at X{xs[0]:g} Y{ys[0]:g},"
            f" linear extrapolation outside the grid)",
            "M370"]
    out[i:i] = note
    tmp = dst + ".tmp"
    open(tmp, "wb").write(("\r\n".join(out) + "\r\n").encode("ascii"))
    import os; os.replace(tmp, dst)
    print(f"wrote {dst}: {len(out)} lines, {stats['lines']} motion lines -> {stats['segs']} moves, {stats['arcs']} arcs split into sub-arcs")


if __name__ == "__main__":
    main()
