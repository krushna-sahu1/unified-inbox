"""Webhooks: signature checks, event handling, the local receiver, setup, and v2 parsing."""

import hashlib
import hmac
import json
from urllib import error as urlerror
from urllib import request

import pytest

from conftest import make_inbox, make_sample_inbox
from unified_inbox import client as client_mod
from unified_inbox.client import InboxError, UnipileClient, describe_attachment, parse_chat, parse_message
from unified_inbox.commands import build_command
from unified_inbox.sync import SyncEngine
from unified_inbox.tools import build_handlers
from unified_inbox.webhooks import (EVENTS, PATH, WebhookHandler, WebhookServer, remove_webhook, setup_webhook,
                                    verify_signature)

SECRET = "whsec_test"


def _sign(raw: bytes, t: int, secret: str = SECRET) -> str:
    return f"t={t},v0=" + hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()


def _event(kind, payload, account="mock-wa", eid="evt_1", provider="WHATSAPP"):
    return {"id": eid, "created_at": "2026-11-09T10:00:00Z", "account_id": account, "account_provider": provider,
            "type": kind, "payload": payload}


def _msg(mid, chat_id, text, ts_iso="2027-01-15T08:00:30Z", **kw):
    return {"object": "Message", "id": mid, "chat_id": chat_id, "timestamp": ts_iso, "is_sender": False,
            "text": text, "sender": {"display_name": "Maya Rivers"}, "attachments": [], **kw}


@pytest.fixture
def hooked(clock):
    inbox = make_sample_inbox(clock)
    SyncEngine(inbox).sync_once()
    inbox.store().set_meta("webhook", {"id": "we_1", "url": "https://x.example/unipile/webhook", "secret": SECRET})
    woke = []
    handler = WebhookHandler(inbox, on_change=lambda: woke.append(1))
    handler.woke = woke

    def deliver(event, *, t=None, secret=SECRET, raw=None):
        raw = raw if raw is not None else json.dumps(event).encode()
        return handler.handle(raw, {"unipile-signature": _sign(raw, int(t or clock()), secret)})

    return inbox, handler, deliver


# ─── signatures ──────────────────────────────────────────────────────────────

def test_signature_check():
    raw, now = b'{"a":1}', 1_800_000_000
    good = _sign(raw, now)
    assert verify_signature(SECRET, good, raw, now)
    assert verify_signature(SECRET, f"t={now},v0=00ff," + good.split(",")[1], raw, now)  # any v0 may match
    assert not verify_signature(SECRET, good, raw + b" ", now)               # body changed
    assert not verify_signature("other", good, raw, now)                     # wrong secret
    assert not verify_signature(SECRET, good, raw, now + 301)                # too old
    assert not verify_signature(SECRET, None, raw, now)
    assert not verify_signature(SECRET, "v0=abc", raw, now)
    assert not verify_signature("", good, raw, now)


# ─── handling ────────────────────────────────────────────────────────────────

def test_new_message_is_stored_announced_once_and_ignored_when_forged(hooked, clock):
    inbox, _, deliver = hooked
    clock.t = 1_800_000_000.0
    payload = _msg("wh-1", "wa-maya", "Plumber is here early!", ts_iso=client_mod.iso(clock() + 5))
    unread_before = inbox.store().cached_chat("wa-maya")["unread"]
    assert deliver(_event("message.new", payload)) == (200, "ok")
    assert deliver(_event("message.new", payload)) == (200, "duplicate")
    assert deliver(_event("message.new", payload, eid="evt_2"), secret="forged") == (401, "bad signature")
    assert deliver(_event("message.new", payload, eid="evt_3", account="acc_someone_else")) == (200, "other account")
    store = inbox.store()
    chat = store.cached_chat("wa-maya")
    assert chat["last_text"] == "Plumber is here early!" and chat["unread"] == unread_before + 1
    ev = store.events_after(0, types=["message.new"])
    assert [e["message_id"] for e in ev] == ["wh-1"]
    up = json.loads(build_handlers(inbox)["inbox_updates"]({}))
    assert up["chats"][0]["name"] == "Maya Rivers" and up["chats"][0]["count"] == 1
    assert store.search_messages("here early")


def test_unknown_chat_is_created_and_filled_in_by_the_next_sync(hooked, clock):
    inbox, handler, deliver = hooked
    payload = _msg("wh-2", "ig-arjun", "yo, climbing sunday?", ts_iso=client_mod.iso(clock() + 5))
    inbox.store().delete_chat("ig-arjun")
    assert deliver(_event("message.new", payload, account="mock-ig", provider="INSTAGRAM")) == (200, "ok")
    chat = inbox.store().cached_chat("ig-arjun")
    assert chat["needs_refresh"] and chat["network"] == "instagram" and handler.woke
    SyncEngine(inbox).sync_once()
    assert inbox.store().cached_chat("ig-arjun")["name"] == "arjun.climbs"


