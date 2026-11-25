"""Programs: local analysis of a .nc file and the Carvera 1.0.3 upload packet format.

Upload protocol (firmware v1.0.3 Player::upload_command, verified against the source):
  host: "upload /sd/gcodes/<name>\\n"   machine: 'C' every ~100 ms (CRC mode)
  host: block 0 = MD5 hex of the file (32 bytes), then blocks 1.. with the data, each
        STX, n & 0xFF, 0xFF - (n & 0xFF), len_hi, len_lo, 8192 data bytes (padded), crc_hi, crc_lo
        crc = CRC-16/XMODEM (binascii.crc_hqx, init 0) over len_hi..data
  machine: ACK 0x06 per block, NAK 0x15 to resend, CAN is 0x16 on this firmware (not 0x18)
  host: EOT 0x04 -> ACK, then the machine prints "Info: upload success: <path>."
  The machine allows ~1 s between blocks. While uploading it reads raw bytes, so nothing else
  (not even the "?" status poll) may be sent until the final ACK.
"""
from __future__ import annotations

import binascii
import hashlib
import math
import os
import re

STX, EOT, ACK, NAK, CAN = 0x02, 0x04, 0x06, 0x15, 0x16
BLOCK = 8192
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")


def xmodem_block(num: int, payload: bytes) -> bytes:
    n = len(payload)
    if n > BLOCK:
        raise ValueError("payload larger than one block")
    body = bytes([n >> 8, n & 0xFF]) + payload + b"\x1a" * (BLOCK - n)
    crc = binascii.crc_hqx(body, 0)
    k = num & 0xFF
    return bytes([STX, k, 0xFF - k]) + body + bytes([crc >> 8, crc & 0xFF])


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def remote_path(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError("remote name must be 1-60 chars of letters, digits, '.', '_' or '-' (no spaces)")
    return "/sd/gcodes/" + name


def analyze(path: str) -> dict:
    """Extents (arcs sampled, not just end points), Z range, tools, spindle/vacuum use, line count.
    Assumes G17 / G90 / mm, which is what the Carvera Fusion post writes; refuses anything else."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    x = y = z = None
    mode = 0
    xs, ys, cut_z = [], [], []
    zmin = zmax = None
    tools, lines, motion = [], 0, 0
    spindle = set()
    has_m370 = False
    with open(path, "rb") as f:
        text = f.read().decode("ascii", errors="replace")
    for raw in text.splitlines():
        lines += 1
        code = re.sub(r"\(.*?\)|;.*$", "", raw).strip().upper()
        if not code:
            continue
        flat = code.replace(" ", "")
        for bad in ("G91", "G20", "G18", "G19", "G92"):
            if re.search(bad + r"(?!\d)", flat):
                raise ValueError(f"unsupported {bad} in line {lines}: {raw.strip()!r}")
        m = re.search(r"T(-?\d+)M0?6(?!\d)|M0?6T(-?\d+)", flat)
        if m:
            tools.append(int(m.group(1) or m.group(2)))
            x = y = z = None
            continue
        if re.search(r"G28|G53", flat):
            x = y = z = None
            continue
        if re.search(r"M370(?!\d)", flat):
            has_m370 = True
        for mm in re.findall(r"M(\d+)", flat):
            if mm in ("3", "4", "03", "04"):
                s = re.search(r"S(\d+\.?\d*)", flat)
                spindle.add(float(s.group(1)) if s else None)
        w = dict((k, float(v)) for k, v in re.findall(r"([A-Z])(-?\d*\.?\d+)", flat))
        for g in re.findall(r"G(\d+)", flat):
            if int(g) in (0, 1, 2, 3):
                mode = int(g)
        if not ({"X", "Y", "Z"} & w.keys()):
            continue
        motion += 1
        tx, ty, tz = w.get("X", x), w.get("Y", y), w.get("Z", z)
        if mode in (2, 3) and None not in (x, y, tx, ty):
            cx, cy = x + w.get("I", 0.0), y + w.get("J", 0.0)
            r = math.hypot(x - cx, y - cy)
            a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ty - cy, tx - cx)
            sweep = ((a0 - a1) if mode == 2 else (a1 - a0)) % (2 * math.pi) or 2 * math.pi
            sgn = -1 if mode == 2 else 1
            for k in range(1, 33):
                a = a0 + sgn * sweep * k / 32
                xs.append(cx + r * math.cos(a)); ys.append(cy + r * math.sin(a))
        if tx is not None:
            xs.append(tx)
        if ty is not None:
            ys.append(ty)
        if tz is not None:
            zmin = tz if zmin is None else min(zmin, tz)
            zmax = tz if zmax is None else max(zmax, tz)
            if tz < 3.0:
                cut_z.append(tz)
        x, y, z = tx, ty, tz
    if not xs or zmin is None:
        raise ValueError("no motion found")
    return {
        "path": path, "bytes": os.path.getsize(path), "lines": lines, "motion_lines": motion,
        "x": [round(min(xs), 4), round(max(xs), 4)], "y": [round(min(ys), 4), round(max(ys), 4)],
        "z": [round(zmin, 4), round(zmax, 4)], "tools": tools, "spindle_s": sorted(s for s in spindle if s),
        "clears_leveling": has_m370,
    }
