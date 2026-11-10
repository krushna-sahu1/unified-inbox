"""Phase 6: your data (export, forget, delete everything), the health check, speed guards and
release consistency."""

import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from conftest import make_inbox, make_sample_inbox
from fake_supabase import FakeSupabase
from unified_inbox import data_control, health
from unified_inbox.api import InboxAPI
from unified_inbox.cloud import CloudSync
from unified_inbox.commands import build_command
from unified_inbox.store import Store
from unified_inbox.sync import SyncEngine
from unified_inbox.tools import build_handlers

ROOT = Path(__file__).resolve().parent.parent


def ask(answer):
    return lambda question, choices: answer


@pytest.fixture
def inbox(clock):
    i = make_sample_inbox(clock)
    i._local_now = lambda: datetime.fromtimestamp(clock())
    while SyncEngine(i).sync_once()["pending_chats"]:
        pass
    return i


def _remember(inbox, chat_id, text):
    store, now = inbox.store(), inbox.clock()
    chat = store.cached_chat(chat_id)
    c = store.ensure_contact(chat_id=chat_id, network=chat["network"], name=chat["name"], now=now)
    return store.add_fact(contact_id=c["id"], text=text, category="other", status="active", source="user", now=now,
                          chat_id=chat_id)


# ─── export ──────────────────────────────────────────────────────────────────

def test_export_holds_everything_but_secrets(inbox, tmp_path):
    _remember(inbox, "wa-mum", "Birthday is 4 June")
    inbox.store().set_meta("webhook", {"id": "we_1", "secret": "whsec_TOPSECRET"})
    out = data_control.export_all(inbox)
    assert out["counts"]["chats"] == 13 and out["counts"]["messages"] > 13 and out["counts"]["facts"] == 1
    assert isinstance(out["tables"]["messages"][0]["extra"], dict)  # JSON columns are parsed
    text = json.dumps(out)
    assert "whsec_TOPSECRET" not in text and "test-key" not in text
    path = data_control.export_to_file(inbox, tmp_path)
    assert path.parent == tmp_path / "exports" and json.loads(path.read_text(encoding="utf-8"))["app"] == "unified-inbox"


def test_export_from_chat_needs_a_database_folder(inbox):
    assert "no folder" in build_command(inbox, ask(None))("export")  # in-memory test database


# ─── forget a person ─────────────────────────────────────────────────────────

def test_forget_removes_derived_memory_not_messages(inbox, clock):
    store, now = inbox.store(), clock()
    _remember(inbox, "li-priya", "Recruiter at Northwind")
    store.set_chat_context("li-priya", now, summary="Call before Friday")
    store.add_loop(chat_id="li-priya", kind="they_wait", text="Send availability", due_ts=None, message_id="", now=now)
    inbox.context.update_chunks("li-priya")
    store.add_question(kind="meaning", text="Which Friday?", data={"chat_id": "li-priya"}, dedupe_key="m1", now=now)
    messages = len(store.cached_messages("li-priya", 100))
    assert "Nothing was forgotten" in build_command(inbox, ask("cancel"))("forget Priya")
    out = build_command(inbox, ask("forget"))("forget Priya Nair (Recruiter, Northwind)")
    assert "Forgot 1 fact" in out
    contact = store.contact_for_chat("li-priya")
    assert store.facts(contact["id"]) == [] and store.chat_context("li-priya")["summary"] == ""
    assert store.loops(chat_id="li-priya") == [] and store.chunks_from("li-priya", 0) == []
    assert store.questions() == []
    assert len(store.cached_messages("li-priya", 100)) == messages  # the inbox itself is untouched


def test_forget_asks_which_one(inbox):
    assert build_command(inbox, ask("forget"))("forget a").startswith("Which one?")
    assert "No chat found" in build_command(inbox, ask("forget"))("forget zzzz")


# ─── delete everything ───────────────────────────────────────────────────────

def test_reset_keeps_only_what_the_limits_need(inbox, clock):
    store = inbox.store()
    h = build_handlers(inbox)
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    assert build_command(inbox, ask("send"))(f"send {d['draft_id']}").startswith("Sent")
    store.log_send(draft_id="old", chat_id="x", account_id="mock-wa", network="whatsapp", text="t", message_id="",
                   now=clock() - 30 * 86400)
    _remember(inbox, "wa-mum", "Birthday is 4 June")
    a = store.account("mock-wa")
    a["paused_until"], a["pause_reason"] = clock() + 600, "Unipile rate limit"
    store.save_account(a)
    assert "Nothing was deleted" in build_command(inbox, ask("cancel"))("reset")
    out = build_command(inbox, ask("device"))("reset")
    assert out.startswith("Deleted.")
    for table in ("chats", "messages", "drafts", "facts", "contacts", "chunks", "notifications", "events"):
        assert store._q(f"SELECT COUNT(*) FROM {table}")[0][0] == 0, table
    assert len(store.recent_sends(0)) == 1  # today's send still counts toward the limits
    assert store.gate("mock-wa").paused_until > clock()  # and the rate-limit pause holds
    assert store.search_messages("plumber") == []
    clock.advance(3601)  # (the hourly Unipile budget was spent above; it is kept on purpose)
    d2 = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "x"}))  # and it all works again
    assert d2["draft_id"]


