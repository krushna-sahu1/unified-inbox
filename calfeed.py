"""Your calendar, read-only: when you are busy, from its private iCal address.

Optional. Google Calendar: Settings -> your calendar -> "Secret address in iCal format".
Outlook: Settings -> Calendar -> Shared calendars -> Publish a calendar -> ICS link. Apple
iCloud: share the calendar publicly and copy the link. The address is stored as a secret
(``CALENDAR_ICS_URL``); the file is fetched over HTTPS at most every 15 minutes and only the
events of the next 60 days are kept, in memory and in the plugin's database. Nothing is ever
written to your calendar.

The parser covers what calendars publish: time zones (TZID, UTC, floating), all-day events,
cancelled and "free" events (ignored), and repeating events (daily, weekly on some days,
monthly on a day or "the second Tuesday", yearly; with an end date or count, skipped
dates and moved occurrences). A rule it cannot read is shown once, at its first date.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .pacing import DAY

REFRESH_SECONDS = 15 * 60
WINDOW_DAYS = 60
MAX_BYTES = 5 * 1024 * 1024
MAX_OCCURRENCES = 2000
_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_UNSUPPORTED = ("BYSETPOS", "BYWEEKNO", "BYYEARDAY", "BYHOUR", "BYMINUTE", "BYSECOND")
# Windows time zone names Outlook writes, to the IANA names Python knows.
_WINDOWS_TZ = {"india standard time": "Asia/Kolkata", "utc": "UTC", "gmt standard time": "Europe/London",
               "pacific standard time": "America/Los_Angeles", "eastern standard time": "America/New_York",
               "central standard time": "America/Chicago", "mountain standard time": "America/Denver",
               "w. europe standard time": "Europe/Berlin", "romance standard time": "Europe/Paris",
               "singapore standard time": "Asia/Singapore", "arabian standard time": "Asia/Dubai",
               "aus eastern standard time": "Australia/Sydney", "tokyo standard time": "Asia/Tokyo",
               "china standard time": "Asia/Shanghai"}


def _tz(name: str):
    """A tzinfo for a TZID, or None (then the time is read as this computer's local time)."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(_WINDOWS_TZ.get(name.strip().strip('"').lower(), name.strip().strip('"')))
    except Exception:  # noqa: BLE001 - unknown name or no time zone data on this computer
        return None


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def _prop(line: str) -> tuple[str, dict, str]:
    """``DTSTART;TZID=Asia/Kolkata:20261112T160000`` -> ("DTSTART", {"TZID": ...}, value)."""
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v
    return parts[0].upper(), params, value


def _when(value: str, params: dict) -> tuple[Optional[datetime], bool]:
    """(an aware or naive local datetime, all-day)."""
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value[:8], "%Y%m%d"), True
        if value.endswith("Z"):
            return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), False
        dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        tz = _tz(params.get("TZID", ""))
        return (dt.replace(tzinfo=tz) if tz else dt), False
    except ValueError:
        return None, False


def _ts(dt: datetime) -> float:
    return dt.timestamp()  # naive = this computer's local time


def _duration(value: str) -> Optional[timedelta]:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return None
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    td = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return -td if m.group(1) == "-" else td


