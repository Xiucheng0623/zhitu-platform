#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 Windows 图标 app.ico（石墨底 + 紫罗兰「智」）。

为什么用脚本而不是塞一个 png：图标要包含 16~256 多档尺寸，Windows 在
标题栏、任务栏、资源管理器大图标里分别取不同档位；只给一档会被系统拉伸，
在小尺寸上糊成一团。多分辨率 .ico 必须逐档绘制。

需要 Pillow（本机只在托管解释器里有）：
    C:\\Users\\G\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "src" / "app.ico"

BG = (20, 22, 26)          # #14161A 石墨
ACCENT = (155, 130, 232)   # #9B82E8
SIZES = [256, 128, 64, 48, 32, 24, 16]
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 Bold
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
]


def pick_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw(size: int) -> Image.Image:
    # 4x 超采样再缩，边缘才不毛刺（直接按目标尺寸画会有锯齿）
    k = 4
    s = size * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方块底：半径按比例，16px 档也能看出圆角而不是一个黑点
    r = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG)

    # 细描边：在浅色背景下（资源管理器白底）不至于糊成一团黑
    d.rounded_rectangle([1, 1, s - 2, s - 2], radius=r,
                        outline=(51, 57, 65), width=max(1, s // 64))

    # 中心「智」，用强调色。字号按字形高度取 0.52，留出呼吸
    font = pick_font(int(s * 0.52))
    text = "智"
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((s - tw) / 2 - box[0], (s - th) / 2 - box[1]), text,
           font=font, fill=ACCENT)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    frames = [draw(s) for s in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print("已生成", OUT, "%.1f KB" % (OUT.stat().st_size / 1024))
    print("包含尺寸:", SIZES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
