"""Safety pacing: every limit that decides whether a message may be sent or a read may run.

Pure functions only (no I/O), so every rule is unit-tested. Settings can be made stricter
than the defaults but never looser than the hard limits below.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, fields
from datetime import datetime, time as dtime
from typing import Any, Callable, Iterable, Optional

HOUR = 3600
NETWORK_LABELS = {"whatsapp": "WhatsApp", "instagram": "Instagram", "linkedin": "LinkedIn"}
DAY = 24 * HOUR


@dataclass(frozen=True)
class Settings:
    # Sending
    daily_send_limit: int = 20
    daily_limit_whatsapp: int = 15
    daily_limit_instagram: int = 10
    daily_limit_linkedin: int = 10
    hourly_send_limit: int = 5
    per_chat_hourly_limit: int = 2
    min_send_gap_seconds: int = 45
    reply_window_days: int = 7
    new_conversations_per_day: int = 5
    duplicate_window_hours: int = 24
    duplicate_min_chars: int = 20
    quiet_hours: str = ""
    mark_read_after_reply: bool = False  # opt-in: the other person may see read receipts
    # Reading
    read_page_size: int = 20
    max_pages: int = 5
    max_triage_chats: int = 15
    api_calls_per_hour: int = 120
    min_call_gap_seconds: float = 2.0
    max_call_gap_seconds: float = 5.0
    cache_ttl_minutes: int = 10
    cache_retention_days: int = 180
    # Background sync
    sync_enabled: bool = True
    poll_seconds: int = 120
    sync_budget_percent: int = 50
    sync_chats_per_cycle: int = 10
    initial_sync_days: int = 14
    webhook_port: int = 0
    webhook_host: str = "127.0.0.1"
    # Automation
    schedule_grace_minutes: int = 30
    # Context engine
    memory_enabled: bool = True
    memory_chats_per_cycle: int = 5
    memory_llm_calls_per_day: int = 200
    auto_draft_per_day: int = 20          # drafts prepared by themselves for urgent chats (0 = off)
    style_learning: bool = True           # learn how you write from your own sent messages
    embedding_provider: str = "none"
    # Safety, plans, languages and people
    scam_shield: bool = True              # warn about messages that look like a scam
    plan_detection: bool = True           # find plans ("meet Thursday 4pm") and offer them for your calendar
    my_language: str = "English"          # translations are written in this language
    also_read: str = ""                   # other languages you read (never translated), e.g. "Hindi, Hinglish"
    auto_translate: bool = True           # translate new messages in other languages (uses the model)
    people_radar: bool = True             # birthday reminders and a weekly people summary
    embedding_model: str = ""
    embedding_chars_per_day: int = 3_000_000
    # Triage
    unanswered_hours: int = 4


# (lowest allowed, highest allowed). A user value outside the range is clamped, so the
# limits can only ever get stricter than these.
_BOUNDS: dict[str, tuple[float, float]] = {
    "daily_send_limit": (0, 40),
    "daily_limit_whatsapp": (0, 30),
    "daily_limit_instagram": (0, 20),
    "daily_limit_linkedin": (0, 20),
    "hourly_send_limit": (0, 8),
    "per_chat_hourly_limit": (0, 3),
    "min_send_gap_seconds": (20, 3600),
    "reply_window_days": (1, 14),
    "new_conversations_per_day": (0, 20),
    "duplicate_window_hours": (24, 168),
    "duplicate_min_chars": (1, 200),
    "read_page_size": (1, 50),
    "max_pages": (1, 10),
    "max_triage_chats": (1, 30),
    "api_calls_per_hour": (1, 200),
    "min_call_gap_seconds": (1.0, 30.0),
    "max_call_gap_seconds": (1.0, 60.0),
    "cache_ttl_minutes": (1, 120),
    "cache_retention_days": (7, 3650),
    "poll_seconds": (60, 3600),
    "sync_budget_percent": (10, 80),
    "sync_chats_per_cycle": (1, 50),
    "initial_sync_days": (1, 90),
    "webhook_port": (0, 65535),
    "schedule_grace_minutes": (5, 240),
    "memory_chats_per_cycle": (1, 20),
    "memory_llm_calls_per_day": (0, 5000),
    "auto_draft_per_day": (0, 100),
    "embedding_chars_per_day": (0, 100_000_000),
    "unanswered_hours": (1, 168),
}


def load_settings(get: Callable[[str, Any], Any]) -> Settings:
    """Build Settings from a ``get(key, default)`` reader, clamping every number to its bounds."""
    defaults = Settings()
    values: dict[str, Any] = {}
    for f in fields(Settings):
        default = getattr(defaults, f.name)
        raw = get(f.name, default)
        if isinstance(default, bool):  # before int: bool is an int subclass, and bool("false") is True
            values[f.name] = raw if isinstance(raw, bool) else str(raw).strip().lower() not in (
                "0", "false", "no", "off", "")
            continue
        if isinstance(default, str):
            values[f.name] = str(raw or "").strip()
            continue
        try:
            num = type(default)(raw)
        except (TypeError, ValueError):
            num = default
        lo, hi = _BOUNDS.get(f.name, (num, num))
        values[f.name] = type(default)(min(max(num, lo), hi))
    if values["max_call_gap_seconds"] < values["min_call_gap_seconds"]:
        values["max_call_gap_seconds"] = values["min_call_gap_seconds"]
    return Settings(**values)


# ─── Text helpers ────────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub("", (text or "").lower())).strip()


def is_near_duplicate(a: str, b: str, threshold: float = 0.9) -> bool:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


# ─── Time helpers ────────────────────────────────────────────────────────────

_CLOCK = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?m?\.?$")


def _parse_hhmm(s: str) -> dtime:
    """"23:00", "23", "7.30", "11pm" or "7 am" as a time of day. Raises ValueError."""
    m = _CLOCK.match(s.strip().lower())
    if not m:
        raise ValueError(s)
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            raise ValueError(s)
        hour = hour % 12 + (12 if half == "p" else 0)
    return dtime(hour, minute)


def parse_quiet_hours(spec: str) -> Optional[tuple[dtime, dtime]]:
    """(start, end) of a quiet-hours setting, or None when it is empty or cannot be read.
    Understands "23:00-07:00", "23-7", "11pm-7am" and "23:00 to 07:00"."""
    text = re.sub(r"\s*(?:–|—|\bto\b|\btill\b|\buntil\b)\s*", "-", (spec or "").strip().lower())
    if not text:
        return None
    try:
        start_s, end_s = text.split("-")
        return _parse_hhmm(start_s), _parse_hhmm(end_s)
    except ValueError:
        return None


def quiet_hours_problem(spec: str) -> str:
    """Why a quiet-hours setting does nothing ('' when it is empty or fine)."""
    if not (spec or "").strip():
        return ""
    parsed = parse_quiet_hours(spec)
    if parsed is None:
        return f"Quiet hours {spec!r} can't be read, so they are off. Write them like 23:00-07:00."
    if parsed[0] == parsed[1]:
        return f"Quiet hours {spec!r} start and end at the same time, so they are off."
    return ""


def in_quiet_hours(spec: str, local_now: datetime) -> bool:
    """``spec`` like ``"23:00-07:00"`` (may cross midnight). Empty or malformed = off (the health
    check and /inbox limits say so: see ``quiet_hours_problem``)."""
    parsed = parse_quiet_hours(spec)
    if parsed is None:
        return False
    start, end = parsed
    now = local_now.time()
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def send_gap_seconds(settings: Settings, rand: Callable[[], float]) -> float:
    """Random gap before the next send is allowed: base + 0-100% extra (45s default → 45-90s)."""
    base = float(settings.min_send_gap_seconds)
    return base + base * rand()


def call_gap_seconds(settings: Settings, rand: Callable[[], float]) -> float:
    lo, hi = settings.min_call_gap_seconds, settings.max_call_gap_seconds
    return lo + (hi - lo) * rand()


def backoff_seconds(consecutive: int, retry_after: Optional[float], rand: Callable[[], float]) -> float:
    """Wait after a rate limit: 1m, 2m, 4m ... capped at 30m, never less than retry_after,
    ±20% jitter. The third limit in a row pauses the account for an hour."""
    if consecutive >= 3:
        return float(HOUR)
    base = min(60.0 * (2 ** max(consecutive - 1, 0)), 1800.0)
    if retry_after:
        base = max(base, float(retry_after))
    return base * (0.8 + 0.4 * rand())


# ─── The send gate ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SendRecord:
    chat_id: str
    network: str
    text: str
    sent_at: float
    started: bool = False  # the first message to someone, or to a chat quiet for longer than the reply window


@dataclass(frozen=True)
class AccountGate:
    paused_until: float = 0.0
    pause_reason: str = ""


def check_send(
    settings: Settings,
    *,
    now: float,
    local_now: datetime,
    chat_id: str,
    network: str,
    text: str,
    recent_sends: Iterable[SendRecord],
    account: AccountGate,
    last_inbound_at: Optional[float],
    next_send_allowed_at: float,
) -> list[str]:
    """Every reason this message may not be sent right now. Empty list = allowed."""
    reasons: list[str] = []
    sends = [s for s in recent_sends if now - s.sent_at < max(DAY, settings.duplicate_window_hours * HOUR)]
    day = [s for s in sends if now - s.sent_at < DAY]
    hour = [s for s in day if now - s.sent_at < HOUR]

    if not (text or "").strip():
        reasons.append("The draft is empty.")
    if in_quiet_hours(settings.quiet_hours, local_now):
        reasons.append(f"Quiet hours ({settings.quiet_hours}) are on.")
    if account.paused_until > now:
        reasons.append(f"This account is paused for {_mins(account.paused_until - now)}"
                       f" ({account.pause_reason or 'after an error'}).")
    if starts_conversation(settings, now, last_inbound_at):
        starts = sum(1 for s in day if s.started)
        if settings.new_conversations_per_day == 0:
            reasons.append("They have never messaged you in this chat. unified-inbox only replies;"
                           " it never starts conversations." if last_inbound_at is None else
                           f"Their last message is older than {settings.reply_window_days} days."
                           " unified-inbox only replies to recent conversations.")
        elif starts >= settings.new_conversations_per_day:
            reasons.append(f"New-conversation limit reached ({settings.new_conversations_per_day} a day): messaging"
                           " people who have not written to you recently is what gets accounts restricted.")
    if len(day) >= settings.daily_send_limit:
        reasons.append(f"Daily limit reached ({settings.daily_send_limit} in 24 hours).")
    net_limit = {"whatsapp": settings.daily_limit_whatsapp,
                 "instagram": settings.daily_limit_instagram,
                 "linkedin": settings.daily_limit_linkedin}.get(network, 0)
    if sum(1 for s in day if s.network == network) >= net_limit:
        reasons.append(f"{NETWORK_LABELS.get(network, network)} daily limit reached ({net_limit} in 24 hours).")
    if len(hour) >= settings.hourly_send_limit:
        reasons.append(f"Hourly limit reached ({settings.hourly_send_limit} per hour).")
    if sum(1 for s in hour if s.chat_id == chat_id) >= settings.per_chat_hourly_limit:
        reasons.append(f"Already {settings.per_chat_hourly_limit} messages to this chat in the last hour.")
    if next_send_allowed_at > now:
        reasons.append(f"Too soon after the last send. Try again in {int(next_send_allowed_at - now) + 1}s.")
    if len(normalize_text(text)) >= settings.duplicate_min_chars:
        window = settings.duplicate_window_hours * HOUR
        for s in sends:
            if now - s.sent_at < window and s.chat_id != chat_id and is_near_duplicate(text, s.text):
                reasons.append("You sent the same or a very similar message to someone else in the"
                               f" last {settings.duplicate_window_hours} hours. Personalise it first.")
                break
    return reasons


def starts_conversation(settings: Settings, now: float, last_inbound_at: Optional[float]) -> bool:
    """True when this message opens a conversation: they never wrote, or not within the reply
    window. These count against ``new_conversations_per_day``."""
    return last_inbound_at is None or now - last_inbound_at > settings.reply_window_days * DAY


def _mins(seconds: float) -> str:
    m = int(seconds // 60) + 1
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"
