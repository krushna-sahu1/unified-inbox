"""Scam warnings, plans into your calendar, translation, the people radar and the spoken brief."""

import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import KEY, make_inbox
from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend
from unified_inbox import calfeed, data_control, plans, sending, shield, translate
from unified_inbox.api import FileResult, InboxAPI
from unified_inbox.commands import build_command
from unified_inbox.context import SETTLE_SECONDS, ContextEngine
from unified_inbox.pacing import DAY
from unified_inbox.sync import SyncEngine, SyncWorker
from unified_inbox.tools import build_handlers
from unified_inbox.voice import make_speaker
from test_smart import Brain

OTP_ASK = "Hi, I sent you a 6 digit code by mistake, can you please send it to me? urgent"


class Model(Brain):
    """Hermes's model, also answering translation requests."""

    def __init__(self):
        super().__init__()
        self.translations = 0

    def __call__(self, instructions, text, schema):
        if instructions == translate.INSTRUCTIONS:
            self.translations += 1
            self.prompts.setdefault("translate", []).append(text)
            items = json.loads(text.rsplit("\n", 1)[1])
            return {"items": [{"id": x["id"], "text": "EN: " + x["text"]} for x in items]}
        if instructions == translate.REPLY_INSTRUCTIONS:
            self.prompts.setdefault("reply", []).append(text)
            return {"text": "HI: " + text.rsplit("\n", 1)[1]}
        return super().__call__(instructions, text, schema)


def _inbox(clock, folder, config=None, secrets=None):
    backend = SampleBackend(clock)
    conn = sqlite3.connect(str(folder / "data.db"), check_same_thread=False)
    conn.execute("PRAGMA synchronous=OFF")
    i = make_inbox(clock, {"unipile_account_id": SAMPLE_ACCOUNT_IDS, **(config or {})},
                   secrets={**KEY, **(secrets or {})}, backend=backend, conn=conn)
    i.backend = backend
    i.brain = Model()
    i.context = ContextEngine(i, llm=i.brain)
    i._local_now = lambda: datetime.fromtimestamp(clock())
    _sync(i)
    _background(i)  # switched on now: history never alerts
    i.autodraft.run_once()
    clock.advance(1)
    return i


def _sync(inbox):
    while SyncEngine(inbox).sync_once()["pending_chats"]:
        pass


def _background(inbox):
    SyncWorker(inbox)._insights()


def _arrive(inbox, clock, chat_id, text, **kw):
    msg = inbox.backend.receive(chat_id, text, **kw)
    _sync(inbox)
    _background(inbox)
    clock.advance(SETTLE_SECONDS + 1)
    return msg


def _tool(inbox, name, args):
    return json.loads(build_handlers(inbox)[name](args))


def _alerts(inbox, kind):
    return [n for n in inbox.store().notifications(unread_only=False, limit=100) if n["kind"] == kind]


@pytest.fixture
def inbox(clock, tmp_path):
    return _inbox(clock, tmp_path)


# ─── scam shield ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, stranger, level", [
    ("Hi, I sent you a 6 digit code by mistake, can you please send it to me?", False, "high"),
    ("send me the OTP jaldi", False, "high"),
    ("otp bata do please", False, "high"),
    ("कृपया ओटीपी बताओ", True, "high"),
    ("Dear customer your SBI account will be blocked today. Update KYC at http://sbi-kyc-update.xyz/login", True,
     "high"),
    ("Congratulations! You have won Rs 25,00,000 in KBC lottery. Pay processing fee to claim", True, "high"),
    ("Hi mum this is my new number, I need 20000 urgently, don't tell dad", False, "high"),
    ("install anydesk so I can help you", True, "high"),
    ("scan this QR to receive your refund", True, "high"),
    ("Guaranteed returns 5% daily in crypto, invest now", True, "high"),
    ("download https://example.com/app.apk", True, "high"),
    ("Part time job: like youtube videos and earn Rs 3000 daily", True, "caution"),
    ("check this https://paypa1.com/signin", False, "caution"),
    # ordinary messages stay quiet
    ("Your OTP is 482913. Do not share it with anyone.", True, None),
    ("send me 500 for dinner on gpay", False, None),
    ("Let's split the bill, 1200 each. Send on paytm", False, None),
    ("Can you send me the updated deck before the deadline?", False, None),
    ("my card got blocked lol", False, None),
    ("check this https://bit.ly/3xYz", True, None),
    ("https://www.amazon.in/dp/B0C and pineapplestore.com/menu", False, None),
    ("kal milte hain, urgent hai", True, None),
])
def test_scam_signs_in_three_languages_without_false_alarms(text, stranger, level):
    v = shield.verdict([text], stranger=stranger)
    assert (v["level"] if v else None) == level, v


