"""A3 drawing of the Carvera spoilboard (vector PDF), built from the hole program so the two always agree.
usage: python3 spoilboard_drawing.py "Spoilboard Holes Carvera.cnc" "Spoilboard drawing.pdf"   (needs matplotlib)
Paper coordinates are mm, origin bottom-left."""
import re, sys
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon, FancyBboxPatch
CNC, OUT = sys.argv[1], sys.argv[2]
W, H, LEN, WID, THK = 420.0, 297.0, 335.0, 225.0, 6.35
DOWELS = {"A": (82.5, 126.0), "B": (171.0, 37.5)}
KEEP_R = 5.0
holes = []
for l in open(CNC):
    m = re.match(r"\(hole \d+: X(-?[\d.]+) Y(-?[\d.]+) D([\d.]+)", l.strip())
    if m: holes.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
assert len(holes) == 48 and sorted(h[2] for h in holes if h[2] < 5) == [4.1, 4.1]
S = 1 / 1.5; OX, OY = 48.0, 112.0                       # plan view scale + paper position of 0,0
P = lambda x, y: (OX + x * S, OY + y * S)
fig = plt.figure(figsize=(W / 25.4, H / 25.4))
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, W); ax.set_ylim(0, H); ax.set_aspect("equal"); ax.axis("off")
K, RED, BLUE = "#111", "#c0392b", "#1f4e9c"
ax.add_patch(Rectangle((8, 8), W - 16, H - 16, fill=False, lw=1.2, color=K))           # border
T = lambda x, y, s, **k: ax.text(x, y, s, fontsize=k.pop("fs", 7), color=k.pop("c", K), family="DejaVu Sans", **k)
def dim(p1, p2, text, off_txt=(0, 0), rot=0, fs=7, c=K):
    ax.annotate("", xy=p2, xytext=p1, arrowprops=dict(arrowstyle="<|-|>", lw=0.6, color=c, shrinkA=0, shrinkB=0, mutation_scale=7))
    mx, my = (p1[0] + p2[0]) / 2 + off_txt[0], (p1[1] + p2[1]) / 2 + off_txt[1]
    T(mx, my, text, fs=fs, c=c, ha="center", va="center", rotation=rot, bbox=dict(fc="white", ec="none", pad=0.6))
line = lambda a, b, **k: ax.plot([a[0], b[0]], [a[1], b[1]], color=k.pop("c", K), lw=k.pop("lw", 0.5), **k)

# ---------------- plan view
x0, y0 = P(0, 0); x1, y1 = P(LEN, WID)
T((x0 + x1) / 2, y1 + 9, "PLAN VIEW  (top, looking down)   SCALE 1:1.5", fs=9, ha="center", weight="bold")
# anchor 1 phantom (machine part, outside the sheet)
anc = [(-15, -15), (85, -15), (85, 0), (0, 0), (0, 85), (-15, 85)]
ax.add_patch(Polygon([P(*p) for p in anc], closed=True, fill=True, fc="#eeeeee", ec="#888", lw=0.6, ls=(0, (4, 2))))
T(*P(18, -10), "ANCHOR 1 (machine)", fs=5.5, c="#666", ha="left", va="center")
ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, lw=1.3, color=K))      # sheet outline
for x, y, d in holes:
    dow = d < 5
    if dow:
        ax.add_patch(Circle(P(x, y), KEEP_R * S, fc="none", ec=RED, hatch="////", lw=0.6))
        ax.add_patch(Circle(P(x, y), d / 2 * S, fc=RED, ec=RED, lw=0.6))
    else:
        ax.add_patch(Circle(P(x, y), d / 2 * S, fc="white", ec=BLUE, lw=0.6))
for name, (x, y) in DOWELS.items():
    cx, cy = P(x, y)
    line((cx - 6, cy), (cx + 6, cy), c=RED, lw=0.4); line((cx, cy - 6), (cx, cy + 6), c=RED, lw=0.4)
    tx, ty = (cx + 17, cy + 4) if name == "A" else (cx + 30, cy + 1.5)
    ax.annotate(f"DOWEL {name}\nØ4.1 THRU (4 mm dowel)", xy=(cx + 2, cy + 1), xytext=(tx, ty), fontsize=6.5, color=RED,
                weight="bold", arrowprops=dict(arrowstyle="-", lw=0.5, color=RED), va="bottom")
# origin
ax.add_patch(Circle((x0, y0), 2.2, fc="white", ec=K, lw=0.8)); ax.add_patch(Polygon([(x0, y0), (x0 + 2.2, y0), (x0, y0 + 2.2)], fc=K))
T(x0 + 3, y0 + 3.5, "0,0  ORIGIN\n(front-left corner, top surface = Z0)", fs=5.5, va="bottom")
ax.annotate("", xy=(x0 + 16, y0 - 1.0), xytext=(x0 + 2.5, y0 - 1.0), arrowprops=dict(arrowstyle="-|>", lw=0.6, color=K, mutation_scale=6))
T(x0 + 17, y0 - 1.0, "+X", fs=5.5, va="center")
# ordinate dims (dowels) from 0,0
yb = y0 - 16
for xv, lab in ((0, "0"), (82.5, "82.5"), (171.0, "171.0"), (LEN, "335.0")):
    px, py = P(xv, 0)
    top = P(xv, [d for d in DOWELS.values() if d[0] == xv][0][1])[1] if xv in (82.5, 171.0) else y0
    line((px, top), (px, yb - 1), lw=0.35, ls=(0, (6, 2)) if xv in (82.5, 171.0) else "-")
    T(px, yb - 2, lab, fs=6.5, ha="center", va="top", rotation=90, c=RED if xv in (82.5, 171.0) else K)
