"""Chhote pure helpers (Telegram / yt-dlp import nahi, isliye test karna aasaan).

  clean_title(raw)       : YouTube title -> sahi gaane ka naam ("Tum Hi Ho (Official Video) | T-Series" -> "Tum Hi Ho")
  song_key(page)         : link -> pakki pehchaan ("yt:<video id>"), taaki alag search ek hi gaana ginein
  radio_key(title, art)  : radio gaane ki pehchaan (songs.csv se), rating isi par judti hai
  title_key(title)       : last fallback pehchaan
  weighted_sample(...)   : rating ke hisaab se radio gaane chunna (jyada rating = jyada chance)
"""
import random
import re
from urllib.parse import parse_qs, urlparse

_JUNK = (r"official|video|audio|lyric|lyrics|lyrical|full song|full video|full audio|hd|4k|1080p|720p|"
         r"music|visuali[sz]er|remaster(?:ed)?|mv|teaser|songs?|hq|jukebox|promo|trailer|status|"
         r"with lyrics|video song|audio song|new song|latest")
_BRACKET_JUNK = re.compile(r"[\(\[\{][^\)\]\}]*\b(?:" + _JUNK + r")\b[^\)\]\}]*[\)\]\}]", re.I)
_TRAIL_JUNK = re.compile(
    r"\s+[-–—]\s+(?:official\b.*|full\s+(?:video|audio|song)\b.*|lyrical\b.*|lyrics?\s+video\b.*|"
    r"video\s+song\b.*|audio\s+song\b.*|with\s+lyrics\b.*)$", re.I)
_HASHTAG = re.compile(r"#\w+")
_SPACES = re.compile(r"\s+")


def norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def clean_title(raw: str | None, maxlen: int = 90) -> str:
    """Sahi, chhota gaane ka naam. Kuch bhi bigde to original (saaf kiya hua) wapas aata hai."""
    raw = _SPACES.sub(" ", (raw or "").replace("&amp;", "&").replace("&quot;", '"')).strip()
    if not raw:
        return ""
    t = _BRACKET_JUNK.sub(" ", raw)
    t = _HASHTAG.sub(" ", t)
    parts = [p.strip() for p in re.split(r"\s+[|｜]\s+|[|｜]", t) if p.strip()]
    if parts:
        t = parts[0] if len(parts[0]) >= 2 or len(parts) == 1 else parts[1]
    t = _TRAIL_JUNK.sub("", t)
    t = _SPACES.sub(" ", t).strip(" -–—_|:,.;")
    if not t:
        t = _SPACES.sub(" ", raw).strip()
    return t[:maxlen].rstrip()


def song_key(page: str | None) -> str | None:
    """Link se pakki key: YouTube video id / SoundCloud path. Link na ho to None."""
    if not page:
        return None
    try:
        u = urlparse(page.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    vid = None
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        vid = (parse_qs(u.query).get("v") or [None])[0]
        if not vid:
            m = re.match(r"^/(?:shorts|embed|live|v)/([\w-]{11})", u.path)
            vid = m.group(1) if m else None
    if vid and re.fullmatch(r"[\w-]{11}", vid):
        return "yt:" + vid
    if host.endswith("soundcloud.com") and u.path.strip("/"):
        return "sc:" + u.path.strip("/").lower()[:150]
    return None


def radio_key(title: str, artist: str = "") -> str:
    return "r:" + norm(title) + "|" + norm(artist)


def title_key(title: str | None) -> str:
    return "t:" + norm(title)[:120]


def rating_weight(rating: int) -> float:
    """-10 .. +10 ke beech: har +3 par chance double, har -3 par aadha."""
    r = max(-10, min(10, int(rating)))
    return 2.0 ** (r / 3.0)


def weighted_sample(items: list, weights: list[float], k: int) -> list:
    """k alag-alag items, weights ke hisaab se (bina repeat)."""
    pool, w = list(items), list(weights)
    out = []
    for _ in range(min(k, len(pool))):
        i = random.choices(range(len(pool)), weights=w)[0]
        out.append(pool.pop(i))
        w.pop(i)
    return out