@pytest.mark.parametrize("url, sign", [
    ("http://paypal.com.secure-login.ru/x", "lookalike"), ("https://amaz0n-rewards.top/a", "lookalike"),
    ("https://appleid-verify.com/x", "lookalike"), ("http://192.168.0.1/login", "ip"),
    ("https://xn--pypal-4ve.com/", "punycode"), ("https://bit.ly/abc", "short"),
    ("https://docs.google.com/x", None), ("https://wa.me/123", None), ("https://onlinesbi.sbi/", None),
    ("https://www.icicibank.com/login", None), ("https://linkedin.com/in/priya", None),
])
def test_link_signs(url, sign):
    keys = [k for k, _r in shield.link_signs(url)]
    assert (sign in keys) if sign else keys == []


def test_a_scam_alerts_once_and_is_flagged_everywhere(inbox, clock):
    _arrive(inbox, clock, "ig-tickets", OTP_ASK)
    alerts = _alerts(inbox, "scam")
    assert len(alerts) == 1 and alerts[0]["title"].startswith("Likely a scam: ")
    assert "code" in alerts[0]["body"].lower()
    _background(inbox)
    assert len(_alerts(inbox, "scam")) == 1  # once per burst
    chat = _tool(inbox, "inbox_read_chat", {"chat_id": "ig-tickets"})
    assert chat["safety_warning"]["level"] == "high"
    assert any(m.get("warning") for m in chat["messages"] if "by mistake" in m["text"])
    triage = inbox.triage(hours=24)
    assert any(a["chat_id"] == "ig-tickets" for a in triage["possible_scams"])
    assert all(a["chat_id"] != "ig-tickets" for a in triage["urgent"] + triage["unanswered"])
    api = InboxAPI(inbox)
    row = next(c for c in api.handle("GET", "/chats")[1]["chats"] if c["id"] == "ig-tickets")
    assert row["scam"]["level"] == "high" and not row["needs_reply"]
    msgs = api.handle("GET", "/chats/ig-tickets")[1]["messages"]
    assert any(m["warning"] for m in msgs)
    assert api.handle("GET", "/safety")[1]["flagged"][0]["chat_id"] == "ig-tickets"
    listed = _tool(inbox, "inbox_safety", {})
    assert listed["flagged"][0]["chat_id"] == "ig-tickets"
    assert "ig-tickets" not in json.dumps(_tool(inbox, "inbox_safety", {"action": "check", "text": "see you at 5"}))
    assert _tool(inbox, "inbox_safety", {"action": "check", "text": "send me the otp"})["looks_like_a_scam"]
    assert "These chats look like a scam" in build_command(inbox, lambda q, c: None)("scams")


def test_hermes_never_auto_drafts_for_a_scam(inbox, clock):
    _arrive(inbox, clock, "wa-mum", "Urgent!! send me the OTP you just got, can you confirm?")
    assert inbox.autodraft.run_once()["drafted"] == 0
    assert not [d for d in inbox.store().list_drafts("pending") if d["origin"] == "auto"]


def test_a_reply_sharing_a_code_with_a_scam_chat_is_stopped_everywhere(inbox, clock):
    _arrive(inbox, clock, "ig-tickets", OTP_ASK)
    draft = inbox.create_draft("ig-tickets", "ok the code is 482913")
    assert any("looks like a scam" in r for r in draft["would_be_blocked_now_because"])
    stored = inbox.store().get_draft(draft["draft_id"])
    assert any("looks like a scam" in r for r in inbox.preflight(stored))
    api = InboxAPI(inbox)
    out = api.handle("POST", f"/drafts/{draft['draft_id']}/send", body={"confirm": True, "text": stored["text"]})[1]
    assert out["sent"] is False
    args = {"chat_id": "ig-tickets", "text": "the code is 482913"}
    assert sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args=args) is None
    assert "looks like a scam" in _tool(inbox, "inbox_send", args)["error"]
    assert not [m for m in inbox.backend.sent if m["from_me"]]
    # a harmless reply is allowed, and the approval prompt warns
    p = sending.plan(inbox, {"chat_id": "ig-tickets", "text": "who is this?"})
    assert p["status"] == "ok" and p["summary"].startswith("CAREFUL: this chat looks like a scam")
    # marked safe: no warnings and no block
    assert _tool(inbox, "inbox_safety", {"action": "safe", "chat_id": "ig-tickets"})["marked_safe"]
    assert inbox.shield.for_chat(inbox.store().cached_chat("ig-tickets")) is None
    assert not any("scam" in r for r in inbox.preflight(stored, check_accounts=False))
    _tool(inbox, "inbox_safety", {"action": "unsafe", "chat_id": "ig-tickets"})
    assert inbox.shield.for_chat(inbox.store().cached_chat("ig-tickets"))


