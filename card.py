""""Now playing" card (JPEG) drawn with Pillow.
With a song photo (YouTube thumbnail): blurred photo as background + the same photo in a box in the middle.
Without a photo: a colourful generated art tile is used instead, so it never fails."""
import colorsys
import hashlib
import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

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


def _blend(img, fn):
    """Draw semi-transparent shapes: fn(draw) paints on a clear layer which is then composited."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fn(ImageDraw.Draw(layer))
    img.alpha_composite(layer)


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


def _cover(im, w, h):
    """Resize + centre-crop so the image fills w x h exactly."""
    r = max(w / im.width, h / im.height)
    nw, nh = max(w, int(im.width * r + 0.5)), max(h, int(im.height * r + 0.5))
    im = im.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return im.crop((x, y, x + w, y + h))


def _unbar(im):
    """YouTube hqdefault/sddefault (4:3) has black bars on top and bottom: cut them."""
    if abs(im.width / im.height - 4 / 3) < 0.02:
        bar = int(im.height * 0.125)
        return im.crop((0, bar, im.width, im.height - bar))
    return im


def _art_tile(h, rnd, w, hh, disc=True):
    """Photo-less fallback: gradient + vinyl, same size as the photo box."""
    t = _gradient(_hsv(h, 0.75, 0.95), _hsv(h + 0.10, 0.85, 0.55)).resize((w, hh))
    t = t.convert("RGBA")
    if not disc:
        return t.convert("RGB")
    d = ImageDraw.Draw(t)
    cx, cy, R = w // 2, hh // 2, hh // 2 - 28
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(14, 14, 18, 255))
    for k in range(1, 7):
        rr = R - k * 11
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(44, 44, 54, 255), width=1)
    d.ellipse((cx - 46, cy - 46, cx + 46, cy + 46), fill=_hsv(h + 0.5, 0.7, 1.0) + (255,))
    d.polygon([(cx - 10, cy - 18), (cx - 10, cy + 18), (cx + 20, cy)], fill=(255, 255, 255, 255))
    return t.convert("RGB")


def make_card(title: str, duration: str = "", by: str = "", seed: str = "", photo: bytes | None = None) -> bytes:
    title = (title or "Unknown").strip()
    rnd = random.Random(int(hashlib.md5((seed or title).encode()).hexdigest()[:8], 16))
    h = rnd.random()
    accent = _hsv(h, 0.45, 1.0)

    BW, BH = 480, 270                      # photo box
    bx, by_ = (W - BW) // 2, 32

    pic = None
    if photo:
        try:
            pic = _unbar(Image.open(io.BytesIO(photo)).convert("RGB"))
        except Exception:
            pic = None
    tile = _cover(pic, BW, BH) if pic is not None else _art_tile(h, rnd, BW, BH)

    # ---- background: same photo, covered, blurred and darkened
    bg_src = tile if pic is not None else _art_tile(h, rnd, BW, BH, disc=False)
    bg = _cover(bg_src, W, H).filter(ImageFilter.GaussianBlur(34))
    bg = ImageEnhance.Color(bg).enhance(1.35).point(lambda v: int(v * 0.68)).convert("RGBA")
    img = bg
    _blend(img, lambda g: g.rectangle((0, 0, W, H), fill=(0, 0, 0, 45)))

    # ---- equalizer bars on both sides of the photo (shows "music is playing")
    n, bw, gap = 9, 10, 9
    span = n * (bw + gap) - gap
    ymid = by_ + BH // 2
    def bars(g):
        for side_x in (70, W - 70 - span):
            for i in range(n):
                hh = 24 + int(110 * abs(math.sin(i * 0.75 + h * 6 + (0 if side_x < 500 else 1.7))) * (0.55 + 0.45 * rnd.random()))
                x = side_x + i * (bw + gap)
                g.rounded_rectangle((x, ymid - hh // 2, x + bw, ymid + hh // 2), 5, fill=(255, 255, 255, 185))
    _blend(img, bars)

    # ---- photo box: shadow, photo, white border
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((bx + 4, by_ + 16, bx + BW + 4, by_ + BH + 16), 26, fill=(0, 0, 0, 120))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)))
    mask = Image.new("L", (BW, BH), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, BW - 1, BH - 1), 24, fill=255)
    img.paste(tile.convert("RGBA"), (bx, by_), mask)
    _blend(img, lambda g: g.rounded_rectangle((bx, by_, bx + BW - 1, by_ + BH - 1), 24,
                                              outline=(255, 255, 255, 200), width=3))
    d = ImageDraw.Draw(img)

    # ---- "NOW PLAYING" tag (top-left)
    f_tag = _font(True, 19)
    _blend(img, lambda g: g.rounded_rectangle((28, 26, 218, 62), 18, fill=(0, 0, 0, 110)))
    d = ImageDraw.Draw(img)
    d.ellipse((42, 38, 54, 50), fill=accent + (255,))
    d.text((64, 44), "NOW PLAYING", font=f_tag, fill=(255, 255, 255, 255), anchor="lm")

    # ---- title inside a blue box (centered, one line, shrinks to fit)
    shown = title if _printable(None, title) else "Now streaming"
    f_title = _fit(d, shown, _font, 780, 40, 24)
    line = _wrap(d, shown, f_title, 780, 1)[0]
    tw = int(d.textlength(line, font=f_title))
    bw_, bh_ = max(220, tw + 64), 58
    x0, y0 = (W - bw_) // 2, 313
    _blend(img, lambda g: g.rounded_rectangle((x0 + 2, y0 + 8, x0 + bw_ + 2, y0 + bh_ + 8), 29, fill=(0, 0, 0, 110)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x0, y0, x0 + bw_, y0 + bh_), 29, fill=(30, 90, 235, 255))
    _blend(img, lambda g: g.rounded_rectangle((x0, y0, x0 + bw_, y0 + bh_), 29, outline=(255, 255, 255, 90), width=2))
    d = ImageDraw.Draw(img)
    d.text((W // 2, y0 + bh_ // 2 + 1), line, font=f_title, fill=(255, 255, 255, 255), anchor="mm")

    # ---- progress bar + times
    px0, px1, py = bx, bx + BW, 400
    f_t = _font(False, 21)
    _blend(img, lambda g: g.rounded_rectangle((px0, py, px1, py + 6), 3, fill=(255, 255, 255, 80)))
    d = ImageDraw.Draw(img)
    pos = px0 + int(BW * 0.33)
    d.rounded_rectangle((px0, py, pos, py + 6), 3, fill=accent + (255,))
    d.ellipse((pos - 9, py - 6, pos + 9, py + 12), fill=(255, 255, 255, 255))
    d.text((px0, py + 24), "0:00", font=f_t, fill=(255, 255, 255, 190))
    if duration:
        d.text((px1, py + 24), duration.replace(" min", ""), font=f_t, fill=(255, 255, 255, 190), anchor="ra")

    # ---- player buttons: prev, pause (we are playing), next
    cy = 468
    d.ellipse((W // 2 - 30, cy - 30, W // 2 + 30, cy + 30), fill=(255, 255, 255, 255))
    d.rounded_rectangle((W // 2 - 11, cy - 13, W // 2 - 3, cy + 13), 2, fill=(20, 20, 26, 255))
    d.rounded_rectangle((W // 2 + 3, cy - 13, W // 2 + 11, cy + 13), 2, fill=(20, 20, 26, 255))
    white = (255, 255, 255, 255)
    xp, xn = W // 2 - 96, W // 2 + 96
    d.polygon([(xp + 12, cy - 15), (xp + 12, cy + 15), (xp - 10, cy)], fill=white)   # previous: |<
    d.rectangle((xp - 14, cy - 15, xp - 11, cy + 15), fill=white)
    d.polygon([(xn - 12, cy - 15), (xn - 12, cy + 15), (xn + 10, cy)], fill=white)   # next: >|
    d.rectangle((xn + 11, cy - 15, xn + 14, cy + 15), fill=white)

    # ---- requested by
    if by and _printable(None, by):
        f_by = _font(False, 21)
        txt = _wrap(d, f"Requested by  {by}", f_by, 700, 1)[0]
        d.text((W // 2, 531), txt, font=f_by, fill=(255, 255, 255, 215), anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()
