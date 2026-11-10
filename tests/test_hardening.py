"""Found in the end-to-end review: each test is something that used to get through."""

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime

import pytest

from conftest import make_sample_inbox
from fake_supabase import FakeSupabase
from unified_inbox import calfeed, data_control, health, sending
from unified_inbox import cloud as cloud_mod
from unified_inbox.api import InboxAPI
from unified_inbox.cloud import CloudSync
from unified_inbox.commands import build_command
from unified_inbox.context import SETTLE_SECONDS, ContextEngine
from unified_inbox.pacing import DAY, in_quiet_hours, parse_quiet_hours, quiet_hours_problem
from unified_inbox.sync import SyncEngine, SyncWorker
from unified_inbox.timeparse import parse_when
from unified_inbox.tools import build_handlers
from unified_inbox.webhooks import WebhookHandler

URL = "https://abcdefghij.supabase.co"


def _synced(clock, config=None):
    inbox = make_sample_inbox(clock, config)
    inbox._local_now = lambda: datetime.fromtimestamp(clock())
    while SyncEngine(inbox).sync_once()["pending_chats"]:
        pass
    inbox.automation.tick()
    clock.advance(1)
    return inbox


def _ask(answer):
    return lambda question, choices: answer


@pytest.mark.parametrize("spec", ["23:00-07:00", "23-7", "11pm-7am", "23:00 to 07:00", "23.00 – 7.00", "11 PM - 7 AM"])
def test_quiet_hours_are_understood_however_they_are_written(spec):
    assert in_quiet_hours(spec, datetime(2026, 1, 1, 23, 30)) and in_quiet_hours(spec, datetime(2026, 1, 1, 6, 59))
    assert not in_quiet_hours(spec, datetime(2026, 1, 1, 12, 0)) and not quiet_hours_problem(spec)


def test_unreadable_quiet_hours_are_reported_not_silently_off(clock):
    assert parse_quiet_hours("nonsense") is None and "can't be read" in quiet_hours_problem("after dinner")
    assert "same time" in quiet_hours_problem("08:00-08:00") and quiet_hours_problem("") == ""
    inbox = make_sample_inbox(clock, {"quiet_hours": "at night"})
    check = {c["name"]: c for c in health.run(inbox)}["Quiet hours"]
    assert check["ok"] is False and "23:00-07:00" in check["fix"]
    assert "can't be read" in build_command(inbox, _ask(None))("limits")


def test_limits_command_describes_what_is_in_force(clock):
    text = build_command(make_sample_inbox(clock), _ask(None))("limits")
    assert "New conversations" in text and "No bulk or scheduled sending" not in text
    assert "Replies only" in build_command(make_sample_inbox(clock, {"new_conversations_per_day": 0}),
                                           _ask(None))("limits")


def test_a_read_only_chat_cannot_be_written_to(clock):
    inbox = _synced(clock)
    inbox.store()._x("UPDATE chats SET is_readonly=1 WHERE id='wa-maya'")
    out = json.loads(build_handlers(inbox)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    assert any("read-only" in r for r in out["would_be_blocked_now_because"])
    assert "read-only" in build_command(inbox, _ask("send"))(f"send {out['draft_id']}")
    assert "send" not in inbox.backend.calls
    status, chat = InboxAPI(inbox).handle("GET", "/chats/wa-maya")
    assert status == 200 and chat["chat"]["readonly"] is True


def test_an_approval_is_for_the_chat_it_named(clock):
    inbox = _synced(clock)
    args = {"to": "Maya", "text": "see you at 8"}
    directive = sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args=args)
    assert "WhatsApp to Maya Rivers" in directive["message"]
    store = inbox.store()  # while the prompt is open, "Maya" comes to mean another chat
    store._x("UPDATE chats SET name='Someone Else' WHERE id='wa-maya'")
    store._x("UPDATE chats SET name='Maya Rivers' WHERE id='li-marco'")
    out = json.loads(build_handlers(inbox)["inbox_send"](args))
    assert out["sent"] is False and "different" in out["error"] and "send" not in inbox.backend.calls