def test_scam_shield_can_be_switched_off(clock, tmp_path):
    inbox = _inbox(clock, tmp_path, {"scam_shield": False})
    _arrive(inbox, clock, "ig-tickets", OTP_ASK)
    assert not _alerts(inbox, "scam") and "safety_warning" not in _tool(inbox, "inbox_read_chat",
                                                                       {"chat_id": "ig-tickets"})


# ─── plans ───────────────────────────────────────────────────────────────────

REF = datetime(2026, 11, 10, 11, 0)  # a Tuesday


@pytest.mark.parametrize("text, expect", [
    ("Let's meet Thursday 4pm?", ("Meet", "Thu 12 Nov 16:00", "17:00", False)),
    ("call tomorrow at 10", ("Call", "Wed 11 Nov 10:00", "10:30", False)),
    ("Dinner on 12 Dec, 8:30", ("Dinner", "Sat 12 Dec 20:30", "22:00", False)),
    ("kal shaam 6 baje milte hain", ("Meet", "Wed 11 Nov 18:00", "19:00", False)),
    ("lunch at 1?", ("Lunch", "Tue 10 Nov 13:00", "14:00", False)),
    ("meeting 3-4pm today", ("Meeting", "Tue 10 Nov 15:00", "16:00", False)),
    ("interview on 2026-11-20 at 14:30", ("Interview", "Fri 20 Nov 14:30", "15:30", False)),
    ("let's talk on Sunday", ("Call", "Sun 15 Nov 00:00", "00:00", True)),
    ("meet next tuesday at 9", ("Meet", "Tue 17 Nov 09:00", "10:00", False)),
    ("standup at 9:30 tomorrow for 15 mins", ("Meeting", "Wed 11 Nov 09:30", "09:45", False)),
    ("Can we do a call on the 15th at 11am", ("Call", "Sun 15 Nov 11:00", "11:30", False)),
    ("Friday drinks at 7!", ("Drinks", "Fri 13 Nov 19:00", "21:00", False)),
    ("Tuesday 9am meeting", ("Meeting", "Tue 17 Nov 09:00", "10:00", False)),  # today's is over: next week
    ("kal mile the, maza aaya", None), ("Sorry can't make it Thursday 4pm", None), ("see you soon", None),
    ("How was the dinner yesterday?", None), ("I sat with him at 5", None), ("meeting today at 10am", None),
    ("We should catch up sometime", None),
])
def test_plans_are_found_in_english_and_hinglish(text, expect):
    f = plans.find(text, REF)
    if expect is None:
        assert f is None
        return
    kind, start, end, all_day = expect
    assert (f["kind"], f"{f['start']:%a %d %b %H:%M}", f"{f['end']:%H:%M}", f["all_day"]) == (kind, start, end,
                                                                                           all_day)


def test_a_plan_alerts_once_and_goes_into_the_calendar(inbox, clock, tmp_path):
    msg = _arrive(inbox, clock, "wa-mum", "Let's meet Thursday 4pm?")
    expected = plans.find(msg["text"], datetime.fromtimestamp(msg["ts"]))
    alerts = _alerts(inbox, "plan")
    assert len(alerts) == 1 and alerts[0]["title"].startswith("Plan: Meet Mum, ")
    _arrive(inbox, clock, "wa-mum", "ok see you thursday 4pm", from_me=True)  # the same plan, confirmed
    listed = _tool(inbox, "inbox_calendar", {})
    assert len(listed["plans"]) == 1 and listed["calendar_connected"] is False
    p = listed["plans"][0]
    assert p["title"] == "Meet Mum" and p["status"] == "suggested" and p["free"] is None
    added = _tool(inbox, "inbox_calendar", {"action": "add", "plan_id": p["plan_id"]})
    utc = expected["start"].astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    assert added["status"] == "added" and utc in added["add_with"]["google"]
    assert "outlook.live.com" in added["add_with"]["outlook"]
    ics = Path(added["ics_file"]).read_bytes().decode("utf-8")
    assert f"DTSTART:{utc}" in ics and "SUMMARY:Meet Mum" in ics and ics.endswith("END:VCALENDAR\r\n")
    api = InboxAPI(inbox)
    page = api.handle("POST", f"/plans/{p['plan_id']}/add")[1]
    assert page["ics"]["filename"] == "Meet-Mum.ics" and "BEGIN:VEVENT" in page["ics"]["text"]
    status, f = api.handle("GET", f"/plans/{p['plan_id']}/ics")
    assert status == 200 and isinstance(f, FileResult) and f.mimetype == "application/octet-stream"
    # an added plan makes you busy for another plan at the same time
    other = inbox.planner.availability(expected["start"].timestamp(), expected["end"].timestamp())
    assert other["free"] is False and other["busy_with"][0]["title"] == "Meet Mum"
    assert _tool(inbox, "inbox_calendar", {"action": "dismiss", "plan_id": p["plan_id"]})["dismissed"]
    assert _tool(inbox, "inbox_calendar", {})["plans"] == []
    assert "No plans" in build_command(inbox, lambda q, c: None)("plans")