def test_reset_with_cloud_on_one_device_or_everywhere(clock):
    supa = FakeSupabase()

    def device():
        i = make_sample_inbox(clock, {"supabase_url": "https://abcdefghij.supabase.co"})
        i._get_secret = {"UNIPILE_API_KEY": "k", "SUPABASE_SECRET_KEY": supa.key,
                         "SUPABASE_ACCESS_TOKEN": supa.token}.get
        i.cloud = CloudSync(i, transport=supa)
        return i

    a = device()
    while SyncEngine(a).sync_once()["pending_chats"]:
        pass
    a.cloud.setup()
    a.cloud.sync()
    b = device()
    b.cloud.activate()
    b.cloud.sync()
    assert len(supa.live("messages")) > 13
    out = data_control.wipe(b, everywhere=False)  # only this device
    assert out["supabase"] == "kept" and len(supa.live("messages")) > 13 and not b.cloud.active
    out = data_control.wipe(a, everywhere=True)
    assert out["supabase"] == "deleted" and out["still_syncing"] == 0
    assert supa.live("messages") == {} and supa.live("chats") == {}
    c = device()  # another device that still had everything
    c.cloud.activate()
    c.store().upsert_chats([{"id": "wa-maya", "account_id": "mock-wa", "network": "whatsapp", "ts": 1}], clock())
    c.cloud.pull()
    assert c.store().cached_chat("wa-maya") is None  # the deletion reached it


def test_page_needs_explicit_confirmation(inbox):
    api = InboxAPI(inbox)
    assert api.handle("POST", "/privacy/wipe", {}, {"confirm": "yes"})[0] == 409
    assert api.handle("POST", "/privacy/wipe", {}, {"confirm": "DELETE", "scope": "everywhere"})[0] == 400
    assert api.handle("POST", "/privacy/forget/wa-mum", {}, {})[0] == 409
    assert api.handle("POST", "/privacy/forget/wa-mum", {}, {"confirm": True})[0] == 200
    status, out = api.handle("GET", "/privacy/export")
    assert status == 200 and out["counts"]["chats"] == 13
    assert api.handle("POST", "/privacy/wipe", {}, {"confirm": "DELETE"})[0] == 200
    assert api.handle("GET", "/chats")[1]["chats"] == []


# ─── health check ────────────────────────────────────────────────────────────

def test_doctor_on_a_healthy_inbox(inbox):
    checks = {c["name"]: c for c in health.run(inbox)}
    assert checks["Unipile API key"]["ok"] and checks["WhatsApp"]["ok"] and checks["Database"]["ok"]
    assert "integrity ok" in checks["Database"]["detail"]
    assert checks["Search by meaning"]["ok"] is None  # optional, off
    text = build_command(inbox, ask(None))("doctor")
    assert "ok  WhatsApp: Connected." in text and text.endswith("All good.")


def test_doctor_points_at_fixes(clock):
    bare = make_inbox(clock)
    checks = health.run(bare)
    assert checks[0]["ok"] is False and "Settings" in checks[0]["fix"]
    inbox = make_sample_inbox(clock)
    inbox.backend.list_accounts = lambda: [{"id": "mock-wa", "network": "whatsapp", "status": "DISCONNECTED",
                                           "connected_at": 0}]
    text = health.format_text(health.run(inbox))
    assert "!!  WhatsApp: Status disconnected." in text and "Reconnect" in text and "need attention" in text
    assert InboxAPI(inbox).handle("GET", "/doctor")[1]["checks"]


# ─── speed guards (query plans, not timings) ─────────────────────────────────

@pytest.mark.parametrize("sql, params, index", [
    ("SELECT id FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT 20", ("c",), "messages_chat"),
    ("SELECT id FROM chats WHERE account_id=? ORDER BY ts DESC LIMIT 20", ("a",), "chats_account_ts"),
    ("SELECT id FROM send_log WHERE sent_at>=?", (0,), "send_log_sent_at"),
    ("SELECT id FROM drafts WHERE status=? ORDER BY created_at DESC LIMIT 20", ("pending",), "drafts_status"),
    ("SELECT id FROM loops WHERE status='open' AND kind=?", ("they_wait",), "loops_open"),
    ("SELECT id FROM schedules WHERE status='scheduled' AND send_at<=?", (1,), "schedules_due"),
    ("SELECT id FROM chunks WHERE chat_id=? AND start_ts<=?", ("c", 1), "chunks_chat"),
])
def test_hot_queries_use_their_index(sql, params, index):
    store = Store(sqlite3.connect(":memory:"))
    plan = " ".join(r[-1] for r in store._q("EXPLAIN QUERY PLAN " + sql, params))
    assert index in plan, plan


