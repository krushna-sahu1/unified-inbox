"""Turn "tomorrow 9am", "kal subah 9 baje", "in 2h", "fri 18:30" or "2026-11-12 09:30" into a
local date-time, always in the future.

English and common Hindi/Hinglish words are understood. When a time could mean morning or
evening ("at 6") the parser asks instead of guessing, because a message sent at the wrong time
of day is worse than one more question.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_WEEKDAYS = {"mon": 0, "monday": 0, "somvar": 0, "tue": 1, "tues": 1, "tuesday": 1, "mangalvar": 1,
             "wed": 2, "wednesday": 2, "budhvar": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
             "guruvar": 3, "fri": 4, "friday": 4, "shukravar": 4, "sat": 5, "saturday": 5, "shanivar": 5,
             "sun": 6, "sunday": 6, "ravivar": 6, "itvaar": 6, "itwar": 6}
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                            "nov", "dec"])}
_UNITS = {"m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "minat": 60, "h": 3600, "hr": 3600,
          "hrs": 3600, "hour": 3600, "hours": 3600, "ghante": 3600, "ghanta": 3600, "d": 86400, "day": 86400,
          "days": 86400, "din": 86400, "w": 604800, "week": 604800, "weeks": 604800, "hafte": 604800}
_PM_WORDS = ("pm", "p.m.", "evening", "night", "tonight", "afternoon", "shaam", "sham", "raat", "dopahar", "dopehar")
_AM_WORDS = ("am", "a.m.", "morning", "subah", "savere", "sawere")


class WhenError(ValueError):
    """The text is not a time we can use; the message says what to write instead."""


def parse_when(text: str, now: datetime) -> datetime:
    """``now`` and the result are naive local date-times. Raises WhenError."""
    raw = (text or "").strip().lower()
    if not raw:
        raise WhenError("Say when, e.g. 'tomorrow 9am', 'in 2h' or '2026-11-12 09:30'.")
    t = re.sub(r"\s+", " ", raw.replace(",", " ")).strip()

    m = re.fullmatch(r"(?:in )?(\d+(?:\.\d+)?) ?([a-z]+)(?: (?:baad|later|bad|mein|me))?", t)
    if m and m.group(2) in _UNITS:
        return (now + timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2)])).replace(microsecond=0)

    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ t](\d{1,2}:\d{2}))?", t)
    if m:
        day = datetime.fromisoformat(m.group(1))
        hh, mm = (int(x) for x in (m.group(2) or "09:00").split(":"))
        return _future(day.replace(hour=hh, minute=mm), now, text)

    day, rest = _day_part(t, now)
    hour, minute = _time_part(rest, explicit_day=day is not None, text=text)
    if day is None:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return _future(day.replace(hour=hour, minute=minute, second=0, microsecond=0), now, text)


def _future(dt: datetime, now: datetime, text: str) -> datetime:
    if dt <= now:
        raise WhenError(f"'{text}' is in the past.")
    return dt


def _day_part(t: str, now: datetime) -> tuple:
    """(date or None, the rest of the text)."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    words = t.split(" ")
    for i, w in enumerate(words):
        rest = " ".join(words[:i] + words[i + 1:])
        if w in ("today", "aaj", "tonight"):
            if w == "tonight":
                rest = f"{rest} pm" if re.search(r"\d", rest) else "8pm"
            return today, rest
        if w in ("tomorrow", "tmrw", "tmr", "kal"):
            return today + timedelta(days=1), rest
        if w == "parso":
            return today + timedelta(days=2), rest
        if w in _WEEKDAYS:
            rest = re.sub(r"\b(next|this|agle|is)\b", "", rest).strip()
            ahead = (_WEEKDAYS[w] - now.weekday()) % 7 or 7
            return today + timedelta(days=ahead), rest
    if "day after tomorrow" in t:
        return today + timedelta(days=2), t.replace("day after tomorrow", "").strip()
    # every "12 dec" / "dec 12" candidate, not just the first: in "shaam 6 baje 12 dec" the first
    # number-and-word pair is "shaam 6", and stopping there would silently drop the date
    pairs = [(m, m.group(1), m.group(2)) for m in re.finditer(r"\b(\d{1,2}) ?([a-z]{3})[a-z]*\b", t)]
    pairs += [(m, m.group(2), m.group(1)) for m in re.finditer(r"\b([a-z]{3})[a-z]* (\d{1,2})\b", t)]
    for m, d, mon in sorted(pairs, key=lambda x: x[0].start()):
        if mon in _MONTHS and 1 <= int(d) <= 31:
            year = now.year
            try:
                day = today.replace(year=year, month=_MONTHS[mon], day=int(d))
            except ValueError:
                raise WhenError(f"There is no {d} {mon}.") from None
            if day < today:
                day = day.replace(year=year + 1)
            return day, (t[:m.start()] + t[m.end():]).strip()
    return None, t


def _time_part(rest: str, *, explicit_day: bool, text: str) -> tuple[int, int]:
    rest = re.sub(r"(baje|bje|o'clock|at)", " ", rest).strip()
    pm = any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", rest) for w in _PM_WORDS)
    am = any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", rest) for w in _AM_WORDS)
    if rest.strip() in ("", "noon", "dopahar", "dopehar") and explicit_day:
        return (12, 0) if rest.strip() else (9, 0)  # a day alone means 9 in the morning
    m = re.search(r"(\d{1,2})(?:[:.](\d{2}))?", rest)
    if not m:
        if "noon" in rest:
            return 12, 0
        if "midnight" in rest:
            raise WhenError("Pick a time other than midnight, e.g. '11:30pm'.")
        raise WhenError(f"I could not read a time in '{text}'. Try e.g. 'tomorrow 9am' or 'in 2h'.")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise WhenError(f"'{text}' is not a valid time.")
    if pm and hour < 12:
        hour += 12
    elif am and hour == 12:
        hour = 0
    elif not pm and not am and 1 <= hour <= 6:
        raise WhenError(f"Is that {hour} in the morning or the evening? Add am or pm.")
    return hour, minute
