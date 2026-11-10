"""Plans in your chats, one tap into your calendar.

"Let's meet Thursday 4pm", "call tomorrow at 10", "dinner on 12 Dec, 8:30", "kal shaam 6 baje
milte hain": when a message proposes a plan with a day or a time, it is saved as a suggested
plan (this machine only) and you get one alert. From the Inbox page or Hermes you can then:
  * add it to your calendar: a ready-made Google Calendar or Outlook link, or an .ics file any
    calendar app opens (Apple, Outlook, Google). Nothing is written to a calendar by itself;
  * see whether you are free: plans you added, plus (optional) your calendar's private iCal
    address (``calendar_ics_url``, read-only) for every event you already have;
  * get drafts that know it: an auto-draft for a chat proposing a time is told whether you are
    free then (only "busy 15:30-16:30", never what the other event is).

Detection is local and rule-based (English, Hindi and Hinglish): a plan word (meet, call,
dinner, interview, milte...) together with a day or a time. A plan that is cancelled ("can't
make it"), in the past, or more than six months away is ignored. "kal" means tomorrow only when
the sentence talks about the future ("kal milte hain", not "kal mile the").
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .pacing import DAY, HOUR, NETWORK_LABELS
from .privacy import redact

HORIZON_DAYS = 180       # plans further out than this are not plans, they are wishes
DEDUPE_SECONDS = 2 * HOUR  # "ok see you Thursday 4" after "Thursday 4pm?" is the same plan

_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                            "nov", "dec"])}
_MONTH_RX = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
             "somvar": 0, "mangalvar": 1, "budhvar": 2, "guruvar": 3, "shukravar": 4, "shanivar": 5, "ravivar": 6,
             "itvaar": 6, "itwar": 6}
_SHORT_WEEKDAYS = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5,
                   "sun": 6}

# (pattern, title, minutes, which half of the day an "at 8" means: "pm", "am" or "")
_ACTIVITIES: list[tuple[re.Pattern, str, int, str]] = [(re.compile(p, re.I), t, mins, bias) for p, t, mins, bias in [
    (r"\b(?:video call|zoom|google meet|gmeet|teams call|facetime)\b", "Video call", 30, ""),
    (r"\bdinner\b", "Dinner", 90, "pm"),
    (r"\blunch\b", "Lunch", 60, "lunch"),
    (r"\bbrunch\b", "Brunch", 90, "am"),
    (r"\bbreakfast\b", "Breakfast", 60, "am"),
    (r"\b(?:drinks|beers?)\b", "Drinks", 120, "pm"),
    (r"\bparty\b", "Party", 180, "pm"),
    (r"\binterview\b", "Interview", 60, ""),
    (r"\b(?:doctor|dentist|appointment|check-?up)\b", "Appointment", 60, ""),
    (r"\b(?:demo|presentation|webinar|workshop|stand-?up|meeting)\b", "Meeting", 60, ""),
    (r"\b(?:movie|film)\b", "Movie", 150, "pm"),
    (r"\b(?:coffee|chai)\b", "Coffee", 60, ""),
    (r"\b(?:call|ring)\b|\bcall kar|\bbaat kar|\blet'?s talk\b", "Call", 30, ""),
    (r"\b(?:meet|meet up|meetup|catch up|catchup|hang out|hangout|see you|see u|visit|come over|"
     r"pick (?:you|u|me) up|milte|milenge|milna|milo|mil lete|aa jao|aajao|aa jaana|aaunga|aaungi)\b", "Meet", 60, ""),
]]

_CANCEL = re.compile(
    r"\b(?:can'?t|cannot|can not|won'?t|not able|unable|couldn'?t|cancel(?:l?ed)?|postpone[d]?|call(?:ed)? off|"
    r"not (?:coming|free|available|possible)|nahi (?:ho paayega|ho payega|aa paunga|aa paungi|aa sakta|aa sakti|"
    r"mil paayenge|mil payenge)|no longer)\b", re.I)
_PAST_HINDI = re.compile(r"\b(?:the|thi|tha|gaye|gayi|gaya|kiya|kia|hua|hui|mile the)\b", re.I)
_PAST_EN = re.compile(r"\b(?:yesterday|last (?:night|week|month|time|monday|tuesday|wednesday|thursday|friday|"
                      r"saturday|sunday)|ago)\b", re.I)

_AMPM = r"(a\.?m\.?|p\.?m\.?)"
_RANGE = re.compile(rf"\b(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{_AMPM}?\s*(?:-|–|to|till|until)\s*(\d{{1,2}})(?:[:.](\d{{2}}))?"
                    rf"\s*{_AMPM}(?![a-z])", re.I)
_CLOCK_AMPM = re.compile(rf"\b(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{_AMPM}(?![a-z])", re.I)
_CLOCK_24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_CLOCK_LOOSE = re.compile(r"(?:\bat|@)\s*(\d{1,2})(?:[:.](\d{2}))?\b(?!\s*(?:%|people|min|hour|day|km|rs|\$))"
                          r"|\b(\d{1,2})(?:[:.](\d{2}))?\s*(?:baje|bje|o'?clock)\b", re.I)
_PARTS = [(re.compile(r"\b(?:morning|subah|savere|sawere)\b", re.I), "am", 9),
          (re.compile(r"\b(?:afternoon|dopahar|dopehar)\b", re.I), "pm", 14),
          (re.compile(r"\b(?:evening|shaam|sham)\b", re.I), "pm", 18),
          (re.compile(r"\b(?:night|raat|tonight)\b", re.I), "pm", 20)]
_NOON = re.compile(r"\b(?:noon|midday)\b", re.I)


def _sentence(text: str, pos: int) -> str:
    start = max(text.rfind(c, 0, pos) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, pos) for c in ".!?\n") if i >= 0]
    return text[start:min(ends) + 1 if ends else len(text)].strip()[:200]


def _day(t: str, ref: datetime) -> tuple[Optional[date], bool, bool, bool]:
    """(the day named, whether it was "tonight", whether it is uncertain, whether it is a weekday
    name: "Friday 4pm" said on Friday evening means next Friday)."""
    today = ref.date()
    m = re.search(r"\b(20\d\d)-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), False, False, False
        except ValueError:
            return None, False, False, False
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?{_MONTH_RX}(?![a-z])", t) or \
        re.search(rf"\b{_MONTH_RX}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m:
        a, b = m.group(1), m.group(2)
        d, mon = (a, b) if a.isdigit() else (b, a)
        try:
            day = date(today.year, _MONTHS[mon[:3]], int(d))
        except (ValueError, KeyError):
            return None, False, False, False
        if day < today:
            try:
                day = day.replace(year=today.year + 1)
            except ValueError:
                return None, False, False, False
        return day, False, False, False
    if re.search(r"\b(?:day after tomorrow|parso|parson)\b", t):
        return today + timedelta(days=2), False, False, False
    if re.search(r"\b(?:tomorrow|tmrw|tmr|tomorow|tommorow|tommorrow)\b", t):
        return today + timedelta(days=1), False, False, False
    if re.search(r"\bkal\b", t):
        if _PAST_HINDI.search(t):
            return None, False, True, False  # "kal mile the": yesterday
        return today + timedelta(days=1), False, False, False
    if re.search(r"\b(?:tonight|aaj raat)\b", t):
        return today, True, False, False
    if re.search(r"\b(?:today|aaj|this (?:morning|afternoon|evening))\b", t):
        return today, False, False, False
    m = re.search(r"\b(?:(next|this|coming|agle|is)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", t)
    wd, later = None, False
    if m:
        wd, later = _WEEKDAYS[m.group(2)], m.group(1) in ("next", "agle")
    else:  # "sat" and "sun" are words too: only after on/this/next, or right before a time
        m = re.search(r"\b(?:on|this|next|coming)\s+(" + "|".join(_SHORT_WEEKDAYS) + r")\b|\b("
                      + "|".join(_SHORT_WEEKDAYS) + r")\s+(?:at\s+)?\d", t)
        if m:
            wd = _SHORT_WEEKDAYS[m.group(1) or m.group(2)]
    if wd is not None:
        ahead = (wd - today.weekday()) % 7 or (7 if later else 0)
        return today + timedelta(days=ahead), False, False, True
    if re.search(r"\b(?:this weekend|the weekend|weekend (?:pe|par|ko))\b", t):
        return today + timedelta(days=(5 - today.weekday()) % 7), False, True, False
    m = re.search(r"\b(?:on )?the (\d{1,2})(?:st|nd|rd|th)\b", t)
    if m and 1 <= int(m.group(1)) <= 31:
        d = int(m.group(1))
        y, mo = today.year, today.month
        for _ in range(3):
            try:
                day = date(y, mo, d)
                if day >= today:
                    return day, False, False, False
            except ValueError:
                pass
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return None, False, False, False


def _hour(h: int, ampm: str, part: str, bias: str) -> Optional[int]:
    if h > 23:
        return None
    if ampm:
        if h > 12 or h == 0:
            return None
        return (h % 12) + (12 if ampm.startswith("p") else 0)
    if h >= 13:
        return h
    if part == "pm":
        return h if h == 12 else h + 12
    if part == "am":
        return 0 if h == 12 else h
    if bias == "pm":
        return h if h == 12 else h + 12
    if bias == "am":
        return h
    if bias == "lunch":
        return h if h >= 11 else h + 12
    return h + 12 if 1 <= h <= 7 else h  # nobody plans a meeting at 4 in the morning


def _time(t: str, bias: str, tonight: bool) -> tuple[Optional[tuple[int, int]], Optional[tuple[int, int]], bool]:
    """(start (h, m) or None, end or None, approximate)."""
    part, part_hour = "", None
    for rx, half, default in _PARTS:
        if rx.search(t):
            part, part_hour = half, default
            break
    if tonight and not part:
        part, part_hour = "pm", 20
    m = _RANGE.search(t)
    if m:
        end_ampm = (m.group(6) or "").lower()
        start_ampm = (m.group(3) or "").lower() or end_ampm
        sh, eh = _hour(int(m.group(1)), start_ampm, part, bias), _hour(int(m.group(4)), end_ampm, part, bias)
        if sh is not None and eh is not None:
            if not m.group(3) and sh > eh:  # "11-1pm": 11 in the morning
                sh -= 12
            start, end = (sh, int(m.group(2) or 0)), (eh, int(m.group(5) or 0))
            if start < end and start[1] < 60 and end[1] < 60:
                return start, end, False
    m = _CLOCK_AMPM.search(t)
    if m:
        h = _hour(int(m.group(1)), m.group(3).lower(), part, bias)
        mm = int(m.group(2) or 0)
        if h is not None and mm < 60:
            return (h, mm), None, False
    m = _CLOCK_24.search(t)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        if h < 13:
            h = _hour(h, "", part, bias)
        if h is not None:
            return (h, mm), None, False
    m = _CLOCK_LOOSE.search(t)
    if m:
        raw_h, raw_m = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        h = _hour(int(raw_h), "", part, bias)
        mm = int(raw_m or 0)
        if h is not None and mm < 60:
            return (h, mm), None, False
    if _NOON.search(t):
        return (12, 0), None, False
    if part_hour is not None:
        return (part_hour, 0), None, True
    return None, None, False


def find(text: str, ref: datetime) -> Optional[dict]:
    """The plan a message proposes, or None. ``ref``: when the message was written (local)."""
    if not text or len(text) > 1500:
        return None
    t = " ".join(text.lower().split())
    if _CANCEL.search(t) or _PAST_EN.search(t):
        return None
    for rx, title, minutes, bias in _ACTIVITIES:
        act = rx.search(t)
        if act:
            break
    else:
        return None
    day, tonight, unsure, weekday = _day(t, ref)
    start_hm, end_hm, approximate = _time(t, bias, tonight)
    if day is None and unsure:
        return None
    if day is None and start_hm is None:
        return None
    if day is None:  # a time alone: its next occurrence
        day = ref.date()
        if datetime.combine(day, datetime.min.time()).replace(hour=start_hm[0], minute=start_hm[1]) <= ref:
            day += timedelta(days=1)
    all_day = start_hm is None
    if all_day:
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
    else:
        start = datetime.combine(day, datetime.min.time()).replace(hour=start_hm[0], minute=start_hm[1])
        end = (datetime.combine(day, datetime.min.time()).replace(hour=end_hm[0], minute=end_hm[1]) if end_hm
               else start + timedelta(minutes=minutes))
        m = re.search(r"\bfor (\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?)\b", t)
        if m and not end_hm:
            amount = float(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
            if 5 <= amount <= 12 * 60:
                end = start + timedelta(minutes=amount)
    if weekday and (end if all_day else start) <= ref:  # "Friday 4pm" on Friday evening: next Friday
        start, end = start + timedelta(days=7), end + timedelta(days=7)
    if (end if all_day else start) <= ref - timedelta(minutes=5) or start > ref + timedelta(days=HORIZON_DAYS):
        return None
    clean = " ".join(text.split())
    return {"kind": title, "start": start, "end": end, "all_day": all_day,
            "approximate": approximate or unsure, "phrase": _sentence(clean, act.start())}


# ─── calendar files and links ────────────────────────────────────────────────

def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lines longer than 75 bytes are folded (RFC 5545), never inside a character."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def ics_text(plan: dict, now: float, description: str = "") -> str:
    uid = hashlib.sha256(f"{plan['id']}:{plan['message_id']}".encode()).hexdigest()[:20]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//unified-inbox//Hermes plugin//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "BEGIN:VEVENT", f"UID:{uid}@unified-inbox", f"DTSTAMP:{_utc(now)}"]
    if plan["all_day"]:
        d = datetime.fromtimestamp(plan["start_ts"]).date()
        lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
    else:
        lines += [f"DTSTART:{_utc(plan['start_ts'])}", f"DTEND:{_utc(plan['end_ts'])}"]
    lines.append("SUMMARY:" + _ics_escape(plan["title"]))
    if description:
        lines.append("DESCRIPTION:" + _ics_escape(description))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def calendar_links(plan: dict, description: str = "") -> dict:
    """Links that open a filled-in "new event" page (nothing is saved until you press Save)."""
    if plan["all_day"]:
        d = datetime.fromtimestamp(plan["start_ts"]).date()
        g_dates = f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}"
        o_start, o_end, all_day = d.isoformat(), (d + timedelta(days=1)).isoformat(), "true"
    else:
        g_dates = f"{_utc(plan['start_ts'])}/{_utc(plan['end_ts'])}"
        o_start = datetime.fromtimestamp(plan["start_ts"]).astimezone().isoformat(timespec="minutes")
        o_end = datetime.fromtimestamp(plan["end_ts"]).astimezone().isoformat(timespec="minutes")
        all_day = "false"
    title, details = quote(plan["title"]), quote(description[:300])
    return {
        "google": f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={title}&dates={g_dates}"
                  f"&details={details}",
        "outlook": f"https://outlook.live.com/calendar/0/deeplink/compose?path=%2Fcalendar%2Faction%2Fcompose"
                   f"&rru=addevent&subject={title}&startdt={quote(o_start)}&enddt={quote(o_end)}&allday={all_day}"
                   f"&body={details}",
    }


def when_label(start_ts: float, end_ts: float, all_day: bool, approximate: bool = False) -> str:
    s = datetime.fromtimestamp(start_ts)
    day = s.strftime("%a %d %b")
    if all_day:
        return day + " (no time set)"
    e = datetime.fromtimestamp(end_ts)
    label = f"{day}, {s:%H:%M}-{e:%H:%M}"
    return label + (" (time is a guess)" if approximate else "")


class Planner:
    """Plans found in one inbox's chats: finding, alerts, free/busy and adding to a calendar."""

    def __init__(self, inbox, feed=None):
        self.inbox = inbox
        self.feed = feed  # CalendarFeed: your calendar's events (optional)

    def enabled(self) -> bool:
        return bool(self.inbox.settings.plan_detection)

    def _title(self, kind: str, chat: dict) -> str:
        name = chat.get("name") or "chat"
        if chat.get("is_group"):
            return f"{kind} ({name})"
        return f"Meet {name}" if kind == "Meet" else f"{kind} with {name}"

    def detect(self, msg: dict, chat: dict) -> Optional[int]:
        """Save the plan a message proposes. The plan's id, or None (no plan, or already known)."""
        found = find(msg.get("text") or "", datetime.fromtimestamp(msg["ts"]))
        if not found:
            return None
        store, now = self.inbox.store(), self.inbox.clock()
        start, end = found["start"].timestamp(), found["end"].timestamp()
        if end <= now:
            return None  # read only after it was over (e.g. the computer was off)
        for p in store.plans(since=now - DAY, chat_id=chat["id"], statuses=("suggested", "added", "dismissed")):
            if abs(p["start_ts"] - start) <= DEDUPE_SECONDS or (found["all_day"] and p["all_day"]
                                                                  and abs(p["start_ts"] - start) < DAY):
                return None  # the same plan, confirmed or repeated
        return store.add_plan(chat_id=chat["id"], message_id=msg["id"], from_me=bool(msg["from_me"]),
                              title=self._title(found["kind"], chat), start_ts=start, end_ts=end,
                              all_day=found["all_day"], approximate=found["approximate"],
                              phrase=redact(found["phrase"]), now=now)

    def scan_new(self) -> int:
        """Look for plans in messages that arrived since last time; alert once per plan."""
        inbox = self.inbox
        store = inbox.store()
        cursor = store.get_meta("plans_cursor")
        if cursor is None or not self.enabled():
            store.set_meta("plans_cursor", store.last_event_id())
            return 0
        events = store.events_after(int(cursor), types=["message.new"], limit=1000)
        if not events:
            return 0
        store.set_meta("plans_cursor", events[-1]["id"])
        found = 0
        for e in events:
            msg = store.get_message(e["message_id"]) if e["message_id"] else None
            chat = store.cached_chat(e["chat_id"]) if e["chat_id"] else None
            if not msg or not chat or chat["is_muted"]:
                continue
            plan_id = self.detect(msg, chat)
            if not plan_id:
                continue
            found += 1
            plan = store.plan(plan_id)
            view = self.view(plan)
            app = NETWORK_LABELS.get(chat["network"], chat["network"])
            busy = view.get("busy_with") or []
            body = (f"From your {app} chat: \"{plan['phrase'][:140]}\"."
                    + (f" You already have something then: {busy[0]['when']}." if busy else "")
                    + " Add it to your calendar from the Inbox page, or ask Hermes.")
            inbox.automation.notify("plan", f"Plan: {plan['title']}, {view['when']}", body, chat_id=chat["id"],
                                    data={"plan_id": plan_id})
        return found

    # ── free / busy ──
    def busy(self, start_ts: float, end_ts: float, *, exclude: Optional[int] = None) -> list[dict]:
        """What you already have between these times: plans you added, and your calendar."""
        store = self.inbox.store()
        out = []
        for p in store.plans(since=start_ts, until=end_ts, statuses=("added",)):
            if p["id"] != exclude and not p["all_day"]:
                out.append({"title": p["title"], "start_ts": p["start_ts"], "end_ts": p["end_ts"], "source": "plan"})
        if self.feed is not None and self.feed.configured:
            for ev in self.feed.events(start_ts, end_ts):
                if not ev["all_day"]:
                    out.append(dict(ev, source="calendar"))
        out.sort(key=lambda x: x["start_ts"])
        return [dict(x, when=when_label(x["start_ts"], x["end_ts"], False)) for x in out]

    def availability(self, start_ts: float, end_ts: float, *, exclude: Optional[int] = None) -> dict:
        busy = self.busy(start_ts, end_ts, exclude=exclude)
        connected = self.feed is not None and self.feed.configured
        out = {"calendar_connected": connected, "free": not busy if (busy or connected) else None,
               "busy_with": busy}
        if self.feed is not None and connected and self.feed.problem():
            out["calendar_problem"] = self.feed.problem()
        return out

    # ── views and actions ──
    def view(self, plan: dict) -> dict:
        store = self.inbox.store()
        chat = store.cached_chat(plan["chat_id"]) or {"id": plan["chat_id"], "name": "", "network": ""}
        out = {"plan_id": plan["id"], "title": plan["title"],
               "when": when_label(plan["start_ts"], plan["end_ts"], plan["all_day"], plan["approximate"]),
               "start": datetime.fromtimestamp(plan["start_ts"]).astimezone().isoformat(timespec="minutes"),
               "end": datetime.fromtimestamp(plan["end_ts"]).astimezone().isoformat(timespec="minutes"),
               "start_ts": plan["start_ts"], "end_ts": plan["end_ts"], "all_day": plan["all_day"],
               "approximate": plan["approximate"], "status": plan["status"], "said": plan["phrase"],
               "proposed_by": "me" if plan["from_me"] else (chat.get("name") or "them"),
               "chat_id": plan["chat_id"], "name": chat.get("name", ""), "network": chat.get("network", ""),
               "message_id": plan["message_id"]}
        if not plan["all_day"]:
            out.update(self.availability(plan["start_ts"], plan["end_ts"], exclude=plan["id"]))
        return out

    def _description(self, plan: dict, view: dict) -> str:
        app = NETWORK_LABELS.get(view["network"], view["network"]) or "chat"
        return f"From your {app} chat with {view['name'] or 'them'}: \"{plan['phrase'][:200]}\""

    def upcoming(self, *, days: int = 14, chat_id: str = "", include_dismissed: bool = False) -> list[dict]:
        now = self.inbox.clock()
        statuses = ("suggested", "added", "dismissed") if include_dismissed else ("suggested", "added")
        return [self.view(p) for p in self.inbox.store().plans(since=now, until=now + days * DAY, chat_id=chat_id,
                                                               statuses=statuses)]

    def _plan(self, plan_id) -> dict:
        from .client import InboxError

        plan = self.inbox.store().plan(plan_id)
        if not plan:
            raise InboxError(f"No plan {plan_id!r}. List them with inbox_calendar.", kind="not_found")
        return plan

    def add(self, plan_id) -> dict:
        """Mark a plan as going into your calendar; returns the links and the .ics file to add it."""
        store, now = self.inbox.store(), self.inbox.clock()
        plan = self._plan(plan_id)
        store.set_plan(plan["id"], now, status="added")
        plan = store.plan(plan["id"])
        view = self.view(plan)
        desc = self._description(plan, view)
        view["add_with"] = calendar_links(plan, desc)
        path = self.ics_file(plan, desc)
        if path:
            view["ics_file"] = str(path)
        view["how"] = ("Open one of the add_with links (Google Calendar or Outlook) and press Save, or open the"
                       " .ics file with your calendar app. Hermes's Google Workspace skill can also create it.")
        return view

    def dismiss(self, plan_id) -> dict:
        plan = self._plan(plan_id)
        self.inbox.store().set_plan(plan["id"], self.inbox.clock(), status="dismissed")
        return {"plan_id": plan["id"], "dismissed": True}

    def ics_file(self, plan: dict, description: str = "") -> Optional[Path]:
        folder = self.inbox.store().data_dir()
        if folder is None:
            return None
        folder = folder / "calendar"
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("plan-*.ics"):  # one file at a time is enough
            try:
                if time.time() - old.stat().st_mtime > DAY:
                    old.unlink()
            except OSError:
                pass
        path = folder / f"plan-{plan['id']}.ics"
        path.write_text(ics_text(plan, self.inbox.clock(), description), encoding="utf-8", newline="")
        return path

    def for_prompt(self, chat_id: str) -> list[str]:
        """Lines for a draft prompt: plans proposed in this chat and whether you are free then
        (times only: never what your other events are)."""
        lines = []
        for v in self.upcoming(days=30, chat_id=chat_id)[:3]:
            if v["all_day"]:
                lines.append(f"Plan proposed in this chat: {v['title']} on {v['when']}.")
                continue
            if v.get("free") is True:
                state = "the user is free then"
            elif v.get("free") is False:
                state = "the user is busy then (" + ", ".join(b["when"].split(", ")[-1] for b in v["busy_with"][:2]) \
                        + "); do not accept, suggest the user picks another time with a blank like [another time]"
            else:
                state = "the user's calendar is unknown: do not confirm the time, leave a blank like [yes/no]"
            lines.append(f"Plan proposed in this chat: {v['title']}, {v['when']}: {state}.")
        return lines
