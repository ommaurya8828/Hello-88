import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import csv
import hashlib
import html
import inspect
import io
import logging
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.types import (
    CallbackQuery,
    BotCommand,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, MediaStream

import card
import costs
import db
import media
import panel
import songtools
from errors import diagnose

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tgmusic")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SONGS_CSV = "songs.csv"
RADIO_COUNT = 15  # radio plays this many songs once, then stops (no auto-refill)



def _client_kw() -> dict:
    """1000 group ke liye: Telegram se file upload/download ek-ek karke nahi (storage channel),
    aur chhoti FloodWait par bot khud ruk kar dobara koshish kare. Purani library me ye option na ho to chhod do."""
    ok = inspect.signature(Client.__init__).parameters
    kw = {}
    if "max_concurrent_transmissions" in ok:
        kw["max_concurrent_transmissions"] = int(os.environ.get("TG_TRANSMISSIONS", "8"))
    if "sleep_threshold" in ok:
        kw["sleep_threshold"] = int(os.environ.get("TG_SLEEP_THRESHOLD", "30"))
    return kw


_SEND_PARAMS = inspect.signature(Client.send_message).parameters


def nopreview() -> dict:
    """Link preview band karne ka kwarg (library version ke hisaab se)."""
    if "disable_web_page_preview" in _SEND_PARAMS:
        return {"disable_web_page_preview": True}
    if "link_preview_options" in _SEND_PARAMS:
        try:
            from pyrogram.types import LinkPreviewOptions
            return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        except Exception:
            pass
    return {}


bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True, **_client_kw())


# ---------------- Assistant pool ----------------
# One Telegram user account can only be in so many groups / join so fast. For big scale put
# several accounts in SESSION_STRINGS (comma or newline separated). Each group is assigned to
# the assistant that currently has the fewest groups and keeps it.
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)  # gets private alerts (dead assistant ...)
IDLE_LEAVE_DAYS = int(os.environ.get("IDLE_LEAVE_DAYS", "30"))  # 0 = assistant never leaves idle groups
RESUME_WAIT = int(os.environ.get("RESUME_WAIT", "300"))  # seconds we wait for a manually added assistant
ASSISTANT_MAX_GROUPS = int(os.environ.get("ASSISTANT_MAX_GROUPS", "450"))  # Telegram limit ~500 groups per account
FULL_REST = 6 * 3600  # an assistant that is "in too many groups" is not used for this long
DEAD_ERRORS = ("AuthKeyUnregistered", "SessionRevoked", "SessionExpired", "UserDeactivated",
               "AuthKeyDuplicated")


class Assistant:
    def __init__(self, idx: int, session: str):
        self.idx = idx
        self.client = Client(f"assistant{idx}", api_id=API_ID, api_hash=API_HASH,
                             session_string=session, in_memory=True, **_client_kw())
        self.call = PyTgCalls(self.client)
        self.join_lock = asyncio.Lock()  # joins are spaced out -> no FloodWait
        self.chats = 0
        self.uid = 0  # Telegram user id (known after start)
        self.dead = False  # session revoked / account deactivated / failed to start
        self.full_until = 0.0  # "too many groups": rests until this time
        self.flood_until = 0.0  # FloodWait: rests until this time
        self.last_error = ""

    @property
    def tag(self) -> str:
        me = getattr(self.client, "me", None)
        return f"@{me.username}" if me and getattr(me, "username", None) else f"assistant {self.idx + 1}"

    @property
    def healthy(self) -> bool:
        now = time.time()
        return not self.dead and self.full_until <= now and self.flood_until <= now

    @property
    def status(self) -> str:
        now = time.time()
        if self.dead:
            return "dead"
        if self.full_until > now:
            return "full"
        if self.flood_until > now:
            return "flood"
        return "ok"


_sessions: list[str] = []
for _s in re.split(r"[,\s]+", (os.environ.get("SESSION_STRINGS", "") + "," +
                               os.environ.get("SESSION_STRING", ""))):
    if _s and _s not in _sessions:
        _sessions.append(_s)
if not _sessions:
    raise SystemExit("Set SESSION_STRING (or SESSION_STRINGS for several assistants).")
ASSISTANTS = [Assistant(i, s) for i, s in enumerate(_sessions)]
ASSISTANT_BY_ID: dict[int, Assistant] = {}  # filled after the assistants started
_assigned: dict[int, Assistant] = {}  # chat -> assistant (also saved in the DB, survives restarts)


def _pick_assistant(exclude=()) -> Assistant | None:
    """Healthy assistant with the fewest groups; if none is healthy, any that is not dead."""
    alive = [a for a in ASSISTANTS if not a.dead and a not in exclude]
    if not alive:
        return None
    roomy = [a for a in alive if a.healthy and a.chats < ASSISTANT_MAX_GROUPS]
    return min(roomy or [a for a in alive if a.healthy] or alive, key=lambda x: x.chats)


def _set_assignment(chat_id: int, a: Assistant):
    old = _assigned.get(chat_id)
    if old is a:
        return
    if old is not None:
        old.chats = max(0, old.chats - 1)
    _assigned[chat_id] = a
    a.chats += 1
    _joined.discard(chat_id)  # new assistant: membership must be checked again
    if a.uid:
        db.assign_put(chat_id, a.uid)
    live = [x for x in ASSISTANTS if not x.dead]
    used, cap = sum(x.chats for x in live), ASSISTANT_MAX_GROUPS * len(live)
    if cap and used >= 0.9 * cap:  # jagah khatam hone wali hai: owner ko batao
        try:
            spawn(notify_owner("capacity", f"⚠️ Assistants {used}/{cap} groups tak bhar gaye. Naya SESSION_STRING "
                                           "(SESSION_STRINGS me) jodo, warna naye groups me gaana nahi chalega.", 6 * 3600))
        except RuntimeError:
            pass


def _peek_assistant(chat_id: int) -> Assistant:
    """Like assistant_for() but never creates / saves an assignment (used by /setup)."""
    return _assigned.get(chat_id) or _pick_assistant() or ASSISTANTS[0]


def assistant_for(chat_id: int) -> Assistant:
    a = _assigned.get(chat_id)
    if a is None or a.dead:  # new group, or its assistant died -> give it a living one
        a = _pick_assistant() or ASSISTANTS[0]
        _set_assignment(chat_id, a)
    return a


def call_for(chat_id: int) -> PyTgCalls:
    return assistant_for(chat_id).call


def call_existing(chat_id: int) -> PyTgCalls:
    """For pause/resume: raises if this group never had an assistant (nothing is playing)."""
    a = _assigned.get(chat_id)
    if a is None:
        raise RuntimeError("no assistant for this chat")
    return a.call


def load_assignments():
    """After a restart: which assistant belongs to which group (saved in the DB)."""
    n = 0
    for chat_id, aid, _last in db.assign_all():
        a = ASSISTANT_BY_ID.get(aid)
        if a is not None:
            _assigned[chat_id] = a
            a.chats += 1
            n += 1
    log.info("assistant assignments restored: %s", n)


_notice_last: dict[tuple, float] = {}  # (key...) -> last time: stops repeated notices


def _once(key: tuple, every: float = 600) -> bool:
    """True if this notice was not sent within the last `every` seconds."""
    now = time.time()
    if now - _notice_last.get(key, 0) < every:
        return False
    if len(_notice_last) > 5000:
        _notice_last.clear()
    _notice_last[key] = now
    return True


# Sab kuch RAM me. Restart par sab saaf, koi record nahi.
queues: dict[int, list[dict]] = {}
loops: dict[int, bool] = {}
radio_src: dict[int, tuple[list, str]] = {}  # chat -> (songs, label): radio refills itself
dm_state: dict[int, dict] = {}  # radio menu state: user_id -> {"chat", "lang"}
BOT_USERNAME = ""
BOT_ID = 0

# ---------------- Radio data (songs.csv) ----------------
# songs.csv columns: title, artist, language, mood   (mood: "Romantic|Sad" = song fits both feelings)
# Artist is NOT shown anywhere: only used silently to find the right song on YouTube.
LANG_ORDER = ["Hindi", "Punjabi", "Haryanvi", "Bhojpuri", "Tamil", "English"]
HIDDEN_MOODS = {m.strip().lower() for m in os.environ.get("HIDDEN_MOODS", "").split(",") if m.strip()}
MOOD_EMOJI = {"romantic": "💕", "sad": "😢", "chill": "😌", "party": "🎉", "party/festival": "🎉",
              "attitude": "😎", "dance": "💃", "melody": "🎶", "18+": "🔞"}
LANGS: list[str] = []
MOODS: dict[str, list[str]] = {}  # lang -> feelings (most songs first)
BY_MOOD: dict[str, dict[str, list[tuple]]] = {}  # lang -> mood -> [(title, artist, lang)]
LANG_ALL: dict[str, list[tuple]] = {}  # lang -> every song (for Mix)


def load_songs():
    if not os.path.exists(SONGS_CSV):
        log.warning("songs.csv not found, /radio disabled")
        return
    tmp: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    allsongs: dict[str, list] = defaultdict(list)
    with open(SONGS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            title, art, lang = r["title"].strip(), (r.get("artist") or "").strip(), r["language"].strip()
            if not (title and lang):
                continue
            moods = [m.strip() for m in (r.get("mood") or "").split("|") if m.strip()]
            shown = [m for m in moods if m.lower() not in HIDDEN_MOODS]
            if moods and not shown:
                continue  # only hidden feelings (e.g. 18+): not played anywhere, not even in Mix
            song = (title, art, lang)
            allsongs[lang].append(song)
            for m in shown:
                tmp[lang][m].append(song)
    langs = [l for l in LANG_ORDER if l in allsongs] + [l for l in allsongs if l not in LANG_ORDER]
    LANGS.extend(langs)
    for l in langs:
        BY_MOOD[l] = dict(tmp[l])
        MOODS[l] = sorted(tmp[l], key=lambda m: (-len(tmp[l][m]), m.lower()))
        LANG_ALL[l] = allsongs[l]


load_songs()


# ---------------- Player ----------------
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "30"))  # songs per group
PLAY_COOLDOWN = float(os.environ.get("PLAY_COOLDOWN", "3"))  # seconds between /play of one user
CONTROLS_ADMIN_ONLY = os.environ.get("CONTROLS_ADMIN_ONLY", "1") == "1"  # pause/skip/stop/loop
STALL_GRACE = int(os.environ.get("STALL_GRACE", "20"))  # watchdog: seconds past song end
MAX_ACTIVE_GROUPS = int(os.environ.get("MAX_ACTIVE_GROUPS", "200"))  # groups jahan ek saath gaana baj sakta hai
MAX_WAITERS = int(os.environ.get("MAX_WAITERS", "150"))  # "assistant ka wait" karne wale groups ki had
AUDIO_QUALITY = getattr(AudioQuality, os.environ.get("AUDIO_QUALITY", "HIGH").upper(), AudioQuality.HIGH)

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # one lock per group: no queue races
_last_play: dict[int, float] = {}  # user_id -> last /play time (anti-spam)
_joined: set[int] = set()  # groups where the assistant is already a member
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}
_bg: set[asyncio.Task] = set()  # keeps background tasks alive


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


media.in_use_provider = lambda: {s["file"] for q in queues.values() for s in q if s.get("file")}


async def say(chat_id: int, text: str, **kw):
    """send_message that never raises (FloodWait, no permission, kicked...)."""
    try:
        return await bot.send_message(chat_id, text, **kw)
    except Exception as ex:
        log.warning("send_message to %s failed: %s %s", chat_id, type(ex).__name__, ex)
        return None


