"""Background sync: incremental reads, events, budget share, pacing and the worker lease."""

import json
import sqlite3

from conftest import KEY, make_inbox, make_sample_inbox
from unified_inbox.commands import build_command
from unified_inbox.sync import LEASE, MAX_IDLE_DELAY, WEBHOOK_RECONCILE, SyncEngine, SyncWorker
from unified_inbox.tools import build_handlers


def _events(inbox, type_="message.new"):
    return inbox.store().events_after(0, types=[type_])


def test_first_sync_stores_history_without_announcing_it(clock):
    inbox = make_sample_inbox(clock, {"initial_sync_days": 7})
    engine = SyncEngine(inbox)
    r = engine.sync_once()
    assert r["accounts"] == 3 and r["chats_checked"] == 13 and r["new_messages"] == 0
    assert _events(inbox) == []  # old messages are history, not news
    assert json.loads(build_handlers(inbox)["inbox_updates"]({}))["chats"] == []
    store = inbox.store()
    assert store.cached_chat("wa-old")["synced_ts"] == store.cached_chat("wa-old")["ts"]  # 12 days quiet
    assert store.cached_messages("wa-old", 10) == []  # ...so not backfilled
    assert store.cached_messages("wa-maya", 10)  # recent chats are
    assert r["chats_synced"] + r["pending_chats"] == 12


def test_next_cycle_reads_only_what_changed(clock):
    inbox = make_sample_inbox(clock)
    engine = SyncEngine(inbox)
    while engine.sync_once()["pending_chats"]:
        pass
    backend = inbox.backend
    clock.advance(300)
    backend.receive("wa-maya", "Plumber confirmed for 8am!")
    backend.calls.clear()
    backend.calls_detail.clear()
    r = engine.sync_once()
    assert r["new_messages"] == 1 and r["chats_synced"] == 1
    assert backend.calls.count("chats") == 3  # one page per account
    assert backend.calls_detail == [("messages", "wa-maya", backend.calls_detail[0][2])]
    assert backend.calls_detail[0][2] is not None  # only messages after the newest stored one
    ev = _events(inbox)
    assert len(ev) == 1 and ev[0]["chat_id"] == "wa-maya" and ev[0]["data"]["text"] == "Plumber confirmed for 8am!"
    store = inbox.store()
    assert store.cached_chat("wa-maya")["last_text"] == "Plumber confirmed for 8am!"
    assert store.search_messages("confirmed for 8")  # searchable right away

    h = build_handlers(inbox)
    up = json.loads(h["inbox_updates"]({}))
    assert [(c["name"], c["count"]) for c in up["chats"]] == [("Maya Rivers", 1)]
    assert up["chats"][0]["messages"][0]["text"] == "Plumber confirmed for 8am!"
    assert json.loads(h["inbox_updates"]({}))["chats"] == []  # already reported

    backend.calls.clear()
    clock.advance(300)
    assert engine.sync_once()["chats_synced"] == 0
    assert backend.calls.count("messages") == 0  # nothing changed: chat lists only


def test_my_own_messages_are_not_news(clock):
    inbox = make_sample_inbox(clock)
    engine = SyncEngine(inbox)
    engine.sync_once()
    clock.advance(60)
    inbox.backend.receive("wa-mum", "On my way!", from_me=True)
    assert engine.sync_once()["new_messages"] == 0
    assert json.loads(build_handlers(inbox)["inbox_updates"]({}))["chats"] == []


def test_a_tool_read_and_the_sync_report_a_message_once(clock):
    inbox = make_sample_inbox(clock)
    engine = SyncEngine(inbox)
    while engine.sync_once()["pending_chats"]:
        pass
    clock.advance(120)
    inbox.backend.receive("li-priya", "Are you free Thursday?")
    h = build_handlers(inbox)
    h["inbox_list_chats"]({})  # a tool sees the change first
    h["inbox_read_chat"]({"chat_id": "li-priya"})
    engine.sync_once()
    assert len(_events(inbox)) == 1


def test_more_changed_chats_than_a_page_are_all_found(clock):
    inbox = make_sample_inbox(clock, {"read_page_size": 2, "sync_chats_per_cycle": 50})
    engine = SyncEngine(inbox)
    while engine.sync_once()["pending_chats"]:
        pass
    clock.advance(120)
    for chat in ("wa-maya", "wa-dev", "wa-mum", "wa-old"):
        inbox.backend.receive(chat, f"news for {chat}")
    inbox.list_chats(network="whatsapp", limit=2)  # a tool read of the top page in between
    engine.sync_once()
    assert {e["chat_id"] for e in _events(inbox)} == {"wa-maya", "wa-dev", "wa-mum", "wa-old"}


def test_deleted_message_leaves_the_cache(clock):
    inbox = make_sample_inbox(clock)
    engine = SyncEngine(inbox)
    while engine.sync_once()["pending_chats"]:
        pass
    clock.advance(60)
    m = inbox.backend.receive("ig-lena", "oops wrong chat")
    engine.sync_once()
    assert inbox.store().get_message(m["id"])
    clock.advance(60)
    inbox.backend.delete(m["id"])
    inbox.backend.receive("ig-lena", "sorry, ignore that")
    engine.sync_once()
    assert inbox.store().get_message(m["id"]) is None
    assert [e["message_id"] for e in _events(inbox, "message.delete")] == [m["id"]]


