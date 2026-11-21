"""Kharcha (cost) tracker: kaun sa feature aur kaun sa gaana kitna bill bana raha hai.

Ye ANUMAAN (estimate) hai, Railway ka asli invoice nahi. Tarika:
  - Har 15 sec par process (+ ffmpeg wagairah child) ka CPU time aur RAM naapte hain.
  - Bot ki "idle" RAM/CPU = BASE (bot chalu rakhne ka kharcha, kisi gaane ka nahi).
  - Idle se jo ZYADA CPU/RAM lagta hai wo us waqt chal rahe kaam (streaming, download, upload,
    card, broadcast) me barabar baant diya jata hai, aur gaane ke naam par bhi likha jata hai.
  - Egress (bahar jane wala data): streaming = seconds x AUDIO_KBPS, upload/card = file size.
Raw usage DB me save hota hai, dollar panel me rates se nikalte hain (rates env se badal sakte ho).
"""
import asyncio
import itertools
import logging
import os
import resource
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import db

try:
    import psutil
except ImportError:  # optional: bina psutil ke sirf python process ki RAM/CPU
    psutil = None

log = logging.getLogger("costs")

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
# Railway ke rates (USD). Pricing badle to env me badlo.
RAM_GB_MONTH = float(os.environ.get("COST_RAM_GB_MONTH", "10"))
CPU_VCPU_MONTH = float(os.environ.get("COST_CPU_VCPU_MONTH", "20"))
EGRESS_GB = float(os.environ.get("COST_EGRESS_GB", "0.05"))
COST_SCALE = float(os.environ.get("COST_SCALE", "1"))  # Railway usage se milane ke liye (calibration)
STREAM_KBPS = float(os.environ.get("AUDIO_KBPS", "64"))  # ek chalte gaane ka andaza (kilobit/sec)
INTERVAL = 15
FLUSH_EVERY = 4  # samples (= 60 sec)
HOURS_MONTH = 730.0
GIB = 1024.0 ** 3

LABELS = {
    "base": "Idle bot (always on: RAM + Telegram connection)",
    "stream_play": "/play streaming (ffmpeg + voice chat)",
    "stream_radio": "Radio streaming (ffmpeg + voice chat)",
    "download": "Song download (YouTube / storage channel)",
    "upload": "Upload to storage channel",
    "card": "Play card image + thumbnail",
    "broadcast": "Broadcast to all groups",
}

# ---------- jo kaam abhi chal rahe hain ----------
_jobs: dict[int, tuple[str, object]] = {}  # id -> (feature, getter)
_ids = itertools.count(1)


@contextmanager
def job(feature: str, getter=None):
    """with costs.job("download", lambda: (key, title, kind)): ... -- is dauran ka CPU/RAM is kaam ko jata hai."""
    i = next(_ids)
    _jobs[i] = (feature, getter)
    try:
        yield
    finally:
        _jobs.pop(i, None)


# ---------- jama (RAM me), har minute DB me ----------
_feat: dict[tuple[str, str], list[float]] = {}  # (day, feature) -> [cpu_s, ram_gbh, egr_gb]
_song: dict[str, list] = {}  # key -> [title, kind, stream_s, cpu_s, ram_gbh, egr_gb]
_state = {"t": None, "cpu": None, "base_rss": None, "idle_cpu": 0.0, "n": 0}


def _day() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def _add(feature: str, ident, cpu=0.0, ram=0.0, egr=0.0, stream_s=0.0):
    f = _feat.setdefault((_day(), feature), [0.0, 0.0, 0.0])
    f[0] += cpu
    f[1] += ram
    f[2] += egr
    if ident:
        key, title, kind = ident
        s = _song.setdefault(key, [title, kind, 0.0, 0.0, 0.0, 0.0])
        if title:
            s[0] = title
        s[2] += stream_s
        s[3] += cpu
        s[4] += ram
        s[5] += egr


def add_egress(feature: str, getter, nbytes: int):
    """Upload / card jaise kaam: file ka size seedha egress me."""
    try:
        _add(feature, getter() if getter else None, egr=max(0, nbytes) / GIB)
    except Exception as ex:
        log.debug("add_egress failed: %s", ex)


# ---------- naap ----------
def _cpu_total() -> float:
    s, c = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    t = s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime  # band ho chuke child
    if psutil:
        try:
            for ch in psutil.Process().children(recursive=True):  # abhi chal rahe child (ffmpeg)
                try:
                    ct = ch.cpu_times()
                    t += ct.user + ct.system
                except Exception:
                    pass
        except Exception:
            pass
    return t


def _rss() -> float:
    """Bytes: bot + uske child process (ffmpeg, deno)."""
    if psutil:
        try:
            p = psutil.Process()
            tot = p.memory_info().rss
            for ch in p.children(recursive=True):
                try:
                    tot += ch.memory_info().rss
                except Exception:
                    pass
            return float(tot)
        except Exception:
            pass
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) * 1024
    except Exception:
        pass
    return 0.0