def test_edits_deletes_and_reactions(hooked, clock):
    inbox, _, deliver = hooked
    t = client_mod.iso(clock() + 5)
    deliver(_event("message.new", _msg("wh-3", "wa-dev", "deck is attached", ts_iso=t), eid="e1"))
    deliver(_event("message.update", _msg("wh-3", "wa-dev", "deck is attached (v2)", ts_iso=t, is_edited=True),
                   eid="e2"))
    store = inbox.store()
    assert store.get_message("wh-3")["text"] == "deck is attached (v2)" and store.get_message("wh-3")["edited"]
    deliver(_event("message.reaction.new", {"chat_id": "wa-dev", "message_id": "wh-3", "reaction": "👍"}, eid="e3"))
    deliver(_event("message.reaction.new", {"chat_id": "wa-dev", "message_id": "wh-3", "reaction": "👍"}, eid="e4"))
    assert store.get_message("wh-3")["extra"]["reactions"] == [{"reaction": "👍", "count": 2}]
    deliver(_event("message.reaction.delete", {"chat_id": "wa-dev", "message_id": "wh-3", "reaction": "👍"},
                   eid="e5"))
    assert store.get_message("wh-3")["extra"]["reactions"] == [{"reaction": "👍", "count": 1}]
    deliver(_event("message.delete", {"chat_id": "wa-dev", "id": "wh-3"}, eid="e6"))
    assert store.get_message("wh-3") is None
    types = [e["type"] for e in store.events_after(0)]
    assert types.count("message.update") == 1 and types.count("message.delete") == 1


def test_account_status_events_pause_and_resume(hooked, clock):
    inbox, _, deliver = hooked
    deliver(_event("account.status.disconnected", {"timestamp": "x"}, eid="a1"))
    gate = inbox.store().gate("mock-wa")
    assert gate.paused_until > clock() and "DISCONNECTED" in gate.pause_reason
    deliver(_event("account.status.running", {"timestamp": "x"}, eid="a2"))
    assert inbox.store().gate("mock-wa").paused_until <= clock()


def test_bad_deliveries(hooked, clock):
    inbox, handler, deliver = hooked
    raw = b"not json"
    assert handler.handle(raw, {"unipile-signature": _sign(raw, int(clock()))}) == (400, "not JSON")
    assert deliver({"no": "type"}) == (400, "not an event")
    inbox.store().set_meta("webhook", {})
    assert deliver(_event("message.new", {})) == (503, "webhook not set up")
    assert WebhookHandler(make_inbox(clock)).handle(b"{}", {}) == (503, "not connected")


# ─── the local receiver (real HTTP on 127.0.0.1) ─────────────────────────────

def test_receiver_end_to_end(hooked, clock):
    inbox, handler, _ = hooked
    server = WebhookServer(handler, "127.0.0.1", 0)
    server.start()
    try:
        host, port = server.address
        url = f"http://{host}:{port}{PATH}"
        raw = json.dumps(_event("message.new", _msg("wh-9", "wa-mum", "call me", ts_iso=client_mod.iso(clock() + 5)),
                                eid="h1")).encode()

        def post(path, body, sig):
            req = request.Request(f"http://{host}:{port}{path}", data=body, method="POST",
                                  headers={"unipile-signature": sig, "Content-Type": "application/json"})
            try:
                with request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urlerror.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())

        assert post(PATH, raw, _sign(raw, int(clock()))) == (200, {"status": "ok"})
        assert inbox.store().get_message("wh-9")["text"] == "call me"
        assert post(PATH, raw, "t=1,v0=00")[0] == 401
        assert post("/elsewhere", raw, "")[0] == 404
        with request.urlopen(url, timeout=5) as resp:  # GET: lets people test their tunnel
            assert resp.status == 200
    finally:
        server.stop()


# ─── setup ───────────────────────────────────────────────────────────────────

class _HookBackend:
    def __init__(self):
        self.created, self.deleted = [], []

    def list_accounts(self):
        return [{"id": "acc_1", "network": "whatsapp", "status": "OK", "connected_at": 0}]

    def create_webhook(self, url, events, account_ids, description=""):
        self.created.append((url, events, account_ids))
        return {"id": f"we_{len(self.created)}", "secret": f"sec{len(self.created)}"}

    def delete_webhook(self, endpoint_id):
        self.deleted.append(endpoint_id)


