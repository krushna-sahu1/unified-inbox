"""The inbox runtime: wires the Unipile client, the local store and the pacing layer.

Every backend call goes through ``_call`` (setup check, read budget, random gaps, backoff and
account pauses). There is no sample mode: until a Unipile API key and account ID are set,
every call fails with a clear setup message.
"""

from __future__ import annotations

import hashlib
import math
import mimetypes
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import triage as triage_mod
from .client import MAX_DOWNLOAD_BYTES, InboxError, UnipileClient, describe_attachment
from .automation import Automation
from .cloud import CloudSync
from .autodraft import AutoDrafter, blanks
from .context import ContextEngine
from .identity import Identity
from .style import Style
from .shield import Shield
from .plans import Planner
from .calfeed import CalendarFeed
from .translate import Translator
from .radar import Radar
from .voice import Voice
from .pacing import (DAY, HOUR, NETWORK_LABELS, Settings, backoff_seconds, call_gap_seconds, check_send,
                     in_quiet_hours, load_settings, send_gap_seconds, starts_conversation)
from .store import Store

# Tools reuse chats and messages read this recently instead of asking Unipile again.
FRESH_SECONDS = 30.0
# activity.log starts a new part past this size (the previous part is kept as activity.log.1).
ACTIVITY_FILE_BYTES = 1_000_000
# Reactions are visible to the other person: kept well below what the apps tolerate.
REACTIONS_PER_HOUR = 6
REACTIONS_PER_DAY = 30
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv")
TEXT_PREVIEW_CHARS = 20_000
_NO_SPEECH = "(no speech recognised)"

SETUP_MESSAGE = ("unified-inbox is not connected yet. Add your Unipile API key in Settings -> Plugins ->"
                 " unified-inbox, then connect your apps from the Inbox page (or the Unipile dashboard).")

# Unipile v2 error types (the part after "api/" or "provider/").
_DISCONNECTED = {"invalid_authorization"}               # the connected account needs re-authentication
_BAD_KEY = {"missing_authorization", "expired_authorization"}  # the API key itself
_RESTRICTED = {"account_restricted"}

# Failures where the message certainly did not leave: the draft stays pending so it can be
# sent once the cause is fixed. (Our own kinds, plus provider answers that refuse the request.)
_KEEP_DRAFT_KINDS = {"rate_limited", "paused", "budget", "setup", "bad_api_key"} | _DISCONNECTED | _RESTRICTED
# Errors that stop a whole read, rather than one account's part of it.
_FATAL_READ_KINDS = ("setup", "bad_api_key", "budget")


