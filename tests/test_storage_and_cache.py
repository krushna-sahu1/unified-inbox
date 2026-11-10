"""Storage upgrades, search, cache reuse, pruning and the Phase 0 bug fixes."""

import json
import sqlite3

import pytest

from conftest import KEY, make_inbox, make_sample_inbox
from unified_inbox import client as client_mod
from unified_inbox.client import InboxError, UnipileClient
from unified_inbox.commands import build_command
from unified_inbox.inbox import FRESH_SECONDS, SETUP_MESSAGE
from unified_inbox.pacing import DAY
from unified_inbox.store import SCHEMA_VERSION, Store
from unified_inbox.tools import build_handlers


def _ask(answer):
    return lambda question, choices: answer


# ─── setup: no sample mode ───────────────────────────────────────────────────

def test_unconfigured_plugin_asks_for_setup_and_spends_nothing(clock):
    inbox = make_inbox(clock)
    h = build_handlers(inbox)
    for name, args in [("inbox_list_chats", {}), ("inbox_triage", {}), ("inbox_search", {"query": "hello"}),
                       ("inbox_read_chat", {"chat_id": "x"}), ("inbox_draft_reply", {"chat_id": "x", "text": "hi"})]:
        out = json.loads(h[name](args))
        assert out["kind"] == "setup" and "Unipile API key" in out["error"], name
    st = json.loads(h["inbox_status"]({}))
    assert st["connected"] is False and st["accounts_error"] == SETUP_MESSAGE
    assert build_command(inbox, _ask(None))("status") == SETUP_MESSAGE
    assert inbox.client_keys == []  # no client was ever built
    assert inbox.store().api_calls_last_hour(clock()) == 0


def test_new_api_key_takes_effect_without_restart(clock):
    secrets = dict(KEY)
    inbox = make_inbox(clock, {"unipile_account_id": "acc_1"}, secrets=secrets)
    inbox._get_secret = lambda n: secrets.get(n)
    inbox.client()
    secrets["UNIPILE_API_KEY"] = "rotated"
    inbox.client()
    assert inbox.client_keys == ["test-key", "rotated"]


# ─── storage ─────────────────────────────────────────────────────────────────

_V1_SQL = """
CREATE TABLE drafts (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, account_id TEXT NOT NULL, network TEXT NOT NULL,
  chat_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE chats (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, network TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', unread INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL DEFAULT 0,
  fetched_at REAL NOT NULL);
CREATE TABLE messages (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, from_me INTEGER NOT NULL,
  sender TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', ts REAL NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO chats VALUES ('c1', 'acc', 'whatsapp', 'Ana', 0, 100, 100);
INSERT INTO messages VALUES ('m1', 'c1', 0, 'Ana', 'Kal milte hain, plumber aayega', 100, 100);
INSERT INTO meta VALUES ('msgs_at:c1', '123.0');
INSERT INTO meta VALUES ('accounts_at', '5.0');
INSERT INTO drafts VALUES ('abc123', 'c1', 'acc', 'whatsapp', 'Ana', 'ok', 'pending', '', 1, 1);
"""


