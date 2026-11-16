#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标 bili.ico（B 站粉的音符）。

用法：
    python gui/make_icon.py
"""
from __future__ import annotations

import struct
from pathlib import Path

OUT = Path(__file__).resolve().parent / "bili.ico"

PINK = (0xFB, 0x72, 0x99, 255)
WHITE = (255, 255, 255, 255)
DARK = (0xE0, 0x5A, 0x80, 255)


def _pixel(x: int, y: int, size: int) -> tuple:
    """画一个圆角方块 + 音符。"""
    r = size * 0.22          # 圆角半径
    # 圆角判定
    cx = min(max(x, r), size - 1 - r)
    cy = min(max(y, r), size - 1 - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return (0, 0, 0, 0)      # 透明

    # 音符：两个圆头 + 竖杆 + 旗帜
    u, v = x / size, y / size
    # 符头（左下圆）
    if (u - 0.34) ** 2 + (v - 0.74) ** 2 < 0.115 ** 2:
        return WHITE
    # 符杆
    if 0.40 <= u <= 0.475 and 0.20 <= v <= 0.74:
        return WHITE
    # 旗帜（右上弯钩）
    if 0.40 <= u <= 0.76 and 0.185 <= v <= 0.275:
        return WHITE
    if 0.66 <= u <= 0.76 and 0.185 <= v <= 0.40:
        return WHITE
    # 右下圆（第二个符头）
    if (u - 0.72) ** 2 + (v - 0.52) ** 2 < 0.10 ** 2:
        return WHITE
    # 底边阴影
    if v > 0.90:
        return DARK
    return PINK


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib_crc(tag + data)))


def zlib_crc(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def _make_png(size: int) -> bytes:
    """生成 PNG 字节流（RGBA）。"""
    import zlib
    raw = bytearray()
    for y in range(size):
        raw.append(0)           # filter type 0
        for x in range(size):
            raw.extend(_pixel(x, y, size))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _png_chunk(b"IEND", b""))


def build_ico(sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    """打包成多尺寸 ICO。"""
    import zlib
    images = []
    for s in sizes:
        raw = bytearray()
        for y in range(s):
            raw.append(0)
            for x in range(s):
                raw.extend(_pixel(x, y, s))
        png = (b"\x89PNG\r\n\x1a\n"
               + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0))
               + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
               + _png_chunk(b"IEND", b""))
        images.append((s, png))

    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII",
                           0 if s >= 256 else s, 0 if s >= 256 else s,
                           0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _s, data in images:
        out += data
    return bytes(out)


if __name__ == "__main__":
    OUT.write_bytes(build_ico())
    print(f"已生成 {OUT}（{OUT.stat().st_size} 字节）")