def _ics_event(start, end, title="Dentist"):
    z = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:{z(start)}\r\n"
            f"DTEND:{z(end)}\r\nSUMMARY:{title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode()


def test_your_calendar_says_when_you_are_busy_and_drafts_never_accept_a_clash(clock, tmp_path):
    inbox = _inbox(clock, tmp_path, secrets={"CALENDAR_ICS_URL": "webcal://calendar.example.com/private.ics"})
    text = "Can we meet tomorrow at 4pm? It's urgent, please confirm"
    proposed = plans.find(text, datetime.fromtimestamp(clock()))["start"]
    fetched = []

    def fetch(url):
        fetched.append(url)
        return _ics_event(proposed - timedelta(minutes=30), proposed + timedelta(minutes=30))

    inbox.calendar._fetch = fetch
    _arrive(inbox, clock, "wa-mum", text)
    plan = _tool(inbox, "inbox_calendar", {})["plans"][0]
    assert plan["free"] is False and plan["busy_with"][0]["what"] == "Dentist"
    assert fetched == ["https://calendar.example.com/private.ics"]  # webcal -> https, fetched once
    assert "something then" in _alerts(inbox, "plan")[0]["body"]
    assert inbox.autodraft.run_once()["drafted"] == 1
    prompt = inbox.brain.prompts["draft"][-1]
    assert "the user is busy then" in prompt and "Dentist" not in prompt  # times only, never what
    free = _tool(inbox, "inbox_calendar", {"action": "free", "start": (proposed + timedelta(hours=2)).isoformat(),
                                           "end": (proposed + timedelta(hours=3)).isoformat()})
    assert free["free"] is True and len(fetched) == 1  # cached for 15 minutes


def test_calendar_problems_are_explained_and_the_last_copy_is_kept(clock, tmp_path):
    inbox = _inbox(clock, tmp_path, secrets={"CALENDAR_ICS_URL": "https://calendar.example.com/private.ics"})
    now = datetime.fromtimestamp(clock())
    inbox.calendar._fetch = lambda url: _ics_event(now + timedelta(hours=1), now + timedelta(hours=2))
    assert len(inbox.calendar.events(clock(), clock() + DAY)) == 1

    def broken(url):
        raise ValueError("network down")

    inbox.calendar._fetch = broken
    clock.advance(calfeed.REFRESH_SECONDS + 1)
    assert len(inbox.calendar.events(clock(), clock() + DAY)) == 1
    assert "network down" in inbox.calendar.problem()
    inbox.calendar._fetch = lambda url: b"<html>login</html>"
    inbox.calendar.refresh(force=True)
    assert "did not return a calendar" in inbox.calendar.problem()
    checks = {c["name"]: c for c in __import__("unified_inbox.health", fromlist=["run"]).run(inbox)}
    assert checks["Plans for your calendar"]["ok"] is False


