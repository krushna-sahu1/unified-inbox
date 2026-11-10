"""Automation: time parsing, scheduled messages, watches, follow-ups, snoozes and alerts."""

import json
from datetime import datetime

import pytest

from conftest import make_sample_inbox
from unified_inbox.client import InboxError
from unified_inbox.commands import build_command
from unified_inbox.context import ContextEngine
from unified_inbox.sync import SyncEngine, SyncWorker
from unified_inbox.timeparse import WhenError, parse_when
from unified_inbox.tools import build_handlers

NOW = datetime(2026, 11, 9, 12, 0)  # a Monday


@pytest.mark.parametrize("text, expected", [
    ("tomorrow 9am", datetime(2026, 11, 10, 9, 0)),
    ("kal subah 9 baje", datetime(2026, 11, 10, 9, 0)),
    ("kal shaam 6 baje", datetime(2026, 11, 10, 18, 0)),
    ("raat 9 baje", datetime(2026, 11, 9, 21, 0)),
    ("parso 11am", datetime(2026, 11, 11, 11, 0)),
    ("in 2h", datetime(2026, 11, 9, 14, 0)),
    ("2 ghante baad", datetime(2026, 11, 9, 14, 0)),
    ("in 30 min", datetime(2026, 11, 9, 12, 30)),
    ("fri 18:30", datetime(2026, 11, 13, 18, 30)),
    ("friday", datetime(2026, 11, 13, 9, 0)),
    ("next monday 10am", datetime(2026, 11, 16, 10, 0)),
    ("tonight", datetime(2026, 11, 9, 20, 0)),
    ("9:30", datetime(2026, 11, 10, 9, 30)),
    ("12 nov 3pm", datetime(2026, 11, 12, 15, 0)),
    ("2026-11-12 09:30", datetime(2026, 11, 12, 9, 30)),
])
def test_parse_when(text, expected):
    assert parse_when(text, NOW) == expected


@pytest.mark.parametrize("text, says", [("at 6", "morning or the evening"), ("2026-01-01 10:00", "past"),
                                        ("whenever", "could not read"), ("", "Say when"), ("31 feb 9am", "no 31")])
def test_parse_when_explains_problems(text, says):
    with pytest.raises(WhenError, match=says):
        parse_when(text, NOW)


# ─── scheduled messages ──────────────────────────────────────────────────────

def _inbox(clock, config=None, llm=None):
    inbox = make_sample_inbox(clock, config)
    inbox._local_now = lambda: datetime.fromtimestamp(clock())
    if llm:
        inbox.context = ContextEngine(inbox, llm=llm)
    SyncEngine(inbox).sync_once()
    inbox.automation.tick()  # starts watching from now
    clock.advance(1)  # anything received from here on is new, not history
    return inbox


def _ask(answer, seen=None):
    def ask(question, choices):
        if seen is not None:
            seen.append(question)
        return answer
    return ask


def _draft(inbox, chat="wa-maya", text="Yes, I'll be home at 8!"):
    return json.loads(build_handlers(inbox)["inbox_draft_reply"]({"chat_id": chat, "text": text}))["draft_id"]


def _schedule(inbox, draft, when="in 2h"):
    out = build_command(inbox, _ask("schedule"))(f"schedule {draft} {when}")
    assert out.startswith("Scheduled"), out
    return out.split("(")[1].split(")")[0]


def _sends(inbox):
    return [c for c in inbox.backend.calls if c == "send"]


def test_scheduled_message_goes_out_once_at_its_time(clock):
    inbox = _inbox(clock)
    seen = []
    d = _draft(inbox)
    out = build_command(inbox, _ask("schedule", seen))(f"schedule {d} in 2h")
    sid = out.split("(")[1].split(")")[0]
    assert "Yes, I'll be home at 8!" in seen[0] and "Maya Rivers" in seen[0]
    assert sid in build_command(inbox, _ask(None))("scheduled")
    inbox.automation.tick()
    assert _sends(inbox) == []
    clock.advance(2 * 3600 + 5)
    assert inbox.automation.tick()["sent"] == 1
    inbox.automation.tick()
    assert len(_sends(inbox)) == 1  # exactly once
    assert inbox.store().schedule(sid)["status"] == "sent"
    alerts = inbox.automation.alerts()
    assert alerts[-1]["title"].startswith("Sent your scheduled message to Maya Rivers")


def test_cancelled_or_declined_schedules_never_send(clock):
    inbox = _inbox(clock)
    d = _draft(inbox)
    assert "Not scheduled" in build_command(inbox, _ask("cancel"))(f"schedule {d} in 1h")
    assert "Not scheduled" in build_command(inbox, _ask(None))(f"schedule {d} in 1h")  # no answer = no
    sid = _schedule(inbox, d, "in 1h")
    assert "Cancelled" in build_command(inbox, _ask(None))(f"schedule cancel {sid}")
    clock.advance(3700)
    inbox.automation.tick()
    assert _sends(inbox) == [] and inbox.store().get_draft(d)["status"] == "pending"