def test_v1_database_upgrades_in_place():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_V1_SQL)
    store = Store(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.cached_chat("c1")["msgs_fetched_at"] == 123.0  # moved off the meta table
    assert store.get_meta("msgs_at:c1") is None and store.get_meta("accounts_at") == 5.0
    assert store.get_draft("abc123")["text"] == "ok"  # user data kept
    assert [r["chat_id"] for r in store.search_messages("PLUMBER")] == ["c1"]  # old rows are indexed
    Store(conn)  # opening again is a no-op


def test_search_is_substring_case_insensitive_and_multilingual():
    store = Store(sqlite3.connect(":memory:"))
    store.upsert_chats([{"id": "c1", "account_id": "a", "network": "whatsapp", "name": "Ravi", "ts": 3}], 0)
    store.upsert_messages([
        {"id": "1", "chat_id": "c1", "from_me": False, "text": "Kal milte hain, 5 baje", "ts": 1},
        {"id": "2", "chat_id": "c1", "from_me": False, "text": "नमस्ते दोस्त, कैसे हो?", "ts": 2},
        {"id": "3", "chat_id": "c1", "from_me": True, "text": 'He said "100% sure" about it_now', "ts": 3},
    ], 0)
    assert store.fts
    find = lambda q, **kw: [r["text"][:4] for r in store.search_messages(q, **kw)]  # noqa: E731
    assert find("MILTE") == ["Kal "]
    assert find("lte ha") == ["Kal "]           # inside words, across a space
    assert find("दोस्त") == ["नमस्"]
    assert find("ok") == []                     # 2 characters: LIKE fallback
    assert find("5") == ["Kal "]                # 1 character
    assert find('"100% sure"') == ["He s"]      # quotes and % are literal
    assert find("it_n") == ["He s"]             # _ is literal
    assert find("milte", network="linkedin") == []
    # an edited message is re-indexed; an unchanged one is not touched
    store.upsert_messages([{"id": "1", "chat_id": "c1", "from_me": False, "text": "Parso milte hain", "ts": 1}], 5)
    assert find("Kal mi") == [] and find("Parso") == ["Pars"]


def test_search_falls_back_to_like_without_fts():
    store = Store(sqlite3.connect(":memory:"))
    store.fts = False
    store.upsert_chats([{"id": "c1", "account_id": "a", "network": "whatsapp", "name": "Ravi", "ts": 3}], 0)
    store.upsert_messages([{"id": "1", "chat_id": "c1", "from_me": False, "text": "Kal milte hain", "ts": 1}], 0)
    assert [r["chat_id"] for r in store.search_messages("MILTE")] == ["c1"]


def test_prune_keeps_recent_data_and_pending_drafts(clock):
    store = Store(sqlite3.connect(":memory:"))
    now = clock()
    old, new = now - 200 * DAY, now - DAY
    store.upsert_chats([{"id": "old", "account_id": "a", "network": "whatsapp", "ts": old},
                        {"id": "old-draft", "account_id": "a", "network": "whatsapp", "ts": old},
                        {"id": "new", "account_id": "a", "network": "whatsapp", "ts": new}], now)
    store.upsert_messages([{"id": "1", "chat_id": "old", "from_me": False, "text": "ancient words", "ts": old},
                           {"id": "2", "chat_id": "new", "from_me": False, "text": "fresh words", "ts": new}], now)
    store.add_draft(chat_id="old-draft", account_id="a", network="whatsapp", chat_name="", text="x", now=old)
    store.log_send(draft_id="d", chat_id="new", account_id="a", network="whatsapp", text="t", message_id="",
                   now=now - 100 * DAY)
    for ts, text in ((now - 100 * DAY, "an old attempt"), (now - DAY, "a recent attempt")):
        store.log_attempt(via="hermes", network="whatsapp", to_name="Ravi", target="new", text=text, files=[],
                          status="sent", now=ts)
    out = store.prune(now, retention_days=180)
    assert out == {"messages": 1, "chats": 1, "drafts": 0, "send_log": 1, "send_attempts": 1, "events": 0,
                   "chunks": 0, "files": 0, "plans": 0, "translations": 0}
    assert [a["text"] for a in store.attempts()] == ["a recent attempt"]  # the activity log ages out too
    assert store.cached_chat("old-draft") and store.cached_chat("new") and not store.cached_chat("old")
    assert store.search_messages("words") and store.search_messages("ancient") == []  # index kept in sync
    assert store.maybe_prune(now, 180) is not None and store.maybe_prune(now + 60, 180) is None


def test_draft_count():
    store = Store(sqlite3.connect(":memory:"))
    for _ in range(3):
        store.add_draft(chat_id="c", account_id="a", network="whatsapp", chat_name="", text="x", now=1)
    assert store.count_drafts("pending") == 3 and store.count_drafts("sent") == 0


# ─── cache reuse ─────────────────────────────────────────────────────────────

def test_tools_reuse_a_fresh_chat_list_then_refetch(clock):
    inbox = make_sample_inbox(clock)
    h = build_handlers(inbox)
    first = json.loads(h["inbox_list_chats"]({}))["chats"]
    calls = inbox.backend.calls.count("chats")
    assert json.loads(h["inbox_list_chats"]({}))["chats"] == first
    assert json.loads(h["inbox_list_chats"]({"network": "linkedin"}))["chats"]
    assert inbox.backend.calls.count("chats") == calls  # served from the cache
    clock.advance(FRESH_SECONDS + 1)
    h["inbox_list_chats"]({})
    assert inbox.backend.calls.count("chats") == calls * 2


def test_unread_list_is_never_built_from_a_partial_full_list(clock):
    inbox = make_sample_inbox(clock)
    h = build_handlers(inbox)
    h["inbox_list_chats"]({"limit": 2})  # partial (not every chat was read)
    calls = inbox.backend.calls.count("chats")
    unread = json.loads(h["inbox_list_chats"]({"unread_only": True, "limit": 2}))["chats"]
    assert inbox.backend.calls.count("chats") > calls  # asked Unipile instead of guessing
    assert all(c["unread"] > 0 for c in unread)


def test_read_chat_reuses_a_fresh_read(clock):
    inbox = make_sample_inbox(clock)
    h = build_handlers(inbox)
    h["inbox_list_chats"]({})
    inbox.messages(inbox.find_chat("wa-maya"), limit=20)
    n = inbox.backend.calls.count("messages")
    assert len(json.loads(h["inbox_read_chat"]({"chat_id": "wa-maya", "limit": 3}))["messages"]) == 3
    assert len(json.loads(h["inbox_read_chat"]({"chat_id": "wa-maya", "limit": 20}))["messages"]) == 4
    assert inbox.backend.calls.count("messages") == n  # both answered by the 20-message read
    inbox.messages(inbox.find_chat("ig-lena"), limit=5)
    n = inbox.backend.calls.count("messages")
    h["inbox_read_chat"]({"chat_id": "ig-lena", "limit": 20})  # a 5-message read can't answer 20
    assert inbox.backend.calls.count("messages") == n + 1


def test_send_reads_the_chat_once(sample_inbox):
    h = build_handlers(sample_inbox)
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))["draft_id"]
    before = sample_inbox.backend.calls.count("messages")
    assert build_command(sample_inbox, _ask("send"))(f"send {d}").startswith("Sent")
    assert sample_inbox.backend.calls.count("messages") == before + 1
    # and the next read shows the sent message straight away
    msgs = json.loads(h["inbox_read_chat"]({"chat_id": "wa-maya", "limit": 2}))["messages"]
    assert msgs[-1]["text"] == "Yes, home at 8!"