def test_the_calendar_feed_reads_repeats_exceptions_moves_and_time_zones():
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:b", "DTSTART:20261102T090000", "DTEND:20261102T093000",
        "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10", "EXDATE:20261111T090000", "SUMMARY:Standup", "END:VEVENT",
        "BEGIN:VEVENT", "UID:b", "RECURRENCE-ID:20261116T090000", "DTSTART:20261116T140000", "DTEND:20261116T143000",
        "SUMMARY:Standup (moved)", "END:VEVENT",
        "BEGIN:VEVENT", "UID:c", "DTSTART;VALUE=DATE:20261113", "DTEND;VALUE=DATE:20261114", "SUMMARY:Holiday",
        "END:VEVENT",
        "BEGIN:VEVENT", "UID:d", "DTSTART:20261112T150000", "DURATION:PT1H", "STATUS:CANCELLED", "END:VEVENT",
        "BEGIN:VEVENT", "UID:e", "DTSTART:20261112T150000", "DTEND:20261112T160000", "TRANSP:TRANSPARENT",
        "END:VEVENT",
        "BEGIN:VEVENT", "UID:f", "DTSTART:20261001T190000", "DTEND:20261001T200000", "RRULE:FREQ=MONTHLY;BYDAY=2TU",
        "SUMMARY:Club", "BEGIN:VALARM", "TRIGGER:-PT15M", "END:VALARM", "END:VEVENT",
        "BEGIN:VEVENT", "UID:g", "DTSTART:20261031T080000", "DTEND:20261031T083000",
        "RRULE:FREQ=MONTHLY;BYMONTHDAY=-1", "SUMMARY:Month end", "END:VEVENT", "END:VCALENDAR"])
    start = datetime(2026, 11, 10).timestamp()
    got = [(datetime.fromtimestamp(e["start_ts"]).strftime("%d %b %H:%M"), e["title"])
           for e in calfeed.parse(ics, start, start + 25 * DAY)]
    assert got == [("10 Nov 19:00", "Club"), ("13 Nov 00:00", "Holiday"), ("16 Nov 14:00", "Standup (moved)"),
                   ("18 Nov 09:00", "Standup"), ("23 Nov 09:00", "Standup"), ("25 Nov 09:00", "Standup"),
                   ("30 Nov 08:00", "Month end"), ("30 Nov 09:00", "Standup"), ("02 Dec 09:00", "Standup")]


# ─── translation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, lang", [
    ("Hola, ¿cómo estás? Nos vemos mañana en la oficina", "Spanish"),
    ("Bonjour, est-ce que tu es libre pour le dîner ce soir ?", "French"),
    ("कल शाम को मिलते हैं, ठीक है?", "Hindi"),
    ("kal milte hain yaar, theek hai", "Hinglish"),
    ("Can you send me the updated deck before the deadline?", "English"),
    ("ok", None), ("👍👍", None), ("Привет, как дела?", "Russian"), ("お元気ですか", "Japanese"),
])
def test_languages_are_recognised_on_this_machine(text, lang):
    assert translate.detect(text) == lang


def test_new_messages_in_other_languages_are_translated_in_one_call(inbox, clock):
    inbox.backend.receive("ig-lena", "Hola, ¿cómo estás? Nos vemos mañana en la oficina")
    inbox.backend.receive("ig-lena", "Bonjour, est-ce que tu es libre pour le dîner ce soir ?")
    inbox.backend.receive("ig-lena", "See you then!")
    _sync(inbox)
    _background(inbox)
    assert inbox.brain.translations == 1  # one batch for the chat; English is never sent
    chat = _tool(inbox, "inbox_read_chat", {"chat_id": "ig-lena"})
    translated = [m for m in chat["messages"] if m.get("translation")]
    assert sorted(m["translation"]["from"] for m in translated) == ["French", "Spanish"]
    assert any(m["translation"]["text"].startswith("EN: Hola") for m in translated)
    assert "not_translated" not in chat
    page = InboxAPI(inbox).handle("GET", "/chats/ig-lena")[1]
    assert sum(1 for m in page["messages"] if m["translation"]) == 2 and page["untranslated"] == 0
    _background(inbox)
    assert inbox.brain.translations == 1  # never twice


def test_languages_you_read_or_write_are_never_translated(clock, tmp_path):
    inbox = _inbox(clock, tmp_path, {"also_read": "Spanish"})
    for _ in range(3):
        inbox.backend.receive("wa-mum", "theek hai yaar, kal milte hain", from_me=True)
    _sync(inbox)
    inbox.store().set_meta("languages_written", None)
    inbox.backend.receive("ig-lena", "Hola, ¿cómo estás? Nos vemos mañana en la oficina")
    inbox.backend.receive("ig-lena", "acha bhai, kal baat karte hain theek hai")
    _sync(inbox)
    _background(inbox)
    assert inbox.brain.translations == 0
    assert "hinglish" in inbox.translator.readable()