def test_held_when_they_write_again(clock):
    inbox = _inbox(clock)
    sid = _schedule(inbox, _draft(inbox), "in 1h")
    clock.advance(600)
    inbox.backend.receive("wa-maya", "Actually never mind, he's coming Thursday")
    clock.advance(3000)
    inbox.automation.tick()
    assert _sends(inbox) == [] and inbox.store().schedule(sid)["status"] == "held"
    assert "wrote again" in inbox.automation.alerts()[-1]["title"]


def test_missed_when_hermes_was_not_running(clock):
    inbox = _inbox(clock, {"schedule_grace_minutes": 30})
    d = _draft(inbox)
    sid = _schedule(inbox, d, "in 1h")
    clock.advance(3600 + 31 * 60)
    inbox.automation.tick()
    assert _sends(inbox) == [] and inbox.store().schedule(sid)["status"] == "missed"
    alert = inbox.automation.alerts()[-1]
    assert alert["title"].startswith("Missed") and d in alert["body"]


def test_short_blocks_are_waited_out(clock):
    inbox = _inbox(clock)
    sid = _schedule(inbox, _draft(inbox, "wa-maya"), "in 1h")
    clock.advance(3600 - 20)
    other = _draft(inbox, "wa-dev", "Sending the deck in 10 minutes, sorry for the delay!")
    assert build_command(inbox, _ask("send"))(f"send {other}").startswith("Sent")
    clock.advance(30)
    inbox.automation.tick()  # too soon after the manual send
    assert inbox.store().schedule(sid)["status"] == "scheduled" and "Too soon" in inbox.store().schedule(sid)["reason"]
    clock.advance(120)
    inbox.automation.tick()
    assert inbox.store().schedule(sid)["status"] == "sent"


def test_hard_blocks_fail_with_the_reason(clock):
    inbox = _inbox(clock, {"daily_limit_whatsapp": 1})
    sid = _schedule(inbox, _draft(inbox, "wa-maya"), "in 1h")
    other = _draft(inbox, "wa-dev", "Sending the deck in 10 minutes, sorry for the delay!")
    build_command(inbox, _ask("send"))(f"send {other}")
    clock.advance(3605)
    inbox.automation.tick()
    sch = inbox.store().schedule(sid)
    assert sch["status"] == "failed" and "daily limit" in sch["reason"]
    assert inbox.automation.alerts()[-1]["title"].startswith("Not sent")


def test_schedule_checks(clock):
    inbox = _inbox(clock, {"quiet_hours": "22:00-07:00"})
    auto, now = inbox.automation, clock()
    draft = inbox.store().get_draft(_draft(inbox))
    assert "past" in " ".join(auto.check_schedule(draft, now - 10))
    assert "30 days" in " ".join(auto.check_schedule(draft, now + 31 * 86400))
    night = datetime.fromtimestamp(now).replace(hour=23, minute=0).timestamp() + 86400
    assert "quiet hours" in " ".join(auto.check_schedule(draft, night))
    assert "older than 7 days" in " ".join(auto.check_schedule(draft, now + 8 * 86400))
    cold = inbox.store().get_draft(_draft(inbox, "wa-plumber", "Any slot?"))
    assert "never messaged" in " ".join(auto.check_schedule(cold, now + 3600))
    _schedule(inbox, draft["id"], "in 1h")
    assert "already scheduled" in " ".join(auto.check_schedule(draft, now + 7200))
    assert "Not scheduled" in build_command(inbox, _ask("schedule"))(f"schedule {draft['id']} in 3h")
    assert "morning or the evening" in build_command(inbox, _ask("schedule"))(f"schedule {cold['id']} at 5")


def test_the_model_can_only_propose(clock):
    inbox = _inbox(clock)
    h = build_handlers(inbox)
    d = _draft(inbox)
    out = json.loads(h["inbox_schedule"]({"action": "propose", "draft_id": d, "when": "in 3h"}))
    assert out["proposed"] is True and out["scheduled"] is False and f"/inbox schedule {d}" in out["how_to_confirm"]
    assert inbox.store().schedules() == []
    assert json.loads(h["inbox_schedule"]({"action": "propose", "draft_id": d, "when": "at 4"}))["kind"] == "input"
    seen = []
    assert build_command(inbox, _ask("schedule", seen))(f"schedule {d}").startswith("Scheduled")  # uses the proposal
    listed = json.loads(h["inbox_schedule"]({"action": "list"}))["scheduled"]
    assert listed[0]["draft_id"] == d
    assert json.loads(h["inbox_schedule"]({"action": "cancel", "schedule_id": listed[0]["schedule_id"]}))["draft_kept"]