# ─── Phase 0 bug fixes ───────────────────────────────────────────────────────

class _Backend:
    def __init__(self, clock, chats_for=("acc_wa",)):
        self.clock, self.chats_for, self.fail_send = clock, set(chats_for), None

    def list_accounts(self):
        return [{"id": "acc_wa", "network": "whatsapp", "status": "OK", "connected_at": 0},
                {"id": "acc_li", "network": "linkedin", "status": "OK", "connected_at": 0}]

    def list_chats(self, account_id, *, limit, cursor=None, unread_only=False):
        if account_id not in self.chats_for:
            return [], None
        return [{"id": "c1", "account_id": account_id, "network": "whatsapp", "name": "Ana", "unread": 1,
                 "ts": self.clock() - 60}], None

    def list_messages(self, account_id, chat_id, *, limit):
        return [{"id": "m1", "chat_id": chat_id, "from_me": False, "sender": "Ana", "text": "hi?",
                 "ts": self.clock() - 60}]

    def send_message(self, account_id, chat_id, text):
        if self.fail_send:
            raise self.fail_send
        return "x"


def test_missing_account_plus_empty_inbox_is_not_an_error(clock):
    backend = _Backend(clock, chats_for=())
    inbox = make_inbox(clock, {"unipile_account_id": "acc_li, acc_typo"}, secrets=KEY, backend=backend)
    chats, problems = inbox.gather_chats()
    assert chats == [] and len(problems) == 1 and "acc_typo" in problems[0]


@pytest.mark.parametrize("error", [
    InboxError("budget", kind="budget"),
    InboxError("Unipile 401", status=401, kind="expired_authorization"),
    InboxError("Unipile 401", status=401, kind="invalid_authorization"),
])
def test_send_errors_that_prove_nothing_was_sent_keep_the_draft(clock, error):
    backend = _Backend(clock)
    inbox = make_inbox(clock, {"unipile_account_id": "acc_wa"}, secrets=KEY, backend=backend)
    inbox.list_chats()
    d = inbox.create_draft("c1", "Sure, see you at five then!")["draft_id"]
    backend.fail_send = error
    out = inbox.send(d)
    assert out["sent"] is False and "still saved" in out["reasons"][0]
    assert inbox.store().get_draft(d)["status"] == "pending"
    assert inbox.store().recent_sends(0) == []


def test_budget_reached_right_before_send_keeps_the_draft(clock):
    backend = _Backend(clock)
    inbox = make_inbox(clock, {"unipile_account_id": "acc_wa", "api_calls_per_hour": 4}, secrets=KEY,
                       backend=backend)
    inbox.list_chats()  # accounts + chats
    d = inbox.create_draft("c1", "Sure, see you at five then!")["draft_id"]  # messages (3 calls)
    out = inbox.send(d)  # preflight reuses the read, so the send itself is call 4
    assert out["sent"] is True
    d2 = inbox.create_draft("c1", "One more thing about Friday")["draft_id"]  # call 5 -> over budget
    clock.advance(120)
    out = inbox.send(d2)
    assert out["sent"] is False and "budget" in " ".join(out["reasons"]).lower()
    assert inbox.store().get_draft(d2)["status"] == "pending"


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_accounts_follow_pagination(monkeypatch):
    pages = iter([
        {"data": [{"id": f"acc_{i}", "provider": "whatsapp", "status": "running"} for i in range(50)],
         "next_cursor": "p2"},
        {"data": [{"id": "acc_50", "provider": "linkedin", "status": "running"}], "next_cursor": None},
    ])
    urls = []

    def fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        return _Resp(next(pages))

    monkeypatch.setattr(client_mod.request, "urlopen", fake_urlopen)
    accounts = UnipileClient("k").list_accounts()
    assert len(accounts) == 51 and accounts[-1]["id"] == "acc_50"
    assert urls[1].endswith("cursor=p2")


def test_a_chat_list_without_previews_keeps_the_known_preview():
    store = Store(sqlite3.connect(":memory:"))
    base = {"id": "c1", "account_id": "a", "network": "whatsapp", "name": "Ana"}
    store.upsert_chats([dict(base, ts=10, last_text="see you at 6", last_from_me=False)], 10)
    store.upsert_chats([dict(base, ts=10)], 20)  # same chat, no preview in this answer
    assert store.cached_chat("c1")["last_text"] == "see you at 6"
    store.upsert_chats([dict(base, ts=30, last_text="on my way", last_from_me=True)], 30)
    assert store.cached_chat("c1")["last_text"] == "on my way" and store.cached_chat("c1")["last_from_me"]
