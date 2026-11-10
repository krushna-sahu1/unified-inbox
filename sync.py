"""Background sync: keeps the local cache current and logs what changed as events.

One cycle (``SyncEngine.sync_once``):
  1. refresh the account list (reused for ``cache_ttl_minutes``);
  2. per account, page through chats newest first and stop at the first chat the cache already
     has up to date (activity order means every older chat is unchanged too). This also
     refreshes unread counts and previews of the most recent page;
  3. fetch only the new messages of changed chats (``after`` the newest stored message), at
     most ``sync_chats_per_cycle`` chats per account per cycle, most recent first. The rest wait
     for the next cycle.

The first cycle of an account records a *baseline* time. Messages older than it are history
and never produce "new message" events, so connecting an account does not flood anyone with
old messages. Chats quiet for longer than ``initial_sync_days`` are not backfilled at all;
they are read on demand like before.

Every request goes through ``Inbox._call``: the read budget, random gaps, backoff and account
pauses apply exactly as for tools. The sync uses at most ``sync_budget_percent`` of the hourly
budget, so tools always keep room. Only one process runs the worker (a lease in the database).
"""

from __future__ import annotations

import contextvars
import threading
import uuid
from typing import Optional

from .client import InboxError
from .pacing import DAY, HOUR, NETWORK_LABELS

LEASE = "sync"
SETTLE_WAKE = 150.0       # seconds: check a settling burst again soon (auto-drafts)
OVERLAP_SECONDS = 1.0     # "after" is exclusive and timestamps can tie; message ids dedupe the overlap
CATCHUP_PAGES = 3          # pages of new messages per chat per cycle
MAX_IDLE_DELAY = 900.0     # an idle inbox is still checked at least every 15 minutes
WEBHOOK_RECONCILE = 900.0  # with live webhooks, polling only reconciles
WEBHOOK_HEALTHY_FOR = 2 * HOUR
ERROR_DELAY = 300.0


class _BudgetStop(Exception):
    """The sync's share of the hourly budget is used up; finish the cycle early."""


class _Stopped(Exception):
    """The plugin is being unloaded: end the cycle at the next step instead of finishing it."""


