"""Generate the slot-machine symbol images for the lottery reel.

Draws the classic slot symbols (7 / BAR / bell / cherry / diamond / star) plus
the crown, as transparent-background PNGs in the app's neon palette, with a soft
glow so they read as lit machine faces instead of the old monochrome text
glyphs. The GUI loads them once and scrolls them on the reel canvases.

The crown belongs to 幸运之王 alone (0.01%), and it has to be impossible to
mistake for the gold "7" one tier down - that mistake is exactly the fake
near-miss the reel used to pull. The silhouette does most of the work; the
magenta jewels keep the two golds from reading as the same prize.

Run once from the repo root:

    python tools/make_slot_symbols.py

Output: assets/slot/<name>.png (repo path, relative to this file's parent).
"""

import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 96
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "assets", "slot")

# name -> (fill colour, optional accent colour). Colours echo the title rarity
# palette so a win colour is already meaningful to a player who has seen the
# badge colours. The accent is only ever used for detail that sits inside the
# silhouette, and it is what separates the crown's gold from the "7"'s.
SYMBOLS = [
    ("seven", "#e0a800", None),
    ("bar", "#e84393", None),
    ("bell", "#2f9bd0", None),
    ("cherry", "#e06a3f", None),
    ("diamond", "#8b5cf6", None),
    ("star", "#ffd76a", None),
    ("crown", "#ffcf40", "#e84393"),
]


def _bold_font(size):
    for path in (r"C:\Windows\Fonts\arialbd.ttf",
                 r"C:\Windows\Fonts\seguisb.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _hex_rgb(colour):
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def _glow(shape_img, colour, blur=8):
    """A soft halo behind the symbol: same shape, blurred, low alpha."""
    rgb = _hex_rgb(colour)
    halo = Image.new("RGBA", shape_img.size, (0, 0, 0, 0))
    halo.paste(rgb + (200,), (0, 0), shape_img)
    return halo.filter(ImageFilter.GaussianBlur(blur))


def _star_pts(cx, cy, r_out, r_in, n=5, start=-90.0):
    import math
    pts = []
    for i in range(n * 2):
        r = r_out if i % 2 == 0 else r_in
        ang = math.radians(start + i * 180.0 / n)
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def draw_symbol(name, colour, accent=None):
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shape = Image.new("L", (SIZE, SIZE), 0)
    detail = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(shape)
    dd = ImageDraw.Draw(detail)
    c = SIZE / 2.0
    m = SIZE * 0.12  # margin so the glow never clips

    if name == "crown":
        # A three-peak diadem over a band: the band is the bottom third, the
        # peaks carry the silhouette. Peaks are deliberately tall and thin so
        # the shape still reads once it is 52px on a spinning reel.
        band_top = SIZE * 0.62
        band_bot = SIZE * 0.82
        left, right = m, SIZE - m
        span = right - left
        d.rounded_rectangle([left, band_top, right, band_bot],
                            radius=SIZE * 0.05, fill=255)
        d.polygon([(left, band_top),
                   (left + span * 0.10, SIZE * 0.26),
                   (left + span * 0.28, band_top - SIZE * 0.04),
                   (c, SIZE * 0.16),
                   (right - span * 0.28, band_top - SIZE * 0.04),
                   (right - span * 0.10, SIZE * 0.26),
                   (right, band_top)], fill=255)
        # Jewels: three on the band, one crowning the middle peak. Drawn as
        # holes in the gold so the accent underneath reads as a set stone.
        jewel_y = (band_top + band_bot) / 2.0
        for fx in (0.24, 0.5, 0.76):
            jx = left + span * fx
            d.ellipse([jx - 5, jewel_y - 5, jx + 5, jewel_y + 5], fill=0)
            dd.ellipse([jx - 5, jewel_y - 5, jx + 5, jewel_y + 5], fill=255)
        d.ellipse([c - 6, SIZE * 0.14, c + 6, SIZE * 0.26], fill=0)
        dd.ellipse([c - 6, SIZE * 0.14, c + 6, SIZE * 0.26], fill=255)
    elif name == "seven":
        font = _bold_font(70)
        bbox = d.textbbox((0, 0), "7", font=font)
        x = (SIZE - (bbox[2] - bbox[0])) / 2 - bbox[0]
        y = (SIZE - (bbox[3] - bbox[1])) / 2 - bbox[1]
        d.text((x, y), "7", font=font, fill=255)
    elif name == "bar":
        font = _bold_font(40)
        bbox = d.textbbox((0, 0), "BAR", font=font)
        x = (SIZE - (bbox[2] - bbox[0])) / 2 - bbox[0]
        y = (SIZE - (bbox[3] - bbox[1])) / 2 - bbox[1]
        d.text((x, y), "BAR", font=font, fill=255)
    elif name == "bell":
        d.arc([m, m * 1.8, SIZE - m, SIZE * 0.95], 180, 360, fill=255, width=8)
        d.polygon([(m, SIZE * 0.55), (SIZE - m, SIZE * 0.55),
                   (SIZE * 0.72, SIZE * 0.78), (SIZE * 0.28, SIZE * 0.78)],
                  fill=255)
        d.ellipse([c - 6, SIZE * 0.78, c + 6, SIZE * 0.90], fill=255)
    elif name == "cherry":
        d.ellipse([m * 1.2, SIZE * 0.30, SIZE * 0.52, SIZE * 0.68], fill=255)
        d.ellipse([SIZE * 0.48, SIZE * 0.22, SIZE - m * 1.2, SIZE * 0.60], fill=255)
        d.line([SIZE * 0.34, SIZE * 0.32, c, SIZE * 0.12], fill=255, width=7)
        d.line([SIZE * 0.66, SIZE * 0.24, c, SIZE * 0.12], fill=255, width=7)
        d.ellipse([c, SIZE * 0.06, SIZE * 0.72, SIZE * 0.20], fill=255)
    elif name == "diamond":
        d.polygon([(c, m * 0.8), (SIZE - m, SIZE * 0.5), (c, SIZE - m * 0.8),
                   (m, SIZE * 0.5)], fill=255)
        d.polygon([(c, m * 0.8), (SIZE - m, SIZE * 0.5), (c, SIZE * 0.5)],
                  fill=120)
    elif name == "star":
        d.polygon(_star_pts(c, c, SIZE * 0.42, SIZE * 0.17), fill=255)

    glow = _glow(shape, colour)
    img.paste(glow, (0, 0), glow)
    img.paste(_hex_rgb(colour) + (255,), (0, 0), shape)
    if accent:
        img.paste(_hex_rgb(accent) + (255,), (0, 0), detail)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, colour, accent in SYMBOLS:
        path = os.path.join(OUT, name + ".png")
        draw_symbol(name, colour, accent).save(path)
        print("wrote", path)


if __name__ == "__main__":
    main()