def test_interrupted_send_is_never_retried(clock):
    inbox = _inbox(clock)
    sid = _schedule(inbox, _draft(inbox), "in 1h")
    inbox.store().set_schedule(sid, clock(), status="sending", expect="scheduled")
    clock.advance(3605)
    inbox.automation.tick()
    assert inbox.store().schedule(sid)["status"] == "unknown" and _sends(inbox) == []
    assert "may not have gone out" in inbox.automation.alerts()[-1]["title"]


def test_uncertain_send_failure_is_not_retried(clock):
    inbox = _inbox(clock)
    sid = _schedule(inbox, _draft(inbox), "in 1h")
    real = inbox.backend.send_message

    def boom(*a, **k):
        raise InboxError("Unipile 502: bad gateway", status=502)

    inbox.backend.send_message = boom
    clock.advance(3605)
    inbox.automation.tick()
    inbox.backend.send_message = real
    clock.advance(120)
    inbox.automation.tick()
    assert inbox.store().schedule(sid)["status"] == "failed" and _sends(inbox) == []


def test_sending_the_draft_by_hand_retires_its_schedule(clock):
    inbox = _inbox(clock)
    d = _draft(inbox)
    sid = _schedule(inbox, d, "in 1h")
    build_command(inbox, _ask("send"))(f"send {d}")
    clock.advance(3605)
    inbox.automation.tick()
    assert inbox.store().schedule(sid)["status"] == "cancelled" and len(_sends(inbox)) == 1


# ─── watches ─────────────────────────────────────────────────────────────────

def _sync(inbox, clock):
    clock.advance(60)
    SyncEngine(inbox).sync_once()
    return inbox.automation.tick()


def test_reply_and_keyword_watches(clock):
    inbox = _inbox(clock)
    h = build_handlers(inbox)
    h["inbox_watch"]({"action": "add", "type": "reply", "person": "Priya"})
    h["inbox_watch"]({"action": "add", "type": "keyword", "text": "invoice"})
    h["inbox_watch"]({"action": "add", "type": "keyword", "text": "tickets", "network": "linkedin"})
    assert len(json.loads(h["inbox_watch"]({"action": "list"}))["watches"]) == 3
    inbox.backend.receive("li-priya", "Sent you the INVOICE, and yes Thursday works")
    inbox.backend.receive("ig-tickets", "Last tickets left!")
    assert _sync(inbox, clock)["alerts"] == 2  # Priya replied + invoice; tickets is not on LinkedIn
    titles = [a["title"] for a in json.loads(h["inbox_updates"]({}))["alerts"]]
    assert any("Priya" in t and "replied" in t for t in titles) and any('"invoice"' in t for t in titles)
    inbox.backend.receive("li-priya", "One more invoice question")
    assert _sync(inbox, clock)["alerts"] == 1  # the reply watch was once; the keyword keeps watching


def test_old_messages_never_fire_watches(clock):
    inbox = make_sample_inbox(clock)
    SyncEngine(inbox).sync_once()
    inbox.automation.add_watch("keyword", text="plumber")
    assert inbox.automation.tick()["alerts"] == 0


def test_topic_watch_uses_the_model(clock):
    calls = []

    def llm(instructions, text, schema):
        calls.append(text)
        if "Watched topics" in text:
            mid = text.split("New messages:")[1].split("[")[1].split("]")[0]
            return {"matches": [{"watch_id": 1, "message_id": mid}]}
        return {"summary": "", "facts_new": [], "facts_replace": [], "loops_new": [], "loops_close": [],
                "questions": [], "language": "English"}

    inbox = _inbox(clock, llm=llm)
    w = inbox.automation.add_watch("topic", text="rent or the flat")
    assert w["watch_id"] == 1
    inbox.backend.receive("wa-mum", "Beta makaan ka kiraya bhej dena")
    assert _sync(inbox, clock)["alerts"] == 1
    assert "rent or the flat" in inbox.automation.alerts()[-1]["title"]
    no_model = _inbox(clock)
    with pytest.raises(InboxError, match="language model"):
        no_model.automation.add_watch("topic", text="rent")