def report(chat_id: int | None, where: str, ex: Exception, song: dict | None = None):
    """Single place where every error is classified, logged (Railway logs + admin panel) and
    returned as a Diag (code / user text / fix hint). Unknown errors also get a full traceback."""
    d = diagnose(ex)
    if d.code == "IGNORE_NOT_MODIFIED":
        return d
    detail = f"{type(ex).__name__}: {ex}"
    if song:
        detail += f" | song={song.get('title') or song.get('query') or song.get('page')}"
    log.warning("[%s] %s | chat=%s | %s | FIX: %s", d.code, where, chat_id, detail, d.hint,
                exc_info=ex if d.code == "UNKNOWN" else None)
    try:
        db.log_error(chat_id, where, ex, code=d.code, hint=d.hint, detail=detail)
    except Exception:
        pass
    return d


try:  # coloured buttons (Bot API 9.4+). Older library -> normal buttons, nothing breaks.
    from pyrogram.enums import ButtonStyle
except Exception:
    ButtonStyle = None
_STYLE_OK = (os.getenv("BUTTON_COLORS", "1") != "0" and ButtonStyle is not None
             and "style" in inspect.signature(InlineKeyboardButton.__init__).parameters)


def ibtn(text: str, style: str | None = None, **kw) -> InlineKeyboardButton:
    """Inline button; style = 'primary' (blue) | 'success' (green) | 'danger' (red) when the library supports it."""
    if style and _STYLE_OK:
        st = getattr(ButtonStyle, style.upper(), None)
        if st is not None:
            try:
                return InlineKeyboardButton(text, style=st, **kw)
            except Exception as ex:
                log.debug("button style not accepted (%s)", ex)
    return InlineKeyboardButton(text, **kw)


def _unstyled(markup):
    """Same keyboard without colours (used if Telegram/library rejects styled buttons)."""
    try:
        return InlineKeyboardMarkup([[InlineKeyboardButton(b.text, callback_data=b.callback_data, url=b.url)
                                      for b in row] for row in markup.inline_keyboard])
    except Exception:
        return markup


def buttons(chat_id: int) -> InlineKeyboardMarkup:
    looping = bool(loops.get(chat_id))
    lp = "🔂 Loop ON" if looping else "🔁 Loop"
    rows = [[
        ibtn("⏸ Pause", "primary", callback_data="pause"),
        ibtn("▶️ Resume", "success", callback_data="resume"),
        ibtn("⏭ Skip", "primary", callback_data="skip"),
    ], [
        ibtn(lp, "success" if looping else "primary", callback_data="loop"),
        ibtn("⏹ Stop", "danger", callback_data="stop"),
    ]]
    q = queues.get(chat_id)
    cur = q[0] if q else None
    if cur and cur.get("radio") and cur.get("tok"):  # radio: 👍 Like = rating +1
        rows.append([ibtn("👍 Like", "success", callback_data=f"like:{cur['tok']}")])
    return InlineKeyboardMarkup(rows)


