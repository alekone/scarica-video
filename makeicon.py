#!/usr/bin/env python3
"""Genera icon_1024.png (rounded-rect con gradiente + freccia di download). Solo stdlib."""
import os
import struct
import zlib

S = 1024
R = 200
top = (255, 99, 99)
bot = (219, 30, 74)
cx = S / 2


def lerp(a, b, t):
    return a + (b - a) * t


def in_rounded(x, y):
    nx = min(max(x, R), S - R)
    ny = min(max(y, R), S - R)
    dx, dy = x - nx, y - ny
    return dx * dx + dy * dy <= R * R


stem_hw, stem_top, stem_bot = 78, 250, 560
tri_top, tri_tip_y, tri_hw = 500, 770, 210
tray_y0, tray_y1, tray_hw = 812, 862, 210


def in_arrow(x, y):
    dx = x - cx
    if -stem_hw <= dx <= stem_hw and stem_top <= y <= stem_bot:
        return True
    if tri_top <= y <= tri_tip_y:
        hw = tri_hw * (tri_tip_y - y) / (tri_tip_y - tri_top)
        if -hw <= dx <= hw:
            return True
    return -tray_hw <= dx <= tray_hw and tray_y0 <= y <= tray_y1


def build():
    SS = 2
    raw = bytearray()
    for y in range(S):
        raw.append(0)
        for x in range(S):
            bgn = fgn = 0
            for oy in range(SS):
                for ox in range(SS):
                    sx, sy = x + (ox + .5) / SS, y + (oy + .5) / SS
                    if in_rounded(sx, sy):
                        bgn += 1
                        if in_arrow(sx, sy):
                            fgn += 1
            n = SS * SS
            if bgn == 0:
                raw += bytes((0, 0, 0, 0))
                continue
            t = y / S
            bg = (lerp(top[0], bot[0], t), lerp(top[1], bot[1], t), lerp(top[2], bot[2], t))
            fg = fgn / n
            raw += bytes((int(lerp(bg[0], 255, fg)), int(lerp(bg[1], 255, fg)),
                          int(lerp(bg[2], 255, fg)), int(255 * bgn / n)))
    return raw


def chunk(typ, data):
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff)


if __name__ == "__main__":
    raw = build()
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon_1024.png")
    with open(out, "wb") as f:
        f.write(png)
    print("wrote", out)
