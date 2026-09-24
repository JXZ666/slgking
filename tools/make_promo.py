"""Render the Bilibili promo for slgking.

    python tools/shot_cards.py --demo promo/shots
    python tools/shot_dialogs.py --demo
    python tools/make_promo.py

Screenshots move through this as animation: the window is composited onto a
dark plate, dimmed everywhere except one region, and a caption chip rides along
the spotlight while the copy explains what is underneath it. There is no
screen recording and no audio - every frame is drawn here and piped to ffmpeg.

Assets come from the two shot tools, which both run against the invented demo
library, so nothing real is photographed. `--cover-only` writes just the
Bilibili cover; `--still N` writes a single frame as a PNG for checking.
"""

import functools
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

import imageio_ffmpeg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "promo", "shots")
PROMO = os.path.join(ROOT, "promo")
OUT = os.path.join(PROMO, "slgking-promo-1080p.mp4")
COVER = os.path.join(PROMO, "cover-1146x717.png")

W, H, FPS = 1920, 1080, 30
XFADE = 0.45

# The burned-in caption box. Every scene keeps its bottom 130px clear for it,
# so the narration reads as subtitles and can be lifted straight into a
# voice-over - see CAPTIONS and `--subs-only`.
CAP_MAXW = 1420
CAP_BOTTOM = 1050
CAP_FADE = 0.28

BG = (28, 28, 30)
BG_EDGE = (16, 16, 18)
CARD = (38, 38, 42)
CHIP = (51, 51, 60)
ACCENT = (74, 142, 224)
TEXT = (232, 232, 234)
MUTED = (154, 154, 162)
STAR = (224, 168, 0)
WARM = (232, 138, 138)

FONT_R = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
# Punctuation that may not open a line, so a wrap does not strand it.
NO_LINE_START = "，。、；：？！）】》」』…—·"


# --- primitives --------------------------------------------------------------

def clamp01(t):
    return 0.0 if t < 0 else (1.0 if t > 1 else t)


def ease(t):
    """Smoothstep. Linear motion reads as cheap; this does not."""
    t = clamp01(t)
    return t * t * (3 - 2 * t)


def ramp(p, start, length):
    """0 before `start`, 1 after `start + length`."""
    return ease((p - start) / length) if length else (1.0 if p >= start else 0.0)


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_rect(r0, r1, t):
    return tuple(lerp(a, b, t) for a, b in zip(r0, r1))


@functools.lru_cache(maxsize=64)
def font(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT_R, size, index=0)


def wrap_cjk(text, f, maxw):
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if f.getlength(cur + ch) <= maxw or not cur:
            cur += ch
        else:
            if ch in NO_LINE_START:
                lines.append(cur + ch)
                cur = ""
            else:
                lines.append(cur)
                cur = ch
    if cur:
        lines.append(cur)
    return lines


def text_img(text, f, fill, alpha=255):
    """The string as its own small RGBA plate, top-aligned on the ascender."""
    asc, desc = f.getmetrics()
    im = Image.new("RGBA", (max(1, int(f.getlength(text)) + 2), asc + desc),
                   (0, 0, 0, 0))
    ImageDraw.Draw(im).text((0, 0), text, font=f, fill=tuple(fill) + (alpha,),
                            anchor="la")
    return im