def fmt_dur(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "Live"
    return f"{sec // 60}:{sec % 60:02d} min"


_SC = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ")


def _sc(text: str) -> str:
    """Small-caps look (ᴛɪᴛʟᴇ) for the labels."""
    return text.lower().translate(_SC)


def now_playing_text(song: dict, quote: bool = True) -> str:
    """Caption under the play card. quote=True: two Telegram quote boxes (blue bar), quote=False: old plain look."""
    raw = (song.get("title") or song.get("page") or "Unknown")[:150]  # short enough that the caption never gets cut mid-tag
    title = html.escape(raw)
    page = song.get("page")
    if page and str(page).startswith("http"):
        title = f'<a href="{html.escape(str(page), quote=True)}">{title}</a>'  # blue clickable title
    by = html.escape(str(song.get("by") or ""))
    rating = ""  # rating is hidden from users (still counted in the DB / admin panel)
    if not quote:
        extra = f"\n<b>Rating:</b> {rating:+d}" if rating != "" else ""
        return (f"<u><b>Started streaming</b></u>\n\n"
                f"<b>Title:</b> {title}\n\n"
                f"<b>Duration:</b> {fmt_dur(song.get('duration'))}\n"
                f"<b>Requested by:</b> {by}{extra}")
    lines = [f"○ <b>{_sc('Title')} :</b> {title}",
             f"○ <b>{_sc('Duration')} :</b> {fmt_dur(song.get('duration'))}",
             f"○ <b>{_sc('By')} :</b> {by}"]
    if rating != "":
        lines.append(f"○ <b>{_sc('Rating')} :</b> {rating:+d}")
    return (f"<blockquote>❖ <b>{_sc('Started')} {_sc('Streaming')}</b></blockquote>\n"
            f"<blockquote>" + "\n".join(lines) + "</blockquote>")


_thumb_cache: dict[str, bytes] = {}


async def song_photo(song: dict) -> bytes | None:
    """YouTube thumbnail (the song's real photo) for the play card. None if not available -> card uses its own art."""
    key = songtools.song_key(song.get("page"))
    if not key or not key.startswith("yt:"):
        return None
    vid = key[3:]
    if vid in _thumb_cache:
        return _thumb_cache[vid]
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as ses:
            for name in ("maxresdefault", "hqdefault"):  # HD first, normal as fallback
                async with ses.get(f"https://i.ytimg.com/vi/{vid}/{name}.jpg") as r:
                    if r.status != 200:
                        continue
                    data = await r.read()
                    if len(data) < 3000:  # tiny grey placeholder, not a real photo
                        continue
                    if len(_thumb_cache) >= 64:
                        _thumb_cache.pop(next(iter(_thumb_cache)))
                    _thumb_cache[vid] = data
                    return data
    except Exception as ex:
        log.debug("thumbnail fetch failed (%s %s)", type(ex).__name__, ex)
    return None


async def say_card(chat_id: int, song: dict, **kw):
    """Now-playing image (blurred song photo + photo in a box) with the text in quote boxes as caption.
    Fallbacks: plain caption -> plain text message."""
    text = now_playing_text(song)
    plain = now_playing_text(song, quote=False)
    try:
        with costs.job("card", lambda: _cost_ident(song)):
            photo = await song_photo(song)
            img = await asyncio.to_thread(card.make_card, song.get("title") or song.get("page") or "",
                                          fmt_dur(song.get("duration")), str(song.get("by") or ""),
                                          song.get("skey") or "", photo)
    except Exception as ex:
        log.warning("play card image failed (%s %s), sending text", type(ex).__name__, ex)
        img = None
    if img:
        attempts = [(text, kw), (plain, kw)]  # quote boxes first; if rejected, the old caption
        if _STYLE_OK and kw.get("reply_markup") is not None:  # last try: same card, buttons without colours
            attempts.append((plain, {**kw, "reply_markup": _unstyled(kw["reply_markup"])}))
        for cap, k in attempts:
            try:
                bio = io.BytesIO(img)
                bio.name = "card.jpg"
                costs.add_egress("card", lambda: _cost_ident(song), len(img))
                return await bot.send_photo(chat_id, bio, caption=cap[:1000], parse_mode=ParseMode.HTML, **k)
            except Exception as ex:
                log.warning("play card send failed (%s %s)", type(ex).__name__, ex)
    return await say(chat_id, plain, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw)


async def is_admin(chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and time.time() - hit[0] < 60:  # cache: fewer Telegram API calls
        return hit[1]
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        ok = m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        ok = False
    if len(_admin_cache) > 5000:
        _admin_cache.clear()
    _admin_cache[key] = (time.time(), ok)
    return ok


async def can_control(chat_id: int, user) -> bool:
    if not CONTROLS_ADMIN_ONLY:
        return True
    return user is not None and await is_admin(chat_id, user.id)


async def msg_can_control(m: Message) -> bool:
    if m.sender_chat and m.sender_chat.id == m.chat.id:  # anonymous admin
        return True
    return await can_control(m.chat.id, m.from_user)


async def _deny(m: Message) -> bool:
    """True (and replies) if the sender may not control playback."""
    if await msg_can_control(m):
        return False
    await m.reply_text("🔒 Only admins can control playback.")
    return True


async def _assistant_member(a: Assistant, chat_id: int):
    """The assistant's ChatMember in the group (None = not in the group / unknown). Asked twice:
    by the assistant itself and, if that fails (assistant has no cached info about the group after
    a restart), by the bot."""
    try:
        return await a.client.get_chat_member(chat_id, "me")
    except Exception:
        pass
    try:
        uid = a.uid or (a.client.me.id if a.client.me else (await a.client.get_me()).id)
        return await bot.get_chat_member(chat_id, uid)
    except Exception:
        return None


async def _assistant_is_member(a: Assistant, chat_id: int) -> bool:
    m = await _assistant_member(a, chat_id)
    return m is not None and m.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)


async def _fresh_link(chat_id: int) -> str:
    """A NEW temporary invite link. export_chat_invite_link() revokes the group's main link every
    time, which breaks links in use (and caused INVITE_HASH_EXPIRED when joins overlapped)."""
    try:
        l = await bot.create_chat_invite_link(chat_id, name="assistant",
                                              expire_date=datetime.now() + timedelta(minutes=10))
        return l.invite_link
    except Exception:
        return await bot.export_chat_invite_link(chat_id)


async def _join(a: Assistant, chat_id: int):
    def already(ex: Exception) -> bool:
        return type(ex).__name__ == "UserAlreadyParticipant"

    try:  # public group: join by @username, no invite link needed
        chat = await bot.get_chat(chat_id)
        if getattr(chat, "username", None):
            try:
                await a.client.join_chat(chat.username)
                return
            except Exception as ex:
                if already(ex):
                    return
    except Exception:
        pass
    last: Exception | None = None
    for _ in range(2):  # expired link -> ask for a fresh one once
        link = await _fresh_link(chat_id)
        try:
            await a.client.join_chat(link)
            return
        except Exception as ex:
            if already(ex):
                return
            if type(ex).__name__ not in ("InviteHashExpired", "InviteHashInvalid"):
                raise
            last = ex
            await asyncio.sleep(1)
    if await _assistant_is_member(a, chat_id):  # link is dead because assistant is already inside
        return
    raise last  # type: ignore[misc]


async def notify_owner(key: str, text: str, every: float = 3600):
    """Private alert to OWNER_ID (he must have pressed /start in the bot once)."""
    if OWNER_ID and _once(("owner", key), every):
        await say(OWNER_ID, text)


def _mark_dead(a: Assistant, ex: Exception):
    if a.dead:
        return
    a.dead = True
    a.last_error = f"{type(ex).__name__}: {ex}"[:200]
    report(None, f"Assistant {a.tag} session is dead", ex)
    db.log_error(None, f"Assistant {a.tag} is dead", None, code="ASSISTANT_DEAD",
                 hint="Session revoked or account banned: make a new SESSION_STRING (gen_session.py)",
                 detail=a.last_error)
    spawn(notify_owner(f"dead:{a.idx}", f"🚨 Assistant {a.tag} is DEAD ({type(ex).__name__}). "
                                        "Make a new SESSION_STRING for it and redeploy.", 6 * 3600))


class AssistantBanned(Exception):
    """Assistant account is banned in this group (name has 'Banned' -> diagnose() = ASSISTANT_BANNED)."""

    def __init__(self, tag: str):
        super().__init__(f"{tag} is banned in this group")
        self.tag = tag


def banned_text(tag: str, wait_min: int | None = None) -> str:
    t = (f"🚫 My assistant {tag} is BANNED in this group, so I can't play music.\n"
         f"Please unban {tag}: Group info → Members/Administrators → Banned (Removed users) → Unban.")
    return t + (f"\n\n⏳ I will start your song by myself within {wait_min} min after you unban it."
                if wait_min else "\nThen try again.")


async def ensure_assistant(chat_id: int):
    cur = _assigned.get(chat_id)
    if chat_id in _joined and cur is not None and not cur.dead:
        return
    # Several assistants and no saved assignment: maybe one of them is already inside this group.
    if cur is None and len(ASSISTANTS) > 1:
        for cand in ASSISTANTS:
            if not cand.dead and await _assistant_is_member(cand, chat_id):
                _set_assignment(chat_id, cand)
                break
    tried: set[Assistant] = set()
    flood_retry = False
    for _ in range(len(ASSISTANTS) + 3):
        a = assistant_for(chat_id)
        am = await _assistant_member(a, chat_id)
        if am is not None and am.status == ChatMemberStatus.BANNED:
            tried.add(a)  # banned: join try karna bekaar. Dusra assistant ho to wo, warna saaf message
            other = _pick_assistant(exclude=tried)
            if other is None:
                raise AssistantBanned(a.tag)
            _set_assignment(chat_id, other)
            continue
        if am is not None and am.status != ChatMemberStatus.LEFT:
            break
        try:
            async with a.join_lock:
                await _join(a, chat_id)
                await asyncio.sleep(1.5)  # space out joins of one account (FloodWait protection)
            break
        except Exception as ex:
            name = type(ex).__name__
            other = None
            if name == "FloodWait":
                secs = int(getattr(ex, "value", 30) or 30)
                if secs <= 30 and not flood_retry:  # short wait: just wait and try once more
                    flood_retry = True
                    await asyncio.sleep(secs + 1)
                    continue
                a.flood_until = time.time() + secs
                a.last_error = f"FloodWait {secs}s"
            elif "ChannelsTooMuch" in name:  # this account cannot join more groups
                a.full_until = time.time() + FULL_REST
                a.last_error = "in too many groups"
                db.log_error(chat_id, f"Assistant {a.tag} is full", None, code="ASSISTANT_FULL",
                             hint="Add another SESSION_STRING (SESSION_STRINGS) or lower IDLE_LEAVE_DAYS",
                             detail=f"{name}: {ex}")
            elif any(k in name for k in DEAD_ERRORS):
                _mark_dead(a, ex)
            else:
                raise  # banned / no invite right / join request ...: caller shows the reason
            tried.add(a)
            other = _pick_assistant(exclude=tried)
            if other is None or not other.healthy:
                raise
            _set_assignment(chat_id, other)  # another assistant takes this group
    else:
        raise RuntimeError("no assistant could join this group")
    _joined.add(chat_id)


def join_error_text(ex: Exception, chat_id: int) -> str:
    d = diagnose(ex)
    tag = getattr(ex, "tag", None) or assistant_for(chat_id).tag
    if d.code == "ASSISTANT_BANNED":
        return banned_text(tag)
    if d.code == "UNKNOWN":
        return ("❌ I could not bring my assistant into this group. Make me admin (with 'Invite "
                f"users' permission), or add {tag} manually, and try again. [JOIN_FAILED]")
    return f"❌ {d.user} (assistant: {tag}) [{d.code}]"


# ---------------- Song storage in a Telegram channel ----------------
# STORAGE_CHANNEL = id of a private channel where the bot is admin (e.g. -1001234567890).
# (RADIO songs only.) First time a song is played it is downloaded from YouTube, played, and uploaded to the channel
# (its file_id is saved in SQLite). Next time ANY group asks for the same song it is downloaded
# from Telegram (fast, no YouTube request, no cookie use). If anything fails -> YouTube is used.
STORAGE_CHANNEL = int(os.environ.get("STORAGE_CHANNEL", "0") or 0)
UPLOAD_GAP = float(os.environ.get("UPLOAD_GAP", "3"))  # seconds between two uploads (FloodWait)
MAX_UPLOAD_BACKLOG = int(os.environ.get("MAX_UPLOAD_BACKLOG", "40"))  # more waiting = skip (re-stored later)
_uploading: set[str] = set()
_upload_lock = asyncio.Lock()  # one upload at a time, with a pause after each


def _cache_key(song: dict) -> str | None:
    if not song.get("radio"):  # only RADIO songs use the storage channel, /play never does
        return None
    if song.get("query"):
        return "q:" + " ".join(song["query"].lower().split())
    if song.get("page"):
        return "u:" + song["page"]
    return None


async def _from_telegram(key: str, row: dict) -> str:
    name = "tg-" + hashlib.sha1(key.encode()).hexdigest()[:16] + row["ext"]
    path = os.path.join(media.DL_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        media._last_used[path] = time.time()
        return path

    async def job():
        tmp = path + ".part"
        got = await bot.download_media(row["file_id"], file_name=tmp)
        if not got or not os.path.exists(got) or os.path.getsize(got) == 0:
            raise RuntimeError("empty download from storage channel")
        os.replace(got, path)
        return path

    res = await media._dedupe(("t", key), job)
    media._last_used[res] = time.time()
    return res


async def _upload_to_channel(key: str, path: str, title: str | None, dur, page: str | None):
    if key in _uploading or len(_uploading) >= MAX_UPLOAD_BACKLOG:
        return
    _uploading.add(key)
    try:
        if db.tgcache_has(key):
            return
        async with _upload_lock:
            try:
                if not os.path.exists(path):  # cache cleanup removed it while we waited
                    return
                m = None
                _ident = lambda: (songtools.song_key(page) or key, title or key, "play")
                for attempt in range(2):
                    try:
                        with costs.job("upload", _ident):
                            m = await bot.send_audio(STORAGE_CHANNEL, path, title=(title or "")[:60],
                                                     duration=int(dur or 0),
                                                     caption=f"{key}\n{page or ''}"[:900])
                        costs.add_egress("upload", _ident, os.path.getsize(path) if os.path.exists(path) else 0)
                        break
                    except Exception as ex:
                        if type(ex).__name__ == "FloodWait" and attempt == 0:
                            await asyncio.sleep(int(getattr(ex, "value", 10) or 10) + 1)
                            continue
                        raise
            finally:
                await asyncio.sleep(UPLOAD_GAP)  # pause after every upload, even a failed one
        if m is None:
            return
        media_obj = m.audio or m.document
        if not media_obj:
            return
        await asyncio.to_thread(db.tgcache_put, key, media_obj.file_id, title, dur, page,
                                os.path.splitext(path)[1] or ".m4a")
        log.info("stored in channel: %s", key)
    except Exception as ex:
        report(None, "Storage upload failed", ex)
    finally:
        _uploading.discard(key)


def _cost_ident(song: dict) -> tuple:
    """(key, title, kind): admin panel ke kharcha table me gaane ki pehchaan (song_stats wali key)."""
    kind = "radio" if song.get("radio") else "play"
    key = (song.get("skey") or song.get("rkey") or songtools.song_key(song.get("page"))
           or songtools.title_key(song.get("title") or song.get("query")))
    return key, (song.get("title") or song.get("query") or key), kind


def _streams_now() -> list:
    """Jo gaane is waqt sach me baj rahe hain (pause nahi). costs.py har 15 sec me puchta hai."""
    out = []
    for q in list(queues.values()):
        try:
            if q and q[0].get("started") and not q[0].get("paused_at"):
                out.append(_cost_ident(q[0]))
        except Exception:
            pass
    return out


async def get_audio(song: dict) -> tuple[str, str, int | None]:
    """(local file, title, duration): Telegram storage first, YouTube/SoundCloud otherwise."""
    key = _cache_key(song)
    if STORAGE_CHANNEL and key:
        row = await asyncio.to_thread(db.tgcache_get, key)
        if row:
            try:
                with costs.job("download", lambda: _cost_ident(song)):
                    path = await _from_telegram(key, row)
                if row.get("page") and not song.get("page"):
                    song["page"] = row["page"]  # keeps the clickable title
                return path, row["title"] or song.get("title") or "song", row["duration"]
            except Exception as ex:
                log.warning("storage download failed (%s): %s %s", key, type(ex).__name__, ex)
                await asyncio.to_thread(db.tgcache_delete, key)  # stale file_id -> re-store below
    if not song.get("page"):
        with costs.job("download", lambda: _cost_ident(song)):
            found = await media.search(song["query"])
        if not found:
            raise media.MediaError("Song not found.")
        song["page"] = found["page"]
        song["duration"] = song.get("duration") or found.get("duration")
    with costs.job("download", lambda: _cost_ident(song)):
        path, title, dur = await media.fetch(song["page"])
    if STORAGE_CHANNEL and key:
        spawn(_upload_to_channel(key, path, title, dur or song.get("duration"), song["page"]))
    return path, title, dur


async def _prefetch(q: list):
    """Download the NEXT song while the current one plays -> no gap between songs."""
    if len(q) < 2:
        return
    nxt = q[1]
    try:
        path, _t, dur = await get_audio(nxt)
        nxt["file"] = path
        nxt["duration"] = nxt.get("duration") or dur
    except Exception as ex:
        log.debug("prefetch failed: %s", ex)


def _radio_items(songs: list[tuple], mood: str = "Mix") -> list[dict]:
    # rating (likes - skips) ke hisaab se: jyada liked gaane jyada baar aate hain
    weights = [songtools.rating_weight(db.song_rating(songtools.radio_key(t, a))) for t, a, _l in songs]
    picked = songtools.weighted_sample(songs, weights, RADIO_COUNT)
    # shown title = song name only (no singer); singer / language is used silently for a better YouTube search
    return [{"title": t, "page": None, "query": f"{t} {a}" if a else f"{t} {lang} song",
             "by": "📻 Radio", "radio": True, "lang": lang, "mood": mood,
             "rkey": songtools.radio_key(t, a)} for t, a, lang in picked]


def _refill_radio(chat_id: int, q: list) -> bool:
    """Radio does NOT refill: after its songs finish the bot leaves the voice chat."""
    return False


async def _play_locked(chat_id: int, announce: bool = True):
    """Play queue head. Caller must hold the chat lock."""
    q = queues.get(chat_id)
    fails = 0
    while q:
        if queues.get(chat_id) is not q:  # stopped meanwhile
            return
        song = q[0]
        try:
            # Search + download only right before playing (cache + dedupe live in media.py)
            path, real_title, dur = await get_audio(song)
            song["file"] = path
            song["title"] = song.get("title") or songtools.clean_title(real_title) or real_title
            song["duration"] = dur or song.get("duration")
            if queues.get(chat_id) is not q or not q or q[0] is not song:
                return  # stopped / skipped while downloading
            await call_for(chat_id).play(
                chat_id, MediaStream(path, audio_parameters=AUDIO_QUALITY,
                                     video_flags=MediaStream.Flags.IGNORE))
        except Exception as ex:
            d = report(chat_id, "Play failed", ex, song)
            if d.fatal:
                # e.g. no voice chat / no admin right: every next song would fail the same way,
                # so stop instead of skipping the whole queue and spamming the group.
                if d.code == "NO_VOICE_CHAT":  # sirf voice chat chalu karne ko bolo, admin banane ko nahi
                    await say(chat_id, "🎙 Voice chat is not started in this group.\n"
                                       "Please start the voice chat (Group info → Start voice chat), "
                                       "then send /play again. [NO_VOICE_CHAT]")
                else:
                    await say(chat_id, f"❌ {d.user} [{d.code}]")
                break
            if d.code == "ASSISTANT_REMOVED":
                _joined.discard(chat_id)  # assistant was removed, re-join next time
            title = song.get("title") or song.get("query") or song.get("page") or "song"
            await say(chat_id, f"⚠️ Couldn't play '{title}': {d.user} [{d.code}]")
            if q and q[0] is song:
                q.pop(0)
            if not q and _refill_radio(chat_id, q):
                pass  # radio never runs dry because of a bad song
            fails += 1
            if fails >= 3:  # avoid spam
                await say(chat_id, "⚠️ Too many errors in a row, stopping.")
                break
            continue
        # ---- playing: the "Now playing" message is separate, its failure must not skip the song
        song["started"] = time.time()
        if song.get("radio") and song.get("lang"):
            try:
                db.radio_count(song["lang"], song.get("mood") or "Mix", songs=1)  # admin panel: language stats
            except Exception as ex:
                log.debug("radio_count failed: %s", ex)
        db.assign_touch(chat_id)  # assistant is in use (idle-leave looks at this)
        song.pop("paused_at", None)
        song.pop("wd", None)
        if announce:
            try:  # sahi naam save + kitni baar baja (loop ka repeat nahi ginte)
                kind = "radio" if song.get("radio") else "play"
                skey = (song.get("rkey") or songtools.song_key(song.get("page"))
                        or songtools.title_key(song.get("title")))
                song["skey"] = skey
                song["tok"] = os.urandom(3).hex()
                song["plays"] = db.song_played(skey, song.get("title"), kind)
            except Exception as ex:
                log.debug("song stats failed: %s", ex)
            await say_card(chat_id, song, reply_markup=buttons(chat_id),
                           reply_to_message_id=song.get("msg_id"))  # colourful card, replies to /play
        spawn(_prefetch(q))
        return
    if queues.get(chat_id) is q:
        await stop_all(chat_id)


async def _play_safe(chat_id: int, announce: bool = True):
    """_play_locked that can never leave the queue stuck because of an unexpected exception."""
    try:
        await _play_locked(chat_id, announce)
    except Exception as ex:
        report(chat_id, "Player crashed", ex)
        await say(chat_id, "⚠️ Player error. Stopped, please /play again.")
        await stop_all(chat_id)


async def play_next(chat_id: int, announce: bool = True, status: Message | None = None):
    try:
        async with _locks[chat_id]:
            await _play_safe(chat_id, announce)
    finally:
        if status:
            try:
                await status.delete()
            except Exception:
                pass


async def stop_all(chat_id: int):
    queues.pop(chat_id, None)
    loops.pop(chat_id, None)
    radio_src.pop(chat_id, None)
    a = _assigned.get(chat_id)
    if a is not None:  # no assistant = nothing to leave (and no assignment gets created)
        try:
            await a.call.leave_call(chat_id)
        except Exception:
            pass


async def _on_stream_end(chat_id: int, expect: dict | None = None):
    async with _locks[chat_id]:
        q = queues.get(chat_id)
        if not q:
            return
        if expect is not None and q[0] is not expect:  # watchdog: song already changed
            return
        if loops.get(chat_id):
            await _play_safe(chat_id, announce=False)
            return
        q.pop(0)
        if q:
            await _play_safe(chat_id)
        else:
            was_radio = chat_id in radio_src
            await stop_all(chat_id)
            await say(chat_id, ("📻 Radio finished. Left the voice chat." if was_radio
                                else "✅ Queue finished. Left the voice chat.") + "\n\n" + CMD_HINT)


async def watchdog_loop():
    """If Telegram never tells us a stream ended (lost event), move on after duration + grace."""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for chat_id, q in list(queues.items()):
            try:
                if not q:
                    continue
                s = q[0]
                st, dur = s.get("started"), s.get("duration")
                if not st or not dur or s.get("paused_at") or s.get("wd"):
                    continue
                if now - st > dur + STALL_GRACE:
                    s["wd"] = True
                    log.warning("watchdog: stream stalled in %s, advancing", chat_id)
                    spawn(_on_stream_end(chat_id, expect=s))
            except Exception as ex:
                log.warning("watchdog error: %s", ex)


async def on_update(_, update):
    try:
        names = {c.__name__ for c in type(update).__mro__}
        if names & {"StreamEnded", "StreamAudioEnded"}:
            await _on_stream_end(update.chat_id)
        elif "UpdatedGroupCallParticipant" in names:
            await _on_vc_participant(update)  # someone joined / left the voice chat
        elif "ChatUpdate" in names:
            # bot kicked / voice chat closed: drop the queue so no stale state is left
            status = str(getattr(update, "status", ""))
            if any(k in status for k in ("KICKED", "LEFT_GROUP", "CLOSED_VOICE_CHAT", "DISCARDED_CALL")):
                queues.pop(update.chat_id, None)
                loops.pop(update.chat_id, None)
                radio_src.pop(update.chat_id, None)
                _joined.discard(update.chat_id)
    except Exception as ex:
        report(getattr(update, "chat_id", None), "on_update failed", ex)


for _a in ASSISTANTS:
    _a.call.on_update()(on_update)


# ---------------- Group add / leave tracking (admin panel) ----------------
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)
ACTIVE_STATUS = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def _enable_notices(chat_id: int):
    """Bot just added: welcome + voice chat notices start ON (a choice made earlier is kept)."""
    for key in ("welcome", "vcnotice", "vcjoin"):
        if not db.setting_get(chat_id, key, ""):
            db.setting_set(chat_id, key, "1")


@bot.on_chat_member_updated(group=5)
async def track_member(_, u: ChatMemberUpdated):
    try:
        if u.chat.type not in GROUP_TYPES:
            return
        old, new = u.old_chat_member, u.new_chat_member
        member = new or old
        if member and member.user.id in ASSISTANT_BY_ID:  # one of OUR assistants changed
            await assistant_member_changed(u, ASSISTANT_BY_ID[member.user.id])
            return
        if not member or member.user.id != BOT_ID:
            return
        if new is None or new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            db.mark_left(u.chat.id, u.chat.title)
        elif new.status in ACTIVE_STATUS:
            db.mark_added(u.chat.id, u.chat.title)
            _enable_notices(u.chat.id)
    except Exception as ex:
        log.warning("track error: %s", ex)


@bot.on_message(filters.group & (filters.new_chat_members | filters.left_chat_member), group=6)
async def track_service(_, m: Message):
    # Backup tracking (same event dobara aaye to db ignore kar deta hai)
    try:
        if m.new_chat_members and any(u.id == BOT_ID for u in m.new_chat_members):
            db.mark_added(m.chat.id, m.chat.title)
            _enable_notices(m.chat.id)
        elif m.left_chat_member and m.left_chat_member.id == BOT_ID:
            db.mark_left(m.chat.id, m.chat.title)
    except Exception as ex:
        log.warning("track error: %s", ex)


# ---------------- Commands ----------------
START_PHOTO = os.environ.get("START_PHOTO", "")  # URL / file_id; else assets/start.jpg is used
SUPPORT_URL = os.environ.get("SUPPORT_URL", "https://t.me/ZenithSsupport")
UPDATE_URL_ENV = os.environ.get("UPDATE_URL", "")  # update channel link (owner can also change it with /setupdate)


def update_url() -> str:
    """Update channel link: /setupdate (saved in DB) wins, else UPDATE_URL env."""
    return db.setting_get(0, "update_url", "") or UPDATE_URL_ENV
START_TEXT = (
    "🎧 <b>Powerful music bot, never falls!</b>\n\n"
    "Lag-free, high quality songs in your group voice chat.\n"
    "Add me to your group, make me admin and enjoy the music.\n\n"
    "<b>Tap Help to see everything I can do.</b>"
)
CMD_HINT = ("/play - play a song\n"
            "/radio - automatic trending songs")
HELP_TEXT = (
    "<b>Help - Commands</b>\n\n"
    "/play <i>song name or link</i> - play a song\n"
    "/pause - pause\n/resume - resume\n/skip - next song\n"
    "/stop - stop and leave\n/queue - show queue\n/loop - loop on/off\n"
    "/radio - 📻 automatic trending songs (choose language + feeling, admins only)\n\n"
    "👍 Like and ⏭ Skip on radio songs help me pick better songs.\n\n"
    "<b>Admins</b>\n"
    "/setup - check what is missing (bot rights, assistant, voice chat)\n"
    "/vcnotice on|off - voice chat start/end messages\n"
    "/vcjoin on|off - 'joined / invited to voice chat' messages (auto-deleted)\n"
    "/welcome on|off - welcome new members"
)
BOT_COMMANDS = [("play", "Play a song (name or link)"), ("pause", "Pause"), ("resume", "Resume"),
                ("skip", "Next song"), ("stop", "Stop and leave"), ("queue", "Show queue"),
                ("loop", "Loop on/off"),
                ("radio", "Automatic trending songs"), ("setup", "Check bot setup (admins)"),
                ("help", "All commands")]


async def reply_safe(m: Message, text: str, **kw):
    """Reply that never silently fails: HTML -> HTML without preview option -> plain text -> normal
    message (not a reply: works even if the command message was deleted by a moderation bot)."""
    plain = html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))
    attempts = [
        lambda: m.reply_text(text, parse_mode=ParseMode.HTML, **nopreview(), **kw),
        lambda: m.reply_text(text, parse_mode=ParseMode.HTML, **kw),
        lambda: m.reply_text(plain, **kw),
        lambda: bot.send_message(m.chat.id, plain, **kw),
    ]
    for n, fn in enumerate(attempts, 1):
        try:
            return await fn()
        except Exception as ex:
            log.warning("reply_safe attempt %s failed in %s: %s %s", n, m.chat.id, type(ex).__name__, ex)
    return None