def test_a_scheduled_message_only_goes_out_with_the_words_you_scheduled(clock):
    inbox = _synced(clock)
    d = json.loads(build_handlers(inbox)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))["draft_id"]
    out = build_command(inbox, _ask("schedule"))(f"schedule {d} in 2h")
    sid = out.split("(")[1].split(")")[0]
    inbox.store()._x("UPDATE drafts SET text='Something else entirely' WHERE id=?", (d,))
    clock.advance(2 * 3600 + 5)
    inbox.automation.tick()
    sch = inbox.store().schedule(sid)
    assert "send" not in inbox.backend.calls and sch["status"] == "failed" and "changed" in sch["reason"]


def test_unloading_ends_a_sync_cycle_at_the_next_request(clock):
    inbox = make_sample_inbox(clock)
    worker = SyncWorker(inbox)
    real = inbox.backend.list_chats

    def list_chats(*a, **kw):
        worker._stop.set()  # the plugin is unloaded while this request is in flight
        return real(*a, **kw)

    inbox.backend.list_chats = list_chats
    assert worker.run_once() == 0.0
    assert inbox.backend.calls.count("chats") == 1 and "messages" not in inbox.backend.calls
    before = list(inbox.backend.calls)
    worker.run_once()
    assert inbox.backend.calls == before


def test_a_model_answer_in_the_wrong_shape_is_not_retried_for_ever(clock):
    bad = {"summary": "s", "facts_new": [{"text": "Likes tea", "category": "other", "confidence": ["high"]}],
           "facts_replace": [], "loops_new": [], "loops_close": [], "questions": [], "language": "English"}
    inbox = make_sample_inbox(clock, {"memory_chats_per_cycle": 1})
    inbox.context = ContextEngine(inbox, llm=lambda i, t, s: json.loads(json.dumps(bad)))
    SyncEngine(inbox).sync_once()
    clock.advance(SETTLE_SECONDS + 1)
    first = inbox.store().chats_behind("memory_ts", since_ts=0, limit=1)[0]
    for _ in range(3):
        assert "wrong shape" in " ".join(inbox.context.process_once()["problems"])
    assert first not in inbox.store().chats_behind("memory_ts", since_ts=0, limit=100)


def test_a_delivery_that_could_not_be_applied_is_accepted_again(clock):
    inbox = _synced(clock)
    inbox.store().set_meta("webhook", {"id": "we_1", "url": "https://x.example/unipile/webhook", "secret": "s"})
    handler = WebhookHandler(inbox)
    raw = json.dumps({"id": "evt_9", "account_id": "mock-wa", "account_provider": "WHATSAPP", "type": "message.new",
                      "payload": {"id": "m-hook", "chat_id": "wa-maya", "timestamp": "2027-01-15T08:00:30Z",
                                  "is_sender": False, "text": "hello again", "attachments": []}}).encode()
    t = int(clock())
    headers = {"unipile-signature": f"t={t},v0=" + hmac.new(b"s", f"{t}.".encode() + raw, hashlib.sha256).hexdigest()}
    real = handler.apply

    def broken(event):
        raise sqlite3.OperationalError("database is locked")

    handler.apply = broken
    with pytest.raises(sqlite3.OperationalError):
        handler.handle(raw, headers)
    handler.apply = real
    assert handler.handle(raw, headers) == (200, "ok") and inbox.store().get_message("m-hook")


def _device(clock, supa):
    inbox = make_sample_inbox(clock, {"supabase_url": URL})
    inbox._get_secret = {"UNIPILE_API_KEY": "test-key", "SUPABASE_SECRET_KEY": supa.key}.get
    inbox._local_now = lambda: datetime.fromtimestamp(clock())
    inbox.cloud = CloudSync(inbox, transport=supa)
    return inbox


