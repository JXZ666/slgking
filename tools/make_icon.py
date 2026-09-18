"""Draws assets/slgking.ico. Needs Pillow - build time only.

Stacked cards with a star: the tool is a shelf of games, and the star is the
one thing it asks of you. The blue is the same accent the window uses, so the
shortcut and the app look like the same object.

    python tools\\make_icon.py
"""

import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "assets", "slgking.ico")

SIZE = 256
ACCENT = (47, 111, 208, 255)      # #2f6fd0, the window accent
BACK = (255, 255, 255, 70)        # the cards peeking out behind
CARD = (255, 255, 255, 255)
STAR = (255, 199, 64, 255)

SUPERSAMPLE = 4  # draw big, shrink once, and the curves stop looking stepped


def star_points(cx, cy, outer, inner, points=5):
    """Alternating outer/inner vertices, starting at the top."""
    out = []
    for i in range(points * 2):
        radius = outer if i % 2 == 0 else inner
        angle = -math.pi / 2 + i * math.pi / points
        out.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return out


def draw():
    s = SIZE * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=ACCENT)

    # Two cards behind the front one, offset up-left and up-right.
    card_w, card_h = int(s * 0.52), int(s * 0.40)
    left = (s - card_w) // 2
    top = int(s * 0.30)
    for dx, dy in ((-int(s * 0.10), -int(s * 0.06)), (int(s * 0.10), -int(s * 0.06))):
        d.rounded_rectangle(
            [left + dx, top + dy, left + card_w + dx, top + card_h + dy],
            radius=int(s * 0.05), fill=BACK)

    d.rounded_rectangle(
        [left, top, left + card_w, top + card_h],
        radius=int(s * 0.05), fill=CARD)

    d.polygon(star_points(s / 2, top + card_h / 2, s * 0.15, s * 0.062), fill=STAR)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    icon = draw()
    icon.save(OUT, sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                          (32, 32), (16, 16)])
    print("已生成：%s (%d 字节)" % (OUT, os.path.getsize(OUT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