def start_kb(back: bool = False) -> InlineKeyboardMarkup:
    if back:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])
    rows = [[ibtn(
        "➕ Add me to your group", "success",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=manage_video_chats+delete_messages+invite_users")]]
    row1 = [ibtn("📖 Help", "primary", callback_data="cmds")]
    if SUPPORT_URL:
        row1.append(ibtn("💬 Support", "primary", url=SUPPORT_URL))
    rows.append(row1)
    upd = update_url()
    if upd:
        rows.append([ibtn("📢 Update", "primary", url=upd)])
    return InlineKeyboardMarkup(rows)


def _start_photo():
    if START_PHOTO:
        return START_PHOTO
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "start.jpg")
    return p if os.path.exists(p) else None


async def _start_help(m: Message):
    photo = _start_photo()
    if photo:
        try:
            return await m.reply_photo(photo, caption=START_TEXT, parse_mode=ParseMode.HTML,
                                       reply_markup=start_kb())
        except Exception as ex:
            log.warning("start photo failed: %s", ex)
    await m.reply_text(START_TEXT, parse_mode=ParseMode.HTML, reply_markup=start_kb())


@bot.on_callback_query(filters.regex(r"^(cmds|home)$"))
async def start_cb(_, c: CallbackQuery):
    back = c.data == "cmds"
    text = HELP_TEXT if back else START_TEXT
    try:
        if c.message.photo:
            await c.message.edit_caption(text, parse_mode=ParseMode.HTML, reply_markup=start_kb(back))
        else:
            await c.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=start_kb(back))
    except Exception as ex:
        if type(ex).__name__ != "MessageNotModified":  # edit fail: naya message bhejo, button chup na rahe
            log.warning("help button edit failed: %s %s", type(ex).__name__, ex)
            await reply_safe(c.message, text, reply_markup=start_kb(back))
    await c.answer()


@bot.on_message(filters.command("start"))
async def start(_, m: Message):
    """/start: works in private chat AND in groups (group: text only, no photo, never fails silently)."""
    log.info("/start in chat %s (%s)", m.chat.id, m.chat.type)
    try:
        if m.chat.type in GROUP_TYPES:
            db.seen(m.chat.id, m.chat.title)
            return await reply_safe(m, START_TEXT, reply_markup=start_kb())
        if len(m.command) > 1 and m.command[1].startswith("radio_"):
            return await radio_menu(m)
        await _start_help(m)
    except Exception as ex:
        log.warning("/start failed in %s: %s %s", m.chat.id, type(ex).__name__, ex)
        try:
            await m.reply_text(re.sub(r"</?[a-z]+>", "", START_TEXT) + "\n\n/help - commands")
        except Exception as ex2:
            log.warning("/start fallback failed in %s: %s %s", m.chat.id, type(ex2).__name__, ex2)


@bot.on_message(filters.command("help"))
async def help_cmd(_, m: Message):
    """/help: full command list (group or private). Always answers (see reply_safe)."""
    log.info("/help in chat %s (%s)", m.chat.id, m.chat.type)
    if m.chat.type in GROUP_TYPES:
        try:
            db.seen(m.chat.id, m.chat.title)
        except Exception as ex:
            log.warning("db.seen failed: %s", ex)
    await reply_safe(m, HELP_TEXT)


@bot.on_message(filters.command("setupdate") & filters.private)
async def set_update(_, m: Message):
    """Owner only: /setupdate https://t.me/channel  (or /setupdate off)."""
    if not OWNER_ID or m.from_user.id != OWNER_ID:
        return
    arg = m.command[1].strip() if len(m.command) > 1 else ""
    if not arg:
        return await m.reply_text(f"Current update link: {update_url() or 'not set'}\n\n"
                                  "Change: /setupdate https://t.me/YourChannel\nRemove: /setupdate off")
    if arg.lower() == "off":
        db.setting_set(0, "update_url", "")
        return await m.reply_text("✅ Update button removed.")
    if arg.startswith("@"):
        arg = "https://t.me/" + arg[1:]
    if not arg.startswith("http"):
        return await m.reply_text("❌ Send a link like https://t.me/YourChannel or @YourChannel")
    db.setting_set(0, "update_url", arg)
    await m.reply_text(f"✅ Update button set: {arg}")


def _csv_file(name: str, head: list, rows: list) -> io.BytesIO:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    bio = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # utf-8-sig: Excel me Hindi/Punjabi naam sahi dikhte hain
    bio.name = name
    return bio


@bot.on_message(filters.command("file"))
async def owner_file(_, m: Message):
    """Owner only: /file -> 2 CSV owner ke private chat me: (1) kaun sa gaana kitni baar baja,
    (2) radio gaano ki rating. Group me chalao to bhi file DM me aati hai."""
    if not OWNER_ID or not m.from_user or m.from_user.id != OWNER_ID:
        return
    tz = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))
    when = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M") if ts else ""
    try:
        played, radio = await asyncio.to_thread(db.song_export)
        f1 = _csv_file("songs_played.csv", ["No", "Song", "Type", "Played (times)", "Likes", "Skips", "Rating", "Last played"],
                       [[i, r[0], r[1], r[2], r[3], r[4], r[3] - r[4], when(r[5])] for i, r in enumerate(played, 1)])
        f2 = _csv_file("radio_ratings.csv", ["No", "Song", "Played (times)", "Likes (+1)", "Skips (-1)", "Rating", "Last played"],
                       [[i, r[0], r[1], r[2], r[3], r[4], when(r[5])] for i, r in enumerate(radio, 1)])
        total = sum(r[2] for r in played)
        await bot.send_document(OWNER_ID, f1, caption=f"🎵 Songs played: {len(played)} songs, {total} plays (most played first)")
        await bot.send_document(OWNER_ID, f2, caption=f"📻 Radio ratings: {len(radio)} songs (best rated first)")
        if m.chat.type != ChatType.PRIVATE:
            await m.reply_text("✅ Files sent to your private chat.")
    except Exception as ex:
        report(None, "/file failed", ex)
        await m.reply_text(f"❌ Couldn't send files: {type(ex).__name__}. Press /start in the bot's private chat once, then try again.")


