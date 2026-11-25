#!/usr/bin/env python3
"""Spoilboard hole program for the Carvera C1: bores every hole of Makera's stock MDF bed through a new
sheet laid on top, so the clamp/screw holes line up with the old bed underneath.

Hole positions: MakeraInc CarveraProfiles `Machine_Design_Files/CarveraC1_BedSimplified.dxf` (inches,
59 x D5.5). That drawing has the anchor at its +X/+Y corner, so it is rotated 180 deg into machine
orientation; the origin is the anchor-1 inner corner (the stock corner), +X right, +Y away from the
operator. The 5 holes under the anchor's arms (anchor screws in the aluminium base) are left out: 54 holes.
USER 2026-11-24 spec: the 6 holes in line with the anchor legs (row Y-4.5, column X-4.5) are skipped,
(82.5,126) and (171,37.5) are dowel holes for 4 mm dowels, cut at 4.1 mm (4.0 was very tight), every other
hole is 5.2 mm, all 7.1 mm deep: 48 holes.
DOWEL RULE: the tool must NEVER travel below Z0 (the spoilboard top) within R5 of a dowel centre.

Work setup the program assumes:
  G54 X/Y = the anchor 1 inner corner          Z0 = top of the new sheet
  (Your machine's anchor 1 position: send "config-get sd coordinate.anchor1_x" and "..._y" to the Carvera.
  On the reference machine it is X-359.503 Y-235.143; it is calibrated per machine.)
Cut: T3 (1/8" flat) helical bore, climb (G3), from Z+0.5 down to -depth, a clean-up circle at the bottom,
then out through the centre. Vacuum M801 on for the job.

usage: spoilboard_holes.py OUT.cnc [--depth 7.1] [--dia 5.2] [--dowel-dia 4.1] [--extra X,Y,D ...]
                           [--comp C | --comp D:C ...] [--with-leg-holes]
                           [--tool-dia 3.175] [--pitch 0.5] [--feed 600] [--edge-feed 1550]
                           [--safe-z 5] [--only X,Y ...] [--no-bed] [--vacuum 80]
--comp D:C adds C mm to every hole of nominal diameter D (e.g. --comp 4.1:0.02 if they measure 0.02 small).
No move (cutting or rapid, at any height) passes over anchor 1; the tour starts and ends at the
back right, where the tool change and G28 happen.
"""
import argparse
import math

HOLES = [
    (110, -4.5), (200, -4.5), (245, -4.5), (65, 15), (110, 15), (155, 15),
    (200, 15), (245, 15), (290, 15), (327, 15), (25, 37.5), (105, 37.5),
    (160, 37.5), (171, 37.5), (308.5, 37.5), (120, 42), (25, 60), (65, 60),
    (110, 60), (155, 60), (200, 60), (245, 60), (290, 60), (327, 60),
    (87, 75), (-4.5, 105), (25, 105), (65, 105), (110, 105), (155, 105),
    (200, 105), (245, 105), (290, 105), (327, 105), (82.5, 115), (82.5, 126),
    (-4.5, 150), (25, 150), (65, 150), (110, 150), (155, 150), (200, 150),
    (245, 150), (290, 150), (327, 150), (65, 185), (-4.5, 195), (25, 195),
    (110, 195), (155, 195), (200, 195), (245, 195), (290, 195), (327, 195),
]
DOWELS = [(82.5, 126), (171, 37.5)]                     # dowel holes for 4 mm dowels (reference spoilboard)
ANCHOR_LEG_LINES = lambda x, y: x < 0 or y < 0          # row Y-4.5 / column X-4.5, in line with the legs
ANCHOR1_MACHINE = (-359.503, -235.143)                  # reference machine only; per-machine calibration
# Anchor 1 footprint (Makera C1 model, work coords): two 15 mm arms, 100 mm long, outside the X0/Y0 lines.
# Nothing may travel over it at any height (USER 2026-11-24), so it is a keep-out box pair, grown by the
# tool radius plus KEEP_MARGIN.
ANCHOR1_ARMS = [(-15.0, -15.0, 85.0, 0.0), (-15.0, -15.0, 0.0, 85.0)]     # xmin, ymin, xmax, ymax
KEEP_MARGIN = 5.0
CLEARANCE_XY = (284.503, 232.143)    # G28 clearance spot (machine X-75 Y-3) in work coords


def keepout(tool_r):
    g = tool_r + KEEP_MARGIN
    return [(a - g, b - g, c + g, d + g) for a, b, c, d in ANCHOR1_ARMS]


def seg_hits_box(p, q, box):
    """Liang-Barsky: does segment p-q touch the closed box?"""
    (x0, y0), (x1, y1) = p, q
    xmin, ymin, xmax, ymax = box
    t0, t1 = 0.0, 1.0
    for d, lo, hi, s in ((x1 - x0, xmin, xmax, x0), (y1 - y0, ymin, ymax, y0)):
        if abs(d) < 1e-12:
            if s < lo or s > hi:
                return False
            continue
        ta, tb = (lo - s) / d, (hi - s) / d
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return False
    return True


def clear(p, q, boxes):
    return not any(seg_hits_box(p, q, b) for b in boxes)