class Inbox:
    def __init__(
        self,
        *,
        get_config: Callable[[str, Any], Any],
        get_secret: Callable[[str], Optional[str]],
        open_db: Callable[[], Any],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        local_now: Callable[[], datetime] = datetime.now,
        client_factory: Callable[[str], Any] = UnipileClient,
        llm: Optional[Callable[[str, str, dict], Optional[dict]]] = None,
        embed_post: Optional[Callable] = None,
        cloud_transport: Optional[Callable] = None,
        transcriber: Optional[Callable[[str], dict]] = None,
        speaker: Optional[Callable[[str, str], dict]] = None,
        calendar_fetch: Optional[Callable[[str], bytes]] = None,
    ):
        self._get_config = get_config
        self._get_secret = get_secret
        self._open_db = open_db
        self.clock = clock
        self._sleep = sleep
        self._rand = rand
        self._local_now = local_now
        self._client_factory = client_factory
        # Speech-to-text for voice notes: (audio file path) -> {"success", "transcript", "error"}.
        self.transcriber = transcriber
        self._store: Optional[Store] = None
        self._client: Any = None
        self._client_key = ""
        self._init_lock = threading.Lock()
        # Serialises sends only. Reads and drafting never wait on a send in progress.
        self._send_lock = threading.Lock()
        # One Unipile request at a time across threads (tools, background sync, sends), so the
        # random gap between requests holds no matter who makes them.
        self._net_lock = threading.RLock()
        # Summaries, facts, open loops and search by meaning (see context.py).
        self.context = ContextEngine(self, llm=llm, post=embed_post)
        # Scheduled messages, watches, follow-ups, snoozes and alerts (see automation.py).
        self.automation = Automation(self)
        # Optional sync with the user's own Supabase (see cloud.py).
        self.cloud = CloudSync(self, transport=cloud_transport)
        # The same person across apps (identity.py), how you write (style.py), and drafts
        # prepared by themselves for urgent chats (autodraft.py).
        self.identity = Identity(self)
        self.style = Style(self)
        self.autodraft = AutoDrafter(self)
        # Scam warnings (shield.py), plans into your calendar (plans.py, calfeed.py), translation
        # (translate.py), the people radar (radar.py) and talking to your inbox (voice.py).
        self.shield = Shield(self)
        self.calendar = CalendarFeed(self, fetch=calendar_fetch)
        self.planner = Planner(self, feed=self.calendar)
        self.translator = Translator(self)
        self.radar = Radar(self)
        self.voice = Voice(self, speaker=speaker)

    # ── wiring ───────────────────────────────────────────────────────────────

    @property
    def settings(self) -> Settings:
        return load_settings(self._get_config)

    def _api_key(self) -> str:
        return (self._get_secret("UNIPILE_API_KEY") or "").strip()

    def account_ids(self) -> list[str]:
        """Optional filter: the Unipile account IDs to use (``acc_...``), from the
        ``unipile_account_id`` setting. Empty means every WhatsApp, Instagram and LinkedIn account
        connected to the Unipile application."""
        raw = str(self._get_config("unipile_account_id", "") or "")
        return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]

    @property
    def configured(self) -> bool:
        return bool(self._api_key())

    def uses_account(self, account_id: str) -> bool:
        wanted = self.account_ids()
        return account_id in wanted if wanted else bool(account_id)

    def store(self) -> Store:
        with self._init_lock:
            if self._store is None:
                self._store = Store(self._open_db())
            return self._store

    def close(self) -> None:
        """Close the database (when the plugin is unloaded). Opened again if used afterwards."""
        with self._init_lock:
            store, self._store = self._store, None
        if store is not None:
            store.close()

    def client(self):
        api_key = self._api_key()
        if not api_key:
            raise InboxError(SETUP_MESSAGE, kind="setup")
        with self._init_lock:
            if self._client is None or self._client_key != api_key:  # a new key takes effect at once
                self._client = self._client_factory(api_key)
                self._client_key = api_key
            return self._client

    # ── the guarded call ─────────────────────────────────────────────────────

    def _call(self, account_id: str, fn: Callable[[], Any], *, is_send: bool = False) -> Any:
        self.client()  # setup check first: an unconfigured plugin never spends budget or waits
        with self._net_lock:
            return self._call_locked(account_id, fn, is_send=is_send)

    def _call_locked(self, account_id: str, fn: Callable[[], Any], *, is_send: bool) -> Any:
        store, s = self.store(), self.settings
        now = self.clock()
        gate = store.gate(account_id)
        if gate.paused_until > now:
            raise InboxError(f"Waiting before contacting Unipile again: {gate.pause_reason}. Try again in"
                             f" {self._wait_text(gate.paused_until - now)}.", kind="paused")
        if store.api_calls_last_hour(now) >= s.api_calls_per_hour:
            raise InboxError(f"Read budget reached ({s.api_calls_per_hour} Unipile calls per hour)."
                             " Try again later.", kind="budget")
        last = float(store.get_meta("last_call_at", 0.0))
        wait = last + call_gap_seconds(s, self._rand) - now
        if wait > 0:
            self._sleep(min(wait, 60.0))
        attempts = 0
        while True:
            store.record_api_call(self.clock())
            store.set_meta("last_call_at", self.clock())
            try:
                result = fn()
            except InboxError as exc:
                # A 429 is never retried here: the account pauses and the caller gets a clear message.
                retryable = (exc.status >= 500 or exc.kind == "network") and not is_send
                if retryable and attempts < 2:
                    attempts += 1
                    self._sleep((5.0 if attempts == 1 else 15.0) * (0.8 + 0.4 * self._rand()))
                    continue
                self._on_error(account_id, exc)
                if exc.status == 429:
                    wait = store.gate(account_id).paused_until - self.clock()
                    what = "This message was NOT sent. " if is_send else ""
                    raise InboxError(
                        f"Unipile is rate-limiting requests{self._for_account(account_id)}. {what}Nothing was"
                        f" retried; unified-inbox will wait {self._wait_text(wait)} before contacting"
                        " Unipile for it again. Try again after that.",
                        status=429, kind="rate_limited", retry_after=exc.retry_after) from None
                if exc.status == 401 and exc.kind in _BAD_KEY:
                    raise InboxError("Unipile rejected the API key. Check the Unipile API key in Settings ->"
                                     " Plugins -> unified-inbox.", status=401, kind="bad_api_key") from None
                raise
            a = store.account(account_id)
            if a["consecutive_429"]:
                a["consecutive_429"] = 0
                store.save_account(a)
            return result

    def _wait_text(self, seconds: float) -> str:
        seconds = max(1.0, seconds)
        until = (self._local_now() + timedelta(seconds=seconds)).strftime("%H:%M")
        if seconds < 120:
            return f"{math.ceil(seconds)} seconds (until {until})"
        return f"{math.ceil(seconds / 60)} minutes (until {until})"

    def _for_account(self, account_id: str) -> str:
        if account_id == "*":
            return ""
        network = self.store().account(account_id)["network"]
        return f" for your {NETWORK_LABELS.get(network, network)} account" if network else f" for {account_id}"

    def _on_error(self, account_id: str, exc: InboxError) -> None:
        store, now = self.store(), self.clock()
        a = store.account(account_id)
        if exc.status == 429:
            a["consecutive_429"] += 1
            a["paused_until"] = now + backoff_seconds(a["consecutive_429"], exc.retry_after, self._rand)
            a["pause_reason"] = (f"Unipile rate limit ({a['consecutive_429']} in a row)" if a["consecutive_429"] > 1
                                 else "Unipile rate limit")
        elif exc.status == 401 and exc.kind in _DISCONNECTED:
            a["status"] = "DISCONNECTED"
            a["paused_until"] = now + 6 * HOUR
            a["pause_reason"] = "account disconnected. Reconnect it in the Unipile dashboard"
        elif exc.kind in _RESTRICTED:
            a["paused_until"] = now + DAY
            a["pause_reason"] = f"provider reported {exc.kind.replace('_', ' ')}"
        else:
            return
        store.save_account(a)

    # ── accounts ─────────────────────────────────────────────────────────────

    def refresh_accounts(self, force: bool = False) -> list[dict]:
        store, now, s = self.store(), self.clock(), self.settings
        if not force and now - float(store.get_meta("accounts_at", 0.0)) < s.cache_ttl_minutes * 60:
            return self._known_accounts()
        accounts = self._call("*", lambda: self.client().list_accounts())
        wanted = self.account_ids()
        found = {a["id"] for a in accounts}
        if wanted:
            accounts = [a for a in accounts if a["id"] in wanted]
        store.set_meta("missing_accounts", [i for i in wanted if i not in found])
        store.set_meta("active_accounts", [a["id"] for a in accounts])
        for acc in accounts:
            prev = store.account(acc["id"])
            a = dict(prev, network=acc["network"], seen_at=now)
            if acc["status"] == "OK":
                if prev["pause_reason"].startswith(("account disconnected", "account status")):
                    a["paused_until"], a["pause_reason"] = 0.0, ""  # reconnected
            else:
                a["paused_until"] = max(a["paused_until"], now + HOUR)
                a["pause_reason"] = f"account status {acc['status']}. Check it in the Unipile dashboard"
            a["status"] = acc["status"]
            store.save_account(a)
        store.set_meta("accounts_at", now)
        return self._known_accounts()

    def _known_accounts(self) -> list[dict]:
        store = self.store()
        wanted = set(self.account_ids()) or set(store.get_meta("active_accounts", []) or [])
        return [a for a in store.accounts() if a["id"] in wanted and a["network"]]

    # ── reading ──────────────────────────────────────────────────────────────

    def _cached_chat_page(self, account_id: str, *, limit: int, unread_only: bool, max_age: float,
                          now: float) -> Optional[list[dict]]:
        """The chats of this account's last chat-list read, if that read is fresh enough and
        answers this request exactly. None means: ask Unipile."""
        rec = self.store().get_meta(f"chats_at:{account_id}")
        if not rec or now - float(rec["at"]) >= max_age:
            return None
        if rec["unread_only"] != unread_only and not (rec["complete"] and not rec["unread_only"]):
            return None
        if not rec["complete"] and len(rec["ids"]) < limit:
            return None
        chats = self.store().chats_by_ids(rec["ids"])
        if unread_only:
            chats = [c for c in chats if c["unread"] > 0]
        return chats[:limit]

    def gather_chats(self, *, network: Optional[str] = None, unread_only: bool = False, limit: int = 20,
                     since: Optional[float] = None, max_age: Optional[float] = None) -> tuple[list[dict], list[str]]:
        """Recent chats across every connected account (newest first), plus a note for each
        account that could not be read (paused, rate-limited, disconnected...). With ``max_age``,
        an account's chat list read within that many seconds is reused."""
        s, store, now = self.settings, self.store(), self.clock()
        self.client()  # setup check
        store.maybe_prune(now, s.cache_retention_days)
        limit = max(1, min(int(limit), s.read_page_size * s.max_pages))
        accounts = [a for a in self.refresh_accounts() if not network or a["network"] == network]
        out: list[dict] = []
        problems: list[str] = [f"Account {i} was not found in Unipile. Check the account ID in settings."
                               for i in store.get_meta("missing_accounts", [])]
        failed = 0
        for acc in accounts:
            cached = (self._cached_chat_page(acc["id"], limit=limit, unread_only=unread_only, max_age=max_age,
                                             now=now) if max_age is not None else None)
            if cached is not None:
                out.extend(cached)
                continue
            got: list[dict] = []
            cursor = None
            try:
                for _ in range(s.max_pages):
                    page_size = min(s.read_page_size, limit - len(got))
                    chats, cursor = self._call(acc["id"], lambda c=cursor, n=page_size, a=acc["id"]: self.client(
                    ).list_chats(a, limit=n, cursor=c, unread_only=unread_only))
                    store.upsert_chats(chats, self.clock())
                    got.extend(chats)
                    if not cursor or len(got) >= limit or (since and chats and chats[-1]["ts"] < since):
                        break
                store.set_meta(f"chats_at:{acc['id']}", {"at": self.clock(), "ids": [c["id"] for c in got],
                                                         "unread_only": unread_only, "complete": not cursor})
            except InboxError as exc:
                if exc.kind in _FATAL_READ_KINDS:
                    raise
                failed += 1
                problems.append(f"{NETWORK_LABELS.get(acc['network'], acc['network'])}: {exc}")
            out.extend(got)
        if accounts and failed == len(accounts) and not out:
            raise InboxError(" ".join(problems), kind="unavailable")
        if since:
            out = [c for c in out if c["ts"] >= since]
        out.sort(key=lambda c: c["ts"], reverse=True)
        store.set_meta("chats_at", self.clock())
        return out[:limit], problems

    def list_chats(self, **kwargs: Any) -> list[dict]:
        return self.gather_chats(**kwargs)[0]

    def messages(self, chat: dict, limit: int = 20, max_age: Optional[float] = None) -> list[dict]:
        """Latest messages of a chat, oldest first. Reuses the cache when the chat's latest
        messages were read within ``max_age`` seconds by a read that asked for at least as many:
        its answer then contains exactly what a new read would return."""
        store, s = self.store(), self.settings
        n = max(1, min(int(limit), s.read_page_size))
        if max_age is not None:
            row = store.cached_chat(chat["id"])
            if row and row["msgs_fetched_n"] >= n and self.clock() - float(row["msgs_fetched_at"]) < max_age:
                return store.cached_messages(chat["id"], n)
        msgs = self._call(chat["account_id"],
                          lambda: self.client().list_messages(chat["account_id"], chat["id"], limit=n))
        msgs = [m for m in msgs if not m.get("deleted")]
        for m in msgs:
            if not m["from_me"] and not m.get("sender"):
                m["sender"] = chat.get("name", "")
        store.upsert_messages(msgs, self.clock(), account_id=chat["account_id"],
                              baseline=self.baseline(chat["account_id"]))
        store.set_messages_fetched(chat["id"], self.clock(), n)
        return msgs

    def baseline(self, account_id: str) -> Optional[float]:
        """When the background sync started watching this account. Messages older than this are
        history, not news. None until the first sync of the account."""
        value = self.store().get_meta(f"baseline:{account_id}")
        return float(value) if value is not None else None

    def find_chat(self, chat_id: str) -> dict:
        chat = self.store().cached_chat(chat_id)
        if chat:
            return chat
        for c in self.list_chats(limit=self.settings.read_page_size * self.settings.max_pages):
            if c["id"] == chat_id:
                return c
        raise InboxError(f"No chat with id {chat_id!r}. Use inbox_list_chats to find it.", kind="not_found")

    def search(self, query: str, network: Optional[str] = None, limit: int = 20) -> dict:
        store, s, now = self.store(), self.settings, self.clock()
        ttl = s.cache_ttl_minutes * 60
        warmed, problems = 0, []
        if now - float(store.get_meta("chats_at", 0.0)) >= ttl or not store.cached_message_count():
            chats, problems = self.gather_chats(network=network, limit=min(10, s.max_triage_chats))
            for chat in chats:
                try:
                    self.messages(chat, limit=s.read_page_size, max_age=ttl)
                    warmed += 1
                except InboxError as exc:
                    if exc.kind in ("rate_limited", "paused", "budget"):
                        problems.append(str(exc))
                        break
                    raise
        hits = store.search_messages(query, network=network, limit=limit)
        seen = {(h["chat_id"], h["ts"]) for h in hits}
        voice = [h for h in store.search_transcripts(query, network=network, limit=limit)
                 if (h["chat_id"], h["ts"]) not in seen]
        if voice:
            hits = sorted(hits + voice, key=lambda h: h["ts"], reverse=True)[:limit]
        out = {"query": query, "results": hits, "searched": "local cache of synced and recently read chats",
               "chats_refreshed": warmed}
        try:  # then conversations that match by meaning, beyond the exact words
            covered = {store.chunk_at(h["chat_id"], h["ts"]) for h in hits}
            related = [r for r in self.context.search_meaning(query, k=8, network=network)
                       if r["chunk_id"] not in covered]
            if related:
                out["related"] = related[:5]
        except InboxError as exc:
            problems = [*problems, f"Search by meaning is unavailable right now: {exc}"]
        if problems:
            out["problems"] = problems
        return out

    def triage(self, hours: int = 24, max_age: Optional[float] = None) -> dict:
        s, now = self.settings, self.clock()
        hours = max(1, min(int(hours), 168))
        chats, problems = self.gather_chats(limit=s.max_triage_chats, since=now - hours * HOUR, max_age=max_age)
        snoozed = self.store().snoozed_chat_ids(now)
        chats = [c for c in chats if c["id"] not in snoozed]  # snoozed until later on purpose
        assessed = []
        for chat in chats[: s.max_triage_chats]:
            try:
                msgs = self.messages(chat, limit=10, max_age=s.cache_ttl_minutes * 60)
            except InboxError as exc:
                if exc.kind in ("rate_limited", "paused", "budget"):
                    problems.append(f"Stopped early: {exc}")
                    break
                raise
            a = triage_mod.assess(chat, msgs, now=now, unanswered_hours=s.unanswered_hours)
            scam = self.shield.for_chat(chat, msgs)
            if scam:  # urgency is the scammer's tool: never "urgent", listed apart with a warning
                a.update(urgent=False, unanswered=False,
                         possible_scam={k: scam[k] for k in ("level", "reasons", "advice")})
            assessed.append(a)
        out = triage_mod.rank(assessed)
        out["window_hours"] = hours
        if snoozed:
            out["snoozed_chats_hidden"] = len(snoozed)
        if problems:
            out["problems"] = problems
        return out

    def updates(self, *, limit_chats: int = 20) -> dict:
        """New messages from other people since the last call (the first call looks back 24 h),
        grouped by chat, newest chat first. Reads only the local event log: no Unipile call."""
        self.client()  # setup check
        store, now = self.store(), self.clock()
        cursor = store.get_meta("updates_cursor")
        events = store.events_after(int(cursor or 0), types=["message.new"], limit=1000)
        if cursor is None:
            events = [e for e in events if e["ts"] >= now - DAY]
        if events:
            store.set_meta("updates_cursor", events[-1]["id"])
        elif cursor is None:
            store.set_meta("updates_cursor", store.last_event_id())
        by_chat: dict[str, dict] = {}
        for e in events:
            if e["data"].get("from_me"):
                continue
            chat = by_chat.get(e["chat_id"])
            if chat is None:
                row = store.cached_chat(e["chat_id"]) or {}
                chat = by_chat[e["chat_id"]] = {"chat_id": e["chat_id"], "name": row.get("name") or "",
                                                "network": row.get("network") or "", "count": 0, "messages": []}
            chat["count"] += 1
            chat["messages"].append({"from": e["data"].get("sender") or chat["name"],
                                     "text": e["data"].get("text", ""), "ts": e["data"].get("ts") or e["ts"]})
        chats = sorted(by_chat.values(), key=lambda c: c["messages"][-1]["ts"], reverse=True)
        for c in chats:
            c["messages"] = c["messages"][-5:]
        out = {"chats": chats[:limit_chats], "more_chats": max(0, len(chats) - limit_chats),
               "sync": self.sync_status()}
        alerts = self.automation.alerts()
        if alerts:
            out["alerts"] = alerts
        return out

    def sync_status(self) -> dict:
        store, s, now = self.store(), self.settings, self.clock()
        st = store.get_meta("sync_state", {}) or {}
        hook = store.get_meta("webhook") or {}
        out = {"enabled": s.sync_enabled, "last_ok_at": st.get("last_ok_at"), "last_error": st.get("last_error", ""),
               "next_run_at": st.get("next_run_at"), "webhook": bool(hook.get("id"))}
        if st.get("last_ok_at"):
            out["minutes_since_last_sync"] = int((now - float(st["last_ok_at"])) // 60)
        if hook.get("id"):
            out["webhook_url"] = hook.get("url", "")
            last = store.get_meta("webhook_last_event_at")
            out["webhook_last_event_at"] = last
        return out

    # ── status ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        store, s, now = self.store(), self.settings, self.clock()
        store.maybe_prune(now, s.cache_retention_days)
        try:
            accounts = self.refresh_accounts()
            accounts_error = ""
        except InboxError as exc:
            accounts, accounts_error = self._known_accounts(), str(exc)
        sends = store.recent_sends(now - DAY)
        nxt = float(store.get_meta("next_send_allowed_at", 0.0))
        acc_out = []
        for a in accounts:
            item = {"id": a["id"], "network": a["network"], "status": a["status"]}
            if a["paused_until"] > now:
                item["paused_for_min"] = int((a["paused_until"] - now) // 60) + 1
                item["pause_reason"] = a["pause_reason"]
            acc_out.append(item)
        g = store.account("*")
        return {
            "connected": self.configured,
            "accounts": acc_out,
            "accounts_error": accounts_error,
            "accounts_not_found": store.get_meta("missing_accounts", []) if self.configured else [],
            "global_pause_min": int((g["paused_until"] - now) // 60) + 1 if g["paused_until"] > now else 0,
            "sent_last_24h": {"total": len(sends), "limit": s.daily_send_limit,
                              "whatsapp": sum(1 for x in sends if x.network == "whatsapp"),
                              "whatsapp_limit": s.daily_limit_whatsapp,
                              "instagram": sum(1 for x in sends if x.network == "instagram"),
                              "instagram_limit": s.daily_limit_instagram,
                              "linkedin": sum(1 for x in sends if x.network == "linkedin"),
                              "linkedin_limit": s.daily_limit_linkedin},
            "sent_last_hour": {"total": sum(1 for x in sends if now - x.sent_at < HOUR),
                               "limit": s.hourly_send_limit},
            "next_send_in_seconds": max(0, int(nxt - now)),
            "unipile_calls_last_hour": {"used": store.api_calls_last_hour(now), "limit": s.api_calls_per_hour},
            "pending_drafts": store.count_drafts("pending"),
            "quiet_hours": s.quiet_hours or "off",
            "sync": self.sync_status(),
            "memory": self.memory_status(),
            "scheduled_messages": len(store.schedules()),
            "active_watches": len(store.watches()),
            "unread_alerts": len(store.notifications(unread_only=True, limit=1000)),
            "cloud": self.cloud.status() if self.cloud.configured else {"configured": False},
        }

    def memory_status(self) -> dict:
        store, s = self.store(), self.settings
        emb = self.context.embedder()
        state = store.get_meta("context_state", {}) or {}
        out = {"enabled": s.memory_enabled, "language_model": bool(self.context.llm),
               "embeddings": emb.name if emb else "off",
               "vector_index": "sqlite-vec" if store.vec_index else "built-in",
               "open_questions": store.count_questions()}
        if state.get("problems"):
            out["problems"] = state["problems"]
        return out

    # ── drafts and sending ───────────────────────────────────────────────────

    def create_draft(self, chat_id: str, text: str, reply_to: str = "", origin: str = "") -> dict:
        text = (text or "").strip()
        if not text:
            raise InboxError("The draft text is empty.", kind="input")
        if len(text) > 4000:
            raise InboxError("Drafts are limited to 4000 characters.", kind="input")
        chat = self.find_chat(chat_id)
        store = self.store()
        quoted = self.quoted_message(chat["id"], reply_to) if reply_to else None
        draft_id = store.add_draft(chat_id=chat["id"], account_id=chat["account_id"], network=chat["network"],
                                   chat_name=chat.get("name", ""), text=text, now=self.clock(),
                                   reply_to=quoted["id"] if quoted else "", origin=origin)
        reasons = self.preflight(store.get_draft(draft_id), max_age=self.settings.cache_ttl_minutes * 60,
                                 check_accounts=False)
        out = {"draft_id": draft_id, "to": chat.get("name", ""), "network": chat["network"], "text": text}
        if quoted:
            out["replying_to"] = quote_view(quoted)
        out.update(sent=False, how_to_send=f"Only the user can send this: they type /inbox send {draft_id} and confirm.",
                   would_be_blocked_now_because=reasons)
        return out

    def preflight(self, draft: dict, *, max_age: Optional[float] = None, check_accounts: bool = True,
                  has_files: bool = False) -> list[str]:
        """Every reason this draft may not be sent now (empty = allowed). ``max_age`` lets a
        recent read of the chat stand in for a new one (None = always read it again)."""
        store, s, now = self.store(), self.settings, self.clock()
        if check_accounts:
            try:
                self.refresh_accounts()
            except InboxError as exc:
                return [f"Could not check the account: {exc}"]
        chat = store.cached_chat(draft["chat_id"]) or {"id": draft["chat_id"], "account_id": draft["account_id"],
                                                      "name": draft["chat_name"]}
        try:
            msgs = self.messages(chat, limit=10, max_age=max_age)
        except InboxError as exc:
            return [f"Could not read the chat: {exc}"]
        inbound = [m["ts"] for m in msgs if not m["from_me"]]
        reasons = check_send(
            s, now=now, local_now=self._local_now(), chat_id=draft["chat_id"], network=draft["network"],
            text=draft["text"] or ("(file)" if has_files else ""),
            recent_sends=store.recent_sends(now - max(DAY, s.duplicate_window_hours * HOUR)),
            account=store.gate(draft["account_id"]),
            last_inbound_at=max(inbound) if inbound else None,
            next_send_allowed_at=float(store.get_meta("next_send_allowed_at", 0.0)),
        )
        if chat.get("is_readonly"):
            reasons.append("This chat is read-only: only its admins can write in it.")
        blank = blanks(draft["text"]) if draft.get("origin") == "auto" else []
        if blank:
            reasons.append(f"This draft still has a blank to fill in: {', '.join(blank)}. Edit the draft first.")
        risk = self.shield.reply_risk(draft["chat_id"], draft["text"])
        if risk:
            reasons.append(risk)
        if draft.get("reply_to") and not store.get_message(draft["reply_to"]):
            reasons.append("The message this replies to is gone (deleted, or no longer in the local copy)."
                           " Write a new draft without the reply, or reply to another message.")
        return reasons

    def send(self, draft_id: str, *, attachments: Optional[list] = None, via: str = "command",
             attempt_id: Optional[int] = None) -> dict:
        """Send one approved draft (plus optional files). Callers must have asked the user first:
        /inbox send and the page confirm in their own way, inbox_send through Hermes's approval."""
        store = self.store()
        with self._send_lock, self._send_lease():
            self.cloud.before_send()  # with cloud sync: every device's sends count; refuses if unreachable
            try:
                return self._send_locked(store, draft_id, attachments or [], via, attempt_id)
            finally:
                self.cloud.after_send()

    def _attempt(self, attempt_id: Optional[int], *, via: str, network: str, to: str, target: str, text: str,
                 files: list, status: str, detail: str = "", message_id: str = "") -> int:
        """Record how a send went (/inbox log, the page's Activity, activity.log)."""
        store, now = self.store(), self.clock()
        if attempt_id:
            store.update_attempt(attempt_id, now, status=status, detail=detail, message_id=message_id, target=target)
        else:
            attempt_id = store.log_attempt(via=via, network=network, to_name=to, target=target, text=text,
                                           files=files, status=status, detail=detail, message_id=message_id, now=now)
        line = (f"{status.upper():7} {via:8} {NETWORK_LABELS.get(network, network)} -> {to}: {text[:120]!r}"
                + (f" +{len(files)} file(s)" if files else "") + (f" | {detail}" if detail else ""))
        self._activity_file(line)
        return attempt_id

    def _activity_file(self, line: str) -> None:
        """Append to activity.log next to the database (a plain file you can open). It holds
        message text, so like the database it is readable by you alone on macOS and Linux. Past
        ACTIVITY_FILE_BYTES it starts over, keeping the previous part as activity.log.1."""
        try:
            row = self.store()._conn.execute("PRAGMA database_list").fetchone()
            if not row or not row[2]:
                return
            import os

            path = os.path.join(os.path.dirname(row[2]), "activity.log")
            try:
                if os.path.getsize(path) > ACTIVITY_FILE_BYTES:
                    os.replace(path, path + ".1")
            except FileNotFoundError:
                pass
            stamp = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d %H:%M:%S")
            with open(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as fh:
                if os.name != "nt":
                    os.fchmod(fh.fileno(), 0o600)  # also a file an older version created
                fh.write(f"{stamp}  {line}\n")
        except OSError:
            pass

    def _send_locked(self, store: Store, draft_id: str, attachments: list, via: str,
                     attempt_id: Optional[int]) -> dict:
        """The send itself (callers hold the send locks)."""
        draft = store.get_draft(draft_id)
        if not draft:
            raise InboxError(f"No draft {draft_id!r}.", kind="not_found")
        if draft["status"] != "pending":
            raise InboxError(f"Draft {draft_id} is already {draft['status']}.", kind="input")
        files = [a.get("filename", "") for a in attachments]
        log = dict(via=via, network=draft["network"], to=draft["chat_name"], target=draft["chat_id"],
                   text=draft["text"], files=files)
        # Re-checked after the confirmation. The chat was read moments ago for the
        # confirmation itself, so a read within FRESH_SECONDS is reused.
        reasons = self.preflight(draft, max_age=FRESH_SECONDS, has_files=bool(attachments))
        if reasons:
            self._attempt(attempt_id, status="blocked", detail="; ".join(reasons), **log)
            return {"sent": False, "reasons": reasons}
        inbound = [m["ts"] for m in store.cached_messages(draft["chat_id"], 10) if not m["from_me"]]
        started = starts_conversation(self.settings, self.clock(), max(inbound) if inbound else None)
        chat = store.cached_chat(draft["chat_id"]) or {}
        client_args = (draft["account_id"], draft["chat_id"], self.with_mentions(chat, draft["text"]))
        options: dict = {}
        if attachments:
            options["attachments"] = attachments
        if draft.get("reply_to"):
            options["quote_id"] = draft["reply_to"]

        def do_send():
            return self.client().send_message(*client_args, **options)

        try:
            message_id = self._call(draft["account_id"], do_send, is_send=True)
        except InboxError as exc:
            if exc.kind in _KEEP_DRAFT_KINDS:
                # Definitely not delivered: keep the draft so it can be sent once this is resolved.
                self._attempt(attempt_id, status="failed", detail=f"not sent: {exc}", **log)
                return {"sent": False, "reasons": [f"{exc} Draft {draft['id']} is still saved."]}
            unsure = exc.status >= 500 or exc.kind == "network"
            note = (" It may or may not have been delivered: check the app before trying again."
                    if unsure else "")
            store.set_draft_status(draft["id"], "failed", self.clock(), str(exc) + note)
            if unsure:  # count it, so a retry cannot double the pace
                store.log_send(draft_id=draft["id"], chat_id=draft["chat_id"], account_id=draft["account_id"],
                               network=draft["network"], text=draft["text"], message_id="", now=self.clock(),
                               started=started)
            self._attempt(attempt_id, status="unsure" if unsure else "failed", detail=str(exc) + note, **log)
            return {"sent": False, "reasons": [str(exc) + note]}
        now = self.clock()
        store.log_send(draft_id=draft["id"], chat_id=draft["chat_id"], account_id=draft["account_id"],
                       network=draft["network"], text=draft["text"], message_id=message_id, now=now, started=started)
        store.set_draft_status(draft["id"], "sent", now)
        store.set_messages_fetched(draft["chat_id"], 0.0, 0)  # the next read shows the sent message
        gap = send_gap_seconds(self.settings, self._rand)
        store.set_meta("next_send_allowed_at", now + gap)
        self._attempt(attempt_id, status="sent", message_id=message_id, **log)
        out = {"sent": True, "to": draft["chat_name"], "network": draft["network"],
               "next_send_in_seconds": int(gap), "message_id": message_id}
        if self.settings.mark_read_after_reply:
            try:
                self.mark_read(draft["chat_id"])
                out["marked_read"] = True
            except InboxError as exc:  # the message is sent; only the read receipt failed
                out["marked_read"] = False
                out["mark_read_problem"] = str(exc)
        return out

    def start_conversation(self, *, account_id: str, network: str, user_id: str, name: str, text: str,
                           attachments: Optional[list] = None, via: str = "hermes",
                           attempt_id: Optional[int] = None, resolve: Optional[Callable[[], str]] = None) -> dict:
        """First message to someone with no chat yet (a WhatsApp number, a LinkedIn profile).
        Same locks, limits and logging as ``send``; counts as a new conversation."""
        store = self.store()
        attachments = attachments or []
        log = dict(via=via, network=network, to=name, target=user_id, text=text,
                   files=[a.get("filename", "") for a in attachments])
        with self._send_lock, self._send_lease():
            self.cloud.before_send()
            try:
                s, now = self.settings, self.clock()
                reasons = check_send(
                    s, now=now, local_now=self._local_now(), chat_id=f"new:{user_id}", network=network,
                    text=text or ("(file)" if attachments else ""),
                    recent_sends=store.recent_sends(now - max(DAY, s.duplicate_window_hours * HOUR)),
                    account=store.gate(account_id), last_inbound_at=None,
                    next_send_allowed_at=float(store.get_meta("next_send_allowed_at", 0.0)))
                if reasons:
                    self._attempt(attempt_id, status="blocked", detail="; ".join(reasons), **log)
                    return {"sent": False, "reasons": reasons}
                try:
                    if resolve:  # e.g. a LinkedIn profile name -> its id (one paced read)
                        user_id = resolve()
                        log["target"] = user_id
                    result = self._call(account_id, lambda: self.client().start_chat(
                        account_id, user_id, text, attachments=attachments or None), is_send=True)
                except InboxError as exc:
                    unsure = exc.kind not in _KEEP_DRAFT_KINDS and (exc.status >= 500 or exc.kind == "network")
                    note = " It may or may not have been delivered: check the app." if unsure else ""
                    if unsure:
                        store.log_send(draft_id="", chat_id=f"new:{user_id}", account_id=account_id, network=network,
                                       text=text, message_id="", now=self.clock(), started=True)
                    self._attempt(attempt_id, status="unsure" if unsure else "failed", detail=str(exc) + note, **log)
                    return {"sent": False, "reasons": [str(exc) + note]}
                now = self.clock()
                chat_id = result.get("chat_id") or f"new:{user_id}"
                store.log_send(draft_id="", chat_id=chat_id, account_id=account_id, network=network, text=text,
                               message_id=result.get("message_id", ""), now=now, started=True)
                if result.get("chat_id"):
                    store.upsert_chats([{"id": chat_id, "account_id": account_id, "network": network, "name": name,
                                         "ts": now, "last_text": text, "last_from_me": True}], now)
                gap = send_gap_seconds(s, self._rand)
                store.set_meta("next_send_allowed_at", now + gap)
                log["target"] = chat_id
                self._attempt(attempt_id, status="sent", message_id=result.get("message_id", ""), **log)
                return {"sent": True, "to": name, "network": network, "chat_id": chat_id, "new_conversation": True,
                        "next_send_in_seconds": int(gap), "message_id": result.get("message_id", "")}
            finally:
                self.cloud.after_send()

    def _send_lease(self):
        """Across processes (the gateway, the dashboard, the CLI), one send at a time: the gap
        between sends is checked and set inside it."""
        inbox = self

        class _Lease:
            # the process id too: two Hermes processes can hold objects at the same address
            owner = f"send-{os.getpid()}-{id(inbox)}-{threading.get_ident()}"

            def __enter__(self):
                store = inbox.store()
                for _ in range(60):
                    if store.acquire_lease("send", self.owner, inbox.clock(), 120.0):
                        return self
                    inbox._sleep(0.5)
                raise InboxError("Another Hermes window is sending a message. Try again in a moment.",
                                 kind="paused")

            def __exit__(self, *exc):
                inbox.store().release_lease("send", self.owner)
                return False

        return _Lease()

    # ── replies, mentions and groups ─────────────────────────────────────────

    def quoted_message(self, chat_id: str, message_id: str) -> dict:
        """The message a reply quotes: it must be in this chat (from the local copy)."""
        message_id = str(message_id or "").strip()
        msg = self.store().get_message(message_id) if message_id else None
        if not msg:
            raise InboxError(f"No message {message_id!r} in the local copy. Read the chat first (inbox_read_chat)"
                             " to see message ids.", kind="not_found")
        if msg["chat_id"] != chat_id:
            raise InboxError("That message is in a different chat: reply to a message of this chat.", kind="input")
        return msg

    def members(self, chat_id: str) -> list[dict]:
        """People seen writing in a group (from the local copy): [{id, name}], most recent first."""
        return self.store().chat_members(chat_id)

    def with_mentions(self, chat: dict, text: str) -> str:
        """In a group, "@Name" of a member becomes the provider's mention ("@" + their id), so the app
        notifies them and shows their name. Unknown names stay as they are."""
        if not chat.get("is_group") or "@" not in (text or ""):
            return text
        people = sorted((m for m in self.members(chat["id"]) if m["name"].strip()),
                        key=lambda m: len(m["name"]), reverse=True)
        for m in people:
            pattern = re.compile(r"(?<![\w@])@" + re.escape(m["name"].strip()) + r"(?![\w])", re.IGNORECASE)
            text = pattern.sub(lambda _m, uid=m["id"]: "@" + uid, text)
        return text

    # ── received files and voice notes ───────────────────────────────────────

    def _attachment_of(self, message_id: str, attachment_id: str = "") -> tuple[dict, dict]:
        msg = self.store().get_message(str(message_id or "").strip())
        if not msg:
            raise InboxError(f"No message {message_id!r} in the local copy. Read the chat first (inbox_read_chat)"
                             " to see message ids.", kind="not_found")
        files = [a for a in (msg.get("extra") or {}).get("attachments", []) if a.get("id")]
        if not files:
            raise InboxError("That message has no file to open.", kind="input")
        if attachment_id:
            match = [a for a in files if a["id"] == attachment_id]
            if not match:
                raise InboxError(f"That message has no file {attachment_id!r}.", kind="not_found")
            return msg, match[0]
        if len(files) > 1:
            raise InboxError("That message has several files: say which one with attachment_id ("
                             + ", ".join(f"{a['id']} {describe_attachment(a)}" for a in files) + ").", kind="input")
        return msg, files[0]

    def fetch_attachment(self, message_id: str, attachment_id: str = "") -> dict:
        """Download a received file (once; later calls reuse it) into the plugin's own folder.
        One paced Unipile read. Returns where it is and what it is."""
        store = self.store()
        msg, att = self._attachment_of(message_id, attachment_id)
        saved = store.attachment_file(msg["id"], att["id"])
        if saved and saved["path"] and Path(saved["path"]).is_file():
            return self._file_view(msg, att, saved)
        chat = store.cached_chat(msg["chat_id"])
        if not chat:
            raise InboxError("The chat of that message is no longer in the local copy.", kind="not_found")
        folder = store.files_dir()
        if folder is None:
            raise InboxError("This database has no folder to keep files in.", kind="input")
        if att.get("is_unavailable"):
            raise InboxError("That file is no longer available (it expired or was removed on the phone).",
                             kind="unavailable")
        size_hint = att.get("file_size")
        if isinstance(size_hint, (int, float)) and size_hint > MAX_DOWNLOAD_BYTES:
            raise InboxError(f"The file is larger than {MAX_DOWNLOAD_BYTES // 1048576} MB: open it in the app.",
                             kind="too_large")
        data, ctype = self._call(chat["account_id"], lambda: self.client().download_attachment(
            chat["account_id"], chat["id"], msg["id"], att["id"]))
        if not data:
            raise InboxError("Unipile returned an empty file (it may have expired on the phone).", kind="unavailable")
        mimetype = str(att.get("mimetype") or ctype.split(";")[0].strip() or "application/octet-stream").lower()
        filename = _safe_filename(att.get("filename"), _default_name(att, mimetype))
        token = hashlib.sha256(f"{msg['id']}|{att['id']}".encode()).hexdigest()[:12]
        folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(folder, 0o700)  # your files: readable by you alone
        path = folder / f"{token}-{filename}"
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            fh.write(data)
        store.save_attachment_file(message_id=msg["id"], attachment_id=att["id"], chat_id=msg["chat_id"],
                                   path=str(path), filename=filename, mimetype=mimetype, size=len(data),
                                   now=self.clock())
        return self._file_view(msg, att, store.attachment_file(msg["id"], att["id"]))

    def _file_view(self, msg: dict, att: dict, saved: dict) -> dict:
        out = {"message_id": msg["id"], "attachment_id": att["id"], "chat_id": msg["chat_id"],
               "kind": describe_attachment(att), "type": att.get("type", "file"), "filename": saved["filename"],
               "mimetype": saved["mimetype"], "size": saved["size"], "path": saved["path"]}
        if saved["transcript"]:
            out["transcript"] = saved["transcript"]
        return out

    def save_attachment_copy(self, message_id: str, attachment_id: str = "") -> Path:
        """Copy a received file (downloading it first if needed) into "saved/" next to the
        database, under its own name (never a path), so you can open it from your file manager."""
        import shutil

        view = self.fetch_attachment(message_id, attachment_id)
        folder = Path(view["path"]).parent.parent / "saved"
        folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(folder, 0o700)
        name = _safe_filename(_original_name(view["filename"]), "file")
        stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
        target, n = folder / name, 2
        while target.exists():
            if target.stat().st_size == view["size"] and target.read_bytes() == Path(view["path"]).read_bytes():
                return target  # already saved
            target = folder / (f"{stem} ({n}){dot}{ext}" if dot else f"{name} ({n})")
            n += 1
        with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out, \
                open(view["path"], "rb") as src:
            shutil.copyfileobj(src, out)
        return target

    def read_attachment_text(self, view: dict) -> Optional[str]:
        """The text of a small plain-text file (.txt, .csv, .json...), else None."""
        mimetype = view.get("mimetype", "")
        if not (mimetype.startswith(_TEXT_TYPES) or mimetype in _TEXT_TYPES) or not view.get("path"):
            return None
        try:
            with open(view["path"], "rb") as fh:
                raw = fh.read(TEXT_PREVIEW_CHARS * 4)
        except OSError:
            return None
        return raw.decode("utf-8", errors="replace")[:TEXT_PREVIEW_CHARS]

    def transcribe(self, message_id: str, attachment_id: str = "") -> dict:
        """A voice note (or other audio) as text, with Hermes's own speech-to-text. Done once per
        file; the text is kept with the message."""
        msg, att = self._attachment_of(message_id, attachment_id)
        if att.get("type") != "audio":
            raise InboxError("Only voice notes and audio can be turned into text.", kind="input")
        store = self.store()
        saved = store.attachment_file(msg["id"], att["id"])
        if saved and saved["transcript"]:
            return self._file_view(msg, att, saved)
        if self.transcriber is None:
            raise InboxError("Speech-to-text is not available in this Hermes. Set it up in Hermes (stt in"
                             " config.yaml: local Whisper, Groq, OpenAI...), then try again.", kind="transcription")
        view = self.fetch_attachment(msg["id"], att["id"])
        try:
            result = self.transcriber(view["path"]) or {}
        except Exception as exc:  # noqa: BLE001 - the STT backend failed; report it plainly
            result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        if not result.get("success"):
            raise InboxError("Could not turn the voice note into text: "
                             f"{result.get('error') or 'speech-to-text failed'}", kind="transcription")
        text = " ".join(str(result.get("transcript") or "").split()) or _NO_SPEECH
        store.save_transcript(msg["id"], att["id"], text, self.clock())
        return self._file_view(msg, att, store.attachment_file(msg["id"], att["id"]))

    def with_files(self, msgs: list[dict]) -> list[dict]:
        """Messages with each attachment marked downloaded or not, with its transcript, and with the
        reactions you added here (copies)."""
        mine = self.store().get_meta("my_reactions", {}) or {}
        if mine:
            msgs = [dict(m, my_reactions=mine[m["id"]]) if m["id"] in mine else m for m in msgs]
        if not any((m.get("extra") or {}).get("attachments") for m in msgs):
            return msgs
        saved = self.store().files_for_messages(m["id"] for m in msgs)
        out = []
        for m in msgs:
            atts = (m.get("extra") or {}).get("attachments")
            if not atts:
                out.append(m)
                continue
            files = []
            for a in atts:
                f = saved.get((m["id"], a.get("id")))
                files.append(dict(a, downloaded=bool(f and f["path"] and Path(f["path"]).is_file()),
                                  **({"transcript": f["transcript"]} if f and f["transcript"] else {})))
            out.append(dict(m, extra=dict(m["extra"], attachments=files)))
        return out

    # ── reactions and read receipts ──────────────────────────────────────────

    def react(self, message_id: str, emoji: str, *, remove: bool = False, via: str = "hermes",
              attempt_id: Optional[int] = None) -> dict:
        """Add (or remove your own) emoji reaction. Callers must have asked the user first, like a
        send. Reactions have their own small limits; quiet hours and account pauses apply."""
        emoji = check_emoji(emoji)
        store, s, now = self.store(), self.settings, self.clock()
        msg = store.get_message(str(message_id or "").strip())
        if not msg:
            raise InboxError(f"No message {message_id!r} in the local copy. Read the chat first.", kind="not_found")
        chat = store.cached_chat(msg["chat_id"])
        if not chat:
            raise InboxError("The chat of that message is no longer in the local copy.", kind="not_found")
        what = f"removed {emoji} reaction" if remove else f"{emoji} reaction"
        log = dict(via=via, network=chat["network"], to=chat["name"] or "this chat", target=chat["id"], text=what,
                   files=[])
        reasons = []
        if chat.get("is_readonly"):
            reasons.append("This chat is read-only.")
        if in_quiet_hours(s.quiet_hours, self._local_now()):
            reasons.append(f"Quiet hours ({s.quiet_hours}) are on.")
        times = [float(t) for t in store.get_meta("reaction_times", []) or [] if now - float(t) < DAY]
        if not remove:
            if sum(1 for t in times if now - t < HOUR) >= REACTIONS_PER_HOUR:
                reasons.append(f"Reaction limit: {REACTIONS_PER_HOUR} in an hour.")
            if len(times) >= REACTIONS_PER_DAY:
                reasons.append(f"Reaction limit: {REACTIONS_PER_DAY} in 24 hours.")
        if reasons:
            self._attempt(attempt_id, status="blocked", detail="; ".join(reasons), **log)
            return {"reacted": False, "reasons": reasons}
        with self._send_lock:
            try:
                self._call(chat["account_id"], lambda: self.client().react(chat["account_id"], chat["id"], msg["id"],
                                                                           emoji, remove=remove), is_send=True)
            except InboxError as exc:
                self._attempt(attempt_id, status="failed", detail=str(exc), **log)
                return {"reacted": False, "reasons": [str(exc)]}
            if not remove:
                store.set_meta("reaction_times", times + [self.clock()])
        mine = dict(store.get_meta("my_reactions", {}) or {})
        current = [e for e in mine.get(msg["id"], []) if e != emoji]
        if not remove:
            current.append(emoji)
        if current:
            mine[msg["id"]] = current
        else:
            mine.pop(msg["id"], None)
        store.set_meta("my_reactions", dict(list(mine.items())[-500:]))
        store.set_messages_fetched(chat["id"], 0.0, 0)  # the next read shows the reaction
        self._attempt(attempt_id, status="sent", **log)
        return {"reacted": True, "removed": remove, "emoji": emoji, "to": chat["name"], "network": chat["network"]}

    def mark_read(self, chat_id: str) -> dict:
        """Mark a chat as read in the app (the other person may see read receipts)."""
        chat = self.find_chat(chat_id)
        self._call(chat["account_id"], lambda: self.client().mark_chat_read(chat["account_id"], chat["id"]),
                   is_send=True)
        self.store().set_chat_unread(chat["id"], 0)
        return {"marked_read": True, "chat_id": chat["id"], "name": chat["name"], "network": chat["network"]}

    def edit_draft(self, draft_id: str, text: str) -> dict:
        """Change a waiting draft's text (e.g. to fill in a [blank] of a draft Hermes prepared)."""
        store = self.store()
        draft = store.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            raise InboxError(f"No pending draft {draft_id!r}.", kind="not_found")
        if store.schedule_for_draft(draft["id"]):
            raise InboxError("This draft is scheduled: cancel the schedule first, then edit it.", kind="input")
        text = (text or "").strip()
        if not text:
            raise InboxError("The draft text is empty.", kind="input")
        if len(text) > 4000:
            raise InboxError("Drafts are limited to 4000 characters.", kind="input")
        store.set_draft_text(draft["id"], text, self.clock())
        draft = store.get_draft(draft["id"])
        return {"draft_id": draft["id"], "to": draft["chat_name"], "network": draft["network"], "text": text,
                "sent": False, "would_be_blocked_now_because": self.preflight(
                    draft, max_age=self.settings.cache_ttl_minutes * 60, check_accounts=False)}

    def discard(self, draft_id: str) -> None:
        store = self.store()
        draft = store.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            raise InboxError(f"No pending draft {draft_id!r}.", kind="not_found")
        store.set_draft_status(draft["id"], "discarded", self.clock())


# ─── helpers ─────────────────────────────────────────────────────────────────

_EMOJI_BAD = re.compile(r"[A-Za-z0-9\s<>\"'`]")


def check_emoji(emoji: str) -> str:
    """One emoji (a short sequence like 👍, ❤️ or 👍🏽), nothing else."""
    emoji = (emoji or "").strip()
    if not emoji or len(emoji) > 16 or _EMOJI_BAD.search(emoji):
        raise InboxError("Give one emoji to react with, e.g. 👍 or ❤️.", kind="input")
    return emoji


def quote_view(msg: dict) -> dict:
    """How a quoted message is shown: who wrote it and the start of its text."""
    return {"message_id": msg["id"], "from": "me" if msg["from_me"] else (msg.get("sender") or "them"),
            "text": (msg.get("text") or "")[:160]}


_SAFE_NAME = re.compile(r"[^\w.\- ]+")


def _original_name(stored: str) -> str:
    """A downloaded file is kept as "<12 hex>-<name>": the part after the prefix."""
    m = re.match(r"^[0-9a-f]{12}-(.+)$", stored or "")
    return m.group(1) if m else stored


def _safe_filename(name: Any, fallback: str) -> str:
    """A file name that cannot leave the folder or hide as a dotfile."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = _SAFE_NAME.sub("_", base).strip(" .")[:100]
    return base or fallback


_EXTENSIONS = {"audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
               "audio/aac": ".aac", "audio/webm": ".webm", "audio/wav": ".wav", "audio/x-wav": ".wav",
               "audio/amr": ".amr", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "video/mp4": ".mp4", "application/pdf": ".pdf"}


def _default_name(att: dict, mimetype: str) -> str:
    ext = _EXTENSIONS.get(mimetype.split(";")[0].strip()) or mimetypes.guess_extension(mimetype) or ".bin"
    kind = {"audio": "voice-note" if att.get("voice_note") else "audio", "img": "photo", "video": "video"}.get(
        att.get("type", ""), "file")
    return kind + ext