@bot.on_message(filters.command("play") & filters.group)
async def play(_, m: Message):
    query = m.text.split(None, 1)[1].strip() if len(m.command) > 1 else ""
    if not query and m.reply_to_message and m.reply_to_message.text:
        query = m.reply_to_message.text.strip()
    if not query:
        return await m.reply_text("Usage: /play song name or link")
    if "spotify.com" in query:
        return await m.reply_text("Spotify links aren't supported. Send the song name instead.")
    query = query[:200]
    is_link = bool(re.match(r"https?://", query))
    link = media.clean_url(query) if is_link else None
    if is_link and not link:
        return await m.reply_text("❌ Only YouTube and SoundCloud links (single songs) are supported.")

    uid = m.from_user.id if m.from_user else None
    if uid is not None:  # anti-spam cooldown per user
        now = time.time()
        if now - _last_play.get(uid, 0) < PLAY_COOLDOWN:
            return await m.reply_text("⏳ Please wait a few seconds before the next /play.")
        _last_play[uid] = now
        if len(_last_play) > 5000:
            _last_play.clear()
    if len(queues.get(m.chat.id, [])) >= MAX_QUEUE:
        return await m.reply_text(f"❌ Queue is full ({MAX_QUEUE} songs). Wait for some songs to finish.")
    if m.chat.id not in queues and len(queues) >= MAX_ACTIVE_GROUPS:
        return await m.reply_text("⏳ I am very busy right now. Please try again in a minute.")

    db.seen(m.chat.id, m.chat.title)
    status = await m.reply_text("🔎 Searching...")
    by = m.from_user.first_name if m.from_user else "Unknown"
    try:
        await ensure_assistant(m.chat.id)
    except Exception as ex:
        d = report(m.chat.id, "Assistant could not join", ex)
        text = join_error_text(ex, m.chat.id)
        if d.code in WAITABLE:  # a human can fix it -> keep the song and start it when fixed
            return await _wait_for_assistant(
                m.chat.id, status, {"query": query, "link": link, "by": by, "msg_id": m.id}, text,
                d.code, getattr(ex, "tag", None))
        return await status.edit_text(text)
    await _run_request(m.chat.id, query, link, by, m.id, status)


async def _run_request(chat_id: int, query: str, link: str | None, by: str, msg_id: int | None,
                       status: Message | None):
    """Find the song (search if it is a name), queue it and start playing when the queue was empty.
    `status` = the 'Searching...' message that is edited (None: plain replies in the group)."""
    async def reply(text: str):
        if status is not None:
            try:
                return await status.edit_text(text)
            except Exception:
                pass
        return await say(chat_id, text, reply_to_message_id=msg_id)

    if link:  # direct link (YouTube, SoundCloud)
        song = {"title": None, "page": link, "query": None, "by": by, "msg_id": msg_id}
    else:
        search_diag = None
        try:
            found = await media.search(query)
        except Exception as ex:
            search_diag = report(chat_id, "Search failed", ex)
            found = None
        if not found:
            if search_diag:  # search itself broke (YouTube block, network...) - not "no such song"
                return await reply(f"❌ {search_diag.user} [{search_diag.code}]")
            return await reply("❌ Song not found.")
        song = {"title": songtools.clean_title(found["title"]) or found["title"], "page": found["page"],
                "query": query, "by": by,
                "msg_id": msg_id, "duration": found.get("duration")}

    q = queues.setdefault(chat_id, [])
    q.append(song)
    if len(q) == 1:
        await reply("⬇️ Preparing audio...")
        await play_next(chat_id, status=status)
    else:
        await reply(f"➕ Added to queue #{len(q) - 1}: {song['title'] or 'link'}")
        if len(q) == 2:
            spawn(_prefetch(q))


# ---------------- Assistant could not join: wait, then start the song automatically ----------------
# Join problems a human can fix (add the assistant by hand, give 'Invite users', unban, approve...).
WAITABLE = {"INVITE_LINK_DEAD", "BOT_NEEDS_INVITE_RIGHT", "ADMIN_REQUIRED", "JOIN_REQUEST_PENDING",
            "ASSISTANT_BANNED", "UNKNOWN"}
_pending: dict[int, list[dict]] = {}  # chat -> requests waiting for the assistant
_waiting: set[int] = set()  # chats that have a waiter task running


