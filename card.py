"""Colourful "Now playing" card (PNG/JPEG) drawn with Pillow. No internet, no extra files needed.
Colours change per song (picked from the title), so every song gets its own look."""
import colorsys
import hashlib
import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1000, 560

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
              "/usr/share/fonts/truetype/noto", "/usr/share/fonts/truetype/freefont")


def _font(bold: bool, size: int):
    names = ("DejaVuSans-Bold.ttf", "FreeSansBold.ttf") if bold else ("DejaVuSans.ttf", "FreeSans.ttf")
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def _hsv(h, s=0.75, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _gradient(c1, c2):
    # diagonal gradient built from a tiny image scaled up (fast)
    small = Image.new("RGB", (64, 36))
    px = small.load()
    for y in range(36):
        for x in range(64):
            t = (x / 63 * 0.65 + y / 35 * 0.35)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return small.resize((W, H), Image.BICUBIC)


def _fit(draw, text, font_fn, max_w, size, min_size, bold=True):
    while size > min_size:
        f = font_fn(bold, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return font_fn(bold, min_size)


def _wrap(draw, text, font, max_w, lines=2):
    words, out, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        while out[-1] and draw.textlength(out[-1] + "…", font=font) > max_w:
            out[-1] = out[-1][:-1]
        out[-1] = out[-1].rstrip() + "…"
    return out


def _printable(font, text) -> bool:
    """True when the font can draw every character (Hindi etc. would show boxes with DejaVu)."""
    try:
        return all(ord(c) < 0x250 or c in "–—’‘“”…•·" for c in text)
    except Exception:
        return False


def make_card(title: str, duration: str = "", by: str = "", seed: str = "") -> bytes:
    title = (title or "Unknown").strip()
    rnd = random.Random(int(hashlib.md5((seed or title).encode()).hexdigest()[:8], 16))
    h = rnd.random()
    c1, c2 = _hsv(h, 0.80, 0.95), _hsv(h + 0.38 + rnd.random() * 0.12, 0.85, 0.80)
    img = _gradient(c1, c2).convert("RGBA")

    # soft glowing blobs
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for _ in range(6):
        cx, cy, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(90, 220)
        col = _hsv(h + rnd.random() * 0.6, 0.6, 1.0) + (110,)
        gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))

    # equalizer bars along the bottom
    bars = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bars)
    n, bw, gap = 46, 14, 7
    x0 = (W - (n * (bw + gap) - gap)) // 2
    for i in range(n):
        hh = 25 + int(70 * abs(math.sin(i * 0.37 + h * 6)) * (0.55 + 0.45 * rnd.random()))
        x = x0 + i * (bw + gap)
        bd.rounded_rectangle((x, H - 28 - hh, x + bw, H - 28), 6, fill=(255, 255, 255, 120))
    img.alpha_composite(bars)

    # vinyl disc on the left
    cx, cy, R = 235, 270, 165
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((cx - R + 10, cy - R + 18, cx + R + 10, cy + R + 18), fill=(0, 0, 0, 120))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(img)
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(18, 18, 24, 255))
    for k in range(1, 9):  # grooves
        rr = R - k * 15
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(60, 60, 75, 255), width=1)
    lab = 78
    d.ellipse((cx - lab, cy - lab, cx + lab, cy + lab), fill=_hsv(h + 0.5, 0.75, 1.0) + (255,))
    d.ellipse((cx - lab + 14, cy - lab + 14, cx + lab - 14, cy + lab - 14), outline=(255, 255, 255, 160), width=3)
    d.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(20, 20, 26, 255))
    f_note = _font(True, 70)
    d.text((cx, cy - 4), "♪", font=f_note, fill=(255, 255, 255, 255), anchor="mm")

    # text panel
    px0, px1 = 450, W - 45
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle((px0 - 25, 90, px1 + 20, 470), 28, fill=(0, 0, 0, 80),
                                            outline=(255, 255, 255, 90), width=2)
    img.alpha_composite(panel)
    d = ImageDraw.Draw(img)
    f_tag = _font(True, 26)
    d.rounded_rectangle((px0, 112, px0 + 232, 154), 21, fill=(255, 255, 255, 235))
    d.text((px0 + 116, 133), "NOW PLAYING", font=f_tag, fill=c2, anchor="mm")

    shown = title if _printable(None, title) else "Now streaming"
    f_title = _fit(d, shown, _font, px1 - px0, 52, 34)
    for i, line in enumerate(_wrap(d, shown, f_title, px1 - px0, 2)):
        d.text((px0, 180 + i * 64), line, font=f_title, fill=(255, 255, 255, 255))

    f_meta = _font(False, 28)
    y = 335
    if duration:
        d.text((px0, y), f"Duration  {duration}", font=f_meta, fill=(255, 255, 255, 235))
        y += 42
    if by and _printable(None, by):
        by_line = _wrap(d, f"Requested by  {by}", f_meta, px1 - px0, 1)[0]
        d.text((px0, y), by_line, font=f_meta, fill=(255, 255, 255, 235))

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()
