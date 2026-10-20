# -*- coding: utf-8 -*-
"""生成应用图标 build/app.ico（纯 Python，不依赖 Pillow）

设计：蓝色渐变圆角方块 + 白色开口环（C 字标）
    - 造型极简，16px 任务栏/文件列表下仍然清晰，不糊成一团
    - 16/32/48/64/128 用 BMP(DIB) 条目，兼容性最好
    - 256 用 PNG 压缩条目（Vista+ 支持），控制总体积

用法：
    python tools/make_icon.py            # 生成 build/app.ico
    python tools/make_icon.py --preview  # additionally 输出 256px PNG 预览
"""
import math
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "build" / "app.ico"
PREVIEW = Path(__file__).resolve().parent.parent / "build" / "app-preview.png"
SIZES = [16, 32, 48, 64, 128, 256]

# 配色：左上偏亮的天蓝 → 右下主蓝，保持与站点主色 #006EFF 同族
C1 = (86, 176, 255)       # #56B0FF
C2 = (0, 82, 196)         # #0052C4
WHITE = (255, 255, 255)
SHADE = (0, 62, 158)      # 环内的极淡投影，仅用于增强层次


def rounded_rect(x, y, left, top, right, bottom, radius):
    """点是否落在圆角矩形内"""
    if x < left or x > right or y < top or y > bottom:
        return False
    r = min(radius, (right - left) / 2, (bottom - top) / 2)
    if r <= 0:
        return True
    cx = left + r if x < left + r else (right - r if x > right - r else x)
    cy = top + r if y < top + r else (bottom - r if y > bottom - r else y)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def sample_color(x, y, size):
    """返回 (r,g,b,a)；坐标为像素内的浮点位置"""
    pad = size * 0.035
    if not rounded_rect(x, y, pad, pad, size - pad, size - pad, size * 0.235):
        return (0, 0, 0, 0)

    # 对角渐变：左上亮、右下深，比纯纵向更有体积感
    t = ((x - pad) + (y - pad)) / max(1e-6, 2 * (size - 2 * pad))
    t = max(0.0, min(1.0, t))
    bg = tuple(int(C1[i] + (C2[i] - C1[i]) * t) for i in range(3))

    # 中心白色开口环（C 字标）
    cx = cy = size / 2.0
    dx, dy = x - cx, y - cy
    dist = math.hypot(dx, dy)
    r_out, r_in = size * 0.300, size * 0.196
    if r_in <= dist <= r_out:
        # 右侧开口：与水平向右方向的夹角在 ±33° 内则镂空
        ang = abs(math.degrees(math.atan2(dy, dx)))
        if ang > 33:
            return WHITE + (255,)
        return bg + (255,)
    # 环内侧的极淡压暗，避免大面积纯色显得平
    if dist < r_in and dist > r_in - size * 0.02:
        mix = 0.82
        return tuple(int(bg[i] * (1 - mix) + SHADE[i] * mix) for i in range(3)) + (255,)
    return bg + (255,)


def render(size: int, ss: int = 4):
    """超采样渲染，返回 size*size 的 RGBA 像素列表"""
    rows = []
    inv = 1.0 / (ss * ss)
    for py in range(size):
        row = []
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                fy = py + (sy + 0.5) / ss
                for sx in range(ss):
                    fx = px + (sx + 0.5) / ss
                    r, g, b, a = sample_color(fx, fy, size)
                    if a:
                        acc[0] += r
                        acc[1] += g
                        acc[2] += b
                        acc[3] += 255
            if acc[3] == 0:
                row.append((0, 0, 0, 0))
            else:
                n = acc[3] / 255.0
                row.append((int(acc[0] / n), int(acc[1] / n), int(acc[2] / n), int(acc[3] * inv)))
        rows.append(row)
    return rows


# ---------------- 编码 ----------------
def to_bmp(rows):
    """32bpp BMP(DIB)：高度写两倍（含 AND 掩码），BGRA，自下而上"""
    h = len(rows)
    header = struct.pack("<IiiHHIIiiII", 40, h, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = bytearray()
    for y in range(h - 1, -1, -1):
        for (r, g, b, a) in rows[y]:
            pixels += bytes((b, g, r, a))
    mask_row_bytes = ((h + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(mask_row_bytes)
        for x in range(h):
            if rows[y][x][3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    return bytes(header) + bytes(pixels) + bytes(mask)


def to_png(rows):
    h = len(rows)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for (r, g, b, a) in rows[y]:
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", h, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def main():
    entries = []
    for size in SIZES:
        rows = render(size, ss=6 if size <= 48 else 4)
        data = to_png(rows) if size >= 256 else to_bmp(rows)
        entry = struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                            len(data), 0)
        entries.append((entry, data))
        if size == 256:
            PREVIEW.parent.mkdir(parents=True, exist_ok=True)
            PREVIEW.write_bytes(data)
        print(f"  渲染 {size}x{size}  ({len(data)} 字节, {'PNG' if size >= 256 else 'BMP'})")

    offset = 6 + 16 * len(entries)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    blob = bytearray()
    for entry, data in entries:
        entry = entry[:12] + struct.pack("<I", offset)
        directory += entry
        blob += data
        offset += len(data)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(bytes(directory) + bytes(blob))
    print(f"图标已生成 -> {OUT}  ({OUT.stat().st_size} 字节)")
    print(f"预览图     -> {PREVIEW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