def _add_months(dt: datetime, n: int) -> Optional[datetime]:
    y, mo = divmod(dt.month - 1 + n, 12)
    try:
        return dt.replace(year=dt.year + y, month=mo + 1)
    except ValueError:
        return None  # e.g. the 31st in a 30-day month: that month has no occurrence


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> Optional[int]:
    """Day of month of the n-th (n<0: from the end) weekday, or None."""
    first = date(year, month, 1)
    days_in = ((first.replace(month=month % 12 + 1, year=year + month // 12)) - first).days
    hits = [d for d in range(1, days_in + 1) if date(year, month, d).weekday() == weekday]
    try:
        return hits[n - 1] if n > 0 else hits[n]
    except IndexError:
        return None


def _occurrences(start: datetime, rule: str, until_window: datetime, exdates: set,
                 floor: Optional[datetime] = None) -> list[datetime]:
    """Start times of a repeating event up to ``until_window`` (and the rule's own end). With
    ``floor``, occurrences before it are counted (a COUNT still ends the series on time) but not
    returned, so a meeting that has repeated for years still reaches today."""
    r = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = r.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or any(k in r for k in _UNSUPPORTED):
        return [start]
    interval = max(1, int(r.get("INTERVAL", "1") or 1))
    count = int(r["COUNT"]) if r.get("COUNT", "").isdigit() else None
    until = None
    if r.get("UNTIL"):
        until, _ = _when(r["UNTIL"], {})
        if until is not None and until.tzinfo and start.tzinfo is None:
            until = until.astimezone().replace(tzinfo=None)
        elif until is not None and until.tzinfo is None and start.tzinfo is not None:
            until = until.replace(tzinfo=start.tzinfo)
    end = min(until_window, until) if until else until_window
    byday = [d.strip() for d in r.get("BYDAY", "").split(",") if d.strip()]
    bymonthday = [int(x) for x in r.get("BYMONTHDAY", "").split(",") if x.strip().lstrip("-").isdigit()]
    out: list[datetime] = []
    made = 0

    def emit(dt: datetime) -> bool:
        nonlocal made
        if dt < start:
            return True
        if dt > end or (count is not None and made >= count) or len(out) >= MAX_OCCURRENCES:
            return False
        made += 1
        if dt not in exdates and (floor is None or dt >= floor):
            out.append(dt)
        return True

    step = 0
    if floor is not None and count is None and floor > start and freq in ("DAILY", "WEEKLY"):
        # nothing to count: start a little before the window instead of at the first occurrence
        unit = interval * (7 if freq == "WEEKLY" else 1)
        step = max(0, (floor - start).days // unit - 1)
    last_step = step + 20000
    while step < last_step:
        if freq == "DAILY":
            dt = start + timedelta(days=step * interval)
            if dt > end or not emit(dt):
                break
        elif freq == "WEEKLY":
            week = start + timedelta(weeks=step * interval)
            if week - timedelta(days=week.weekday()) > end:
                break
            days = sorted(_DAYS[d[-2:]] for d in byday if d[-2:] in _DAYS) or [start.weekday()]
            monday = week - timedelta(days=week.weekday())
            if not all(emit(monday + timedelta(days=d)) for d in days):
                break
        elif freq == "MONTHLY":
            base = _add_months(start.replace(day=1), step * interval)
            if base is None or base > end:
                break
            candidates = []
            if byday:
                for d in byday:
                    m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", d)
                    if m and m.group(1):
                        day = _nth_weekday(base.year, base.month, _DAYS[m.group(2)], int(m.group(1)))
                        if day:
                            candidates.append(base.replace(day=day))
            else:
                nxt = _add_months(base, 1)
                last = (nxt - timedelta(days=1)).day if nxt else 28
                for md in bymonthday or [start.day]:
                    day = md if md > 0 else last + md + 1  # -1 = the last day of the month
                    if 1 <= day <= last:
                        candidates.append(base.replace(day=day))
            if not all(emit(c) for c in sorted(candidates)):
                break
        else:  # YEARLY
            try:
                dt = start.replace(year=start.year + step * interval)
            except ValueError:
                step += 1
                continue
            if dt > end or not emit(dt):
                break
        step += 1
    return out


def parse(text: str, window_start: float, window_end: float) -> list[dict]:
    """Busy events overlapping the window: [{title, start_ts, end_ts, all_day}], soonest first."""
    events, overrides = [], []
    cur: Optional[dict] = None
    depth = 0
    for line in _unfold(text):
        name, params, value = _prop(line)
        if name == "BEGIN":
            if value.upper() == "VEVENT":
                cur, depth = {"exdates": set()}, 0
            elif cur is not None:
                depth += 1  # VALARM inside an event
            continue
        if name == "END":
            if value.upper() == "VEVENT" and cur is not None:
                (overrides if "recurrence_id" in cur else events).append(cur)
                cur = None
            elif cur is not None:
                depth -= 1
            continue
        if cur is None or depth:
            continue
        if name in ("DTSTART", "DTEND"):
            cur[name.lower()], cur[name.lower() + "_all_day"] = _when(value, params)
        elif name == "DURATION":
            cur["duration"] = _duration(value)
        elif name == "SUMMARY":
            cur["title"] = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()[:120]
        elif name == "STATUS":
            cur["status"] = value.strip().upper()
        elif name == "TRANSP":
            cur["transp"] = value.strip().upper()
        elif name == "RRULE":
            cur["rrule"] = value.strip()
        elif name == "UID":
            cur["uid"] = value.strip()
        elif name == "RECURRENCE-ID":
            cur["recurrence_id"], _ = _when(value, params)
        elif name == "EXDATE":
            for v in value.split(","):
                dt, _ = _when(v, params)
                if dt is not None:
                    cur["exdates"].add(dt)
    moved = {(o.get("uid"), _key(o["recurrence_id"])) for o in overrides if o.get("recurrence_id") is not None}
    horizon = datetime.fromtimestamp(window_end)
    out = []
    for ev in events + overrides:
        start = ev.get("dtstart")
        if start is None or ev.get("status") == "CANCELLED" or ev.get("transp") == "TRANSPARENT":
            continue
        all_day = bool(ev.get("dtstart_all_day"))
        if ev.get("dtend") is not None:
            length = ev["dtend"] - start
        elif ev.get("duration") is not None:
            length = ev["duration"]
        else:
            length = timedelta(days=1) if all_day else timedelta(0)
        if length < timedelta(0):
            continue
        local_horizon = horizon.replace(tzinfo=start.tzinfo) if start.tzinfo else horizon
        starts = [start]
        if ev.get("rrule") and "recurrence_id" not in ev:
            floor = datetime.fromtimestamp(window_start) - length - timedelta(days=1)
            if start.tzinfo:
                floor = floor.replace(tzinfo=start.tzinfo)
            try:
                starts = _occurrences(start, ev["rrule"], local_horizon + timedelta(days=1), ev["exdates"], floor)
            except (ValueError, TypeError, OverflowError):
                starts = [start]  # a rule that cannot be read: shown once, at its first date
        for s in starts:
            if ev.get("rrule") and (ev.get("uid"), _key(s)) in moved:
                continue  # this occurrence was moved: the override is listed instead
            s_ts, e_ts = _ts(s), _ts(s + length)
            if e_ts > window_start and s_ts < window_end:
                out.append({"title": ev.get("title") or "Busy", "start_ts": s_ts, "end_ts": max(e_ts, s_ts),
                            "all_day": all_day})
    out.sort(key=lambda e: e["start_ts"])
    return out


def _key(dt: Optional[datetime]) -> Optional[float]:
    return _ts(dt) if dt is not None else None


class CalendarFeed:
    """The user's calendar feed, cached. ``fetch(url) -> bytes`` is injectable for tests."""

    def __init__(self, inbox, fetch: Optional[Callable[[str], bytes]] = None):
        self.inbox = inbox
        self._fetch = fetch or _fetch
        self._events: Optional[list[dict]] = None
        self._fetched_at = 0.0
        self._url = ""

    def url(self) -> str:
        raw = (self.inbox._get_secret("CALENDAR_ICS_URL") or "").strip()
        if raw.lower().startswith("webcal://"):
            raw = "https://" + raw[len("webcal://"):]
        return raw

    @property
    def configured(self) -> bool:
        return bool(self.url())

    def problem(self) -> str:
        return str((self.inbox.store().get_meta("calendar_state", {}) or {}).get("error") or "")

    def events(self, start_ts: float, end_ts: float) -> list[dict]:
        self.refresh()
        return [e for e in (self._events or []) if e["end_ts"] > start_ts and e["start_ts"] < end_ts]

    def refresh(self, force: bool = False) -> None:
        url = self.url()
        if not url:
            self._events = None
            return
        now = self.inbox.clock()
        store = self.inbox.store()
        if self._events is None:  # after a restart: the last copy, if it is of this address
            saved = store.get_meta("calendar_events") or {}
            if saved.get("url_hash") == _hash(url):
                self._events, self._fetched_at, self._url = saved.get("events") or [], float(saved.get("at") or 0), url
        if not force and url == self._url and now - self._fetched_at < REFRESH_SECONDS:
            return
        if not url.lower().startswith("https://"):
            store.set_meta("calendar_state", {"at": now, "error": "The calendar address must start with https://"
                                                                   " (or webcal://)."})
            self._fetched_at, self._url = now, url
            return
        try:
            raw = self._fetch(url)
            text = raw.decode("utf-8", errors="replace")
            if "BEGIN:VCALENDAR" not in text[:2000].upper():
                raise ValueError("that address did not return a calendar (.ics)")
            events = parse(text, now - DAY, now + WINDOW_DAYS * DAY)
        except Exception as exc:  # noqa: BLE001 - keep the last good copy; say what went wrong
            store.set_meta("calendar_state", {"at": now, "error": f"Could not read your calendar: {_reason(exc)}"})
            self._fetched_at, self._url = now, url  # try again after REFRESH_SECONDS, not on every call
            return
        self._events, self._fetched_at, self._url = events, now, url
        store.set_meta("calendar_events", {"url_hash": _hash(url), "at": now, "events": events})
        store.set_meta("calendar_state", {"at": now, "error": "", "events": len(events)})

    def forget(self) -> None:
        self._events, self._fetched_at, self._url = None, 0.0, ""


def _hash(url: str) -> str:
    import hashlib

    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"the calendar server answered {exc.code}" + (" (check the address)" if exc.code in (401, 403, 404)
                                                              else "")
    if isinstance(exc, urllib.error.URLError):
        return f"could not connect ({exc.reason})"
    return str(exc) or type(exc).__name__


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "unified-inbox (Hermes plugin)",
                                               "Accept": "text/calendar, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - https only, checked above
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("the calendar is larger than 5 MB")
    return data