def test_pull_gets_everything_when_many_rows_were_written_within_seconds(clock, monkeypatch):
    monkeypatch.setattr(cloud_mod, "PULL_LIMIT", 5)
    supa = FakeSupabase()
    supa.installed = True
    a = _device(clock, supa)
    while SyncEngine(a).sync_once()["pending_chats"]:
        pass
    a.cloud.activate()
    a.cloud.sync()
    b = _device(clock, supa)
    b.cloud.activate()
    for _ in range(4):
        b.cloud.pull()
    assert len(b.store().cached_chats(limit=100)) == 13
    assert b.store().cached_message_count() == a.store().cached_message_count()


def test_older_cloud_tables_keep_syncing_and_gain_the_new_columns_when_updated(clock):
    supa = FakeSupabase()
    supa.installed, supa.version = True, 1  # tables made by 1.4.0
    a = _device(clock, supa)
    while SyncEngine(a).sync_once()["pending_chats"]:
        pass
    a.cloud.activate()
    mid = next(m["id"] for m in a.store().cached_messages("wa-maya", 10) if not m["from_me"])
    d = a.create_draft("wa-maya", "Yes, home at 8!", reply_to=mid)["draft_id"]
    a.cloud.sync()
    assert "reply_to" not in supa.live("drafts")[d] and a.cloud.status()["update_available"] is True
    assert "Cloud tables" in {c["name"] for c in health.run(a)}
    supa.version = 2  # the user ran the new SQL
    a.cloud.activate()
    a.cloud.sync()
    assert supa.live("drafts")[d]["reply_to"] == mid and a.cloud.status()["update_available"] is False
    b = _device(clock, supa)
    b.cloud.activate()
    b.cloud.sync()
    assert b.store().get_draft(d)["reply_to"] == mid


def test_long_running_repeats_still_show_and_a_bad_rule_hides_nothing():
    ics = "\r\n".join([
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT", "UID:a", "DTSTART:20180101T093000", "DTEND:20180101T094500", "RRULE:FREQ=DAILY",
        "SUMMARY:Standup", "END:VEVENT",
        "BEGIN:VEVENT", "UID:b", "DTSTART:20261111T150000", "DTEND:20261111T160000", "RRULE:FREQ=WEEKLY;INTERVAL=x",
        "SUMMARY:Odd", "END:VEVENT",
        "BEGIN:VEVENT", "UID:c", "DTSTART:20100104T110000", "DTEND:20100104T113000",
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "SUMMARY:Daily sync", "END:VEVENT", "END:VCALENDAR"])
    start = datetime(2026, 11, 10).timestamp()
    got = [(datetime.fromtimestamp(e["start_ts"]).strftime("%d %b %H:%M"), e["title"])
           for e in calfeed.parse(ics, start, start + 2 * DAY)]
    assert got == [("10 Nov 09:30", "Standup"), ("10 Nov 11:00", "Daily sync"), ("11 Nov 09:30", "Standup"),
                   ("11 Nov 11:00", "Daily sync"), ("11 Nov 15:00", "Odd")]


def test_a_date_after_a_time_is_not_dropped():
    assert parse_when("shaam 6 baje 12 dec", datetime(2026, 11, 9, 12, 0)) == datetime(2026, 12, 12, 18, 0)


def test_reset_keeps_the_reaction_allowance_and_bad_schedule_times_are_refused(clock):
    inbox = _synced(clock)
    inbox.store().set_meta("reaction_times", [clock()])
    d = json.loads(build_handlers(inbox)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    status, _ = InboxAPI(inbox).handle("POST", f"/drafts/{d['draft_id']}/schedule",
                                       body={"confirm": True, "text": "Yes, home at 8!", "ts": "soon"})
    assert status == 400
    data_control.wipe(inbox, everywhere=False)
    assert inbox.store().get_meta("reaction_times") == [clock()]
