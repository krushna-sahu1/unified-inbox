"""Live mode: the Unipile v2 request format, pacing and error handling. No network is used."""

import io
import json
from email.message import Message
from urllib import error as urlerror

import pytest

from conftest import KEY, make_inbox
from unified_inbox import client as client_mod
from unified_inbox.client import InboxError, UnipileClient
from unified_inbox.pacing import HOUR

CFG = {"unipile_account_id": "acc_wa"}


class FakeUnipile:
    """Stands in for UnipileClient with the same v2-shaped method signatures."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.fail = {}  # method -> errors to raise, in order
        self.account_status = "OK"
        self.extra_accounts = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        queue = self.fail.get(name) or []
        if queue:
            raise queue.pop(0)

    def list_accounts(self):
        self._maybe_fail("accounts")
        return [{"id": "acc_wa", "network": "whatsapp", "status": self.account_status,
                 "connected_at": self.clock() - 60}] + self.extra_accounts

    def list_chats(self, account_id, *, limit, cursor=None, unread_only=False):
        self._maybe_fail("chats")
        net = "whatsapp" if account_id == "acc_wa" else "linkedin"
        return [{"id": f"c-{account_id}", "account_id": account_id, "network": net, "name": "Ana",
                 "unread": 1, "ts": self.clock() - 60}], None

    def list_messages(self, account_id, chat_id, *, limit):
        self._maybe_fail("messages")
        return [{"id": "m1", "chat_id": chat_id, "from_me": False, "sender": "Ana", "text": "hi?",
                 "ts": self.clock() - 60}]

    def send_message(self, account_id, chat_id, text):
        self._maybe_fail("send")
        return "msg-1"


def _live(clock, sleeps=None, config=None):
    fake = FakeUnipile(clock)
    inbox = make_inbox(clock, {**CFG, **(config or {})}, secrets=KEY, sleeps=sleeps, backend=fake)
    return inbox, fake


def _rate_limit(retry_after=18):
    return InboxError("Unipile 429: Too many requests", status=429, kind="too_many_requests",
                      retry_after=retry_after)


# ─── HTTP layer (urlopen is faked) ───────────────────────────────────────────

class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, body=None, error=None):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        if error:
            raise error
        return _Resp(body or {})

    monkeypatch.setattr(client_mod.request, "urlopen", fake_urlopen)
    return seen


def test_v2_base_url_and_routes(monkeypatch):
    seen = _capture(monkeypatch, {"data": [{"id": "chat1", "name": "Ana", "provider": "whatsapp",
                                            "unread_count": -1,
                                            "last_message_timestamp": "2026-11-09T10:00:00Z"}],
                                  "next_cursor": "abc"})
    c = UnipileClient("secret")
    chats, cursor = c.list_chats("acc_1", limit=20, unread_only=True)
    req = seen[0]
    assert req.full_url == "https://api.unipile.com/v2/acc_1/chats?limit=20&is_unread=true"
    assert req.get_header("X-api-key") == "secret"
    assert chats[0]["unread"] == 1 and chats[0]["network"] == "whatsapp" and cursor == "abc"


def test_v2_send_is_json_to_messages_send(monkeypatch):
    seen = _capture(monkeypatch, {"object": "MessageSent", "message_id": ["a", "b"]})
    assert UnipileClient("k").send_message("acc_1", "chat/1", "hello") == "a,b"
    req = seen[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.unipile.com/v2/acc_1/chats/chat%2F1/messages/send"
    assert json.loads(req.data) == {"text": "hello"}
    assert req.get_header("Content-type") == "application/json"


def test_v2_accounts_and_messages_parsing(monkeypatch):
    _capture(monkeypatch, {"object": "Accounts", "data": [
        {"id": "acc_1", "provider": "linkedin", "status": "running", "is_locked": False},
        {"id": "acc_2", "provider": "whatsapp", "status": "disconnected"},
        {"id": "acc_3", "provider": "google", "status": "running"}]})
    accts = UnipileClient("k").list_accounts()
    assert [(a["id"], a["network"], a["status"]) for a in accts] == [
        ("acc_1", "linkedin", "OK"), ("acc_2", "whatsapp", "DISCONNECTED")]
    _capture(monkeypatch, {"data": [
        {"id": "m2", "is_sender": True, "text": "yes", "timestamp": "2026-11-09T10:05:00Z"},
        {"id": "m1", "is_sender": False, "text": "hi?", "timestamp": "2026-11-09T10:00:00Z",
         "sender": {"display_name": "Ana"}},
        {"id": "m0", "is_event": True, "timestamp": "2026-11-09T09:00:00Z"}]})
    msgs = UnipileClient("k").list_messages("acc_1", "c1", limit=10)
    assert [(m["id"], m["from_me"], m["sender"]) for m in msgs] == [("m1", False, "Ana"), ("m2", True, "you")]


def test_429_is_parsed_and_not_retried_by_the_client(monkeypatch):
    headers = Message()
    headers["retry-after"] = "18"
    body = io.BytesIO(json.dumps({"object": "Error", "type": "api/too_many_requests", "status": 429,
                                  "title": "Too many requests"}).encode())
    seen = _capture(monkeypatch, error=urlerror.HTTPError("u", 429, "Too Many Requests", headers, body))
    with pytest.raises(InboxError) as info:
        UnipileClient("k").list_chats("acc_1", limit=5)
    assert (info.value.status, info.value.kind, info.value.retry_after) == (429, "too_many_requests", 18.0)
    assert len(seen) == 1


def test_api_key_never_in_url_or_error(monkeypatch):
    headers = Message()
    body = io.BytesIO(b'{"type": "api/expired_authorization", "status": 401, "title": "Expired"}')
    seen = _capture(monkeypatch, error=urlerror.HTTPError("u", 401, "Unauthorized", headers, body))
    with pytest.raises(InboxError) as info:
        UnipileClient("super-secret-key").list_chats("acc_1", limit=5)
    assert "super-secret-key" not in seen[0].full_url and "super-secret-key" not in str(info.value)


# ─── pacing layer ────────────────────────────────────────────────────────────

def test_needs_the_api_key(clock):
    inbox = make_inbox(clock, {"unipile_account_id": "acc_wa"})
    with pytest.raises(InboxError, match="API key") as info:
        inbox.client()
    assert info.value.kind == "setup"


def test_without_a_filter_every_connected_app_is_used(clock):
    fake = FakeUnipile(clock)
    fake.extra_accounts = [{"id": "acc_li", "network": "linkedin", "status": "OK", "connected_at": 0}]
    inbox = make_inbox(clock, secrets=KEY, backend=fake)
    assert sorted(a["id"] for a in inbox.refresh_accounts(force=True)) == ["acc_li", "acc_wa"]
    assert {c["account_id"] for c in inbox.list_chats()} == {"acc_li", "acc_wa"}
    fake.extra_accounts = []  # an app disconnected from Unipile drops out
    assert [a["id"] for a in inbox.refresh_accounts(force=True)] == ["acc_wa"]


def test_random_gap_between_reads(clock):
    sleeps = []
    inbox, _ = _live(clock, sleeps)
    inbox.list_chats()
    inbox.list_chats()
    assert sleeps and all(2.0 <= s <= 5.0 for s in sleeps)


def test_hourly_read_budget(clock):
    inbox, _ = _live(clock, config={"api_calls_per_hour": 2})
    inbox.list_chats()  # accounts + chats = 2 calls
    with pytest.raises(InboxError) as info:
        inbox.list_chats()
    assert info.value.kind == "budget"


def test_429_waits_with_a_clear_message_and_never_loops(clock):
    inbox, fake = _live(clock)
    inbox.refresh_accounts(force=True)
    fake.fail["chats"] = [_rate_limit(retry_after=90)]
    with pytest.raises(InboxError) as info:
        inbox.list_chats()
    msg = str(info.value)
    assert info.value.kind == "unavailable"
    assert "rate-limiting" in msg and "WhatsApp" in msg and "Nothing was retried" in msg and "until" in msg
    assert fake.calls.count("chats") == 1  # exactly one request, no retry loop

    # while waiting, nothing reaches Unipile and the message says why
    with pytest.raises(InboxError) as info:
        inbox.list_chats()
    assert "Waiting before contacting Unipile again" in str(info.value)
    assert fake.calls.count("chats") == 1

    clock.advance(91)  # retry-after (90 s) honoured
    assert inbox.list_chats()
    assert fake.calls.count("chats") == 2


def test_429_on_send_keeps_the_draft(clock):
    inbox, fake = _live(clock)
    d = inbox.create_draft("c-acc_wa", "Sure, Thursday works for me!")["draft_id"]
    fake.fail["send"] = [_rate_limit()]
    out = inbox.send(d)
    assert out["sent"] is False
    assert "NOT sent" in out["reasons"][0] and "still saved" in out["reasons"][0]
    assert fake.calls.count("send") == 1
    assert inbox.store().get_draft(d)["status"] == "pending"
    assert inbox.store().recent_sends(0) == []


def test_one_rate_limited_account_does_not_hide_the_others(clock):
    inbox, fake = _live(clock, config={"unipile_account_id": "acc_wa, acc_li"})
    fake.extra_accounts = [{"id": "acc_li", "network": "linkedin", "status": "OK", "connected_at": 0}]
    inbox.refresh_accounts(force=True)
    fake.fail["chats"] = [_rate_limit()]
    chats, problems = inbox.gather_chats()  # the first account read gets the 429
    assert len(chats) == 1 and len(problems) == 1 and "rate-limiting" in problems[0]
    limited = "LinkedIn" if chats[0]["account_id"] == "acc_wa" else "WhatsApp"
    assert problems[0].startswith(limited)


def test_unknown_account_id_is_reported(clock):
    inbox, _ = _live(clock, config={"unipile_account_id": "acc_wa, acc_typo"})
    chats, problems = inbox.gather_chats()
    assert chats and any("acc_typo" in p for p in problems)
    assert inbox.status()["accounts_not_found"] == ["acc_typo"]


def test_only_configured_accounts_are_used(clock):
    inbox, fake = _live(clock)
    fake.extra_accounts = [{"id": "acc_other", "network": "linkedin", "status": "OK", "connected_at": 0}]
    assert [a["id"] for a in inbox.refresh_accounts(force=True)] == ["acc_wa"]


def test_bad_api_key_message(clock):
    inbox, fake = _live(clock)
    fake.fail["accounts"] = [InboxError("Unipile 401", status=401, kind="expired_authorization")]
    with pytest.raises(InboxError, match="rejected the API key"):
        inbox.list_chats()


def test_server_errors_retry_reads_but_never_sends(clock):
    inbox, fake = _live(clock)
    inbox.refresh_accounts(force=True)
    fake.fail["chats"] = [InboxError("503", status=503), InboxError("503", status=503)]
    assert inbox.list_chats()
    assert fake.calls.count("chats") == 3

    d = inbox.create_draft("c-acc_wa", "Hello Ana, yes that works for me!")["draft_id"]
    fake.fail["send"] = [InboxError("Unipile 500: boom", status=500)]
    out = inbox.send(d)
    assert out["sent"] is False and "may or may not have been delivered" in out["reasons"][0]
    assert fake.calls.count("send") == 1
    assert len(inbox.store().recent_sends(0)) == 1  # counted against the limits anyway


def test_disconnected_account_pauses_until_reconnected(clock):
    inbox, fake = _live(clock)
    inbox.list_chats()
    fake.fail["messages"] = [InboxError("401", status=401, kind="invalid_authorization")]
    d = inbox.create_draft("c-acc_wa", "hi Ana!")["draft_id"]  # cached chat, no fetch needed
    assert inbox.send(d)["sent"] is False
    assert "disconnected" in inbox.store().gate("acc_wa").pause_reason

    fake.account_status = "DISCONNECTED"
    clock.advance(11 * 60)
    inbox.refresh_accounts(force=True)
    fake.account_status = "OK"
    inbox.refresh_accounts(force=True)
    assert inbox.store().gate("acc_wa").paused_until <= clock()
    assert inbox.send(d)["sent"] is True  # works right away after reconnecting


def test_newly_connected_account_can_send_right_away(clock):
    inbox, fake = _live(clock)
    inbox.list_chats()
    d = inbox.create_draft("c-acc_wa", "hello!")["draft_id"]
    assert inbox.send(d)["sent"] is True
    assert fake.calls.count("send") == 1


def test_restricted_account_pauses_a_day(clock):
    inbox, fake = _live(clock)
    inbox.list_chats()
    fake.fail["send"] = [InboxError("403", status=403, kind="account_restricted")]
    d = inbox.create_draft("c-acc_wa", "Sure, see you then")["draft_id"]
    assert inbox.send(d)["sent"] is False
    assert inbox.store().gate("acc_wa").paused_until >= clock() + 23 * HOUR