def test_auto_translate_off_and_the_daily_cap_are_respected(clock, tmp_path):
    inbox = _inbox(clock, tmp_path, {"auto_translate": False})
    _arrive(inbox, clock, "ig-lena", "Hola, ¿cómo estás? Nos vemos mañana en la oficina")
    assert inbox.brain.translations == 0
    assert _tool(inbox, "inbox_read_chat", {"chat_id": "ig-lena"})["not_translated"]["languages"] == ["Spanish"]
    out = _tool(inbox, "inbox_translate", {"action": "chat", "chat_id": "ig-lena"})
    assert out["translated_now"] == 1 and inbox.brain.translations == 1
    sub = tmp_path / "capped"
    sub.mkdir()
    capped = _inbox(clock, sub, {"memory_llm_calls_per_day": 1})
    capped.store().add_usage(capped.context._day(), "llm_calls", 1)
    _arrive(capped, clock, "ig-lena", "Hola, ¿cómo estás? Nos vemos mañana en la oficina")
    assert capped.brain.translations == 0


def test_a_draft_is_translated_into_their_language_and_keeps_your_words(inbox, clock):
    _arrive(inbox, clock, "ig-lena", "कल शाम को मिलते हैं, ठीक है?")
    draft = inbox.create_draft("ig-lena", "Yes, see you tomorrow evening")
    out = _tool(inbox, "inbox_translate", {"action": "draft", "draft_id": draft["draft_id"]})
    assert out["language"] == "Hindi" and out["text"] == "HI: Yes, see you tomorrow evening"
    assert out["your_words"] == "Yes, see you tomorrow evening"
    assert "Target language: Hindi" in inbox.brain.prompts["reply"][0]
    listed = _tool(inbox, "inbox_list_drafts", {})["drafts"][0]
    assert listed["in_your_words"] == "Yes, see you tomorrow evening"
    asked = []
    build_command(inbox, lambda q, c: asked.append(q))(f"send {draft['draft_id']}")
    assert "In your words: “Yes, see you tomorrow evening”" in asked[0]
    row = InboxAPI(inbox).handle("GET", "/drafts")[1]["drafts"][0]
    assert row["your_words"] == "Yes, see you tomorrow evening"
    _tool(inbox, "inbox_translate", {"action": "draft", "draft_id": draft["draft_id"]})
    assert inbox.store().get_draft(draft["draft_id"])["text"] == "HI: Yes, see you tomorrow evening"  # from yours
    inbox.edit_draft(draft["draft_id"], "Haan, kal milte hain")
    assert inbox.store().get_draft(draft["draft_id"])["original_text"] == ""  # edited by hand: no stale words
    secret = _tool(inbox, "inbox_translate", {"action": "text", "chat_id": "ig-lena", "text": "my otp is 123456"})
    assert "never sent to a model" in secret["error"] and len(inbox.brain.prompts["reply"]) == 2


# ─── people radar ────────────────────────────────────────────────────────────

def _old_friend(inbox, clock, chat_id="wa-ravi", name="Ravi Kumar", last_days_ago=40, n=24):
    store, now = inbox.store(), clock()
    msgs = [{"id": f"{chat_id}-{i}", "chat_id": chat_id, "from_me": i % 2 == 0, "sender": "" if i % 2 == 0 else name,
             "text": "hey!", "ts": now - (last_days_ago + (n - i) * 3) * DAY} for i in range(n)]
    store.upsert_chats([{"id": chat_id, "account_id": "mock-wa", "network": "whatsapp", "name": name,
                         "ts": msgs[-1]["ts"], "last_text": "hey!", "last_from_me": msgs[-1]["from_me"]}], now)
    store.upsert_messages(msgs, now)


def test_the_radar_finds_who_waits_who_drifts_your_reply_time_and_birthdays(inbox, clock):
    _old_friend(inbox, clock)
    now = clock()
    store = inbox.store()
    # you wished Sam happy birthday a year ago tomorrow
    when = datetime.fromtimestamp(now) + timedelta(days=1)
    store.upsert_messages([{"id": "bday", "chat_id": "wa-bookclub", "from_me": True, "text": "Happy birthday Sam!! 🎉",
                            "ts": when.replace(year=when.year - 1).timestamp()}], now)
    # Mum asked, and you answered
    for m in ({"id": "q", "chat_id": "wa-mum", "from_me": False, "text": "Did you eat?", "ts": now - 1800},
              {"id": "a", "chat_id": "wa-mum", "from_me": True, "text": "Yes!", "ts": now - 60}):
        store.upsert_messages([m], now)
        store.set_chat_preview(m)
    rep = _tool(inbox, "inbox_radar", {})
    assert [d["name"] for d in rep["drifting"]] == ["Ravi Kumar"] and rep["drifting"][0]["used_to_talk_every_days"] == 3
    assert rep["your_reply_time"]["replies_this_week"] >= 1  # exact numbers: see the next test
    assert rep["birthdays"][0]["name"] == "Sam Okafor" and rep["birthdays"][0]["in_days"] == 1
    names = [w["name"] for w in rep["waiting_longest"]]
    assert "Maya Rivers" in names and "Mum" not in names and "lena.makes" not in names  # answered / stranger
    assert "Ravi Kumar" in rep["summary"] and "Sam Okafor" in rep["summary"]
    # a birthday reminder the day before, once
    store.set_meta("radar_checked_at", 0)
    inbox.radar.run_alerts()
    store.set_meta("radar_checked_at", 0)
    inbox.radar.run_alerts()
    bday = _alerts(inbox, "birthday")
    assert len(bday) == 1 and bday[0]["title"] == "Sam Okafor's birthday is tomorrow"
    assert "drifting: Ravi Kumar" in build_command(inbox, lambda q, c: None)("radar")
    # the wish is a year old: cleaned up with old messages, the birthday is still remembered
    store.prune(now, retention_days=180)
    assert store.get_message("bday") is None
    assert inbox.radar.report()["birthdays"][0]["name"] == "Sam Okafor"
    data_control.forget_person(inbox, "wa-bookclub")
    assert inbox.radar.report()["birthdays"] == []