async def _wait_for_assistant(chat_id: int, status: Message, item: dict, reason: str,
                              code: str | None = None, tag: str | None = None):
    tag = tag or _peek_assistant(chat_id).tag
    if chat_id not in _waiting and len(_waiting) >= MAX_WAITERS:  # bahut groups wait me: sirf wajah batao
        try:
            await status.edit_text(reason)
        except Exception:
            pass
        return
    item["status"] = status
    lst = _pending.setdefault(chat_id, [])
    if len(lst) < 5:
        lst.append(item)
    try:
        if code == "ASSISTANT_BANNED":
            await status.edit_text(banned_text(tag, RESUME_WAIT // 60))
        else:
            await status.edit_text(f"{reason}\n\n⏳ Fix this (or add {tag} to the group by hand). "
                                   f"I will start your song by myself within {RESUME_WAIT // 60} min.")
    except Exception:
        pass
    if chat_id not in _waiting:
        _waiting.add(chat_id)
        spawn(_resume_when_joined(chat_id))


async def _resume_when_joined(chat_id: int):
    """Every 8 s: is the assistant in the group now? Every ~32 s: try to bring it in again (the
    admin may have just given me 'Invite users'). When it is in: play everything that was waiting."""
    try:
        end, n = time.time() + RESUME_WAIT, 0
        while time.time() < end:
            await asyncio.sleep(8)
            n += 1
            a = assistant_for(chat_id)
            ok = await _assistant_is_member(a, chat_id)
            if not ok and n % 4 == 0:
                try:
                    await ensure_assistant(chat_id)
                    ok = True
                except Exception as ex:
                    log.debug("resume: join retry failed: %s", ex)
                    a = assistant_for(chat_id)
            if ok:
                _joined.add(chat_id)
                items = _pending.pop(chat_id, [])
                await say(chat_id, f"✅ {assistant_for(chat_id).tag} is in the group. Starting your song...")
                for it in items:
                    await _run_request(chat_id, it["query"], it["link"], it["by"], it["msg_id"],
                                       it.get("status"))
                return
        _pending.pop(chat_id, None)
        await say(chat_id, "⌛ My assistant did not join in time. Please fix it and send /play again "
                           "(send /setup to see what is missing).")
    except Exception as ex:
        report(chat_id, "Resume waiter crashed", ex)
        _pending.pop(chat_id, None)
    finally:
        _waiting.discard(chat_id)


async def do_pause(chat_id: int):
    await call_existing(chat_id).pause(chat_id)
    q = queues.get(chat_id)
    if q and not q[0].get("paused_at"):
        q[0]["paused_at"] = time.time()


async def do_resume(chat_id: int):
    await call_existing(chat_id).resume(chat_id)
    q = queues.get(chat_id)
    if q:
        p = q[0].pop("paused_at", None)
        if p and q[0].get("started"):
            q[0]["started"] += time.time() - p  # pause time does not count for the watchdog


@bot.on_message(filters.command("pause") & filters.group)
async def pause(_, m: Message):
    if await _deny(m):
        return
    try:
        await do_pause(m.chat.id)
        await m.reply_text("⏸ Paused")
    except Exception:
        await m.reply_text("Nothing is playing.")


@bot.on_message(filters.command("resume") & filters.group)
async def resume(_, m: Message):
    if await _deny(m):
        return
    try:
        await do_resume(m.chat.id)
        await m.reply_text("▶️ Resumed")
    except Exception:
        await m.reply_text("Nothing is playing.")


async def do_skip(chat_id: int) -> str:
    async with _locks[chat_id]:
        q = queues.get(chat_id)
        if not q:
            return "Nothing is playing."
        gone = q.pop(0)
        note = ""
        if gone.get("radio") and gone.get("started") and gone.get("skey"):  # radio skip = rating -1
            try:
                r = db.song_vote(gone["skey"], gone.get("title"), -1, "radio")
                note = ""
            except Exception as ex:
                log.debug("skip vote failed: %s", ex)
        loops[chat_id] = False
        if not q:
            _refill_radio(chat_id, q)
        if q:
            await _play_safe(chat_id)
            return "⏭ Skipped" + note
        await stop_all(chat_id)
        return "⏭ Skipped" + note + ". Queue is empty, left the voice chat."


@bot.on_message(filters.command("skip") & filters.group)
async def skip(_, m: Message):
    if await _deny(m):
        return
    await m.reply_text(await do_skip(m.chat.id))


@bot.on_message(filters.command("stop") & filters.group)
async def stop(_, m: Message):
    if await _deny(m):
        return
    await stop_all(m.chat.id)
    await m.reply_text("⏹ Stopped.")


@bot.on_message(filters.command("loop") & filters.group)
async def loop_cmd(_, m: Message):
    if await _deny(m):
        return
    if not queues.get(m.chat.id):
        return await m.reply_text("Nothing is playing.")
    loops[m.chat.id] = not loops.get(m.chat.id, False)
    await m.reply_text("🔁 Loop ON" if loops[m.chat.id] else "🔁 Loop OFF")


@bot.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_, m: Message):
    q = queues.get(m.chat.id)
    if not q:
        return await m.reply_text("Queue is empty.")
    name = lambda s: s.get("title") or s.get("page")
    text = f"🎵 Now: {name(q[0])}\n"
    for i, s in enumerate(q[1:11], 1):
        text += f"{i}. {name(s)}\n"
    if len(q) > 11:
        text += f"...and {len(q) - 11} more"
    await m.reply_text(text)


# ---------------- Notices: assistant banned, voice chat started / ended, welcome ----------------
VC_NOTICE_DEFAULT = os.environ.get("VC_NOTICE_DEFAULT", "1")  # "1" = on for groups that never chose
WELCOME_DEFAULT = os.environ.get("WELCOME_DEFAULT", "1")
WELCOME_TEXT = os.environ.get("WELCOME_TEXT", "")  # optional, use {name} and {group}
_DEFAULT_WELCOME = ("👋 Welcome {name}!\nYou joined <b>{group}</b>.\n"
                    "🎧 I'm a music bot: play any song in the group voice chat with "
                    "<code>/play song name</code>.")
_welcome_log: dict[int, list[float]] = {}  # chat -> recent welcome times (mass-join guard)


def _fmt_clock(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), TZ).strftime("%I:%M %p").lstrip("0")


def _fmt_span(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    mi, s = divmod(rem, 60)
    return f"{h}h {mi}m" if h else (f"{mi} min" if mi else f"{s} sec")


def _setting_on(chat_id: int, key: str, default: str) -> bool:
    return (db.setting_get(chat_id, key, "") or default) == "1"


def _msg_ts(m: Message) -> float:
    try:
        return m.date.timestamp()
    except Exception:
        return time.time()


async def _is_admin_msg(m: Message) -> bool:
    """Real admin check (not affected by CONTROLS_ADMIN_ONLY): for /setup and settings."""
    if m.sender_chat and m.sender_chat.id == m.chat.id:  # anonymous admin
        return True
    return m.from_user is not None and await is_admin(m.chat.id, m.from_user.id)


async def assistant_member_changed(u: ChatMemberUpdated, a: Assistant):
    """One of our assistants was banned / unbanned / removed in a group: tell the group."""
    chat_id = u.chat.id
    old, new = u.old_chat_member, u.new_chat_member
    st = new.status if new else ChatMemberStatus.LEFT
    was = old.status if old else None
    if st in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
        _joined.discard(chat_id)  # next /play checks again / rejoins
    if st == ChatMemberStatus.BANNED:
        db.log_error(chat_id, f"Assistant {a.tag} is banned in this group", None,
                     code="ASSISTANT_BANNED", hint="Unban the assistant in the group's removed-users list",
                     detail=f"assistant={a.tag} chat={chat_id}")
        if _once(("ban", chat_id)):
            await say(chat_id, banned_text(a.tag))
    elif was == ChatMemberStatus.BANNED:
        if _once(("unban", chat_id)):
            await say(chat_id, f"✅ Assistant {a.tag} is unbanned. Send /play to start the music.")
    elif st == ChatMemberStatus.LEFT and queues.get(chat_id):
        if _once(("left", chat_id)):
            await say(chat_id, f"⚠️ My assistant {a.tag} was removed from this group. "
                               "I will bring it back on the next /play.")


@bot.on_message(filters.group & filters.video_chat_started, group=7)
async def vc_started(_, m: Message):
    try:
        if _setting_on(m.chat.id, "vcnotice", VC_NOTICE_DEFAULT):
            await say(m.chat.id, f"🎙 Voice chat started at {_fmt_clock(_msg_ts(m))}.\n\n" + CMD_HINT)
    except Exception as ex:
        log.warning("vc_started error: %s", ex)


@bot.on_message(filters.group & filters.video_chat_ended, group=7)
async def vc_ended(_, m: Message):
    chat_id = m.chat.id
    try:
        had = bool(queues.get(chat_id))
        if had:  # the voice chat is gone: drop the stale queue / loop / call
            await stop_all(chat_id)
        if _setting_on(chat_id, "vcnotice", VC_NOTICE_DEFAULT):
            dur = getattr(m.video_chat_ended, "duration", 0)
            text = f"🔇 Voice chat ended at {_fmt_clock(_msg_ts(m))}"
            text += f" (it lasted {_fmt_span(dur)})" if dur else ""
            await say(chat_id, text + (". Queue cleared." if had else ".") + "\n\n" + CMD_HINT)
    except Exception as ex:
        log.warning("vc_ended error: %s", ex)


# ---------------- VC: "X joined the voice chat" + "X invited Y to the voice chat" (auto-deleted) ----------------
VC_JOIN_DEFAULT = os.environ.get("VC_JOIN_DEFAULT", "1")  # "1" = on for groups that never chose
VC_JOIN_DELETE_SEC = int(os.environ.get("VC_JOIN_DELETE_SEC", "10"))  # "joined" message is deleted after this
VC_INVITE_DELETE_SEC = int(os.environ.get("VC_INVITE_DELETE_SEC", "30"))  # "invited" message is deleted after this
_vc_seen: dict[tuple[int, int], float] = {}  # (chat, user) -> time: same join can arrive twice
_vc_log: dict[int, list[float]] = {}  # chat -> recent join messages (mass-join guard)


def _mention(u) -> str:
    return f'<a href="tg://user?id={u.id}">{html.escape(getattr(u, "first_name", None) or "friend")}</a>'


def _open_bot_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[ibtn("🎧 Open bot", "success", url=f"https://t.me/{BOT_USERNAME}?start=hi")]])


async def _on_vc_participant(update):
    """Assistant sees a participant change in the voice chat. Only 'joined' is announced."""
    p = getattr(update, "participant", None)
    chat_id = getattr(update, "chat_id", None)
    if p is None or chat_id is None:
        return
    if "JOINED" not in str(getattr(p, "action", "")).upper():
        return
    uid = getattr(p, "user_id", None)
    if not uid or uid < 0 or uid == BOT_ID or any(a.uid == uid for a in ASSISTANTS):
        return
    if not _setting_on(chat_id, "vcjoin", VC_JOIN_DEFAULT):
        return
    now = time.time()
    if now - _vc_seen.get((chat_id, uid), 0) < 20:
        return
    if len(_vc_seen) > 5000:
        _vc_seen.clear()
    _vc_seen[(chat_id, uid)] = now
    recent = [t for t in _vc_log.get(chat_id, []) if now - t < 60]
    if len(recent) >= 6:  # many people joining at once: stay quiet
        return
    recent.append(now)
    if len(_vc_log) > 2000:
        _vc_log.clear()
    _vc_log[chat_id] = recent
    try:
        u = await bot.get_users(uid)
        who = _mention(u)
    except Exception:
        who = f'<a href="tg://user?id={uid}">Someone</a>'
    msg = await say(chat_id, f"🎙 {who} joined the voice chat.", parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True)
    spawn(_delete_later(msg, VC_JOIN_DELETE_SEC))


@bot.on_message(filters.group & filters.video_chat_members_invited, group=7)
async def vc_invited(_, m: Message):
    """Someone invited people to the voice chat: say it, and invite them to the bot too."""
    try:
        if not _setting_on(m.chat.id, "vcjoin", VC_JOIN_DEFAULT):
            return
        users = [u for u in (getattr(m.video_chat_members_invited, "users", None) or []) if not u.is_bot]
        if not users:
            return
        names = ", ".join(_mention(u) for u in users[:5])
        by = _mention(m.from_user) if m.from_user else "Someone"
        msg = await say(m.chat.id,
                        f"📨 {by} invited {names} to the voice chat.\n"
                        "🎧 I'm a music bot: play any song here with <code>/play song name</code>.",
                        parse_mode=ParseMode.HTML, reply_markup=_open_bot_kb(),
                        disable_web_page_preview=True)
        spawn(_delete_later(msg, VC_INVITE_DELETE_SEC))
    except Exception as ex:
        log.warning("vc_invited error: %s", ex)


def _welcome_text(name_html: str, group: str) -> str:
    tpl = WELCOME_TEXT or _DEFAULT_WELCOME
    return tpl.replace("{name}", name_html).replace("{group}", html.escape(group))


async def _delete_later(msg: Message | None, secs: int):
    await asyncio.sleep(secs)
    try:
        if msg:
            await msg.delete()
    except Exception:
        pass


_welcomed: dict[tuple[int, int], float] = {}  # (chat, user) -> time: the same join can come twice
_req_dm: dict[tuple[int, int], float] = {}  # (chat, user) -> time: private message already sent on join request