xl = x0 - 16
for yv, lab in ((0, "0"), (37.5, "37.5"), (126.0, "126.0"), (WID, "225.0")):
    px, py = P(0, yv)
    right = P([d for d in DOWELS.values() if d[1] == yv][0][0], 0)[0] if yv in (37.5, 126.0) else x0
    line((right, py), (xl - 1, py), lw=0.35, ls=(0, (6, 2)) if yv in (37.5, 126.0) else "-")
    T(xl - 2, py, lab, fs=6.5, ha="right", va="center", c=RED if yv in (37.5, 126.0) else K)
# overall size
yo = y0 - 33
line((x0, y0 - 1), (x0, yo - 2), lw=0.35); line((x1, y0 - 1), (x1, yo - 2), lw=0.35)
dim((x0, yo), (x1, yo), "335.0  LENGTH (X)", fs=7.5)
xo = x0 - 33
line((x0 - 1, y1), (xo - 2, y1), lw=0.35)
dim((xo, y0), (xo, y1), "225.0  WIDTH (Y)", rot=90, fs=7.5)
T((x0 + x1) / 2 + 60, yo - 8, "FRONT  (operator side)", fs=7, ha="center", style="italic")
T(x1 - 1, y1 - 4, "BACK", fs=6, ha="right", style="italic", c="#555")
# legend
lx, ly = x0 + 2, y1 + 3
ax.add_patch(Circle((lx + 1.5, ly), 1.73, fc="white", ec=BLUE, lw=0.6)); T(lx + 5, ly, "46× Ø5.2 THRU bolt holes (Makera C1 bed pattern, not dimensioned)", fs=5.8, va="center")
ax.add_patch(Circle((lx + 128, ly), 1.33, fc=RED, ec=RED)); ax.add_patch(Circle((lx + 128, ly), 3.0, fc="none", ec=RED, hatch="////", lw=0.5))
T(lx + 133, ly, "dowel + R5 zone: NO TOOL BELOW Z0", fs=5.8, va="center", c=RED, weight="bold")

# ---------------- detail: section through a dowel, 4:1
D = 4.0; dx0, dz0 = 100.0, 50.0                     # paper position of (dowel centre, Z0)
Q = lambda u, z: (dx0 + u * D, dz0 + z * D)
T(dx0, dz0 + 21, "SECTION THROUGH DOWEL B  (X171.0 Y37.5)   SCALE 4:1", fs=8, ha="center", weight="bold")
half = 11.0
ax.add_patch(Polygon([Q(-half, 0), Q(-2.05, 0), Q(-2.05, -THK), Q(-half, -THK)], fc="#dfe6ee", ec=K, lw=0.8, hatch="xx"))
ax.add_patch(Polygon([Q(2.05, 0), Q(half, 0), Q(half, -THK), Q(2.05, -THK)], fc="#dfe6ee", ec=K, lw=0.8, hatch="xx"))
ax.add_patch(Polygon([Q(-half, -THK), Q(half, -THK), Q(half, -THK - 1.6), Q(-half, -THK - 1.6)], fc="#f3efe6", ec="#999", lw=0.5, hatch=".."))
T(*Q(half + 0.6, -THK - 0.8), "original Makera bed", fs=5.5, c="#777", va="center")
T(*Q(half + 0.6, -THK / 2), "new spoilboard\nPVC 1/4\"", fs=5.5, va="center")
ax.add_patch(Rectangle(Q(-KEEP_R, -THK - 1.2), 2 * KEEP_R * D, (THK + 1.2) * D, fc=RED, alpha=0.13, ec=RED, lw=0.8, ls=(0, (3, 2))))
T(*Q(0, -THK / 2 - 0.2), "NO TOOL\nBELOW Z0", fs=6.5, c=RED, ha="center", va="center", weight="bold")
line(Q(-half - 2, 0), Q(half + 2, 0), c=BLUE, lw=0.8, ls=(0, (8, 3)))
T(*Q(-half - 2.4, 0), "Z0 = spoilboard top", fs=6, c=BLUE, ha="right", va="center")
line(Q(0, 2.5), Q(0, -THK - 2.2), lw=0.4, ls=(0, (10, 2, 2, 2)))
dim(Q(-2.05, 1.4), Q(2.05, 1.4), "Ø4.1 THRU", fs=6, off_txt=(0, 3.2))
dim(Q(-KEEP_R, 3.1), Q(KEEP_R, 3.1), "R5.0 no-go zone below Z0", fs=6, c=RED, off_txt=(0, 3.0))
dim(Q(-half - 1.2, 0), Q(-half - 1.2, -THK), "6.35", rot=90, fs=6.5)
dim(Q(half + 6.5, 0), Q(half + 6.5, -7.1), "7.1 cut depth", rot=90, fs=6)
line(Q(2.05, -7.1), Q(half + 7.5, -7.1), lw=0.35)

