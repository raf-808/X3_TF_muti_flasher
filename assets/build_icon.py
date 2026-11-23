#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the TF batch flasher app icon (PNG + multi-resolution Windows ICO).

Build-only dependency: Pillow. The application itself remains stdlib-only.
Run: python assets/build_icon.py
"""
from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFilter


SIZE = 512
SCALE = 3
OUTPUT_DIR = Path(__file__).resolve().parent


def px(n):
    return round(n * SCALE)


def path(points):
    return [(px(x), px(y)) for x, y in points]


def layer():
    return Image.new("RGBA", (px(SIZE), px(SIZE)), (0, 0, 0, 0))


def rounded(draw, xy, fill=None, radius=0, outline=None, width=1):
    draw.rounded_rectangle(
        tuple(map(px, xy)), radius=px(radius),
        fill=fill, outline=outline, width=px(width),
    )


def card(width, height, body, edge, front=False):
    im = Image.new("RGBA", (px(width), px(height)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    shape = path([
        (3, 4), (width - 45, 4), (width - 3, 46),
        (width - 3, height - 3), (3, height - 3),
    ])
    d.polygon(shape, fill=body)
    d.line(shape + [shape[0]], fill=edge, width=px(6), joint="curve")
    d.line(
        path([(17, 21), (width - 51, 21), (width - 21, 54)]),
        fill=(255, 255, 255, 80 if front else 37), width=px(3), joint="curve",
    )

    if not front:
        rounded(d, (30, 100, width - 30, 120), (255, 255, 255, 25), 5)
        rounded(d, (30, 133, width - 54, 145), (255, 255, 255, 18), 4)
        return im

    # Four contacts identify the device as a memory card without tiny labels.
    for x in (29, 65, 101, 137):
        rounded(d, (x, 50, x + 22, 96), "#D49D3E", 6)
        rounded(d, (x + 2, 52, x + 20, 86), "#F7D17A", 4)
    d.line(path([(23, 114), (width - 23, 114)]),
           fill=(73, 130, 147, 51), width=px(4))

    # Large download/write arrow and destination tray remain legible at 16 px.
    ink = "#007E8A"
    d.line(path([(104, 141), (104, 214)]), fill=ink,
           width=px(22), joint="curve")
    d.ellipse((px(93), px(131), px(115), px(153)), fill=ink)
    d.line(path([(69, 188), (104, 224), (139, 188)]),
           fill=ink, width=px(21), joint="curve")
    d.ellipse((px(59), px(179), px(79), px(199)), fill=ink)
    d.ellipse((px(129), px(179), px(149), px(199)), fill=ink)
    tray = "#1BAEAD"
    d.line(path([(57, 251), (57, 263), (152, 263), (152, 251)]),
           fill=tray, width=px(13), joint="curve")
    d.ellipse((px(50), px(244), px(64), px(258)), fill=tray)
    d.ellipse((px(145), px(244), px(159), px(258)), fill=tray)
    return im


def build():
    canvas = layer()
    shadow = layer()
    rounded(ImageDraw.Draw(shadow), (30, 31, 486, 490),
            (1, 8, 19, 200), radius=103)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(px(14))))

    # Transparent rounded corners with an ink-blue gradient inside.
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (px(22), px(18), px(490), px(486)), radius=px(105), fill=255,
    )
    background = layer()
    pixels = background.load()
    side = px(SIZE)
    for y in range(side):
        ty = y / side
        for x in range(side):
            tx = x / side
            bloom = max(0, 1 - math.sqrt(
                ((tx - .24) / .78) ** 2 + ((ty - .02) / .72) ** 2
            ))
            high, low = (18, 42, 64), (11, 28, 49)
            rgb = tuple(min(255, int(
                high[i] * (1 - ty) + low[i] * ty
                + (12 if i == 1 else 19 if i == 2 else 0) * bloom
            )) for i in range(3))
            pixels[x, y] = (*rgb, 255)
    background.putalpha(mask)
    canvas.alpha_composite(background)
    rounded(ImageDraw.Draw(canvas), (23, 19, 489, 485),
            radius=104, outline=(92, 161, 182, 85), width=2)

    halo = layer()
    ImageDraw.Draw(halo).ellipse(
        (px(83), px(87), px(423), px(430)), fill=(32, 210, 191, 54),
    )
    canvas.alpha_composite(halo.filter(ImageFilter.GaussianBlur(px(56))))

    def place(im, x, y, degrees, blur=12, offset=(3, 10)):
        rotated = im.rotate(degrees, resample=Image.Resampling.BICUBIC, expand=True)
        left = px(x) - (rotated.width - im.width) // 2
        top = px(y) - (rotated.height - im.height) // 2
        silhouette = Image.new("RGBA", rotated.size, (4, 10, 21, 0))
        silhouette.putalpha(rotated.getchannel("A").point(
            lambda value: int(value * .6)
        ))
        shade = layer()
        shade.alpha_composite(
            silhouette, (left + px(offset[0]), top + px(offset[1]))
        )
        canvas.alpha_composite(shade.filter(
            ImageFilter.GaussianBlur(px(blur))
        ))
        canvas.alpha_composite(rotated, (left, top))

    place(card(185, 249, "#305C73", "#4B8798"), 90, 153, -13, blur=8)
    place(card(179, 252, "#286A70", "#4EA19A"), 238, 143, 13, blur=8)
    place(card(211, 295, "#E9F7F6", "#B8E4E1", front=True),
          150, 104, 0, blur=13, offset=(5, 10))

    icon = canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    png = OUTPUT_DIR / "tf_flasher.png"
    ico = OUTPUT_DIR / "tf_flasher.ico"
    icon.save(png)
    icon.save(ico, format="ICO", sizes=[
        (16, 16), (24, 24), (32, 32), (48, 48),
        (64, 64), (128, 128), (256, 256),
    ])
    print(f"Created {png} and {ico}")


if __name__ == "__main__":
    build()