async def _do_welcome(chat, users):
    """Shared by both triggers (join service message AND chat_member update)."""
    chat_id = chat.id
    if not _setting_on(chat_id, "welcome", WELCOME_DEFAULT):
        return
    now = time.time()
    if len(_welcomed) > 5000:
        _welcomed.clear()
    fresh = []
    for u in users:
        if u.is_bot or u.id == BOT_ID or now - _welcomed.get((chat_id, u.id), 0) < 30:
            continue
        _welcomed[(chat_id, u.id)] = now
        fresh.append(u)
    users = fresh[:5]
    if not users:
        return
    recent = [t for t in _welcome_log.get(chat_id, []) if now - t < 60]
    if len(recent) >= 10:  # mass join / raid: stay quiet
        return
    recent.append(now)
    if len(_welcome_log) > 2000:
        _welcome_log.clear()
    _welcome_log[chat_id] = recent
    group = chat.title or "this group"
    in_group = []
    for u in users:
        if _req_dm.pop((chat_id, u.id), 0):  # approval group: he already got the private message
            continue
        in_group.append(u)
    if in_group:  # auto-join group: short welcome in the group
        names = ", ".join(f'<a href="tg://user?id={u.id}">{html.escape(u.first_name or "friend")}</a>'
                          for u in in_group)
        kb = InlineKeyboardMarkup([[ibtn(
            "🎧 Open bot", "success", url=f"https://t.me/{BOT_USERNAME}?start=hi")]])
        msg = await say(chat_id, _welcome_text(names, group), parse_mode=ParseMode.HTML,
                        reply_markup=kb, disable_web_page_preview=True)
        spawn(_delete_later(msg, 60))


@bot.on_message(filters.group & filters.new_chat_members, group=7)
async def welcome_new(_, m: Message):
    """Trigger 1: the "X joined" service message (not sent in big groups / some invite links)."""
    try:
        await _do_welcome(m.chat, list(m.new_chat_members or []))
    except Exception as ex:
        log.warning("welcome error: %s", ex)


@bot.on_chat_member_updated(group=8)
async def welcome_by_update(_, u: ChatMemberUpdated):
    """Trigger 2: member update (works when the join message is hidden). Bot must be admin."""
    try:
        if u.chat.type not in GROUP_TYPES:
            return
        old, new = u.old_chat_member, u.new_chat_member
        if not new or new.status != ChatMemberStatus.MEMBER:
            return
        if old and old.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                                  ChatMemberStatus.OWNER, ChatMemberStatus.RESTRICTED):
            return  # was already inside (promoted / unrestricted), not a new join
        await _do_welcome(u.chat, [new.user])
    except Exception as ex:
        log.warning("welcome update error: %s", ex)


_REQUEST_TEXT = ("👋 Hi {name}!\nYour request to join <b>{group}</b> is sent. "
                 "You will get in as soon as an admin approves it.\n"
                 "🎧 I'm a music bot: play any song in the group voice chat with "
                 "<code>/play song name</code>.")


@bot.on_chat_join_request(group=9)
async def welcome_join_request(_, r):
    """Someone asked to join (group needs admin approval). Telegram lets the bot write to that
    person privately right now, even if he never pressed /start. Bot needs 'Invite users' right."""
    try:
        chat, user = r.chat, r.from_user
        if chat.type not in GROUP_TYPES or user.is_bot:
            return
        now = time.time()  # private message is always sent, /welcome off does not stop it
        if len(_req_dm) > 5000:
            _req_dm.clear()
        text = (_REQUEST_TEXT.replace("{name}", html.escape(user.first_name or "friend"))
                .replace("{group}", html.escape(chat.title or "this group")))
        _req_dm[(chat.id, user.id)] = now  # approval group: no group welcome later (even if DM fails)
        await bot.send_message(getattr(r, "user_chat_id", None) or user.id, text,
                               parse_mode=ParseMode.HTML, reply_markup=start_kb(),
                               disable_web_page_preview=True)
    except Exception as ex:
        log.warning("join request welcome error: %s %s", type(ex).__name__, ex)


async def _toggle_cmd(m: Message, key: str, default: str, label: str):
    if not await _is_admin_msg(m):
        return await m.reply_text("🔒 Only admins can change this.")
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg in ("on", "off"):
        db.setting_set(m.chat.id, key, "1" if arg == "on" else "0")
        return await m.reply_text(f"{label}: {'ON ✅' if arg == 'on' else 'OFF ⛔'}")
    cur = _setting_on(m.chat.id, key, default)
    await m.reply_text(f"{label} is {'ON' if cur else 'OFF'}.\n"
                       f"Change: /{m.command[0]} on  or  /{m.command[0]} off")


@bot.on_message(filters.command("vcnotice") & filters.group)
async def vcnotice_cmd(_, m: Message):
    await _toggle_cmd(m, "vcnotice", VC_NOTICE_DEFAULT, "Voice chat notices")


@bot.on_message(filters.command("vcjoin") & filters.group)
async def vcjoin_cmd(_, m: Message):
    await _toggle_cmd(m, "vcjoin", VC_JOIN_DEFAULT, "Voice chat join/invite messages")


@bot.on_message(filters.command("welcome") & filters.group)
async def welcome_cmd(_, m: Message):
    await _toggle_cmd(m, "welcome", WELCOME_DEFAULT, "Welcome messages")


# ---------------- /setup: what is missing in this group? ----------------
def _right(member, name: str):
    p = getattr(member, "privileges", None)
    return None if p is None else bool(getattr(p, name, False))


@bot.on_message(filters.command("setup") & filters.group)
async def setup_cmd(_, m: Message):
    if not await _is_admin_msg(m):
        return await m.reply_text("🔒 Only admins can run /setup.")
    chat_id = m.chat.id
    if not _once(("setup", chat_id), 15):
        return await m.reply_text("⏳ Please wait a few seconds.")
    ok, bad, warn = "✅", "❌", "⚠️"
    lines, fixes = ["🔧 <b>Setup check</b>", ""], []
    ADM = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

    # 1) the bot
    try:
        bm = await bot.get_chat_member(chat_id, BOT_ID)
    except Exception:
        bm = None
    is_adm = bm is not None and bm.status in ADM
    lines.append(f"{ok if is_adm else bad} I am admin")
    if not is_adm:
        fixes.append("Make me admin and give me the 'Invite users' right.")
    else:
        inv = _right(bm, "can_invite_users")
        lines.append(f"{bad if inv is False else ok} Right 'Invite users' (to bring my assistant)")
        if inv is False:
            fixes.append("Give me the admin right 'Invite users'.")

    # 2) the assistant
    a = _peek_assistant(chat_id)
    tag = html.escape(a.tag)
    am = await _assistant_member(a, chat_id)
    st = am.status if am else None
    if a.dead:
        lines.append(f"{bad} Assistant {tag}: account is dead (session revoked)")
        fixes.append("The bot owner must make a new SESSION_STRING for the assistant.")
    elif st == ChatMemberStatus.BANNED:
        lines.append(f"🚫 Assistant {tag} is BANNED in this group")
        fixes.append(f"Unban {tag} (group's 'Removed users' list).")
    elif st is None or st == ChatMemberStatus.LEFT:
        lines.append(f"{bad} Assistant {tag} is not in this group")
        fixes.append("Send /play and I will add it myself (needs 'Invite users'), or add "
                     f"{tag} to the group by hand.")
    else:
        lines.append(f"{ok} Assistant {tag} is in the group")
    if not a.dead and not a.healthy:
        lines.append(f"{warn} Assistant is resting right now ({a.status})")

    # 3) the voice chat
    vc = await _voice_chat_active(chat_id)
    if vc is True:
        lines.append(f"{ok} Voice chat is running")
    elif vc is False:
        lines.append(f"{warn} No voice chat is running")
        fixes.append("Start the voice chat in the group (Group info → Start voice chat).")
    else:
        lines.append("❔ Could not check the voice chat")

    on = lambda b: "ON" if b else "OFF"
    lines += ["", f"🎙 Voice chat notices: {on(_setting_on(chat_id, 'vcnotice', VC_NOTICE_DEFAULT))} (/vcnotice)",
              f"🎧 VC join/invite messages: {on(_setting_on(chat_id, 'vcjoin', VC_JOIN_DEFAULT))} (/vcjoin)",
              f"👋 Welcome messages: {on(_setting_on(chat_id, 'welcome', WELCOME_DEFAULT))} (/welcome)", ""]
    if fixes:
        lines.append("<b>To fix:</b>")
        lines += [f"• {html.escape(f)}" for f in fixes]
    else:
        lines.append("🎉 Everything is ready. Send /play song name")
    await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _voice_chat_active(chat_id: int):
    """True / False, or None if it cannot be checked."""
    try:
        chat = await bot.get_chat(chat_id)
        v = getattr(chat, "is_call_active", None)
        return None if v is None else bool(v)
    except Exception:
        return None


# ---------------- Background jobs: assistant health, idle leave, DB flush ----------------
async def assistant_health_loop():
    """Every 5 min: is each assistant session still alive? A dead one is marked (panel + owner alert)."""
    while True:
        await asyncio.sleep(300)
        for a in ASSISTANTS:
            if a.dead:
                continue
            try:
                await asyncio.wait_for(a.client.get_me(), 30)
            except Exception as ex:
                if any(k in type(ex).__name__ for k in DEAD_ERRORS):
                    _mark_dead(a, ex)
                else:  # network hiccup: not dead, just log it
                    log.warning("assistant %s health check failed: %s %s", a.tag, type(ex).__name__, ex)


async def idle_leave_once() -> int:
    """One round: assistants leave groups unused for IDLE_LEAVE_DAYS. Returns how many were left."""
    rows = await asyncio.to_thread(db.assign_idle, time.time() - IDLE_LEAVE_DAYS * 86400)
    left = 0
    for chat_id, aid in rows:
        a = ASSISTANT_BY_ID.get(aid)
        if a is None or a.dead or queues.get(chat_id):
            continue
        try:
            await a.client.leave_chat(chat_id)
        except Exception as ex:
            if not any(k in type(ex).__name__ for k in
                       ("UserNotParticipant", "ChannelPrivate", "ChatForbidden", "PeerIdInvalid",
                        "ChannelInvalid")):
                log.warning("idle leave failed in %s: %s %s", chat_id, type(ex).__name__, ex)
                continue  # a real error: keep the assignment, try again next round
        if _assigned.get(chat_id) is a:
            _assigned.pop(chat_id, None)
            a.chats = max(0, a.chats - 1)
        _joined.discard(chat_id)
        db.assign_delete(chat_id)
        left += 1
        await asyncio.sleep(3)  # spread out: no FloodWait
    if left:
        log.info("assistants left %s idle groups", left)
        for a in ASSISTANTS:
            a.full_until = 0.0  # slots are free again
    return left


async def idle_leave_loop():
    """Assistants leave groups that did not play a song for IDLE_LEAVE_DAYS: keeps the account far
    from Telegram's group limit. They come back by themselves on the next /play."""
    if IDLE_LEAVE_DAYS <= 0:
        return
    await asyncio.sleep(600)
    while True:
        try:
            await idle_leave_once()
        except Exception as ex:
            log.warning("idle leave loop error: %s", ex)
        await asyncio.sleep(6 * 3600)


async def flush_loop():
    """Hit counts and last_used times are kept in RAM and written once a minute."""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(db.flush_buffers)
        except Exception as ex:
            log.warning("db flush failed: %s", ex)


def assistants_info() -> list[dict]:
    """For the admin panel."""
    return [{"tag": a.tag, "status": a.status, "groups": a.chats, "cap": ASSISTANT_MAX_GROUPS,
             "error": a.last_error}
            for a in ASSISTANTS]


# ---------------- Radio ----------------
# Flow: group me admin /radio -> bot "Open radio menu" button deta hai -> admin ke private chat me
# language -> feeling (mood) choose -> gaane group ke VC me bajne lagte hain.
@bot.on_message(filters.command("radio") & filters.group)
async def radio_cmd(_, m: Message):
    db.seen(m.chat.id, m.chat.title)
    if not LANGS:
        return await m.reply_text("Radio is not available (songs.csv missing).")
    if not m.from_user:
        return await m.reply_text("Please turn off anonymous admin and send /radio again.")
    if not await is_admin(m.chat.id, m.from_user.id):
        return await m.reply_text("📻 Only admins can use radio.")
    await m.reply_text(
        "📻 Radio = automatic trending songs.\nTap Start to choose language and feeling in private chat.",
        reply_markup=InlineKeyboardMarkup([[ibtn(
            "▶️ Start", "primary", url=f"https://t.me/{BOT_USERNAME}?start=radio_{m.chat.id}")]]),
    )


