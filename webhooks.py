"""Unipile v2 webhooks: real-time updates for always-on Hermes installs.

How it fits together:
  * ``/inbox webhook setup <public https URL>`` registers an endpoint with Unipile and keeps the
    signing secret it returns (in the plugin database).
  * ``WebhookServer`` is a small local HTTP receiver, off unless ``webhook_port`` is set. It
    listens on ``webhook_host:webhook_port`` (127.0.0.1 by default) at ``/unipile/webhook``.
    The public URL must forward there, e.g. through a reverse proxy or a tunnel. (Hermes's own
    dashboard routes sit behind the dashboard login, which Unipile cannot pass, and Unipile v2
    cannot add custom headers, so a separate receiver is needed.)
  * ``WebhookHandler`` checks the ``unipile-signature`` header (HMAC-SHA256 of
    ``"{t}.{raw body}"`` with the endpoint secret, at most 5 minutes old), drops redeliveries,
    and applies the event to the same cache and event log the background sync uses.

Without webhooks everything still works: the background sync polls instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

from .client import InboxError, parse_chat, parse_message
from .pacing import HOUR

PATH = "/unipile/webhook"
MAX_BODY = 1_000_000
TOLERANCE_SECONDS = 300

EVENTS = [
    "message.new", "message.update", "message.delete", "message.reaction.new", "message.reaction.delete",
    "chat.update", "chat.delete",
    "account.status.disconnected", "account.status.running", "account.status.errored",
    "account.status.degraded", "account.status.partial", "account.locked", "account.unlocked",
    "account.reconnect", "account.remove",
]
_ACCOUNT_DOWN = {"account.status.disconnected": "DISCONNECTED", "account.status.errored": "ERRORED",
                 "account.locked": "LOCKED", "account.remove": "REMOVED"}
_ACCOUNT_UP = {"account.status.running", "account.status.degraded", "account.status.partial",
               "account.unlocked", "account.reconnect"}


def verify_signature(secret: str, header: Optional[str], raw: bytes, now: float,
                     tolerance: float = TOLERANCE_SECONDS) -> bool:
    """Check ``unipile-signature: t=<unix seconds>,v0=<hex>`` in constant time."""
    if not secret or not header:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        t = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        return False
    if abs(now - t) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{t}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v.strip().lower()) for v in parts.get("v0", []))


class WebhookHandler:
    def __init__(self, inbox, on_change: Optional[Callable[[], None]] = None):
        self.inbox = inbox
        self.on_change = on_change

    def handle(self, raw: bytes, headers: Mapping[str, str]) -> tuple[int, str]:
        """Verify and apply one delivery. Returns (HTTP status, short reason)."""
        inbox = self.inbox
        if not inbox.configured:
            return 503, "not connected"
        store, now = inbox.store(), inbox.clock()
        secret = (store.get_meta("webhook") or {}).get("secret") or ""
        if not secret:
            return 503, "webhook not set up"
        if not verify_signature(secret, headers.get("unipile-signature"), raw, now):
            return 401, "bad signature"
        try:
            event = json.loads(raw)
        except ValueError:
            return 400, "not JSON"
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return 400, "not an event"
        account_id = str(event.get("account_id") or "")
        if not inbox.uses_account(account_id):
            return 200, "other account"
        event_id = str(event.get("id") or "")
        if event_id and not store.first_delivery(event_id, now):
            return 200, "duplicate"
        try:
            changed = self.apply(event)
        except Exception:
            if event_id:  # not applied: let Unipile's redelivery through instead of calling it a duplicate
                store.forget_delivery(event_id)
            raise
        store.set_meta("webhook_last_event_at", now)
        if changed and self.on_change:
            self.on_change()
        return 200, "ok"

    def apply(self, event: dict) -> bool:
        """Apply one verified event. True when the background sync should run soon (an unknown
        chat to fetch, or an account that came back)."""
        inbox = self.inbox
        store, now = inbox.store(), inbox.clock()
        kind, aid = event["type"], str(event.get("account_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        network = str(event.get("account_provider") or "").lower() or store.account(aid)["network"]

        if kind in ("message.new", "message.update"):
            chat_id = str(payload.get("chat_id") or "")
            msg = parse_message(payload, chat_id) if chat_id else None
            if not msg:
                return False
            new_chat = store.ensure_chat(chat_id=chat_id, account_id=aid, network=network, now=now)
            chat = store.cached_chat(chat_id) or {}
            if not msg["from_me"] and not msg["sender"]:
                msg["sender"] = chat.get("name") or ""
            in_step = not new_chat and chat.get("synced_ts", 0) >= chat.get("ts", 0)
            baseline = inbox.baseline(aid)
            if baseline is None:  # webhooks arrived before the first sync: this one is news
                baseline = msg["ts"] - 1.0
                store.set_meta(f"baseline:{aid}", baseline)
            res = store.upsert_messages([msg], now, account_id=aid, baseline=baseline)
            if res["new"] or res["edited"]:
                store.note_chat_message(msg, inbound_new=bool(res["new"]) and not msg["from_me"])
            if in_step:  # the cache was complete up to now, so it still is
                store.mark_chat_synced(chat_id, msg["ts"])
            return new_chat

        if kind == "message.delete":
            mid = str(payload.get("id") or payload.get("message_id") or "")
            if mid and store.delete_message(mid):
                store.add_event(type="message.delete", now=now, account_id=aid,
                                chat_id=str(payload.get("chat_id") or ""), message_id=mid)
            return False

        if kind in ("message.reaction.new", "message.reaction.delete"):
            mid, reaction = str(payload.get("message_id") or ""), str(payload.get("reaction") or "")
            msg = store.get_message(mid) if mid else None
            if msg and reaction:
                extra = msg.get("extra") or {}
                counts = {r["reaction"]: r["count"] for r in extra.get("reactions", [])}
                counts[reaction] = max(0, counts.get(reaction, 0) + (1 if kind.endswith(".new") else -1))
                extra["reactions"] = [{"reaction": r, "count": n} for r, n in counts.items() if n > 0]
                if not extra["reactions"]:
                    extra.pop("reactions")
                store.set_message_extra(mid, extra)
                store.add_event(type="message.reaction", now=now, account_id=aid, chat_id=msg["chat_id"],
                                message_id=mid, data={"reaction": reaction, "added": kind.endswith(".new")})
            return False

        if kind == "chat.update":
            chat = parse_chat(payload, aid)
            if chat:
                store.upsert_chats([chat], now)
                store.add_event(type="chat.update", now=now, account_id=aid, chat_id=chat["id"])
            return False

        if kind == "chat.delete":
            cid = str(payload.get("id") or "")
            if cid:
                store.delete_chat(cid)
                store.add_event(type="chat.delete", now=now, account_id=aid, chat_id=cid)
            return False

        if kind in _ACCOUNT_DOWN or kind in _ACCOUNT_UP:
            a = store.account(aid)
            a["network"] = a["network"] or network
            if kind in _ACCOUNT_DOWN:
                a["status"] = _ACCOUNT_DOWN[kind]
                a["paused_until"] = max(a["paused_until"], now + HOUR)
                a["pause_reason"] = f"account status {a['status']}. Check it in the Unipile dashboard"
            else:
                a["status"] = "OK"
                if a["pause_reason"].startswith(("account disconnected", "account status")):
                    a["paused_until"], a["pause_reason"] = 0.0, ""
            store.save_account(a)
            store.set_meta("accounts_at", 0.0)  # re-read the account list on the next call
            store.add_event(type="account.status", now=now, account_id=aid, data={"event": kind})
            return kind in _ACCOUNT_UP
        return False


# ─── setup ───────────────────────────────────────────────────────────────────

def setup_webhook(inbox, url: str) -> dict:
    """Register ``url`` with Unipile for every event above and keep its secret. Replaces an
    endpoint registered earlier. Events of accounts not in settings are ignored on arrival, so
    adding an account later needs no new setup."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or len(url) > 255:
        raise InboxError("Give the public https:// address that forwards to this machine's webhook receiver,"
                         f" ending in {PATH}.", kind="input")
    store = inbox.store()
    old = store.get_meta("webhook") or {}
    endpoint = inbox._call("*", lambda: inbox.client().create_webhook(url, EVENTS, [], "unified-inbox (Hermes)"))
    if not endpoint.get("id") or not endpoint.get("secret"):
        raise InboxError("Unipile did not return a webhook id and secret. Nothing was changed.", kind="unavailable")
    store.set_meta("webhook", {"id": endpoint["id"], "url": url, "secret": endpoint["secret"],
                               "created_at": inbox.clock()})
    if old.get("id") and old["id"] != endpoint["id"]:
        try:
            inbox._call("*", lambda: inbox.client().delete_webhook(old["id"]))
        except InboxError:
            pass  # the new endpoint works; the old one can be removed in the Unipile dashboard
    return {"id": endpoint["id"], "url": url}


