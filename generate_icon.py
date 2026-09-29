#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 icon.ico / icon.png（构建期使用，需要 Pillow）。"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow 未安装：pip install pillow")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
SIZES = [16, 24, 32, 48, 64, 128, 256]

BG = (37, 99, 235)       # 蓝
BG2 = (30, 64, 175)      # 深蓝
FG = (255, 255, 255)


def _font(size: int):
    for name in ("arialbd.ttf", "seguisb.ttf", "msyhbd.ttc", "arial.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_icon(px: int) -> Image.Image:
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角背景
    r = max(2, px // 6)
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=r,
                        fill=BG, outline=BG2, width=max(1, px // 24))

    # 两条错位横条，示意"切换"
    bar_h = max(2, px // 8)
    gap = max(2, px // 6)
    y1 = px // 2 - gap
    y2 = px // 2 + gap - bar_h
    x0, x1 = px // 6, px - px // 6
    d.rounded_rectangle([x0, y1, x1 - px // 4, y1 + bar_h],
                        radius=bar_h // 2, fill=FG)
    d.rounded_rectangle([x0 + px // 4, y2, x1, y2 + bar_h],
                        radius=bar_h // 2, fill=(254, 243, 199))

    # 箭头
    ax = x1 - px // 4
    ay = y1 + bar_h // 2
    s = max(2, px // 10)
    d.polygon([(ax - s, ay - s), (ax + s, ay), (ax - s, ay + s)], fill=FG)
    return img


def main() -> int:
    ico = HERE / "icon.ico"
    png = HERE / "icon.png"

    images = [draw_icon(px) for px in SIZES]
    images[-1].save(ico, format="ICO",
                    sizes=[(px, px) for px in SIZES])
    images[-1].save(png, format="PNG")
    print(f"OK  {ico.name}  ({ico.stat().st_size} bytes)")
    print(f"OK  {png.name}  ({png.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