def test_follow_up_resolves_on_reply_or_reminds_with_a_nudge_draft(clock):
    def llm(instructions, text, schema):
        if "follow-up" in instructions:
            return {"text": "Hi Marco, just checking in on this - any thoughts?"}
        return None

    inbox = _inbox(clock, llm=llm)
    h = build_handlers(inbox)
    out = json.loads(h["inbox_watch"]({"action": "add", "type": "no_reply", "person": "Marco", "until": "in 2 days",
                                       "nudge": True}))
    assert out["added"]["type"] == "no_reply" and out["added"]["prepare_nudge_draft"]
    clock.advance(2 * 86400 + 60)
    inbox.automation.tick()
    alert = inbox.automation.alerts()[-1]
    assert "has not replied" in alert["title"] and "Follow-up draft" in alert["body"]
    drafts = json.loads(h["inbox_list_drafts"]({}))["drafts"]
    assert drafts[0]["text"].startswith("Hi Marco") and _sends(inbox) == []  # drafted, never sent

    inbox2 = _inbox(clock)
    inbox2.automation.add_watch("no_reply", chat=inbox2.store().cached_chat("li-elena"),
                                until=clock() + 86400)
    inbox2.backend.receive("li-elena", "Sorry for the delay!")
    _sync(inbox2, clock)
    assert "the follow-up you set is done" in inbox2.automation.alerts()[-1]["title"]
    assert inbox2.store().watches() == []


def test_snooze_hides_from_triage_then_reminds(clock):
    inbox = _inbox(clock)
    h = build_handlers(inbox)
    before = {a["chat_id"] for a in json.loads(h["inbox_triage"]({}))["urgent"]}
    assert "wa-maya" in before
    h["inbox_watch"]({"action": "add", "type": "snooze", "person": "Maya Rivers", "until": "in 3h"})
    out = json.loads(h["inbox_triage"]({}))
    assert "wa-maya" not in {a["chat_id"] for a in out["urgent"]} and out["snoozed_chats_hidden"] == 1
    clock.advance(3 * 3600 + 60)
    inbox.automation.tick()
    assert inbox.automation.alerts()[-1]["title"] == "Reminder: Maya Rivers (WhatsApp)"


def test_watch_input_errors(clock):
    inbox = _inbox(clock)
    h = build_handlers(inbox)
    assert json.loads(h["inbox_watch"]({"action": "add", "type": "reply"}))["kind"] == "input"
    assert json.loads(h["inbox_watch"]({"action": "add", "type": "snooze", "person": "Mum"}))["kind"] == "input"
    assert json.loads(h["inbox_watch"]({"action": "add", "type": "keyword", "text": "x"}))["kind"] == "input"
    assert json.loads(h["inbox_watch"]({"action": "remove", "watch_id": 99}))["kind"] == "not_found"


# ─── alerts ──────────────────────────────────────────────────────────────────

def test_alerts_are_pushed_in_one_batch_to_the_chosen_chat(clock):
    inbox = _inbox(clock)
    pushed = []
    inbox.automation.push = lambda text: pushed.append(text) or True
    cmd = build_command(inbox, _ask(None))
    assert "New alerts will be sent to this chat" in cmd("alerts here", session_key="ses_123")
    inbox.automation.add_watch("keyword", text="urgent")
    inbox.backend.receive("wa-dev", "urgent: call me")
    inbox.backend.receive("wa-mum", "not urgent, but call me")
    _sync(inbox, clock)
    assert len(pushed) == 1 and pushed[0].count("\n- ") == 2
    _sync(inbox, clock)
    assert len(pushed) == 1  # nothing new, nothing pushed
    assert "no longer be sent" in cmd("alerts off") and inbox.store().get_meta("alerts_session") == ""
    assert "Alerts:" in cmd("alerts")  # still readable here


def test_failed_push_keeps_alerts_for_later(clock):
    inbox = _inbox(clock)
    inbox.store().set_meta("alerts_session", "ses_1")
    inbox.automation.push = lambda text: False
    inbox.automation.notify("watch", "Something happened")
    inbox.automation.tick()
    assert [n["title"] for n in inbox.store().notifications(unpushed_only=True)] == ["Something happened"]
    inbox.automation.push = lambda text: True
    inbox.automation.tick()
    assert inbox.store().notifications(unpushed_only=True) == []  # delivered on the next try


def test_alerts_here_needs_a_session(clock):
    inbox = _inbox(clock)
    assert "cannot receive alerts" in build_command(inbox, _ask(None))("alerts here")


# ─── the worker ──────────────────────────────────────────────────────────────

def test_worker_wakes_up_for_the_next_scheduled_message(clock):
    inbox = _inbox(clock)
    _schedule(inbox, _draft(inbox), "in 5 min")
    delay = SyncWorker(inbox).run_once()
    assert 15 <= delay <= 301


def test_scheduled_messages_go_out_even_with_the_sync_off(clock):
    inbox = _inbox(clock, {"sync_enabled": False})
    _schedule(inbox, _draft(inbox), "in 1h")
    clock.advance(3605)
    SyncWorker(inbox).run_once()
    assert len(_sends(inbox)) == 1


def test_status_counts(clock):
    inbox = _inbox(clock)
    _schedule(inbox, _draft(inbox), "in 1h")
    inbox.automation.add_watch("keyword", text="invoice")
    st = json.loads(build_handlers(inbox)["inbox_status"]({}))
    assert st["scheduled_messages"] == 1 and st["active_watches"] == 1
