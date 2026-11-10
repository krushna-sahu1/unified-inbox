import json

from conftest import KEY, make_inbox, make_sample_inbox
from unified_inbox.commands import build_command
from unified_inbox.tools import build_handlers


def _ask(answer):
    calls = []

    def ask(question, choices):
        calls.append(question)
        return answer

    ask.calls = calls
    return ask


def test_connected_needs_only_the_api_key(clock):
    assert not make_inbox(clock).configured
    assert not make_inbox(clock, {"unipile_account_id": "acc_1"}).configured  # no API key yet
    assert make_inbox(clock, secrets=KEY).configured  # every connected app is used
    assert make_inbox(clock, {"unipile_account_id": "acc_1"}, secrets=KEY).configured
    inbox = make_inbox(clock, {"unipile_account_id": " acc_1, acc_2 ;acc_3,"})
    assert inbox.account_ids() == ["acc_1", "acc_2", "acc_3"]


def test_list_and_read(sample_inbox):
    h = build_handlers(sample_inbox)
    chats = json.loads(h["inbox_list_chats"]({}))["chats"]
    assert len(chats) == 13
    assert {c["network"] for c in chats} == {"whatsapp", "instagram", "linkedin"}
    ig = json.loads(h["inbox_list_chats"]({"network": "instagram"}))["chats"]
    assert all(c["network"] == "instagram" for c in ig)
    chat = json.loads(h["inbox_read_chat"]({"chat_id": "wa-maya"}))
    assert chat["messages"][-1]["text"] == "??"
    assert json.loads(h["inbox_read_chat"]({"chat_id": "nope"}))["error"]


def test_search(sample_inbox):
    h = build_handlers(sample_inbox)
    out = json.loads(h["inbox_search"]({"query": "plumber"}))
    assert [r["chat_id"] for r in out["results"]] == ["wa-maya"]
    assert json.loads(h["inbox_search"]({"query": ""}))["error"]


def test_triage_flags_the_right_chats(sample_inbox):
    out = json.loads(build_handlers(sample_inbox)["inbox_triage"]({"hours": 24}))
    urgent = {a["chat_id"] for a in out["urgent"]}
    unanswered = {a["chat_id"] for a in out["unanswered"]}
    assert {"wa-maya", "wa-dev", "ig-tickets", "li-priya"} <= urgent
    assert {"wa-mum", "ig-lena", "li-marco"} <= unanswered
    assert "wa-bookclub" not in urgent | unanswered  # I replied last


def test_draft_never_sends(sample_inbox):
    h = build_handlers(sample_inbox)
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, I'll be home at 8!"}))
    assert d["sent"] is False and d["would_be_blocked_now_because"] == []
    assert sample_inbox.store().recent_sends(0) == []
    drafts = json.loads(h["inbox_list_drafts"]({}))["drafts"]
    assert drafts[0]["draft_id"] == d["draft_id"]


def test_send_needs_confirmation(sample_inbox):
    h = build_handlers(sample_inbox)
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))["draft_id"]

    no_answer = _ask(None)
    assert "Not sent" in build_command(sample_inbox, no_answer)(f"send {d}")
    assert "Not sent" in build_command(sample_inbox, _ask("cancel"))(f"send {d}")
    assert sample_inbox.store().recent_sends(0) == []

    yes = _ask("send")
    out = build_command(sample_inbox, yes)(f"send {d}")
    assert out.startswith("Sent to Maya Rivers on WhatsApp.")
    assert "Yes, home at 8!" in yes.calls[0] and "Maya Rivers" in yes.calls[0]
    assert len(sample_inbox.store().recent_sends(0)) == 1
    # the sent message now shows in the chat
    msgs = json.loads(h["inbox_read_chat"]({"chat_id": "wa-maya"}))["messages"]
    assert msgs[-1] == {"message_id": msgs[-1]["message_id"], "from": "me", "text": "Yes, home at 8!",
                        "time": msgs[-1]["time"]}
    # and a draft cannot be sent twice
    assert "already sent" in build_command(sample_inbox, yes)(f"send {d}")


def test_pacing_blocks_before_asking(sample_inbox, clock):
    h = build_handlers(sample_inbox)
    send = build_command(sample_inbox, _ask("send"))
    text = "Thanks for the message, I'll get back to you properly tomorrow morning."
    d1 = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-dev", "text": text}))["draft_id"]
    assert send(f"send {d1}").startswith("Sent")

    # too soon, and the same text to someone else
    d2 = json.loads(h["inbox_draft_reply"]({"chat_id": "ig-lena", "text": text}))["draft_id"]
    ask = _ask("send")
    out = build_command(sample_inbox, ask)(f"send {d2}")
    assert "Too soon" in out and "very similar" in out and ask.calls == []

    # after the gap, a personal reply goes through
    clock.advance(120)
    d3 = json.loads(h["inbox_draft_reply"]({"chat_id": "ig-lena", "text": "I'd love to collab! DM me details"}))
    assert send(f"send {d3['draft_id']}").startswith("Sent")


def test_reply_only_rules(clock):
    sample_inbox = make_sample_inbox(clock, {"new_conversations_per_day": 0})
    h = build_handlers(sample_inbox)
    send = build_command(sample_inbox, _ask("send"))
    cold = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-plumber", "text": "Any slot?"}))
    assert any("never messaged you" in r for r in cold["would_be_blocked_now_because"])
    assert "never messaged you" in send(f"send {cold['draft_id']}")
    old = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-old", "text": "Hey Jordan!"}))["draft_id"]
    assert "older than 7 days" in send(f"send {old}")


def test_quiet_hours(clock):
    inbox = make_sample_inbox(clock, {"quiet_hours": "23:00-07:00"}, local_hour=23)
    d = json.loads(build_handlers(inbox)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "ok!"}))
    assert "Quiet hours" in build_command(inbox, _ask("send"))(f"send {d['draft_id']}")


def test_status_and_limits_commands(sample_inbox):
    cmd = build_command(sample_inbox, _ask(None))
    assert "Connected to Unipile" in cmd("")
    assert "New conversations" in cmd("limits") and "at most 5 a day" in cmd("limits")
    assert "No drafts" in cmd("drafts")
    assert "/inbox send" in cmd("help")
    st = json.loads(build_handlers(sample_inbox)["inbox_status"]({}))
    assert st["connected"] is True and len(st["accounts"]) == 3
    assert st["sent_last_24h"]["linkedin_limit"] == 10


def test_discard(sample_inbox):
    h = build_handlers(sample_inbox)
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "x y z"}))["draft_id"]
    assert "discarded" in build_command(sample_inbox, _ask(None))(f"discard {d}")
    assert json.loads(h["inbox_list_drafts"]({}))["drafts"] == []


def test_linkedin_read_search_draft_send(sample_inbox):
    h = build_handlers(sample_inbox)
    li = json.loads(h["inbox_list_chats"]({"network": "linkedin"}))["chats"]
    assert {c["chat_id"] for c in li} == {"li-priya", "li-marco", "li-elena"}
    hits = json.loads(h["inbox_search"]({"query": "availability", "network": "linkedin"}))["results"]
    assert [r["chat_id"] for r in hits] == ["li-priya"]
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "li-priya", "text": "Thursday 3-5pm works for me!"}))
    assert d["network"] == "linkedin" and d["sent"] is False
    out = build_command(sample_inbox, _ask("send"))(f"send {d['draft_id']}")
    assert out.startswith("Sent to Priya Nair (Recruiter, Northwind) on LinkedIn.")
    assert "LinkedIn 1/10" in build_command(sample_inbox, _ask(None))("status")