def _sample(streams: list[tuple]):
    now = time.time()
    cpu_now, rss = _cpu_total(), _rss() / GIB
    if _state["t"] is None:
        _state.update(t=now, cpu=cpu_now, base_rss=rss)
        return
    dt = now - _state["t"]
    cpu = max(0.0, cpu_now - _state["cpu"])
    _state["t"], _state["cpu"] = now, cpu_now
    if dt <= 0:
        return

    items = []  # (feature, ident)
    for ident in streams:
        items.append(("stream_radio" if ident[2] == "radio" else "stream_play", ident))
    for feature, getter in list(_jobs.values()):
        try:
            items.append((feature, getter() if getter else None))
        except Exception:
            items.append((feature, None))

    base = _state["base_rss"] or rss
    if not items:  # kuch nahi chal raha: ye bot ka asli idle kharcha hai
        _state["base_rss"] = base = (0.9 * base + 0.1 * rss) if _state["n"] else rss
        _state["idle_cpu"] = 0.8 * _state["idle_cpu"] + 0.2 * (cpu / dt)
    _state["n"] += 1
    base_rss = min(rss, base)
    base_cpu = cpu if not items else min(cpu, _state["idle_cpu"] * dt)
    _add("base", None, cpu=base_cpu, ram=base_rss * dt / 3600.0)
    if not items:
        return
    n = len(items)
    work_cpu = cpu - base_cpu
    work_ram = max(0.0, rss - base_rss) * dt / 3600.0
    egr_stream = dt * STREAM_KBPS * 1000.0 / 8.0 / GIB
    for feature, ident in items:
        is_stream = feature.startswith("stream_")
        _add(feature, ident, cpu=work_cpu / n, ram=work_ram / n,
             egr=egr_stream if is_stream else 0.0, stream_s=dt if is_stream else 0.0)

    if _state["n"] % FLUSH_EVERY == 0:
        flush()


def flush():
    """RAM me jama usage DB me jodo (additive)."""
    if not _feat and not _song:
        return
    feats, songs = dict(_feat), dict(_song)
    _feat.clear()
    _song.clear()
    try:
        db.cost_flush(feats, songs)
    except Exception as ex:
        log.warning("cost flush failed: %s", ex)


async def loop(streams_fn):
    """streams_fn() -> [(key, title, kind)] jo gaane abhi sach me baj rahe hain."""
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await asyncio.to_thread(_sample, list(streams_fn()))
        except Exception as ex:
            log.warning("cost sample failed: %s", ex)


# ---------- panel ----------
def _usd(cpu_s: float, ram_gbh: float, egr_gb: float) -> float:
    return COST_SCALE * (ram_gbh * RAM_GB_MONTH / HOURS_MONTH
                         + (cpu_s / 3600.0) * CPU_VCPU_MONTH / HOURS_MONTH
                         + egr_gb * EGRESS_GB)


def panel(tzname: str = TIMEZONE) -> dict:
    flush()
    now = datetime.now(ZoneInfo(tzname))
    today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    rows = db.cost_features()  # [(day, feature, cpu, ram, egr)]
    feats: dict[str, dict] = {}
    for day, feat, cpu, ram, egr in rows:
        d = feats.setdefault(feat, {"today": 0.0, "month": 0.0, "total": 0.0,
                                    "cpu_h": 0.0, "ram_gbh": 0.0, "egr_gb": 0.0})
        c = _usd(cpu, ram, egr)
        d["total"] += c
        d["cpu_h"] += cpu / 3600.0
        d["ram_gbh"] += ram
        d["egr_gb"] += egr
        if day.startswith(month):
            d["month"] += c
        if day == today:
            d["today"] += c
    order = list(LABELS)
    out = [dict(feature=LABELS.get(k, k), **{kk: round(v, 4) for kk, v in feats[k].items()})
           for k in order if k in feats]
    tot = {k: round(sum(f[k] for f in feats.values()), 4) for k in ("today", "month", "total")}
    days_in = max(1, now.day)
    tot["est_month"] = round(tot["month"] / days_in * 30, 2)
    a, b, c = (COST_SCALE * RAM_GB_MONTH / HOURS_MONTH, COST_SCALE * CPU_VCPU_MONTH / HOURS_MONTH / 3600.0,
               COST_SCALE * EGRESS_GB)
    songs = []
    for key, title, kind, plays, stream_s, cpu, ram, egr in db.cost_songs(a, b, c, 40):
        cost = _usd(cpu, ram, egr)
        songs.append({"title": title or key, "kind": kind, "plays": plays,
                      "stream_min": round(stream_s / 60.0, 1), "egr_mb": round(egr * 1024, 1),
                      "cost": round(cost, 4),
                      "per_play": round(cost / plays, 5) if plays else None})
    return {"features": out, "totals": tot, "songs": songs,
            "rates": {"ram_gb_month": RAM_GB_MONTH, "cpu_vcpu_month": CPU_VCPU_MONTH,
                      "egress_gb": EGRESS_GB, "kbps": STREAM_KBPS, "scale": COST_SCALE}}