def test_setup_replace_and_remove(clock):
    backend = _HookBackend()
    inbox = make_inbox(clock, {"unipile_account_id": "acc_1"}, secrets={"UNIPILE_API_KEY": "k"}, backend=backend)
    with pytest.raises(InboxError, match="https"):
        setup_webhook(inbox, "http://insecure.example/unipile/webhook")
    assert setup_webhook(inbox, "https://a.example/unipile/webhook")["id"] == "we_1"
    assert backend.created[0][1] == EVENTS and backend.created[0][2] == []
    setup_webhook(inbox, "https://b.example/unipile/webhook")
    assert backend.deleted == ["we_1"]  # the old endpoint is cleaned up
    assert inbox.store().get_meta("webhook")["secret"] == "sec2"
    cmd = build_command(inbox, lambda q, c: None, {})
    assert "b.example" in cmd("webhook status") and "no events received yet" in cmd("webhook")
    assert "local receiver is off" in cmd("webhook setup https://c.example/unipile/webhook")
    assert remove_webhook(inbox) and backend.deleted[-1] == "we_3"
    assert remove_webhook(inbox) is False


class _Resp:
    def __init__(self, body, headers=None):
        self._body, self.headers = json.dumps(body).encode(), headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_webhook_and_message_page_requests(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return _Resp({"id": "we_1", "secret": "s", "data": [], "next_cursor": None},
                     {"x-ratelimit-remaining": "7", "x-ratelimit-reset": "30"})

    monkeypatch.setattr(client_mod.request, "urlopen", fake_urlopen)
    c = UnipileClient("k")
    c.create_webhook("https://x/unipile/webhook", ["message.new"], [])
    assert seen[0].full_url == "https://api.unipile.com/v2/webhooks/endpoints/"
    assert json.loads(seen[0].data) == {"url": "https://x/unipile/webhook", "trigger_events": ["message.new"]}
    c.list_messages_page("acc_1", "c1", limit=20, after=1_800_000_000.0)
    assert seen[1].full_url.endswith("/acc_1/chats/c1/messages?limit=20&after=2027-01-15T08%3A00%3A00.000Z")
    assert c.last_rate == {"remaining": 7, "reset": 30.0}


# ─── v2 parsing ──────────────────────────────────────────────────────────────

def test_parse_message_keeps_what_matters():
    m = parse_message({
        "id": "m1", "chat_id": "c1", "timestamp": "2026-11-09T10:00:00Z", "is_sender": False, "text": "",
        "sender_id": "u1", "sender": {"display_name": "Ana"}, "is_edited": True, "is_mentionned": True,
        "attachments": [{"type": "audio", "voice_note": True, "duration": 12, "id": "a1", "url": "https://tmp"}],
        "quoted": {"id": "m0", "text": "earlier", "attachments": []},
        "reactions_counter": [{"reaction": "❤️", "count": 2}]}, "c1")
    assert m["text"] == "[voice note]" and m["edited"] and m["sender_id"] == "u1"
    assert m["extra"] == {"attachments": [{"type": "audio", "id": "a1", "voice_note": True, "duration": 12}],
                          "quoted": {"id": "m0", "text": "earlier"},
                          "reactions": [{"reaction": "❤️", "count": 2}], "mentioned": True}
    assert "url" not in m["extra"]["attachments"][0]  # expiring links are never stored
    assert parse_message({"id": "e", "is_event": True}, "c1") is None
    assert parse_message({"id": "d", "is_deleted": True, "timestamp": "2026-11-09T10:00:00Z"}, "c1")["deleted"]
    assert describe_attachment({"type": "file", "filename": "cv.pdf"}) == "[file: cv.pdf]"
    assert describe_attachment({"type": "img"}) == "[photo]"
    assert describe_attachment({"type": "shared_content", "media_type": "reel"}) == "[shared reel]"


def test_parse_chat_flags():
    c = parse_chat({"id": "c1", "provider": "WHATSAPP", "name": "Family", "type": "group", "unread_count": 3,
                    "muted_until": True, "is_archived": True, "last_message_timestamp": "2026-11-09T10:00:00Z",
                    "last_message": {"text": "dinner?", "is_sender": False}}, "acc_1")
    assert (c["is_group"], c["is_muted"], c["is_archived"], c["last_text"], c["unread"]) == (
        True, True, True, "dinner?", 3)
    assert parse_chat({"id": "c2", "provider": "WHATSAPP", "muted_until": "2000-01-01T00:00:00Z"}, "a")["is_muted"] \
        is False
    assert parse_chat({"id": "c3", "provider": "GMAIL"}, "a") is None
