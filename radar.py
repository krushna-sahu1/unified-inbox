"""Relationship radar: the people side of your inbox, once a week (or whenever you ask).

Worked out on this machine from the messages already kept (no Unipile or model call):
  * waiting longest: people (not groups, not strangers, not likely scams) whose last message
    to you is still unanswered, longest first;
  * drifting: people you used to talk to regularly who have gone quiet, compared with how
    often you used to talk (all their apps count: a person you now talk to on LinkedIn is not
    drifting on WhatsApp);
  * your reply time: how long you take to answer, this week against the four weeks before, and
    per app;
  * birthdays: from what memory learned about people, and from the chats themselves (a
    "happy birthday" you sent on a date, or "my birthday is on 12 March"), coming up in the next
    two weeks. You get an alert the day before.
A weekly alert sums it up (with ``people_radar`` on). Group chats never count as people.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime
from typing import Optional

from .pacing import DAY, NETWORK_LABELS

WAITING_DAYS = 30            # an unanswered message older than this is not "waiting" any more
DRIFT_LOOKBACK_DAYS = 180
DRIFT_MIN_MESSAGES = 15      # you talked at least this much...
DRIFT_MIN_ACTIVE_WEEKS = 3   # ...over at least this many different weeks...
DRIFT_MIN_SILENCE_DAYS = 21  # ...and it has been at least this quiet (and 3x your usual gap)
REPLY_MAX_SECONDS = 7 * DAY  # a "reply" after a week is a new conversation, not a reply
BIRTHDAY_AHEAD_DAYS = 14
WEEKLY_SECONDS = 7 * DAY

_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                            "nov", "dec"])}
_BDAY_WORD = r"(?:birthday|bday|b'day|b-day|bdy|janamdin|janmdin|जन्मदिन)"
_WISH = re.compile(r"\b(?:happy|hapy|hppy)\s+(?:birthday|bday|b'day|b-day|bdy)\b|\bhbd\b|"
                   r"janamdin (?:ki|ki bahut) (?:badhai|shubhkamnaye|shubhkamnayein|mubarak)|जन्मदिन की", re.I)
_MY_DAY_TODAY = re.compile(r"\b(?:it'?s|its|today is|aaj)\s+(?:my|mera)\s+" + _BDAY_WORD, re.I)
_DATE_TEXT = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                        r"[a-z]*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})"
                        r"(?:st|nd|rd|th)?\b", re.I)
_MY_DAY_ON = re.compile(r"\b(?:my|mera)\s+" + _BDAY_WORD + r"\b[^.\n]{0,25}", re.I)


def _date_in(text: str) -> Optional[tuple[int, int]]:
    """(month, day) written in a text, if valid."""
    m = _DATE_TEXT.search(text or "")
    if not m:
        return None
    d, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    month, day = _MONTHS[mon[:3].lower()], int(d)
    try:
        date(2024, month, day)  # a leap year: 29 Feb is a real birthday
    except ValueError:
        return None
    return month, day


def _human(seconds: Optional[float]) -> str:
    if seconds is None:
        return "no replies yet"
    minutes = seconds / 60
    if minutes < 60:
        return f"{max(1, round(minutes))} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} h".replace(".0 h", " h")
    return f"{hours / 24:.1f} days".replace(".0 days", " days")


class Radar:
    def __init__(self, inbox):
        self.inbox = inbox

    # ── people ──
    def _people(self) -> dict[str, dict]:
        """One entry per person: the chats they have (merged across apps), not groups."""
        store = self.inbox.store()
        chats = {c["id"]: c for c in store.cached_chats(limit=5000) if not c["is_group"]}
        people: dict[str, dict] = {}
        seen: set = set()
        for contact in store.contacts():
            ids = [c["chat_id"] for c in contact["chats"] if c["chat_id"] in chats]
            if ids:
                people[contact["id"]] = {"name": contact["name"], "chats": [chats[i] for i in ids]}
                seen.update(ids)
        for cid, c in chats.items():
            if cid not in seen:
                people["chat:" + cid] = {"name": c["name"], "chats": [c]}
        return people

    def _messages(self, chat_ids: list[str], since: float) -> list[tuple]:
        if not chat_ids:
            return []
        store = self.inbox.store()
        out = []
        for i in range(0, len(chat_ids), 500):
            part = chat_ids[i:i + 500]
            out += store._q(f"SELECT chat_id, from_me, ts, text FROM messages WHERE ts>=? AND chat_id IN"
                            f" ({','.join('?' * len(part))}) ORDER BY ts", (since, *part))
        return out

    # ── sections ──
    def waiting(self, people: dict, now: float, limit: int = 8) -> list[dict]:
        store = self.inbox.store()
        shield = getattr(self.inbox, "shield", None)
        snoozed = store.snoozed_chat_ids(now)
        out = []
        for person in people.values():
            for c in person["chats"]:
                if c["last_from_me"] or c["is_muted"] or c["is_archived"] or c["is_readonly"] or c["id"] in snoozed:
                    continue
                if now - c["ts"] > WAITING_DAYS * DAY or not c["ts"]:
                    continue
                last = store.cached_messages(c["id"], 1)  # the messages themselves, not the list's preview
                if not last or last[-1]["from_me"]:
                    continue
                if any(o["ts"] > c["ts"] and o["last_from_me"] for o in person["chats"]):
                    continue  # you answered them on another app since
                if shield is not None and (shield.stranger(c) or shield.for_chat(c)):
                    continue
                out.append({"chat_id": c["id"], "name": person["name"] or c["name"], "network": c["network"],
                            "waiting_days": round((now - c["ts"]) / DAY, 1),
                            "last_message": (last[-1]["text"] or "(a file or voice note)")[:120]})
        out.sort(key=lambda x: -x["waiting_days"])
        return out[:limit]

    def drifting(self, people: dict, msgs_by_chat: dict, now: float, limit: int = 5) -> list[dict]:
        out = []
        for person in people.values():
            ts = sorted(t for c in person["chats"] for (_f, t) in msgs_by_chat.get(c["id"], []))
            mine = sum(1 for c in person["chats"] for (f, _t) in msgs_by_chat.get(c["id"], []) if f)
            if len(ts) < DRIFT_MIN_MESSAGES or mine < 3:
                continue
            weeks = {int(t // (7 * DAY)) for t in ts}
            if len(weeks) < DRIFT_MIN_ACTIVE_WEEKS:
                continue
            days = sorted({int(t // DAY) for t in ts})
            gaps = [b - a for a, b in zip(days, days[1:])]
            usual = statistics.median(gaps) if gaps else 7
            silent = (now - ts[-1]) / DAY
            if silent < max(DRIFT_MIN_SILENCE_DAYS, usual * 3):
                continue
            last_chat = max(person["chats"], key=lambda c: c["ts"])
            out.append({"chat_id": last_chat["id"], "name": person["name"] or last_chat["name"],
                        "network": last_chat["network"], "quiet_days": int(silent),
                        "used_to_talk_every_days": max(1, round(usual)),
                        "messages_before": len(ts)})
        out.sort(key=lambda x: -(x["quiet_days"] / x["used_to_talk_every_days"]))
        return out[:limit]

    def reply_times(self, people: dict, msgs_by_chat: dict, now: float) -> dict:
        week, month = [], []
        by_app: dict[str, list] = {}
        networks = {c["id"]: c["network"] for p in people.values() for c in p["chats"]}
        for chat_id, rows in msgs_by_chat.items():
            waiting_since = None
            for from_me, ts in rows:
                if not from_me:
                    if waiting_since is None:
                        waiting_since = ts
                    continue
                if waiting_since is not None:
                    gap = ts - waiting_since
                    if 0 <= gap <= REPLY_MAX_SECONDS:
                        if ts >= now - 7 * DAY:
                            week.append(gap)
                            by_app.setdefault(networks.get(chat_id, ""), []).append(gap)
                        elif ts >= now - 35 * DAY:
                            month.append(gap)
                waiting_since = None
        this = statistics.median(week) if week else None
        before = statistics.median(month) if month else None
        trend = "steady"
        if this is not None and before is not None and before > 0:
            ratio = this / before
            trend = "faster" if ratio <= 0.8 else "slower" if ratio >= 1.25 else "steady"
        return {"this_week": _human(this), "this_week_seconds": this, "replies_this_week": len(week),
                "four_weeks_before": _human(before), "four_weeks_before_seconds": before, "trend": trend,
                "by_app": {NETWORK_LABELS.get(k, k): _human(statistics.median(v)) for k, v in by_app.items() if k}}

    def birthdays(self, people: dict, now: float, days_ahead: int = BIRTHDAY_AHEAD_DAYS) -> list[dict]:
        store = self.inbox.store()
        found: dict[str, dict] = {}  # person key -> {month, day, source}
        chat_owner = {c["id"]: key for key, p in people.items() for c in p["chats"]}
        for key, person in people.items():  # what memory learned
            if key.startswith("chat:"):
                continue
            for f in store.facts(key, limit=50):
                if re.search(_BDAY_WORD + r"|\bborn\b", f["text"], re.I):
                    md = _date_in(f["text"])
                    if md:
                        found[key] = {"month": md[0], "day": md[1], "source": "memory"}
                        break
        rows = store._q("SELECT chat_id, from_me, ts, text FROM messages WHERE"
                        " text LIKE '%birthday%' OR text LIKE '%bday%' OR text LIKE '%b''day%' OR text LIKE '%hbd%'"
                        " OR text LIKE '%janamdin%' OR text LIKE '%janmdin%' OR text LIKE '%जन्मदिन%'"
                        " ORDER BY ts DESC LIMIT 2000")
        # Birthdays read from messages are remembered: the message itself (a wish a year ago) is
        # usually gone by the next birthday, when old messages are cleaned up.
        saved = dict(store.get_meta("radar_birthdays", {}) or {})
        changed = False
        for chat_id, from_me, ts, text in rows:
            key = chat_owner.get(chat_id)
            if not key or key in found:
                continue
            when = datetime.fromtimestamp(ts)
            entry = None
            if from_me and _WISH.search(text or ""):  # you wished them: their birthday was that day
                entry = {"month": when.month, "day": when.day, "source": "you wished them on this day"}
            elif not from_me and _MY_DAY_TODAY.search(text or ""):
                entry = {"month": when.month, "day": when.day, "source": "they said so"}
            elif not from_me:
                m = _MY_DAY_ON.search(text or "")
                md = _date_in(m.group(0)) if m else None
                if md:
                    entry = {"month": md[0], "day": md[1], "source": "they said so"}
            if entry:
                found[key] = entry
                if saved.get(chat_id) != entry:
                    saved[chat_id], changed = entry, True
        for chat_id, entry in saved.items():
            key = chat_owner.get(chat_id)
            if key and key not in found and isinstance(entry, dict) and entry.get("month") and entry.get("day"):
                found[key] = entry
        if changed:
            store.set_meta("radar_birthdays", dict(list(saved.items())[-2000:]))
        today = datetime.fromtimestamp(now).date()
        out = []
        for key, b in found.items():
            nxt = _next(b["month"], b["day"], today)
            ahead = (nxt - today).days
            if 0 <= ahead <= days_ahead:
                person = people[key]
                chat = max(person["chats"], key=lambda c: c["ts"])
                out.append({"name": person["name"] or chat["name"], "chat_id": chat["id"], "network": chat["network"],
                            "date": nxt.strftime("%a %d %b"), "in_days": ahead, "how_we_know": b["source"]})
        out.sort(key=lambda x: x["in_days"])
        return out

    # ── report ──
    def report(self) -> dict:
        now = self.inbox.clock()
        people = self._people()
        chat_ids = [c["id"] for p in people.values() for c in p["chats"]]
        msgs_by_chat: dict[str, list] = {}
        for chat_id, from_me, ts, _text in self._messages(chat_ids, now - DRIFT_LOOKBACK_DAYS * DAY):
            msgs_by_chat.setdefault(chat_id, []).append((bool(from_me), ts))
        out = {"waiting_longest": self.waiting(people, now),
               "drifting": self.drifting(people, msgs_by_chat, now),
               "your_reply_time": self.reply_times(people, msgs_by_chat, now),
               "birthdays": self.birthdays(people, now)}
        out["summary"] = summary_line(out)
        return out

    def run_alerts(self) -> int:
        """Birthday reminders (the day before and on the day) and the weekly summary."""
        inbox = self.inbox
        if not inbox.settings.people_radar:
            return 0
        store, now = inbox.store(), inbox.clock()
        last_check = float(store.get_meta("radar_checked_at", 0) or 0)
        if now - last_check < 6 * 3600:
            return 0
        store.set_meta("radar_checked_at", now)
        made = 0
        people = self._people()
        sent = dict(store.get_meta("radar_birthday_alerts", {}) or {})
        year = datetime.fromtimestamp(now).year
        for b in self.birthdays(people, now, days_ahead=1):
            mark = f"{b['chat_id']}:{year}:{b['date']}"
            if sent.get(mark):
                continue
            sent[mark] = now
            when = "today" if b["in_days"] == 0 else "tomorrow"
            app = NETWORK_LABELS.get(b["network"], b["network"])
            inbox.automation.notify("birthday", f"{b['name']}'s birthday is {when}",
                                    f"Worked out from your {app} chat ({b['how_we_know']}). Want Hermes to draft a"
                                    " wish?", chat_id=b["chat_id"])
            made += 1
        store.set_meta("radar_birthday_alerts", dict(list(sent.items())[-300:]))
        if now - float(store.get_meta("radar_weekly_at", 0) or 0) >= WEEKLY_SECONDS:
            if store.get_meta("radar_weekly_at") is not None:  # not on the very first day
                rep = self.report()
                inbox.automation.notify("radar", "Your week with people", rep["summary"])
                made += 1
            store.set_meta("radar_weekly_at", now)
        return made


def _next(month: int, day: int, today: date) -> date:
    for year in (today.year, today.year + 1, today.year + 2):
        try:
            d = date(year, month, day)
        except ValueError:  # 29 Feb in a normal year: the 28th
            d = date(year, month, 28)
        if d >= today:
            return d
    return today


def summary_line(rep: dict) -> str:
    parts = []
    w = rep["waiting_longest"]
    if w:
        top = w[0]
        waited = _human(top["waiting_days"] * DAY) if top["waiting_days"] < 2 else f"{top['waiting_days']:g} days"
        parts.append(f"{len(w)} {'person is' if len(w) == 1 else 'people are'} waiting on you, longest {top['name']}"
                     f" ({waited})")
    d = rep["drifting"]
    if d:
        parts.append("drifting apart: " + ", ".join(f"{x['name']} ({x['quiet_days']} days quiet)" for x in d[:3]))
    r = rep["your_reply_time"]
    if r["this_week_seconds"] is not None:
        trend = {"faster": "faster than", "slower": "slower than", "steady": "about the same as"}[r["trend"]]
        parts.append(f"you replied in {r['this_week']} on average, {trend} before"
                     if r["four_weeks_before_seconds"] is not None else f"you replied in {r['this_week']} on average")
    b = rep["birthdays"]
    if b:
        parts.append("birthdays: " + ", ".join(f"{x['name']} {x['date']}" for x in b[:3]))
    if not parts:
        return "All caught up: nobody is waiting on you and nobody is drifting away."
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]