def remove_webhook(inbox) -> bool:
    store = inbox.store()
    hook = store.get_meta("webhook") or {}
    if not hook.get("id"):
        return False
    try:
        inbox._call("*", lambda: inbox.client().delete_webhook(hook["id"]))
    except InboxError as exc:
        if exc.status != 404:
            raise
    store.set_meta("webhook", {})
    return True


# ─── local receiver ──────────────────────────────────────────────────────────

class WebhookServer:
    """A minimal threaded HTTP receiver for Unipile deliveries. Answers within Unipile's
    5-second window: the work is a few local database writes."""

    def __init__(self, handler: WebhookHandler, host: str, port: int, path: str = PATH):
        self.handler, self.host, self.port, self.path = handler, host, port, path
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        assert self._server is not None
        return self._server.server_address[:2]

    def start(self) -> None:
        outer = self

        class _Request(BaseHTTPRequestHandler):
            timeout = 10  # a slow or stalled client cannot hold a thread

            def _reply(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                # Read the body before any answer: replying with unread data in the socket can
                # reset the connection on some systems, and the caller would never see the reply.
                try:
                    length = int(self.headers.get("Content-Length") or "")
                except ValueError:
                    self.close_connection = True
                    return self._reply(411, {"error": "length required"})
                if length < 0 or length > MAX_BODY:
                    self.close_connection = True
                    return self._reply(413, {"error": "too large"})
                raw = self.rfile.read(length)
                if self.path.split("?", 1)[0] != outer.path:
                    return self._reply(404, {"error": "not found"})
                try:
                    code, reason = outer.handler.handle(raw, _Headers(self.headers))
                except Exception:  # noqa: BLE001 - never leak details to the caller
                    code, reason = 500, "error"
                self._reply(code, {"status": reason})

            def do_GET(self) -> None:  # noqa: N802 - lets you test a tunnel with a browser
                if self.path.split("?", 1)[0] != outer.path:
                    return self._reply(404, {"error": "not found"})
                self._reply(200, {"status": "unified-inbox webhook receiver"})

            def log_message(self, *args: Any) -> None:  # keep message content out of logs
                pass

        self._server = ThreadingHTTPServer((self.host, self.port), _Request)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="unified-inbox-webhooks",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class _Headers:
    """Case-insensitive view of request headers."""

    def __init__(self, headers: Any):
        self._h = headers

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._h.get(key, default)