def para_img(text, f, fill, maxw, leading=1.45, alpha=255, align="left"):
    lines = wrap_cjk(text, f, maxw)
    asc, desc = f.getmetrics()
    lh = int(f.size * leading)
    im = Image.new("RGBA", (maxw, lh * len(lines)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        x = 0
        if align == "center":
            x = (maxw - f.getlength(line)) / 2
        d.text((x, i * lh), line, font=f, fill=tuple(fill) + (alpha,), anchor="la")
    return im


def rounded(size, radius, fill, outline=None, width=1):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius, fill=fill,
        outline=outline, width=width if outline else 0)
    return im


def paste(dst, src, xy, alpha=1.0):
    """Blend an RGBA plate onto the frame, optionally faded."""
    if alpha <= 0.004 or src is None:
        return
    if alpha < 0.996:
        src = src.copy()
        src.putalpha(src.getchannel("A").point(lambda v: int(v * alpha)))
    dst.paste(src, (int(xy[0]), int(xy[1])), src)


def chip(text, f, bg=CARD, fg=TEXT, border=CHIP, pad=(18, 9), radius=10,
         number=None, alpha=255):
    """A caption pill. `number` puts an accent dot with a digit before the text."""
    tw = int(f.getlength(text))
    asc, desc = f.getmetrics()
    th = asc + desc
    lead = 0 if number is None else 44
    im = Image.new("RGBA", (tw + pad[0] * 2 + lead, th + pad[1] * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius,
                        fill=tuple(bg) + (min(235, alpha),),
                        outline=tuple(border) + (alpha,), width=1)
    x = pad[0]
    if number is not None:
        cy = im.height // 2
        r = 15
        d.ellipse([x, cy - r, x + 2 * r, cy + r], fill=ACCENT + (alpha,))
        nf = font(19, True)
        d.text((x + r, cy), str(number), font=nf, fill=(255, 255, 255, alpha),
               anchor="mm")
        x += 2 * r + 10
    d.text((x, pad[1]), text, font=f, fill=tuple(fg) + (alpha,), anchor="la")
    return im


def caption_alpha(t, t0, t1):
    """Fade in over the lead-in, hold, fade out over the tail. The window runs
    a CAP_FADE wider than the declared span on both sides, so two back-to-back
    lines hand over at full opacity instead of both reaching zero on the same
    frame and blinking the box out."""
    return min(ease((t - t0 + CAP_FADE) / CAP_FADE),
               ease((t1 + CAP_FADE - t) / CAP_FADE))


def draw_caption(frame, t):
    """The subtitle box at the bottom, keyed on absolute time so a line runs
    across a cross-fade instead of blinking out at the scene cut. Whichever
    line is furthest through its fade wins, so overlapping neighbours never
    stack."""
    best = max(CAPTIONS, key=lambda c: caption_alpha(t, c[0], c[1]), default=None)
    if best is None:
        return frame
    a = caption_alpha(t, best[0], best[1])
    if a <= 0.004:
        return frame
    f = font(34, True)
    lines = wrap_cjk(best[2], f, CAP_MAXW)
    lh = int(34 * 1.45)
    tw = max(int(f.getlength(s)) for s in lines)
    pad = (40, 24)
    plate = rounded((tw + pad[0] * 2, lh * len(lines) + pad[1] * 2), 14,
                    (0, 0, 0, 172), (255, 255, 255, 26), width=1)
    d = ImageDraw.Draw(plate)
    for i, s in enumerate(lines):
        d.text((pad[0], pad[1] + i * lh), s, font=f, fill=TEXT + (255,),
               anchor="la")
    paste(frame, plate, ((W - plate.width) / 2, CAP_BOTTOM - plate.height), a)
    return frame


@functools.lru_cache(maxsize=8)
def radial(cx, cy, rx, ry, inner, edge, scale=8):
    sw, sh = W // scale, H // scale
    im = Image.new("RGB", (sw, sh))
    px = im.load()
    for y in range(sh):
        for x in range(sw):
            dx = (x / sw - cx) / rx
            dy = (y / sh - cy) / ry
            d = clamp01((dx * dx + dy * dy) ** 0.5)
            px[x, y] = tuple(int(lerp(a, b, d)) for a, b in zip(inner, edge))
    return im.resize((W, H), Image.BICUBIC)


@functools.lru_cache(maxsize=32)
def window(shot_path, height, radius=16, spread=34, shadow=(0, 14), darkness=155):
    """A screenshot dressed as a floating window: rounded, with a soft shadow."""
    shot = Image.open(shot_path).convert("RGB")
    shot = ImageEnhance.Brightness(shot).enhance(1.10)
    w = int(round(shot.width * height / shot.height))
    im = shot.resize((w, height), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (w, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, height - 1], radius,
                                           fill=255)
    im.putalpha(mask)
    pad = spread * 2
    sh = Image.new("L", (w + pad * 2, height + pad * 2), 0)
    sh.paste(mask, (pad + shadow[0], pad + shadow[1]))
    sh = sh.filter(ImageFilter.GaussianBlur(spread * 0.8))
    sh = sh.point(lambda v: int(v * darkness / 255))
    out = Image.new("RGBA", sh.size, (0, 0, 0, 0))
    out.putalpha(sh)
    out.paste(im, (pad, pad), im)
    return out, (pad, pad), (w, height)


def place_window(frame, shot_path, height, x, y, radius=16):
    """Paste a window so that its content box starts at (x, y). Returns the box."""
    plate, (pad, _), (w, h) = window(shot_path, height, radius=radius)
    frame.paste(plate, (int(x - pad), int(y - pad)), plate)
    return (x, y, x + w, y + h)


def dim_mask(rect, radius=14, amount=132, blur=7):
    """Black at `amount`, punched through at `rect`. Half-res, then blown up:
    the blur costs a quarter of the pixels and the feather reads better."""
    m = Image.new("L", (W // 2, H // 2), amount)
    x0, y0, x1, y1 = (v / 2 for v in rect)
    ImageDraw.Draw(m).rounded_rectangle([x0, y0, x1, y1], radius / 2, fill=0)
    m = m.filter(ImageFilter.GaussianBlur(blur))
    return m.resize((W, H), Image.BICUBIC)


def region(box, fx0, fy0, fx1, fy1):
    x, y, x1_, y1_ = box
    w, h = x1_ - x, y1_ - y
    return (x + fx0 * w, y + fy0 * h, x + fx1 * w, y + fy1 * h)


# Where the window and its legend sit in the chapters that have both.
WIN_X, WIN_Y, WIN_H = 88, 250, 700
LEGEND_X, LEGEND_Y = 1258, 300


def head(frame, title, desc, alpha=1.0, x=88, y=58):
    paste(frame, text_img(title, font(54, True), TEXT), (x, y), alpha)
    paste(frame, text_img(desc, font(29), MUTED), (x, y + 68), alpha)


def active_rect(calls, p):
    """The live spotlight, interpolating between calls.

    calls: [(t_start, t_end, rect)]. Between two calls the rect slides from one
    to the next, which turns a change of focus into a camera move rather than a
    cut. Returns the rect and the 1-based index of the callout it belongs to.
    """
    if p <= calls[0][0]:
        return calls[0][2], 1
    for i, (t0, t1, rect) in enumerate(calls):
        if p < t0:
            prev = calls[i - 1]
            t = ease((p - prev[1]) / max(1e-6, t0 - prev[1]))
            return lerp_rect(prev[2], rect, t), i + 1
        if p < t1:
            return rect, i + 1
    return calls[-1][2], len(calls)


def spotlight(frame, rect, radius=14, amount=178):
    """Dim everything but `rect`, and ring the ones that stay lit.

    The labels live in the legend column, not on the window: a chip parked over
    a screenshot hides the thing it is pointing at, and there is never room for
    one in a 324px margin.
    """
    frame.paste((0, 0, 0), (0, 0), dim_mask(rect, radius=radius, amount=amount))
    ImageDraw.Draw(frame).rounded_rectangle(rect, radius,
                                            outline=ACCENT, width=3)


def legend(frame, items, starts, active, p, x=LEGEND_X, y=LEGEND_Y):
    """The numbered list beside the window. Each item arrives with its callout
    and lights up while its region is the one under the spotlight."""
    lf = font(30, True)
    y0 = y
    for i, label in enumerate(items):
        t = ramp(p, starts[i] - 0.05, 0.12)
        on = (i + 1) == active
        im = chip(label, lf, bg=ACCENT if on else (46, 46, 54),
                  fg=(255, 255, 255) if on else MUTED,
                  border=ACCENT if on else CHIP, number=i + 1, pad=(20, 13))
        paste(frame, im, (x, y0), t)
        y0 += im.height + 18


def chapter(frame, title, desc, items, calls, p):
    """Headline, one lit region, and the numbered legend beside it. The caller
    places the window first, so a chapter that moves its window can."""
    head(frame, title, desc)
    rect, active = active_rect(calls, p)
    spotlight(frame, rect, radius=12)
    legend(frame, items, [c[0] for c in calls], active, p)


# --- scenes ------------------------------------------------------------------

def scene_intro(p):
    frame = radial(0.5, 0.42, 0.75, 1.15, (42, 42, 48), BG_EDGE).copy()
    icon = Image.open(os.path.join(ROOT, "assets", "slgking.ico")).convert("RGBA")
    icon = icon.resize((196, 196), Image.LANCZOS)
    a = ramp(p, 0.02, 0.30)
    grow = 1.0 + 0.05 * (1 - ease(clamp01(p / 0.35)))
    size = int(196 * grow)
    ic = icon.resize((size, size), Image.LANCZOS)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [W / 2 - 300, 330 - 150, W / 2 + 300, 330 + 150],
        fill=ACCENT + (int(70 * a),))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    frame.paste(glow, (0, 0), glow)
    paste(frame, ic, (W / 2 - size / 2, 330 - size / 2), a)

    t = ramp(p, 0.20, 0.22)
    paste(frame, text_img("SLG黄油之王", font(88, True), TEXT),
          (W / 2 - font(88, True).getlength("SLG黄油之王") / 2, 520 - 30 * (1 - t)), t)
    line = int(320 * ramp(p, 0.34, 0.24))
    if line:
        ImageDraw.Draw(frame).rectangle([W / 2 - line / 2, 650, W / 2 + line / 2, 653],
                                        fill=ACCENT)
    paste(frame, text_img("把 dikgames 的清单和硬盘上的游戏，合成一本账", font(32), MUTED),
          (W / 2 - font(32).getlength("把 dikgames 的清单和硬盘上的游戏，合成一本账") / 2, 690),
          ramp(p, 0.36, 0.22))
    return frame


def scene_pain(p):
    frame = radial(0.5, 0.5, 0.85, 1.2, (34, 34, 38), BG_EDGE).copy()
    lines = ["一千多款游戏，站里没有「你」这个概念",
             "硬盘上的游戏文件夹不记账",
             "每次想玩，都要重新翻一遍"]
    for i, line in enumerate(lines):
        t = ramp(p, 0.05 + i * 0.23, 0.18)
        if t <= 0.004:
            continue
        y = 400 + i * 104
        f = font(40, True)
        paste(frame, text_img(line, f, TEXT), (348, y), t)
        ImageDraw.Draw(frame).rectangle([286, y + 6, 291, y + 6 + 44 * t],
                                        fill=ACCENT)
    return frame


def scene_find(p):
    frame = radial(0.34, 0.34, 0.80, 1.10, (38, 38, 44), BG_EDGE).copy()
    shot = os.path.join(SHOTS, "cards-dark.png")
    # A push-in for the first beat, then it holds and the focus does the moving.
    push = 1.0 - 0.055 * (1 - ease(clamp01(p / 0.16)))
    # Stepped to 4px: the plate is cached per height, and per-frame heights
    # would rebuild it - resize plus two blurs - on every frame of the push.
    h = int(WIN_H * push / 4) * 4
    box = place_window(frame, shot, h, WIN_X + (WIN_H - h) * 0.5,
                       WIN_Y + (WIN_H - h) * 0.5)
    calls = [
        (0.16, 0.42, region(box, 0.20, 0.23, 0.62, 0.58)),
        (0.42, 0.70, region(box, 0.19, 0.145, 0.79, 0.24)),
        (0.70, 0.99, region(box, 0.63, 0.22, 0.99, 0.99)),
    ]
    items = ["封面墙：点一张看详情", "标签求交集：左键加入、右键排除",
             "站内评分、状态、五星，全落 sqlite"]
    chapter(frame, "找游戏", "封面墙 + 标签求交集筛选，点几下缩小到能看的一屏",
            items, calls, p)
    # Top-right, opposite the headline: the bottom strip belongs to the caption.
    note = text_img("演示数据：条目与封面均为虚构", font(21), (120, 120, 128))
    paste(frame, note, (W - note.width - 88, 66), 0.85)
    return frame


def scene_manage(p):
    frame = radial(0.34, 0.34, 0.80, 1.10, (38, 38, 44), BG_EDGE).copy()
    box = place_window(frame, os.path.join(SHOTS, "detail-dark.png"),
                       WIN_H, WIN_X, WIN_Y)
    calls = [
        (0.08, 0.38, region(box, 0.63, 0.38, 0.99, 0.57)),
        (0.38, 0.68, region(box, 0.63, 0.56, 0.99, 0.71)),
        (0.68, 0.99, region(box, 0.44, 0.70, 0.99, 0.84)),
    ]
    items = ["状态三选一 + 五星 + 备注", "标签左键加入筛选、右键排除",
             "简介「原文 / 中文」对照，能就地手改"]
    chapter(frame, "管游戏", "翻了哪款、打了几星、想不想玩，都写进本地库",
            items, calls, p)
    return frame


def scene_updates(p):
    frame = radial(0.34, 0.34, 0.80, 1.10, (38, 38, 44), BG_EDGE).copy()
    box = place_window(frame, os.path.join(SHOTS, "dlg-updates-dark.png"),
                       WIN_H, WIN_X, WIN_Y, radius=12)
    calls = [
        (0.06, 0.40, region(box, 0.0, 0.0, 1.0, 0.17)),
        (0.40, 0.99, region(box, 0.0, 0.17, 1.0, 0.80)),
    ]
    items = ["一眼看到几款落后了", "每一行：本地 X → 站点 Y"]
    chapter(frame, "追更新", "拿文件夹名里的版本号和站上当前版本比，列出落后的",
            items, calls, p)
    return frame


ENGINES = ["DeepSeek 官方", "硅基流动", "月之暗面 Kimi", "智谱 GLM",
           "通义千问", "OpenAI", "自定义…"]


def scene_translate(p):
    frame = radial(0.30, 0.34, 0.80, 1.10, (38, 38, 44), BG_EDGE).copy()
    head(frame, "看得懂", "机翻不留痕：你手改过的译文，机翻不会覆盖")
    place_window(frame, os.path.join(SHOTS, "dlg-settings-dark.png"), 700,
                 88, 250, radius=12)
    f = font(28)
    # Two columns, so the chips sit beside the dialog instead of in a stripe
    # down the middle with the right third of the frame empty.
    for i, name in enumerate(ENGINES):
        t = ramp(p, 0.08 + i * 0.045, 0.16)
        if t <= 0.004:
            continue
        im = chip(name, f, bg=CHIP, fg=TEXT, border=(72, 72, 84))
        x = 900 + (i % 2) * 400
        y = 316 + (i // 2) * (im.height + 16)
        paste(frame, im, (x, y + 12 * (1 - t)), t)
    t = ramp(p, 0.48, 0.18)
    if t > 0.004:
        im = chip("免费机翻（不用 API Key）", font(29), bg=(30, 46, 36),
                  fg=(150, 214, 170), border=(52, 84, 62))
        paste(frame, im, (900, 620 + 12 * (1 - t)), t)
        note = "译文按内容哈希缓存\n翻过一次就一直用"
        paste(frame, para_img(note, font(25), MUTED, 460), (900, 690),
              ramp(p, 0.62, 0.18))
    return frame


def scene_taste(p):
    frame = radial(0.5, 0.42, 0.80, 1.15, (36, 36, 42), BG_EDGE).copy()
    head(frame, "学口味", "你打的星反推成标签权重，列表按 站内评分 + Σ标签权重 排")

    # Five stars, four of them filling in.
    sx, sy, step = 210, 400, 96
    filled = 4 * ease(clamp01(p / 0.26))
    sf = font(80, True)
    for i in range(5):
        on = clamp01(filled - i)
        col = tuple(int(lerp(a, b, on)) for a, b in zip((64, 64, 72), STAR))
        paste(frame, text_img("★", sf, col), (sx + i * step, sy), 1.0)
    paste(frame, text_img("我的评分", font(30), MUTED), (sx, sy + 110),
          ramp(p, 0.16, 0.2))

    # Tag weights, length proportional to the weight.
    weights = [("堕落", 2.3), ("剧情", 1.1), ("日常", 0.4), ("恐怖", -0.8)]
    wx, wy = 210, 620
    f = font(28, True)
    for i, (tag, val) in enumerate(weights):
        t = ramp(p, 0.26 + i * 0.07, 0.18)
        if t <= 0.004:
            continue
        im = chip(tag, f, bg=CHIP, fg=TEXT, border=(72, 72, 84))
        paste(frame, im, (wx, wy + i * 58 + 10 * (1 - t)), t)
        bar = int(150 * min(1.0, abs(val) / 2.3) * t)
        col = (74, 142, 224) if val > 0 else (150, 90, 90)
        if bar:
            ImageDraw.Draw(frame).rounded_rectangle(
                [wx + 132, wy + i * 58 + 16, wx + 132 + bar, wy + i * 58 + 30],
                6, fill=col)
        paste(frame, text_img("%+.1f" % val, font(24),
                              (150, 190, 240) if val > 0 else (220, 150, 150)),
              (wx + 300, wy + i * 58 + 12), t)

    # The list reordering itself, which is the point of the weights.
    rows = [("The Last Tram", 7.1), ("Midnight Bakery", 8.4),
            ("Wool & Thunder", 6.8), ("Lantern & Ledger", 9.0)]
    order = sorted(range(len(rows)), key=lambda i: -rows[i][1])
    ry, rh = 400, 92
    lx = 1180
    for slot, idx in enumerate(order):
        t = ease(clamp01((p - 0.52) / 0.30))
        from_y = ry + idx * rh
        to_y = ry + slot * rh
        y = lerp(from_y, to_y, t)
        title, score = rows[idx]
        card = rounded((620, rh - 16), 12, CARD + (235,), (60, 60, 70, 255))
        paste(frame, card, (lx, y), ramp(p, 0.46, 0.18))
        paste(frame, text_img(title, font(28, True), TEXT), (lx + 22, y + 16),
              ramp(p, 0.46, 0.18))
        # The bar gets its own column: the widest title is 246px, so the track
        # starts at 300 and the score right-aligns against the card edge.
        bar = int(245 * ease(clamp01((p - 0.68) / 0.28)) * (score / 10))
        if bar:
            ImageDraw.Draw(frame).rounded_rectangle(
                [lx + 300, y + 30, lx + 300 + bar, y + 42], 6, fill=ACCENT)
        sc_im = text_img("%.1f" % score, font(26, True), STAR)
        paste(frame, sc_im, (lx + 620 - 26 - sc_im.width, y + 16),
              ramp(p, 0.68, 0.2))
    return frame


THEME_ORDER = [("浅色", "light"), ("深色", "dark"), ("跟随系统", None)]


def scene_theme(p):
    frame = radial(0.5, 0.32, 0.75, 1.05, (44, 44, 50), BG_EDGE).copy()
    head(frame, "看得舒服", "浅色 / 深色 / 跟随系统，右侧顶部一键切换")
    # dark -> light -> dark, so the film never leaves its own palette for long.
    if p < 0.20 or p >= 0.88:
        mood, active = 0.0, ("system" if p >= 0.90 else "dark")
    elif p < 0.44:
        mood = ease((p - 0.20) / 0.24)
        active = "light" if mood > 0.5 else "dark"
    elif p < 0.64:
        mood, active = 1.0, "light"
    else:
        mood = 1.0 - ease((p - 0.64) / 0.24)
        active = "dark" if mood < 0.5 else "light"

    # Blend the two prepared plates rather than handing window() a path it
    # would cache against changing content.
    plate_dark, pad, (w, h) = window(os.path.join(SHOTS, "cards-dark.png"),
                                     600, radius=14)
    plate_light, _, _ = window(os.path.join(SHOTS, "cards-light.png"),
                               600, radius=14)
    plate = plate_dark if mood <= 0 else Image.blend(plate_dark, plate_light, mood)
    x, y = 494, 200
    frame.paste(plate, (int(x - pad[0]), int(y - pad[1])), plate)

    f = font(27, True)
    cx = W / 2 - (len(THEME_ORDER) * 170) / 2
    for name, mode in THEME_ORDER:
        on = mode == active
        im = chip(name, f, bg=ACCENT if on else CARD,
                  fg=(255, 255, 255) if on else MUTED,
                  border=ACCENT if on else CHIP)
        paste(frame, im, (cx, 812), 1.0)
        cx += 170
    note = "跟随系统时，系统换主题跟着换"
    paste(frame, text_img(note, font(24), MUTED),
          (W / 2 - font(24).getlength(note) / 2, 880), 0.9)
    return frame


PROMISES = ["完全免费，没有收费版",
            "无账号 · 无上传 · 无遥测",
            "数据只存在这台电脑上"]


def scene_outro(p):
    frame = radial(0.5, 0.38, 0.75, 1.15, (40, 40, 46), BG_EDGE).copy()
    icon = Image.open(os.path.join(ROOT, "assets", "slgking.ico")).convert("RGBA")
    icon = icon.resize((132, 132), Image.LANCZOS)
    paste(frame, icon, (W / 2 - 66, 172), ramp(p, 0.02, 0.22))
    title = "SLG黄油之王"
    paste(frame, text_img(title, font(62, True), TEXT),
          (W / 2 - font(62, True).getlength(title) / 2, 336), ramp(p, 0.10, 0.22))
    f = font(31)
    y = 470
    for i, line in enumerate(PROMISES):
        t = ramp(p, 0.22 + i * 0.10, 0.22)
        if t <= 0.004:
            continue
        w = int(f.getlength(line))
        paste(frame, text_img(line, f, TEXT), (W / 2 - w / 2 + 20, y + 8 * (1 - t)), t)
        ImageDraw.Draw(frame).ellipse([W / 2 - w / 2 - 6, y + 15,
                                       W / 2 - w / 2 + 6, y + 27],
                                      fill=ACCENT if t > 0.4 else (70, 70, 80))
        y += 62
    a = ramp(p, 0.56, 0.24)
    link = text_img("github.com/JXZ666", font(34, True), ACCENT)
    paste(frame, link, (W / 2 - link.width / 2, 700), a)
    group = text_img("QQ 交流群：1124074040", font(25), MUTED)
    paste(frame, group, (W / 2 - group.width / 2, 762), ramp(p, 0.66, 0.24))
    star = ramp(p, 0.76, 0.20)
    if star > 0.004:
        bounce = 6 * (1 - ease(clamp01((p - 0.76) / 0.14)))
        msg = "求个 GitHub star，也欢迎推荐给朋友"
        im = chip(msg, font(30, True), bg=(32, 40, 54), fg=(190, 214, 245),
                  border=ACCENT)
        paste(frame, im, (W / 2 - im.width / 2, 838 + bounce), star)
    fade = 1.0 - ramp(p, 0.94, 0.06)
    return frame if fade >= 1 else Image.blend(
        Image.new("RGB", (W, H), BG_EDGE), frame, fade)


# The narration, as (start, end, line) in seconds from the top of the film.
# One track drives both the burned-in box and the .srt, so the two can never
# drift apart. Starts clear the 0.45s cross-fade, ends stay inside their scene.
CAPTIONS = [
    (0.9, 4.2, "本地游戏库管理工具：免费、离线，数据只存在这台电脑上"),
    (5.0, 8.3, "一千多款游戏，站里没有「你」这个概念"),
    (8.3, 11.8, "硬盘上的游戏文件夹不记账，每次想玩都要重新翻一遍"),
    (12.6, 17.8, "封面墙加标签求交集：左键加入、右键排除"),
    (17.8, 23.6, "点几下就缩到能看的一屏，评分状态全落 sqlite"),
    (24.6, 28.8, "翻了哪款、打了几星、想不想玩，都写进本地库"),
    (28.8, 32.6, "简介原文中文对照，翻译不满意就地手改"),
    (33.6, 37.8, "文件夹名里的版本号，和站上当前版本逐一对比"),
    (37.8, 42.6, "落后的游戏，连着本地和站点两头的版本一起列出来"),
    (43.6, 47.8, "DeepSeek、Kimi、GLM、千问，选一个就能翻"),
    (47.8, 52.6, "机翻不留痕：你手改过的译文，它不会再覆盖"),
    (53.6, 56.8, "你打的星会反推成标签权重"),
    (56.8, 60.6, "列表按站内评分加标签权重重排，越用越懂你"),
    (61.6, 64.3, "浅色、深色、跟随系统，右侧顶上一键切换"),
    (64.3, 67.1, "选跟随系统时，系统换主题它跟着换"),
    (68.1, 71.0, "完全免费，无账号、无上传、无遥测"),
    (71.0, 74.4, "下载与常见问题看简介和置顶评论，遇收费请立即举报"),
]


TIMELINE = [
    (4.5, scene_intro),
    (7.5, scene_pain),
    (12.0, scene_find),
    (9.0, scene_manage),
    (10.0, scene_updates),
    (10.0, scene_translate),
    (8.0, scene_taste),
    (6.5, scene_theme),
    (7.5, scene_outro),
]


def frame_at(t):
    starts, acc = [], 0.0
    for dur, _ in TIMELINE:
        starts.append(acc)
        acc += dur
    for i in range(len(TIMELINE) - 1, -1, -1):
        if t >= starts[i]:
            dur, fn = TIMELINE[i]
            img = fn(clamp01((t - starts[i]) / dur))
            if i > 0 and t - starts[i] < XFADE:
                img = Image.blend(TIMELINE[i - 1][1](1.0), img,
                                  ease((t - starts[i]) / XFADE))
            return draw_caption(img, t)
    return draw_caption(TIMELINE[0][1](0.0), t)


def total_frames():
    return int(round(sum(d for d, _ in TIMELINE) * FPS))


def render(still=None, cover=False):
    os.makedirs(PROMO, exist_ok=True)
    if cover:
        # Composed at 1920x1080 and cropped, never squashed: 1146x717 (B站's
        # ratio) against 16:9 would cost the text an 11% horizontal squeeze.
        # Everything lives inside x 140..1780 so the crop to 1726 loses nothing.
        frame = radial(0.30, 0.42, 0.90, 1.15, (34, 34, 42), BG_EDGE).copy()
        place_window(frame, os.path.join(SHOTS, "cards-dark.png"), 620, 790, 300,
                     radius=14)
        icon = Image.open(os.path.join(ROOT, "assets", "slgking.ico")).convert("RGBA")
        paste(frame, icon.resize((150, 150), Image.LANCZOS), (170, 176))
        paste(frame, text_img("SLG黄油之王", font(92, True), TEXT), (170, 368))
        ImageDraw.Draw(frame).rectangle([170, 494, 470, 502], fill=ACCENT)
        paste(frame, text_img("本地游戏库管理工具", font(36), TEXT), (170, 540))
        for i, line in enumerate(["把 dikgames 的清单和硬盘上的游戏，",
                                  "合成一本账"]):
            paste(frame, text_img(line, font(30), MUTED), (170, 604 + i * 46))
        paste(frame, text_img("完全免费 · 无账号 · 无上传 · 无遥测",
                              font(28), MUTED), (170, 928))
        paste(frame, text_img("演示数据：条目与封面均为虚构",
                              font(23), (110, 110, 120)), (170, 990))
        crop = frame.crop((97, 0, 1823, 1080))
        crop.resize((1146, 717), Image.LANCZOS).save(COVER)
        print(COVER)
        return
    if still is not None:
        path = os.path.join(PROMO, "still-%s.png" % still.replace(".", "_"))
        frame_at(float(still)).save(path)
        print(path)
        return

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(FPS), "-i", "-", "-an",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT]
    n = total_frames()
    print("rendering %d frames (%.1fs) -> %s" % (n, n / FPS, OUT))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for i in range(n):
            proc.stdin.write(frame_at(i / FPS).convert("RGB").tobytes())
            if i % 150 == 0:
                print("  %d/%d  %.0f%%" % (i, n, 100 * i / n), flush=True)
    finally:
        proc.stdin.close()
        proc.wait()
    print("done:", OUT, "%.1f MB" % (os.path.getsize(OUT) / 1e6))


def srt_stamp(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def mmss(sec):
    return "%02d:%04.1f" % (int(sec // 60), sec % 60)


def write_subs():
    """CAPTIONS -> the two things a voice-over needs. The .srt is what B站
    accepts as CC subtitles and what its AI dubbing reads; the .txt is the same
    17 lines with timecodes, for reading aloud. Both come off the one table the
    film burns in, so picture and script cannot drift."""
    os.makedirs(PROMO, exist_ok=True)
    srt = os.path.join(PROMO, "字幕.srt")
    with open(srt, "w", encoding="utf-8") as fh:
        for i, (t0, t1, text) in enumerate(CAPTIONS, 1):
            fh.write("%d\n%s --> %s\n%s\n\n"
                     % (i, srt_stamp(t0), srt_stamp(t1), text))
    print(srt)

    txt = os.path.join(PROMO, "配音文稿.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("SLG黄油之王 · B站宣传片配音文稿\n")
        fh.write("总长 %.1f 秒 / 共 %d 句 —— 逐句念，句间留白\n"
                 % (sum(d for d, _ in TIMELINE), len(CAPTIONS)))
        fh.write("=" * 44 + "\n\n")
        for t0, t1, text in CAPTIONS:
            fh.write("[%s - %s]\n%s\n\n" % (mmss(t0), mmss(t1), text))
    print(txt)


CHECK_TIMES = [2, 8, 18, 28, 38, 48, 57, 64, 71]


def check_sheet(times=CHECK_TIMES, scale=0.42, cols=2, out="check-sheet.png"):
    """Every scene as one frame, tiled with its timestamp. The QA loop: render,
    look at the sheet, fix, render again. Kept at two columns because the whole
    film in one image is over the size the viewer will accept."""
    tiles = []
    for t in times:
        im = frame_at(float(t))
        im = im.resize((int(W * scale), int(H * scale)), Image.LANCZOS)
        tiles.append((t, im))
    tw, th = tiles[0][1].size
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 10) + 10, rows * (th + 34) + 10),
                      (12, 12, 14))
    d = ImageDraw.Draw(sheet)
    f = font(26, True)
    for i, (t, im) in enumerate(tiles):
        x = 10 + (i % cols) * (tw + 10)
        y = 10 + (i // cols) * (th + 34)
        d.text((x + 4, y), "%ds" % t, font=f, fill=(255, 130, 130))
        sheet.paste(im, (x, y + 28))
    path = os.path.join(PROMO, out)
    sheet.save(path)
    print(path)


def main():
    argv = sys.argv[1:]
    if "--cover-only" in argv:
        return render(cover=True)
    if "--subs-only" in argv:
        return write_subs()
    if "--still" in argv:
        return render(still=argv[argv.index("--still") + 1])
    if "--check" in argv:
        spec = argv[argv.index("--check") + 1] if len(argv) > argv.index("--check") + 1 else None
        if spec and not spec.startswith("--"):
            head_, tail = spec.split(":")
            return check_sheet([int(x) for x in head_.split(",")],
                               out="check-%s.png" % tail)
        check_sheet(CHECK_TIMES[:6], out="check-a.png")
        check_sheet(CHECK_TIMES[6:], out="check-b.png")
        return
    render()


if __name__ == "__main__":
    main()