def test_reply_time_this_week_against_the_weeks_before(inbox, clock):
    now = clock()
    people = {"p": {"name": "A", "chats": [{"id": "c1", "network": "whatsapp"}, {"id": "c2", "network": "linkedin"}]}}
    rows = {
        # this week: 10 min (two messages from them, one reply) and 50 min; a reply after 8 days does not count
        "c1": [(False, now - 3 * DAY), (False, now - 3 * DAY + 60), (True, now - 3 * DAY + 600),
               (False, now - 2 * DAY), (True, now - 2 * DAY + 3000)],
        # the four weeks before: 2 hours; your own follow-up without a message from them is not a reply
        "c2": [(False, now - 20 * DAY), (True, now - 20 * DAY + 7200), (True, now - 19 * DAY),
               (False, now - 30 * DAY), (True, now - 22 * DAY)],
    }
    rt = inbox.radar.reply_times(people, rows, now)
    assert (rt["this_week"], rt["replies_this_week"], rt["four_weeks_before"], rt["trend"]) ==         ("30 min", 2, "2 h", "faster")
    assert rt["by_app"] == {"WhatsApp": "30 min"}


def test_a_person_you_talk_to_on_another_app_is_not_drifting(inbox, clock):
    _old_friend(inbox, clock, "wa-ravi", "Ravi Kumar")
    store, now = inbox.store(), clock()
    store.upsert_chats([{"id": "li-ravi", "account_id": "mock-li", "network": "linkedin", "name": "Ravi Kumar",
                         "ts": now - DAY, "last_text": "Thanks!", "last_from_me": True}], now)
    store.upsert_messages([{"id": "li-ravi-1", "chat_id": "li-ravi", "from_me": True, "text": "Thanks!",
                            "ts": now - DAY}], now)
    a = store.ensure_contact(chat_id="wa-ravi", network="whatsapp", name="Ravi Kumar", now=now)
    b = store.ensure_contact(chat_id="li-ravi", network="linkedin", name="Ravi Kumar", now=now)
    store.merge_contacts(a["id"], b["id"], now)
    assert inbox.radar.report()["drifting"] == []


def test_the_weekly_summary_comes_once_a_week(inbox, clock):
    inbox.radar.run_alerts()  # the first day only starts the clock
    assert not _alerts(inbox, "radar")
    clock.advance(7 * DAY)
    inbox.radar.run_alerts()
    clock.advance(DAY)
    inbox.radar.run_alerts()
    assert len(_alerts(inbox, "radar")) == 1


# ─── voice ───────────────────────────────────────────────────────────────────

class Speaker:
    def __init__(self):
        self.texts = []

    def __call__(self, text, path):
        self.texts.append(text)
        Path(path).write_bytes(b"ID3fake-mp3")
        return {"success": True, "file_path": path, "media_tag": f"[[audio_as_voice]]\nMEDIA:{path}"}


def test_the_brief_says_what_matters_first(inbox, clock):
    _arrive(inbox, clock, "wa-dev", "Can you confirm by tonight? It's urgent")
    _arrive(inbox, clock, "ig-tickets", OTP_ASK)
    _arrive(inbox, clock, "wa-mum", "call tomorrow at 10?")
    inbox.create_draft("wa-maya", "On it!")
    say = _tool(inbox, "inbox_brief", {})["say"]
    assert say.startswith(("Good morning.", "Good afternoon.", "Good evening."))
    assert "urgent" in say and "Dev Patel (work) on WhatsApp wrote: Can you confirm by tonight?" in say
    assert "Careful:" in say and "1 draft is ready" in say and "Tomorrow: Call with Mum at 10 AM." in say
    assert say.index("Dev Patel") < say.index("Careful")
    assert "audio_problem" in _tool(inbox, "inbox_brief", {"spoken": True})  # no text-to-speech here
    assert build_command(inbox, lambda q, c: None)("brief") == inbox.voice.brief()["say"]