# ---------------- right column: notes, dowel table, rule, title block
rx = 292.0
T(rx, 276, "NOTES", fs=9, weight="bold")
notes = [
    "1. Origin 0,0 = front-left corner of the sheet; the top surface is Z0.",
    "   The sheet butts into the anchor 1 corner, so 0,0 = Carvera G54 X0 Y0",
    "   (reference machine: X-359.503 Y-235.143). +X right, +Y away from you.",
    "2. Reference machine: top surface Z0 at machine Z-103.12 (probe as the",
    "   length reference, probed at X177.5 Y82.5). Re-probe on your machine.",
    "3. 46x Ø5.2 THRU bolt holes on the Makera C1 bed pattern. Positions in",
    "   the hole table of spoilboard/README.md (not dimensioned here).",
    "4. 2x Ø4.1 THRU dowel holes for 4 mm dowels, located from the origin.",
    "5. All holes cut 7.1 deep from the top: 0.75 into the original bed.",
    "6. The bed holes in line with the anchor legs (X-4.5 / Y-4.5) are off",
    "   this sheet. Nothing may travel over anchor 1 at any height.",
    "7. CNC program: Spoilboard Holes Carvera.cnc (T3 1/8\" flat, helical).",
]
for i, n in enumerate(notes):
    T(rx, 268 - i * 5.0, n, fs=5.9)
ty = 196
T(rx, ty, "DOWEL LOCATIONS (from 0,0, mm)", fs=8, weight="bold")
cols = [rx, rx + 24, rx + 50, rx + 76]
for j, h in enumerate(("DOWEL", "X", "Y", "Ø")):
    T(cols[j], ty - 7, h, fs=7, weight="bold")
for i, (nm, (x, y)) in enumerate(DOWELS.items()):
    for j, v in enumerate((nm, f"{x:.1f}", f"{y:.1f}", "4.1 THRU")):
        T(cols[j], ty - 14 - i * 6, v, fs=7, c=RED)
line((rx - 1, ty - 9.5), (rx + 104, ty - 9.5), lw=0.4)
by = 112
ax.add_patch(FancyBboxPatch((rx - 2, by), 112, 52, boxstyle="square,pad=0", fc="#fdecea", ec=RED, lw=1.6))
T(rx + 54, by + 45, "⚠  DOWEL RULE", fs=11, c=RED, ha="center", weight="bold")
rule = ["THE TOOL HEAD MUST NEVER TRAVEL",
        "BELOW Z0 (THE SPOILBOARD TOP)",
        "AT THE DOWEL LOCATIONS.",
        "",
        "Dowel A X82.5 Y126.0   Dowel B X171.0 Y37.5",
        "No-go below Z0: R5.0 around each dowel centre,",
        "measured to the tool's edge. Every program, probe,",
        "grid and jog. Only exception: the spoilboard program."]
for i, r in enumerate(rule):
    T(rx + 54, by + 37 - i * 4.6, r, fs=7.6 if i < 3 else 6.2, c=RED, ha="center", weight="bold" if i < 3 else None)
# title block
tb = [(rx - 2, 14, 112, 48)]
ax.add_patch(Rectangle((rx - 2, 14), 112, 48, fill=False, lw=1.0, color=K))
for yy in (50, 42, 34, 26):
    line((rx - 2, yy), (rx + 110, yy), lw=0.5)
line((rx + 54, 14), (rx + 54, 42), lw=0.5)
T(rx + 54, 55, "SPOILBOARD - MAKERA CARVERA C1", fs=9.5, ha="center", weight="bold")
T(rx + 54, 45.5, "335 x 225 x 6.35 mm   (1/4\" PVC sheet)", fs=7.5, ha="center")
cells = [("MATERIAL", "PVC 1/4\" (6.35)"), ("UNITS", "mm"), ("SCALE", "1:1.5 (A3), detail 4:1"), ("DATE", "2026-11-24"),
         ("HOLES", "46x Ø5.2 + 2x Ø4.1"), ("REV", "A")]
for i, (k, v) in enumerate(cells):
    cx = rx if i % 2 == 0 else rx + 56
    top = (42, 34, 26)[i // 2]
    T(cx + 1, top - 2.6, k, fs=4.8, c="#555"); T(cx + 1, top - 6.6, v, fs=6.5)
T(W - 12, 10.5, "Spoilboard drawing.pdf  |  carvera-ai / machines / makera-carvera-c1 / spoilboard", fs=5, ha="right", c="#666")
fig.savefig(OUT, metadata={"Title": "Spoilboard - Makera Carvera C1", "Author": "Carvera AI", "Subject": "Spoilboard drawing",
                           "CreationDate": None})
if "--preview" in sys.argv:
    fig.savefig(OUT.replace(".pdf", "_preview.png"), dpi=110)
print("ok", OUT)
