"""M375.1 reply JSON -> grid.json. Rows print back-to-front as '<dy>| v v v', last line '<dx> <dx> ...'
(offsets relative to the first probe point). usage: parse_grid.py REPLY.json X0 Y0 DATE OUT.json"""
import json, re, sys
d = json.load(open(sys.argv[1])); x0, y0 = float(sys.argv[2]), float(sys.argv[3])
lines = [x for r in d["result"]["replies"] for x in r["reply"]]
rows = {}
for l in lines:
    m = re.match(r"^\s*(-?[\d.]+)\|\s*(.+)$", l)
    if m: rows[float(m.group(1))] = [float(v) for v in m.group(2).split()]
dx = [float(v) for v in next(l for l in reversed(lines) if re.match(r"^\s*-?[\d.]+( +-?[\d.]+)+\s*$", l)).split()]
dys = sorted(rows)
assert all(len(rows[y]) == len(dx) for y in dys), "ragged grid"
g = {"date": sys.argv[4], "origin": [x0, y0], "xs": [round(x0 + v, 4) for v in dx], "ys": [round(y0 + v, 4) for v in dys],
     "z": [rows[y] for y in dys]}
json.dump(g, open(sys.argv[5], "w"), indent=1); print(json.dumps(g))