def lang_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, l in enumerate(LANGS):
        row.append(InlineKeyboardButton(f"🎵 {l}", callback_data=f"rl:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def mood_kb(li: int) -> InlineKeyboardMarkup:
    lang = LANGS[li]
    rows, row = [], []
    for mi, m in enumerate(MOODS[lang]):
        row.append(InlineKeyboardButton(f"{MOOD_EMOJI.get(m.lower(), '🎧')} {m}", callback_data=f"rm:{li}:{mi}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🎲 Mix (any feeling)", callback_data=f"rx:{li}"),
                 InlineKeyboardButton("🔙 Languages", callback_data="rb")])
    return InlineKeyboardMarkup(rows)


async def radio_menu(m: Message):
    try:
        chat_id = int(m.command[1].split("_", 1)[1])
    except Exception:
        return await m.reply_text("Send /radio in your group first.")
    if not await is_admin(chat_id, m.from_user.id):
        return await m.reply_text("📻 Only admins of that group can use radio.")
    dm_state[m.from_user.id] = {"chat": chat_id, "lang": None}
    if len(dm_state) > 5000:
        dm_state.clear()
        dm_state[m.from_user.id] = {"chat": chat_id, "lang": None}
    await m.reply_text("📻 Radio - automatic trending songs\n\nChoose a language:", reply_markup=lang_kb())


async def start_radio(user_id: int, songs: list[tuple], label: str, lang: str, mood: str) -> str:
    st = dm_state.get(user_id)
    if not st:
        return "Session expired. Send /radio in the group again."
    chat_id = st["chat"]
    if not await is_admin(chat_id, user_id):
        return "Only admins can use radio."
    try:
        await ensure_assistant(chat_id)
    except Exception as ex:
        report(chat_id, "Assistant could not join (radio)", ex)
        return join_error_text(ex, chat_id)
    async with _locks[chat_id]:
        queues[chat_id] = _radio_items(songs, mood)
        radio_src[chat_id] = (songs, label)
        loops.pop(chat_id, None)
    try:
        db.radio_count(lang, mood, starts=1)  # admin panel: language stats
    except Exception as ex:
        log.debug("radio_count failed: %s", ex)
    await say(chat_id, f"📻 Radio started: {label}\nSend /stop to end it.")
    await play_next(chat_id)
    if chat_id not in queues:  # first song failed (e.g. no voice chat): the reason is in the group
        return "❌ Radio could not start. Check the group message for the reason (for example: start the voice chat first)."
    return f"✅ Radio started in the group: {label}"


@bot.on_callback_query(filters.regex(r"^(rl|rm|rx|rb)"))
async def radio_cb(_, c: CallbackQuery):
    d = c.data
    uid = c.from_user.id
    st = dm_state.get(uid)
    if not st:
        return await c.answer("Session expired. Send /radio in the group again.", show_alert=True)
    p = d.split(":")
    try:
        if p[0] == "rb":
            st["lang"] = None
            await c.message.edit_text("📻 Radio - automatic trending songs\n\nChoose a language:",
                                      reply_markup=lang_kb())
        elif p[0] == "rl":
            li = int(p[1])
            st["lang"] = LANGS[li]
            await c.message.edit_text(f"📻 {LANGS[li]}\n\nHow are you feeling? Choose a mood:",
                                      reply_markup=mood_kb(li))
        elif p[0] == "rm":
            lang = LANGS[int(p[1])]
            mood = MOODS[lang][int(p[2])]
            await c.answer("Starting...")
            msg = await start_radio(uid, BY_MOOD[lang][mood], f"{lang} - {mood}", lang, mood)
            await c.message.edit_text(msg)
        elif p[0] == "rx":
            lang = LANGS[int(p[1])]
            await c.answer("Starting...")
            msg = await start_radio(uid, LANG_ALL[lang], f"{lang} - Mix", lang, "Mix")
            await c.message.edit_text(msg)
    except Exception as ex:
        try:
            if type(ex).__name__ == "MessageNotModified":  # same button pressed twice: harmless
                return await c.answer()
            report(dm_state.get(uid, {}).get("chat"), "Radio menu error", ex)
            await c.answer("Something went wrong, try again.", show_alert=True)
        except Exception:
            pass  # query already answered


# ---------------- Radio: 👍 Like = rating +1 ----------------
LIKE_COOLDOWN_MIN = int(os.environ.get("LIKE_COOLDOWN_MIN", "20"))  # ek user ek gaane ko dobara itne minute baad like kar sake
_liked: dict[tuple[str, int], float] = {}  # (song key, user) -> last like time


@bot.on_callback_query(filters.regex(r"^like:"))
async def like_cb(_, c: CallbackQuery):
    try:
        chat_id = c.message.chat.id
        tok = c.data.split(":", 1)[1]
        q = queues.get(chat_id)
        cur = q[0] if q else None
        if not cur or cur.get("tok") != tok or not cur.get("radio") or not cur.get("skey"):
            return await c.answer("This song has already ended.")
        uid, key, now = c.from_user.id, cur["skey"], time.time()
        if now - _liked.get((key, uid), 0) < LIKE_COOLDOWN_MIN * 60:
            return await c.answer(f"You already liked this song 👍 (again after {LIKE_COOLDOWN_MIN} min)")
        if len(_liked) > 20000:
            _liked.clear()
        _liked[(key, uid)] = now
        r = db.song_vote(key, cur.get("title"), +1, "radio")
        await c.answer("👍 Liked!")
        try:
            await c.message.edit_reply_markup(buttons(chat_id))
        except Exception:
            pass
    except Exception as ex:
        log.warning("like failed: %s %s", type(ex).__name__, ex)


# ---------------- Player buttons ----------------
@bot.on_callback_query(filters.regex(r"^(pause|resume|skip|stop|loop)$"))
async def cb(_, c: CallbackQuery):
    chat_id = c.message.chat.id
    d = c.data
    if not await can_control(chat_id, c.from_user):
        return await c.answer("Only admins can do this.", show_alert=True)
    try:
        if d == "pause":
            await do_pause(chat_id)
            await c.answer("Paused")
        elif d == "resume":
            await do_resume(chat_id)
            await c.answer("Resumed")
        elif d == "skip":
            await c.answer(await do_skip(chat_id))
        elif d == "stop":
            await stop_all(chat_id)
            await c.answer("Stopped")
        elif d == "loop":
            loops[chat_id] = not loops.get(chat_id, False)
            await c.answer("Loop ON" if loops[chat_id] else "Loop OFF")
            await c.message.edit_reply_markup(buttons(chat_id))
    except Exception:
        await c.answer("Nothing is playing.", show_alert=True)


# ---------------- Broadcast (admin panel se) ----------------
GONE_ERRORS = {"ChatWriteForbidden", "ChannelPrivate", "UserBannedInChannel", "PeerIdInvalid",
               "ChannelInvalid", "ChatIdInvalid", "ChatRestricted", "ChatAdminRequired"}


async def do_broadcast(text: str, st: dict):
    with costs.job("broadcast"):
        await _do_broadcast(text, st)


async def _do_broadcast(text: str, st: dict):
    ids = db.active_group_ids()
    st["total"] = len(ids)
    for cid in ids:
        for attempt in range(3):
            try:
                await bot.send_message(cid, text)
                st["sent"] += 1
                break
            except Exception as ex:
                name = type(ex).__name__
                if name == "FloodWait" and attempt < 2:
                    await asyncio.sleep(getattr(ex, "value", 30) + 1)
                    continue
                st["failed"] += 1
                if name in GONE_ERRORS:  # bot hata diya gaya / likh nahi sakta
                    db.mark_left(cid, None)
                    st["removed"] += 1
                else:
                    db.log_error(cid, "Broadcast failed", ex)
                break
        await asyncio.sleep(0.3)  # Telegram limit se bachne ke liye


# ---------------- Run ----------------
async def notify_shutdown():
    """Redeploy / SIGTERM: tell groups that are playing, so nobody is left wondering."""
    chats = list(queues)
    if not chats:
        return
    msg = "🔄 Bot is restarting for an update. Queue was cleared, please /play again in a minute."
    sem = asyncio.Semaphore(25)  # 1000 groups: Telegram limit ke andar

    async def one(c):
        async with sem:
            await say(c, msg)

    try:
        await asyncio.wait_for(asyncio.gather(*(one(c) for c in chats)), timeout=25)
    except Exception:
        pass


async def main():
    global BOT_USERNAME, BOT_ID
    await bot.start()
    me = await bot.get_me()
    BOT_USERNAME, BOT_ID = me.username, me.id
    try:  # command menu (/help etc. dikhne lagte hain)
        await bot.set_bot_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    except Exception as ex:
        log.warning("set_bot_commands failed: %s %s", type(ex).__name__, ex)
    if STORAGE_CHANNEL:
        try:
            ch = await bot.get_chat(STORAGE_CHANNEL)
            log.info("storage channel OK: %s | songs stored: %s", ch.title, db.tgcache_count())
        except Exception as ex:
            log.warning("STORAGE CHANNEL NOT USABLE (%s %s). Make the bot admin there and check "
                        "STORAGE_CHANNEL id (-100...). Falling back to YouTube only.",
                        type(ex).__name__, ex)
    else:
        log.info("STORAGE_CHANNEL not set: every song comes from YouTube")
    for a in ASSISTANTS:
        # One broken assistant must not take the whole bot down: retry, then mark it dead and go on.
        for attempt in range(3):
            try:
                await a.client.start()
                await a.call.start()
                a.uid = (a.client.me or await a.client.get_me()).id
                ASSISTANT_BY_ID[a.uid] = a
                break
            except Exception as ex:
                if any(k in type(ex).__name__ for k in DEAD_ERRORS) or attempt == 2:
                    _mark_dead(a, ex)
                    break
                log.warning("assistant %s failed to start (%s %s), retrying", a.tag, type(ex).__name__, ex)
                await asyncio.sleep(5)
    if all(a.dead for a in ASSISTANTS):
        raise SystemExit("No assistant could start. Check SESSION_STRING(S).")
    load_assignments()
    panel.assistants_fn = assistants_info
    panel.library = {
        "total_songs": sum(len(v) for v in LANG_ALL.values()),
        "total_moods": sum(len(v) for v in MOODS.values()),
        "languages": [{"language": l, "songs": len(LANG_ALL[l]), "moods": len(MOODS[l])} for l in LANGS],
    }
    panel.broadcast_fn = do_broadcast
    await panel.start()
    spawn(panel.cleanup_loop())
    spawn(media.evict_loop())
    spawn(watchdog_loop())
    spawn(assistant_health_loop())
    spawn(idle_leave_loop())
    spawn(flush_loop())
    spawn(costs.loop(_streams_now))
    log.info("Bot is running as @%s | assistants: %s | radio languages: %s",
             BOT_USERNAME, [a.tag for a in ASSISTANTS], LANGS)
    await idle()
    await notify_shutdown()
    try:
        db.flush_buffers()  # save buffered hit counts / last_used before exit
        costs.flush()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