def route(p, q, boxes):
    """Waypoints (excluding p, including q) for a rapid from p to q that stays out of the boxes."""
    if clear(p, q, boxes):
        return [q]
    xs = sorted({b[0] - 1 for b in boxes} | {b[2] + 1 for b in boxes})
    ys = sorted({b[1] - 1 for b in boxes} | {b[3] + 1 for b in boxes})
    cands = [(x, y) for x in xs for y in ys
             if not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)]
    best = None
    for w in cands:
        if clear(p, w, boxes) and clear(w, q, boxes):
            d = math.dist(p, w) + math.dist(w, q)
            if best is None or d < best[0]:
                best = (d, [w, q])
    for w1 in cands:
        for w2 in cands:
            if clear(p, w1, boxes) and clear(w1, w2, boxes) and clear(w2, q, boxes):
                d = math.dist(p, w1) + math.dist(w1, w2) + math.dist(w2, q)
                if best is None or d < best[0]:
                    best = (d, [w1, w2, q])
    if best is None:
        raise SystemExit(f"no anchor-free route from {p} to {q}")
    return best[1]


def order(holes):
    """Nearest-neighbour tour from the hole nearest the clearance spot (back right), so the moves in
    from the tool change and out to G28 never cross the front-left anchor."""
    left = list(holes)
    cur = min(left, key=lambda p: math.dist(p, CLEARANCE_XY))
    out = [cur]
    left.remove(cur)
    while left:
        cur = min(left, key=lambda p: math.dist(p, cur))
        out.append(cur)
        left.remove(cur)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--depth", type=float, default=7.1)
    ap.add_argument("--dia", type=float, default=5.2, help="diameter of the bed (screw/clamp) holes")
    ap.add_argument("--dowel-dia", type=float, default=4.1, help="diameter of the dowel holes (4 mm dowels; 4.0 was very tight)")
    ap.add_argument("--with-leg-holes", action="store_true",
                    help="also cut the 6 holes in line with the anchor legs (skipped by default)")
    ap.add_argument("--edge-feed", type=float, default=1550.0,
                    help="feed at the hole wall; small holes slow down to keep it (mm/min)")
    ap.add_argument("--extra", action="append", default=[], metavar="X,Y,D",
                    help="extra hole (e.g. a dowel hole) at work X,Y with diameter D; repeatable")
    ap.add_argument("--comp", action="append", default=[],
                    help="diameter correction from a measured hole (wanted - measured): C for all, "
                         "or D:C for holes of nominal diameter D; repeatable")
    ap.add_argument("--tool-dia", type=float, default=3.175)
    ap.add_argument("--pitch", type=float, default=0.5, help="mm down per helix turn")
    ap.add_argument("--feed", type=float, default=600.0)
    ap.add_argument("--plunge", type=float, default=300.0)
    ap.add_argument("--safe-z", type=float, default=5.0)
    ap.add_argument("--rpm", type=int, default=20000)
    ap.add_argument("--vacuum", type=int, default=80)
    ap.add_argument("--only", action="append", default=[], metavar="X,Y",
                    help="cut only this hole; repeatable")
    ap.add_argument("--no-bed", action="store_true", help="leave out the 54 bed holes (extras only)")
    a = ap.parse_args()

    holes = [] if a.no_bed else [(x, y, a.dowel_dia if (x, y) in DOWELS else a.dia) for x, y in HOLES
                                 if a.with_leg_holes or not ANCHOR_LEG_LINES(x, y)]
    comp_all, comp_by = 0.0, {}
    for c in a.comp:
        if ":" in c:
            d, v = c.split(":"); comp_by[round(float(d), 3)] = float(v)
        else:
            comp_all = float(c)
    cut_d = lambda d: d + comp_by.get(round(d, 3), comp_all)
    for e in a.extra:
        x, y, d = (float(v) for v in e.split(","))
        holes.append((x, y, d))
    if a.only:
        want = [tuple(float(v) for v in o.split(",")) for o in a.only]
        holes = [h for h in holes if any(math.dist(h[:2], w) < 0.01 for w in want)]
        if len(holes) != len(want):
            raise SystemExit(f"--only {a.only}: not all of these are holes in the program")
    tool_r = a.tool_dia / 2.0
    boxes = keepout(tool_r)
    for x, y, d in holes:
        r = cut_d(d) / 2.0
        if any(b[0] - r + tool_r <= x <= b[2] + r - tool_r and b[1] - r + tool_r <= y <= b[3] + r - tool_r
               for b in boxes):
            raise SystemExit(f"hole X{x} Y{y} D{d} is too close to the anchor")
        if cut_d(d) - a.tool_dia < 0.4:
            raise SystemExit(f"hole D{d} is too small to bore with a {a.tool_dia} mm tool")
    tour_xy = order([h[:2] for h in holes])
    dia_of = {h[:2]: h[2] for h in holes}
    top = 0.5
    turns = max(1, math.ceil((top + a.depth) / a.pitch))
    dz = (top + a.depth) / turns

    f = lambda v: f"{v:.4f}".rstrip("0").rstrip(".")
    sizes = sorted({dia_of[p] for p in tour_xy})
    count = {d: sum(1 for p in tour_xy if dia_of[p] == d) for d in sizes}
    L = [f"(Spoilboard holes - Carvera C1 stock bed pattern: {len(tour_xy)} holes: "
         + ", ".join(f"{count[d]} x D{f(d)} cut at D{f(cut_d(d))}" for d in sizes) + f", depth {f(a.depth)})",
         "(Source: MakeraInc CarveraProfiles CarveraC1_BedSimplified.dxf + C1 machine model wasteboard; both agree)",
         f"(XY origin G54 = anchor 1 inner corner - reference machine X{ANCHOR1_MACHINE[0]} Y{ANCHOR1_MACHINE[1]})",
         "(Z origin = top of the new sheet)",
         f"(T3 1/8in flat end mill D={f(a.tool_dia)}: helical bore, climb, {f(dz)} mm/turn, wall feed"
         f" {f(a.edge_feed)}, centre feed max {f(a.feed)}, spindle {a.rpm})",
         f"(No move passes over anchor 1: keep-out = its arms + {f(tool_r + KEEP_MARGIN)} mm. Safe Z {f(a.safe_z)}.)",
         "(DOWEL RULE: no tool below Z0 within R5 of the dowels X82.5 Y126 and X171 Y37.5 -)",
         "(- this program is the one exception, because it cuts those dowel holes.)",
         "G90 G94", "G17", "G21",
         "T3M6", "M400"]
    if a.vacuum:
        L.append(f"M801 S{a.vacuum}")
    # lift to machine clearance before any XY travel, then go down only over the first hole: the start XY
    # is wherever the spindle was (e.g. the G28 clearance spot, behind the bed) and T3M6 does nothing when
    # T3 is already loaded, so Z may be anywhere
    L += [f"S{a.rpm} M3", "G4 S10", "G54", "G53 G0 Z-3."]
    prev = None
    detours = 0
    for n, (cx, cy) in enumerate(tour_xy, 1):
        dcut = cut_d(dia_of[(cx, cy)])
        rc = (dcut - a.tool_dia) / 2.0
        feed = min(a.feed, a.edge_feed * (dcut - a.tool_dia) / dcut)
        sx = cx + rc
        L.append(f"(hole {n}: X{f(cx)} Y{f(cy)} D{f(dia_of[(cx, cy)])}"
                 f"{' dowel' if (cx, cy) in DOWELS else ''}, cut D{f(dcut)} F{feed:.0f})")
        if prev is not None:
            path = route(prev, (sx, cy), boxes)
            for wx, wy in path[:-1]:
                L.append(f"G0 X{f(wx)} Y{f(wy)}   (around anchor 1)")
                detours += 1
        L.append(f"G0 X{f(sx)} Y{f(cy)}")
        if prev is None:
            L.append(f"G0 Z{f(a.safe_z)}")
        L += ["G0 Z2.", f"G1 Z{f(top)} F{f(a.plunge)}"]
        z = top
        for _ in range(turns):
            z -= dz
            L.append(f"G3 X{f(sx)} Y{f(cy)} Z{f(z)} I{f(-rc)} J0 F{feed:.0f}")
        L.append(f"G3 X{f(sx)} Y{f(cy)} I{f(-rc)} J0")          # clean-up turn at full depth
        L.append(f"G1 X{f(cx)} Y{f(cy)}")                         # off the wall before lifting
        L.append(f"G0 Z{f(a.safe_z)}")
        prev = (cx, cy)
    L += ["M5"]
    if a.vacuum:
        L.append("M802")
    L += ["G28", "M30"]
    # every comment must be one flat (...) group: the firmware ends a comment at the first ')', so a nested
    # '(' would leave the rest of the line to be parsed as G-code (caught 2026-11-24 before any run)
    for line in L:
        rest = line
        code = ""
        while "(" in rest:
            i = rest.index("(")
            j = rest.find(")", i)
            if j < 0 or "(" in rest[i + 1:j]:
                raise SystemExit(f"refusing to write: unbalanced or nested comment in {line!r}")
            code += rest[:i]
            rest = rest[j + 1:]
        code += rest
        if ")" in code:
            raise SystemExit(f"refusing to write: stray ')' in {line!r}")
    with open(a.out, "wb") as fh:
        fh.write(("\r\n".join(L) + "\r\n").encode("ascii"))
    path = sum(math.dist(p, q) for p, q in zip(tour_xy, tour_xy[1:]))
    mins = path / 3000 + 10 / 60 + len(tour_xy) * 1.5 / a.plunge
    for p in tour_xy:
        dcut = cut_d(dia_of[p]); rc = (dcut - a.tool_dia) / 2.0
        feed = min(a.feed, a.edge_feed * (dcut - a.tool_dia) / dcut)
        mins += (turns * math.hypot(2 * math.pi * rc, dz) + 2 * math.pi * rc) / feed
    print(f"wrote {a.out}: {len(tour_xy)} holes, {turns} turns/hole, {detours} anchor detour points, "
          f"~{mins:.1f} min cutting+travel")


if __name__ == "__main__":
    main()