class SyncEngine:
    def __init__(self, inbox):
        self.inbox = inbox
        self._idle_streak = 0
        self._lock = threading.Lock()  # one cycle at a time (worker and /inbox sync)
        # Set by the worker: True once the plugin is unloading, so a cycle in progress ends at its
        # next request instead of running on (and reopening the database) after the unload.
        self.should_stop = lambda: False

    # ── budget ──
    def _cap(self) -> int:
        s = self.inbox.settings
        return max(1, s.api_calls_per_hour * s.sync_budget_percent // 100)

    def _check_budget(self) -> None:
        inbox = self.inbox
        if self.should_stop():
            raise _Stopped()
        if inbox.store().api_calls_last_hour(inbox.clock()) >= self._cap():
            raise _BudgetStop()

    # ── one cycle ──
    def sync_once(self) -> dict:
        with self._lock:
            return self._sync_once()

    def _sync_once(self) -> dict:
        inbox = self.inbox
        summary: dict = {"accounts": 0, "chats_checked": 0, "chats_synced": 0, "new_messages": 0,
                         "pending_chats": 0, "problems": [], "budget_limited": False}
        if not inbox.configured:
            summary["skipped"] = "setup"
            return summary
        store = inbox.store()
        try:
            self._check_budget()
            for acc in inbox.refresh_accounts():
                summary["accounts"] += 1
                try:
                    self._sync_account(acc, summary)
                except InboxError as exc:
                    if exc.kind in ("setup", "bad_api_key", "budget"):
                        raise
                    summary["problems"].append(f"{NETWORK_LABELS.get(acc['network'], acc['network'])}: {exc}")
                summary["pending_chats"] += len(store.chats_needing_sync(acc["id"], 1000))
        except _Stopped:
            summary["stopped"] = True
            return summary
        except _BudgetStop:
            summary["budget_limited"] = True
        except InboxError as exc:
            if exc.kind == "budget":
                summary["budget_limited"] = True
            else:
                self._save_state(error=str(exc), summary=summary)
                raise
        self._save_state(error="; ".join(summary["problems"]), summary=summary)
        return summary

    def _save_state(self, *, error: str, summary: dict) -> None:
        store, now = self.inbox.store(), self.inbox.clock()
        prev = store.get_meta("sync_state", {}) or {}
        state = dict(prev, last_run_at=now, last_error=error,
                     last_summary={k: v for k, v in summary.items() if k != "problems"})
        if not error or summary.get("accounts", 0) > len(summary.get("problems", [])):
            state["last_ok_at"] = now
        store.set_meta("sync_state", state)

    def _sync_account(self, acc: dict, summary: dict) -> None:
        inbox, store, s = self.inbox, self.inbox.store(), self.inbox.settings
        aid = acc["id"]
        now = inbox.clock()
        if store.gate(aid).paused_until > now:
            return  # the pause reason already shows in status; nothing to do until it ends
        first = inbox.baseline(aid) is None
        if first:
            store.set_meta(f"baseline:{aid}", now)

        # 1. chats, newest first, down to the activity time the previous cycle had already seen.
        # (The sync keeps its own mark: tool reads also update chats, and must not make the sync
        # stop early and miss a changed chat further down.)
        seen_until = float(store.get_meta(f"list_ts:{aid}", 0.0))
        fetched: list[dict] = []
        cursor: Optional[str] = None
        for _ in range(s.max_pages):
            self._check_budget()
            chats, cursor = inbox._call(aid, lambda c=cursor: inbox.client().list_chats(
                aid, limit=s.read_page_size, cursor=c))
            store.upsert_chats(chats, inbox.clock())
            fetched.extend(chats)
            if not cursor or not chats or chats[-1]["ts"] <= seen_until:
                break
        if fetched:
            store.set_meta(f"list_ts:{aid}", max(seen_until, max(c["ts"] for c in fetched)))
        summary["chats_checked"] += len(fetched)
        if fetched:  # the same record a tool read leaves: lets tools reuse this list while fresh
            store.set_meta(f"chats_at:{aid}", {"at": inbox.clock(), "ids": [c["id"] for c in fetched],
                                               "unread_only": False, "complete": not cursor})
        if first:
            quiet_before = now - s.initial_sync_days * DAY
            for c in fetched:
                if c["ts"] < quiet_before:
                    store.mark_chat_synced(c["id"], c["ts"])

        # 2. new messages of changed chats
        for chat in store.chats_needing_sync(aid, s.sync_chats_per_cycle):
            self._check_budget()
            if chat["needs_refresh"]:
                fresh = inbox._call(aid, lambda cid=chat["id"]: inbox.client().get_chat(aid, cid))
                if fresh:
                    store.upsert_chats([fresh], inbox.clock())
                    chat = dict(chat, **fresh)
                self._check_budget()
            summary["new_messages"] += self._sync_chat(chat)
            summary["chats_synced"] += 1

    def _sync_chat(self, chat: dict) -> int:
        inbox, store, s = self.inbox, self.inbox.store(), self.inbox.settings
        aid, cid = chat["account_id"], chat["id"]
        newest = store.newest_message_ts(cid)
        # Catch up from the sync's own mark, not from the newest stored message: a webhook or a
        # tool read may have stored a newer message while older new ones are still missing.
        synced = float(chat.get("synced_ts") or 0.0)
        after = synced - OVERLAP_SECONDS if synced > 0 else None
        got: list[dict] = []
        cursor: Optional[str] = None
        for page in range(CATCHUP_PAGES if after else 1):
            if page:
                self._check_budget()
            msgs, cursor = inbox._call(aid, lambda c=cursor: inbox.client().list_messages_page(
                aid, cid, limit=s.read_page_size, after=after, cursor=c))
            got.extend(msgs)
            if not cursor:
                break
        for m in got:
            if not m["from_me"] and not m.get("sender"):
                m["sender"] = chat.get("name") or ""
        res = store.upsert_messages(got, inbox.clock(), account_id=aid, baseline=inbox.baseline(aid))
        kept = [m for m in got if not m.get("deleted")]
        if kept:
            store.set_chat_preview(max(kept, key=lambda m: m["ts"]))
        store.mark_chat_synced(cid, max([chat["ts"], newest, *(m["ts"] for m in kept)]))
        # A full latest page, or an unbroken catch-up on top of an earlier full read, means the
        # cache now holds the chat's latest messages: tools may reuse them while fresh.
        if after is None:
            store.set_messages_fetched(cid, inbox.clock(), s.read_page_size)
        elif not cursor and chat.get("msgs_fetched_n"):
            store.set_messages_fetched(cid, inbox.clock(), chat["msgs_fetched_n"])
        baseline = inbox.baseline(aid) or 0.0
        by_id = {m["id"]: m for m in got}
        return sum(1 for mid in res["new"] if not by_id[mid]["from_me"] and by_id[mid]["ts"] > baseline)

    # ── pacing between cycles ──
    def next_delay(self, summary: dict) -> float:
        """Seconds until the next cycle. Fast while things are happening, slower while idle,
        never faster than the budget allows, and slow when webhooks already deliver live."""
        s = self.inbox.settings
        if summary.get("skipped"):
            return 60.0
        accounts = max(1, summary.get("accounts", 1))
        # Chat-list checks may use at most half of the sync's share; the rest is for messages.
        floor = accounts * 3600.0 / max(1.0, self._cap() / 2)
        base = max(float(s.poll_seconds), floor)
        if self._webhook_live():
            base = max(base, WEBHOOK_RECONCILE)
        if summary.get("budget_limited"):
            self._idle_streak = 0
            return max(base, 600.0)
        if summary.get("new_messages") or summary.get("pending_chats"):
            self._idle_streak = 0
            return base
        self._idle_streak += 1
        return min(base * (1.5 ** self._idle_streak), max(base, MAX_IDLE_DELAY))

    def _webhook_live(self) -> bool:
        store, now = self.inbox.store(), self.inbox.clock()
        if not (store.get_meta("webhook") or {}).get("id"):
            return False
        last = store.get_meta("webhook_last_event_at")
        return bool(last) and now - float(last) < WEBHOOK_HEALTHY_FOR


class SyncWorker:
    """Runs SyncEngine cycles on a daemon thread. Safe to start in every process: only the
    holder of the database lease syncs; the others idle and take over if it stops."""

    def __init__(self, inbox, engine: Optional[SyncEngine] = None, owner: Optional[str] = None):
        self.inbox = inbox
        self.engine = engine or SyncEngine(inbox)
        self.owner = owner or uuid.uuid4().hex
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.engine.should_stop = self._stop.is_set

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Run in a copy of the starting context: Hermes's ctx.llm reads the profile scope from it.
        ctx = contextvars.copy_context()
        self._thread = threading.Thread(target=ctx.run, args=(self._run,), name="unified-inbox-sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        try:
            if self.inbox.configured:
                self.inbox.store().release_lease(LEASE, self.owner)
        except Exception:  # noqa: BLE001 - shutting down; nothing useful to report
            pass

    def wake(self) -> None:
        """Run the next cycle now (e.g. a webhook introduced an unknown chat)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            delay = self.run_once()
            self._wake.wait(delay)
            self._wake.clear()

    def _auto_draft(self) -> bool:
        """Draft replies for chats that just became urgent. True while some burst is still settling.
        Its own failures never stop the sync."""
        drafter = getattr(self.inbox, "autodraft", None)
        if drafter is None:
            return False
        try:
            drafter.run_once()
            return bool(self.inbox.store().get_meta("autodraft_pending", {}))
        except Exception as exc:  # noqa: BLE001
            try:
                self.inbox.store().set_meta("autodraft_state", {"at": self.inbox.clock(),
                                                                "problems": [str(exc) or type(exc).__name__]})
            except Exception:  # noqa: BLE001
                pass
            return False

    def _insights(self) -> None:
        """Scam alerts, plans, translations and the people radar for what just arrived. Each one's
        failure is recorded and never stops the others or the sync."""
        inbox = self.inbox
        for name, method in (("shield", "scan_new"), ("planner", "scan_new"), ("translator", "run_auto"),
                             ("radar", "run_alerts")):
            part = getattr(inbox, name, None)
            if part is None:
                continue
            error = ""
            try:
                getattr(part, method)()
            except Exception as exc:  # noqa: BLE001
                error = str(exc) or type(exc).__name__
            try:
                problems = dict(inbox.store().get_meta("insights_state", {}) or {})
                if error or name in problems:  # a later success clears the problem
                    if error:
                        problems[name] = {"at": inbox.clock(), "error": error}
                    else:
                        problems.pop(name, None)
                    inbox.store().set_meta("insights_state", problems)
            except Exception:  # noqa: BLE001
                pass

    def _process_context(self) -> bool:
        """Update summaries, facts, loops and embeddings from the fresh cache. True if more is
        waiting. Its own failures never stop the sync."""
        context = getattr(self.inbox, "context", None)
        if context is None:
            return False
        try:
            return bool(context.process_once().get("backlog"))
        except Exception as exc:  # noqa: BLE001
            try:
                self.inbox.store().set_meta("context_state", {"last_run_at": self.inbox.clock(),
                                                              "problems": [str(exc) or type(exc).__name__]})
            except Exception:  # noqa: BLE001
                pass
            return False

    def _cloud_step(self) -> tuple:
        """With cloud sync: share changes, and find out whether this device leads. If Supabase
        can't be reached, keep the last role (sends stay blocked until it is back)."""
        cloud = getattr(self.inbox, "cloud", None)
        if cloud is None:
            return False, "solo"
        try:
            if not cloud.ensure():
                return False, "solo"
            cloud.sync()
            return True, cloud.worker_role()
        except Exception as exc:  # noqa: BLE001
            try:
                cloud._state(error=str(exc))
            except Exception:  # noqa: BLE001
                pass
            return False, cloud.last_role() if cloud.configured else "solo"

    def _run_automation(self, delay: float) -> float:
        """Scheduled sends, watches and alerts; wake up in time for the next one that is due.
        Runs even with the sync off: a message the user scheduled must still go out."""
        automation = getattr(self.inbox, "automation", None)
        if automation is None:
            return delay
        try:
            automation.tick()
            due = automation.next_due_in()
        except Exception as exc:  # noqa: BLE001 - recorded; the worker keeps going
            try:
                self.inbox.store().set_meta("automation_error", str(exc) or type(exc).__name__)
            except Exception:  # noqa: BLE001
                pass
            return min(delay, 60.0)
        if due is not None:
            delay = min(delay, max(15.0, due + 1.0))
        return delay

    def run_once(self) -> float:
        """One worker step; returns seconds to wait. Never raises: the thread must not die."""
        inbox = self.inbox
        try:
            if not inbox.configured:
                return 60.0
            store = inbox.store()
            if not store.acquire_lease(LEASE, self.owner, inbox.clock(), 600.0):
                return 60.0  # another Hermes process is running the worker
            cloud_up, role = self._cloud_step()
            if role == "follower":
                return 30.0  # another device runs the background work; this one mirrors it
            if inbox.settings.sync_enabled:
                summary = self.engine.sync_once()
                if self._stop.is_set():
                    return 0.0  # unloading: no memory work, drafts or scheduled sends after this point
                delay = self.engine.next_delay(summary)
                if self._process_context() and delay > 120.0:
                    delay = 120.0  # local memory work is waiting; it costs no Unipile calls
                self._insights()
                if self._auto_draft() and delay > SETTLE_WAKE:
                    delay = SETTLE_WAKE  # a burst is settling: look again soon to draft for it
            else:
                delay = 60.0
            if self._stop.is_set():
                return 0.0
            delay = self._run_automation(delay)
            if cloud_up:
                delay = min(delay, 60.0)
                try:
                    inbox.cloud.push()
                    inbox.cloud.daily_cleanup()
                except Exception as exc:  # noqa: BLE001 - retried next step
                    inbox.cloud._state(error=str(exc))
        except Exception as exc:  # noqa: BLE001 - recorded in status, retried later
            delay = ERROR_DELAY
            try:
                state = inbox.store().get_meta("sync_state", {}) or {}
                state.update(last_error=str(exc) or type(exc).__name__, last_run_at=inbox.clock())
                inbox.store().set_meta("sync_state", state)
            except Exception:  # noqa: BLE001
                pass
        if self._stop.is_set():
            return 0.0  # stop() releases the lease; the database is not touched again
        try:
            now = inbox.clock()
            store = inbox.store()
            store.acquire_lease(LEASE, self.owner, now, delay + 300.0)
            state = store.get_meta("sync_state", {}) or {}
            state["next_run_at"] = now + delay
            store.set_meta("sync_state", state)
        except Exception:  # noqa: BLE001
            pass
        return delay