def test_a_spoken_brief_uses_hermes_text_to_speech(inbox, tmp_path):
    inbox.voice.speaker = Speaker()
    out = _tool(inbox, "inbox_brief", {"spoken": True})
    assert out["media_tag"].endswith(out["audio_file"]) and Path(out["audio_file"]).parent == tmp_path / "voice"
    api = InboxAPI(inbox)
    audio = api.handle("POST", "/voice/brief/audio", body={})[1]
    assert base64.b64decode(audio["data"]) == b"ID3fake-mp3" and audio["mimetype"] == "audio/mpeg"
    status, f = api.handle("GET", f"/voice/audio/{audio['file']}")
    assert status == 200 and f.inline and f.mimetype == "audio/mpeg"
    assert api.handle("GET", "/voice/audio/..%5Cdata.db")[0] == 404
    assert api.handle("GET", "/voice/audio/data.db")[0] == 404


def test_make_speaker_reads_what_the_tts_tool_returns(tmp_path):
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        return json.dumps({"success": True, "file_path": args["output_path"], "media_tag": "MEDIA:x"})

    out = make_speaker(dispatch)("hello", str(tmp_path / "a.mp3"))
    assert out["success"] and calls == [("text_to_speech", {"text": "hello", "output_path": str(tmp_path / "a.mp3")})]
    assert make_speaker(lambda n, a: json.dumps({"error": "no provider"}))("x", "y") == {"success": False,
                                                                                         "error": "no provider"}


def test_a_dictated_reply_becomes_text_and_the_recording_is_deleted(inbox, tmp_path):
    seen = []

    def stt(path):
        seen.append((path, Path(path).read_bytes()))
        return {"success": True, "transcript": "  haan kal   milte hain "}

    inbox.transcriber = stt
    api = InboxAPI(inbox)
    body = {"audio": "data:audio/webm;base64," + base64.b64encode(b"webm-bytes").decode(), "mimetype": "audio/webm"}
    assert api.handle("POST", "/voice/dictate", body=body)[1] == {"text": "haan kal milte hain", "heard_nothing": False}
    assert seen[0][1] == b"webm-bytes" and not Path(seen[0][0]).exists()
    assert api.handle("POST", "/voice/dictate", body={"audio": "AAAA", "mimetype": "text/html"})[0] == 400
    assert api.handle("POST", "/voice/dictate", body={"audio": "!!", "mimetype": "audio/ogg"})[0] == 400
    inbox.transcriber = None
    assert api.handle("POST", "/voice/dictate", body=body)[0] == 502


# ─── your data ───────────────────────────────────────────────────────────────

def test_plans_and_translations_are_exported_forgotten_and_wiped(inbox, clock, tmp_path):
    _arrive(inbox, clock, "ig-lena", "Hola, ¿cómo estás? Nos vemos mañana en la oficina")
    _arrive(inbox, clock, "ig-lena", "Let's meet tomorrow at 5pm")
    inbox.voice.speaker = Speaker()
    inbox.voice.speak("hello")
    exported = data_control.export_all(inbox)["tables"]
    assert exported["plans"] and exported["translations"]
    data_control.forget_person(inbox, "ig-lena")
    store = inbox.store()
    assert not store.plans(since=0) and not store.translations_for(m["id"] for m in store.cached_messages("ig-lena", 50))
    _arrive(inbox, clock, "ig-lena", "Let's meet tomorrow at 6pm")
    data_control.wipe(inbox, everywhere=False)
    assert not store.plans(since=0) and not (tmp_path / "voice").exists()


def test_setup_and_doctor_report_the_new_parts(inbox):
    setup = InboxAPI(inbox).handle("GET", "/setup")[1]
    assert setup["safety"] == {"scam_shield": True} and setup["calendar"]["connected"] is False
    assert setup["translation"]["my_language"] == "English" and setup["voice"]["text_to_speech"] is False
    names = {c["name"] for c in __import__("unified_inbox.health", fromlist=["run"]).run(inbox)}
    assert {"Scam warnings", "Plans for your calendar", "Translation", "Spoken brief"} <= names