def test_search_uses_the_full_text_index():
    store = Store(sqlite3.connect(":memory:"))
    plan = " ".join(r[-1] for r in store._q(
        "EXPLAIN QUERY PLAN SELECT m.id FROM messages_fts f JOIN messages m ON m.rid=f.rowid"
        " WHERE messages_fts MATCH ?", ('"kiraya"',)))
    assert "VIRTUAL TABLE INDEX 0:M" in plan  # an FTS5 MATCH lookup, not a scan of the messages


# ─── release consistency ─────────────────────────────────────────────────────

def test_versions_agree_and_changelog_has_this_release():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    version = re.search(r"^version: (\S+)$", manifest, re.M).group(1)
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))["version"] == version
    assert f"## {version}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_every_setting_in_code_is_declared_in_the_manifest():
    from dataclasses import fields

    from unified_inbox.pacing import Settings

    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    declared = set(re.findall(r"^  (\w+):$", manifest, re.M))
    internal = {"duplicate_window_hours", "duplicate_min_chars", "read_page_size", "max_pages", "max_triage_chats",
                "api_calls_per_hour", "min_call_gap_seconds", "max_call_gap_seconds", "cache_ttl_minutes",
                "sync_chats_per_cycle", "initial_sync_days", "memory_chats_per_cycle"}
    missing = {f.name for f in fields(Settings)} - declared - internal
    assert not missing, f"settings without a manifest entry: {missing}"


def test_no_secrets_or_machine_paths_in_the_repo():
    pattern = re.compile(r"sb_secret_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|sbp_[a-f0-9]{20,}|C:/Users/|C:\\\\Users")
    for path in ROOT.rglob("*"):
        if path.name == "test_release.py":
            continue  # this file spells the patterns out
        if path.is_file() and ".git" not in path.parts and ".claude" not in path.parts and path.suffix in (
                ".py", ".js", ".json", ".md", ".yaml", ".css", ".txt"):
            assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path


# ─── the activity log is your data too ───────────────────────────────────────

def _file_inbox(clock, folder):
    """A connected inbox whose database is a real file in ``folder`` (so activity.log exists)."""
    from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend

    conn = sqlite3.connect(str(folder / "data.db"), check_same_thread=False)
    i = make_inbox(clock, {"unipile_account_id": SAMPLE_ACCOUNT_IDS}, secrets={"UNIPILE_API_KEY": "k"},
                   backend=SampleBackend(clock), conn=conn)
    i._local_now = lambda: datetime.fromtimestamp(clock())
    while SyncEngine(i).sync_once()["pending_chats"]:
        pass
    return i


def test_export_and_reset_include_the_activity_log(clock, tmp_path):
    inbox = _file_inbox(clock, tmp_path)
    h = build_handlers(inbox)
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    assert build_command(inbox, ask("send"))(f"send {d['draft_id']}").startswith("Sent")
    assert (tmp_path / "activity.log").exists()
    (tmp_path / "activity.log.1").write_text("an older part\n", encoding="utf-8")
    exported = data_control.export_all(inbox)
    assert [a["text"] for a in exported["tables"]["send_attempts"]] == ["Yes, home at 8!"]
    assert exported["tables"]["send_attempts"][0]["files"] == []  # JSON columns are parsed
    out = build_command(inbox, ask("device"))("reset")
    assert out.startswith("Deleted.")
    assert inbox.store().attempts() == []  # who you wrote to, and what, is gone
    assert not (tmp_path / "activity.log").exists() and not (tmp_path / "activity.log.1").exists()
    assert len(inbox.store().recent_sends(0)) == 1  # the limits still count the send


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_files_holding_messages_are_readable_by_you_alone(clock, tmp_path):
    import os
    import stat

    inbox = _file_inbox(clock, tmp_path)
    log = tmp_path / "activity.log"
    log.write_text("from an older version\n", encoding="utf-8")
    os.chmod(log, 0o644)
    h = build_handlers(inbox)
    d = json.loads(h["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    assert build_command(inbox, ask("send"))(f"send {d['draft_id']}").startswith("Sent")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    export = data_control.export_to_file(inbox)
    assert stat.S_IMODE(export.stat().st_mode) == 0o600


def test_activity_file_starts_a_new_part_when_large(clock, tmp_path, monkeypatch):
    from unified_inbox import inbox as inbox_mod

    inbox = _file_inbox(clock, tmp_path)
    monkeypatch.setattr(inbox_mod, "ACTIVITY_FILE_BYTES", 100)
    (tmp_path / "activity.log").write_text("x" * 150 + "\n", encoding="utf-8")
    inbox._activity_file("SENT    test")
    assert (tmp_path / "activity.log.1").read_text(encoding="utf-8").startswith("x" * 150)
    assert (tmp_path / "activity.log").read_text(encoding="utf-8").endswith("SENT    test\n")