def test_sync_keeps_its_budget_share(clock):
    inbox = make_sample_inbox(clock, {"api_calls_per_hour": 20, "sync_budget_percent": 50})
    engine = SyncEngine(inbox)
    r = engine.sync_once()
    assert r["budget_limited"] is True
    assert inbox.store().api_calls_last_hour(clock()) <= 10  # half of 20, the rest is for tools
    assert inbox.list_chats(network="linkedin")  # tools still work
    clock.advance(3601)
    engine.sync_once()  # carries on where it stopped
    assert inbox.store().cached_messages("wa-maya", 5)


def test_paused_accounts_are_skipped(clock):
    inbox = make_sample_inbox(clock)
    inbox.refresh_accounts()
    a = inbox.store().account("mock-ig")
    a["paused_until"], a["pause_reason"] = clock() + 600, "Unipile rate limit"
    inbox.store().save_account(a)
    SyncEngine(inbox).sync_once()
    assert not inbox.store().cached_chats(network="instagram")


def test_unconfigured_sync_does_nothing(clock):
    inbox = make_inbox(clock)
    assert SyncEngine(inbox).sync_once() == {
        "accounts": 0, "chats_checked": 0, "chats_synced": 0, "new_messages": 0, "pending_chats": 0,
        "problems": [], "budget_limited": False, "skipped": "setup"}


def test_delay_backs_off_when_idle_and_respects_the_budget(clock):
    inbox = make_sample_inbox(clock)
    engine = SyncEngine(inbox)
    busy = {"accounts": 3, "new_messages": 2, "pending_chats": 0}
    idle = {"accounts": 3, "new_messages": 0, "pending_chats": 0}
    # 3 accounts, 120 calls/h, 50% for sync, half of that for chat lists: at least every 6 minutes
    assert engine.next_delay(busy) == 360
    delays = [engine.next_delay(idle) for _ in range(6)]
    assert delays == sorted(delays) and delays[-1] == MAX_IDLE_DELAY
    assert engine.next_delay(busy) == 360  # activity resets the back-off
    one = SyncEngine(make_sample_inbox(clock, {"poll_seconds": 60}))
    assert one.next_delay({"accounts": 1, "new_messages": 1}) == 120  # budget floor beats poll_seconds
    assert engine.next_delay({"accounts": 3, "budget_limited": True}) == 600


def test_live_webhooks_slow_polling_to_reconciliation(clock):
    inbox = make_sample_inbox(clock)
    store = inbox.store()
    store.set_meta("webhook", {"id": "we_1", "url": "https://x/unipile/webhook", "secret": "s"})
    engine = SyncEngine(inbox)
    assert engine.next_delay({"accounts": 1, "new_messages": 1}) == 120  # no event yet: keep polling
    store.set_meta("webhook_last_event_at", clock())
    assert engine.next_delay({"accounts": 1, "new_messages": 1}) == WEBHOOK_RECONCILE


def test_only_one_worker_syncs_across_processes(clock):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    a = make_sample_inbox(clock, conn=conn)
    b = make_sample_inbox(clock, conn=conn)
    wa, wb = SyncWorker(a, owner="process-a"), SyncWorker(b, owner="process-b")
    assert wa.run_once() > 60
    assert wb.run_once() == 60 and b.backend.calls == []  # a holds the lease
    clock.advance(wa.engine.next_delay({"accounts": 3}) + 301)  # a stopped renewing: lease expired
    assert wb.run_once() > 60 and b.backend.calls
    assert a.store().acquire_lease(LEASE, "process-a", clock(), 10) is False


def test_worker_never_dies_and_records_the_error(clock):
    inbox = make_sample_inbox(clock)

    class Broken(SyncEngine):
        def sync_once(self):
            raise RuntimeError("boom")

    worker = SyncWorker(inbox, engine=Broken(inbox))
    assert worker.run_once() == 300
    assert inbox.sync_status()["last_error"] == "boom"


def test_worker_thread_starts_and_stops(clock):
    worker = SyncWorker(make_inbox(clock))  # not connected: idles
    worker.start()
    worker.stop(timeout=2)
    assert not worker._thread.is_alive()


def test_sync_and_status_commands(clock):
    inbox = make_sample_inbox(clock)
    cmd = build_command(inbox, lambda q, c: None, {})
    assert cmd("sync").startswith("Checked 3 account(s)")
    out = cmd("status")
    assert "Background sync: last checked 0 min ago" in out
    assert "No webhook is set up" in cmd("webhook")
    assert "not connected" in build_command(make_inbox(clock), lambda q, c: None)("sync")
    assert json.loads(build_handlers(inbox)["inbox_status"]({}))["sync"]["enabled"] is True


def test_sync_can_be_turned_off(clock):
    inbox = make_sample_inbox(clock, {"sync_enabled": "false"})
    assert inbox.settings.sync_enabled is False
    assert SyncWorker(inbox).run_once() == 60 and inbox.backend.calls == []
    assert make_inbox(clock, {"sync_enabled": True}, secrets=KEY).settings.sync_enabled is True


def test_messages_between_the_sync_mark_and_a_webhook_message_are_not_skipped(clock):
    inbox = make_sample_inbox(clock)
    engine = SyncEngine(inbox)
    while engine.sync_once()["pending_chats"]:
        pass
    clock.advance(60)
    first = inbox.backend.receive("wa-dev", "first new one")
    clock.advance(60)
    second = inbox.backend.receive("wa-dev", "second new one")
    # only the second arrives live (e.g. by webhook) before the sync runs
    inbox.store().upsert_messages([second], clock(), account_id="mock-wa", baseline=inbox.baseline("mock-wa"))
    engine.sync_once()
    assert inbox.store().get_message(first["id"]) is not None
