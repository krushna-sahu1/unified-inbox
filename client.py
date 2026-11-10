"""The Unipile v2 backend.

In Unipile v2 every route is scoped to one connected account (``acc_...``): one WhatsApp
number, one Instagram login or one LinkedIn login. The client returns these shapes:
  account: {id, network, status, connected_at}
  chat:    {id, account_id, network, name, unread, ts, is_group, is_archived, is_muted, is_readonly,
            last_text, last_from_me}
  message: {id, chat_id, from_me, sender, sender_id, text, ts, deleted, edited, extra}
           (extra: attachments, quoted, reactions, mentioned; only keys that are present)

``parse_chat`` and ``parse_message`` are shared with the webhook handler, which receives the
same v2 objects.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib import error as urlerror
from urllib import parse, request

BASE_URL = "https://api.unipile.com/v2"
NETWORKS = ("whatsapp", "instagram", "linkedin")
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # a received file larger than this is opened in the app instead
_MAX_ACCOUNT_PAGES = 20  # 50 accounts per page -> up to 1000 connected accounts

# Unipile v2 account status -> our status. "degraded"/"partial" still have a running product.
_STATUS = {"running": "OK", "degraded": "OK", "partial": "OK", "errored": "ERRORED",
           "disconnected": "DISCONNECTED"}


class InboxError(Exception):
    """A backend error, with what the pacing layer needs to react to it."""

    def __init__(self, message: str, *, status: int = 0, kind: str = "", retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.retry_after = retry_after


def _ts(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _q(value: str) -> str:
    return parse.quote(str(value), safe="")


def _message_id(data: dict) -> str:
    mid = (data or {}).get("message_id")
    if isinstance(mid, list):
        mid = ",".join(str(x) for x in mid)
    return str(mid or "")


def iso(ts: float) -> str:
    """Unix seconds -> the ISO 8601 UTC form Unipile's before/after filters take."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ─── v2 object parsing (shared with webhooks.py) ─────────────────────────────

def parse_chat(c: dict, account_id: str) -> Optional[dict]:
    """A v2 Chat object -> our chat shape, or None for other providers."""
    network = str(c.get("provider") or "").lower()
    if network not in NETWORKS or not c.get("id"):
        return None
    unread = _int(c.get("unread_count"))
    muted = c.get("muted_until")
    last = c.get("last_message") or {}
    return {
        "id": c["id"], "account_id": account_id, "network": network,
        "name": c.get("name") or (c.get("user") or {}).get("display_name") or "Unknown contact",
        "unread": 1 if unread < 0 else unread,  # -1 = manually marked unread
        "ts": _ts(c.get("last_message_timestamp") or c.get("updated_at")),
        "is_group": c.get("type") == "group" or bool(c.get("is_group")),
        "is_archived": bool(c.get("is_archived")),
        "is_muted": muted is True or (isinstance(muted, str) and _ts(muted) > time.time()),
        "is_readonly": bool(c.get("is_readonly")),
        "last_text": str(last.get("text") or ""),
        "last_from_me": bool(last.get("is_sender")),
    }


def _attachment(a: dict) -> dict:
    out = {"type": str(a.get("type") or "file")}
    for key in ("id", "mimetype", "filename", "voice_note", "duration", "sticker", "gif", "media_type",
                "display_name", "file_size"):
        if a.get(key) not in (None, ""):
            out[key] = a[key]
    return out


def describe_attachment(a: dict) -> str:
    """A short readable stand-in for an attachment, e.g. "[voice note]" or "[file: cv.pdf]"."""
    kind = a.get("type")
    if kind == "audio":
        return "[voice note]" if a.get("voice_note") else "[audio]"
    if kind == "img":
        return "[sticker]" if a.get("sticker") else "[photo]"
    if kind == "video":
        return "[GIF]" if a.get("gif") else "[video]"
    if kind == "file":
        return f"[file: {a['filename']}]" if a.get("filename") else "[file]"
    if kind == "shared_content":
        return f"[shared {a.get('media_type') or 'content'}]"
    if kind == "contact_card":
        return f"[contact: {a['display_name']}]" if a.get("display_name") else "[contact]"
    return "[attachment]"


def parse_message(m: dict, chat_id: str) -> Optional[dict]:
    """A v2 Message object -> our message shape. Events (calls, joins...) and hidden messages
    give None. Deleted messages are returned with ``deleted`` so a cache can drop them."""
    if m.get("is_event") or m.get("is_hidden") or not m.get("id"):
        return None
    mine = bool(m.get("is_sender"))
    attachments = [_attachment(a) for a in (m.get("attachments") or []) if isinstance(a, dict)]
    text = str(m.get("text") or "") or " ".join(describe_attachment(a) for a in attachments)
    extra: dict = {}
    if attachments:
        extra["attachments"] = attachments
    quoted = m.get("quoted") or {}
    if quoted.get("id"):
        q = {"id": str(quoted["id"]), "text": str(quoted.get("text") or "")[:300]}
        q_sender = quoted.get("sender") if isinstance(quoted.get("sender"), dict) else {}
        if q_sender.get("display_name"):
            q["sender"] = str(q_sender["display_name"])
        if not q["text"] and quoted.get("attachments"):
            q["text"] = " ".join(describe_attachment(_attachment(a)) for a in quoted["attachments"]
                                 if isinstance(a, dict))
        extra["quoted"] = q
    reactions = [{"reaction": r.get("reaction"), "count": _int(r.get("count"))}
                 for r in (m.get("reactions_counter") or []) if isinstance(r, dict) and r.get("reaction")]
    if reactions:
        extra["reactions"] = reactions
    if m.get("is_mentionned") or m.get("is_mentioned"):
        extra["mentioned"] = True
    sender = m.get("sender") or {}
    return {"id": m["id"], "chat_id": m.get("chat_id") or chat_id, "from_me": mine,
            "sender": "you" if mine else str(sender.get("display_name") or ""),
            "sender_id": str(m.get("sender_id") or sender.get("id") or ""),
            "text": text, "ts": _ts(m.get("timestamp")),
            "deleted": bool(m.get("is_deleted")), "edited": bool(m.get("is_edited")), "extra": extra}


# ─── Unipile v2 ──────────────────────────────────────────────────────────────

class UnipileClient:
    """Minimal Unipile v2 client over HTTPS (stdlib only). Talks only to api.unipile.com.

    Makes one HTTP request per method call (list_accounts follows pagination, which is rare)
    and never retries; retry and pause decisions belong to the pacing layer (inbox.py)."""

    def __init__(self, api_key: str, timeout: float = 30.0, base_url: str = BASE_URL):
        self.base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        # Unipile's x-ratelimit-* headers from the last accepted response, if it sent them.
        self.last_rate: Optional[dict] = None

    def _call(self, method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None) -> dict:
        raw, _ = self._request(method, path, query, body)
        return json.loads(raw) if raw else {}

    def _request(self, method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None, *,
                 accept: str = "application/json", max_bytes: Optional[int] = None) -> tuple[bytes, str]:
        """One HTTP request; returns (body, content type). ``max_bytes`` caps a download."""
        url = self.base + path
        if query:
            url += "?" + parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        headers = {"X-API-KEY": self._key, "Accept": accept}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=self._timeout) as resp:
                headers = getattr(resp, "headers", None)
                self._note_rate(headers)
                ctype = (headers.get("Content-Type") or "") if headers else ""
                if max_bytes is None:
                    return resp.read(), ctype
                raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise InboxError(f"The file is larger than {max_bytes // 1048576} MB: open it in the app.",
                                     kind="too_large")
                return raw, ctype
        except urlerror.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            finally:
                exc.close()  # the error response holds the connection open until closed
            kind = str(payload.get("type") or "").split("/")[-1]
            detail = payload.get("detail") or payload.get("title") or exc.reason
            retry = exc.headers.get("retry-after") if exc.headers else None
            try:
                retry_after = float(retry) if retry else None
            except ValueError:
                retry_after = None
            if retry_after is not None and not 0 <= retry_after <= 86400:
                # nan, infinite, negative or absurd: never pause an account for ever on a bad header
                retry_after = None if not retry_after > 0 else 86400.0
            raise InboxError(f"Unipile {exc.code}: {detail}", status=exc.code, kind=kind,
                             retry_after=retry_after) from None
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise InboxError(f"Could not reach Unipile: {exc}", status=0, kind="network") from None

    def _note_rate(self, headers: Any) -> None:
        if not headers or headers.get("x-ratelimit-remaining") is None:
            return
        try:
            self.last_rate = {"remaining": int(headers.get("x-ratelimit-remaining")),
                              "reset": float(headers.get("x-ratelimit-reset") or 0)}
        except (TypeError, ValueError):
            self.last_rate = None

    def list_accounts(self) -> list[dict]:
        """Every connected WhatsApp, Instagram and LinkedIn account, following pagination."""
        raw: list[dict] = []
        cursor: Optional[str] = None
        for _ in range(_MAX_ACCOUNT_PAGES):
            data = self._call("GET", "/accounts/", {"limit": "50", "cursor": cursor})
            raw.extend(data.get("data", []))
            cursor = data.get("next_cursor") or None
            if not cursor:
                break
        out = []
        for a in raw:
            network = str(a.get("provider") or "").lower()
            if network not in NETWORKS:
                continue
            status = "LOCKED" if a.get("is_locked") else _STATUS.get(str(a.get("status") or "").lower(), "ERRORED")
            out.append({"id": a.get("id"), "network": network, "status": status,
                        "connected_at": _ts(a.get("created_at"))})
        return out

    def list_chats(self, account_id: str, *, limit: int, cursor: Optional[str] = None,
                   unread_only: bool = False) -> tuple[list[dict], Optional[str]]:
        """One page of chats, most recent activity first."""
        data = self._call("GET", f"/{_q(account_id)}/chats",
                          {"limit": limit, "cursor": cursor, "is_unread": "true" if unread_only else None})
        chats = [c for c in (parse_chat(x, account_id) for x in data.get("data", [])) if c]
        return chats, data.get("next_cursor") or None

    def get_chat(self, account_id: str, chat_id: str) -> Optional[dict]:
        return parse_chat(self._call("GET", f"/{_q(account_id)}/chats/{_q(chat_id)}"), account_id)

    def list_messages_page(self, account_id: str, chat_id: str, *, limit: int, after: Optional[float] = None,
                           cursor: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
        """One page of messages, newest first, including deleted ones (flagged). ``after`` is an
        exclusive Unix-time lower bound."""
        data = self._call("GET", f"/{_q(account_id)}/chats/{_q(chat_id)}/messages",
                          {"limit": limit, "cursor": cursor, "after": iso(after) if after else None})
        msgs = [m for m in (parse_message(x, chat_id) for x in data.get("data", [])) if m]
        return msgs, data.get("next_cursor") or None

    def list_messages(self, account_id: str, chat_id: str, *, limit: int) -> list[dict]:
        """The latest messages, oldest first, without deleted ones."""
        msgs, _ = self.list_messages_page(account_id, chat_id, limit=limit)
        return sorted((m for m in msgs if not m["deleted"]), key=lambda m: m["ts"])

    def send_message(self, account_id: str, chat_id: str, text: str, attachments: Optional[list] = None,
                     quote_id: str = "") -> str:
        """Send in an existing chat. ``attachments``: [{filename, content (base64), content_type}].
        (WhatsApp delivers each file as its own message, then the text.) ``quote_id``: the message
        this one replies to."""
        body: dict = {"text": text}
        if attachments:
            body["attachments"] = attachments
        if quote_id:
            body["quote_id"] = quote_id
        data = self._call("POST", f"/{_q(account_id)}/chats/{_q(chat_id)}/messages/send", body=body)
        return _message_id(data)

    def start_chat(self, account_id: str, user_id: str, text: str, attachments: Optional[list] = None) -> dict:
        """First message to someone with no chat yet (v2 POST /{account}/chats/send).
        Returns {chat_id, message_id}."""
        body: dict = {"users_ids": user_id, "text": text}
        if attachments:
            body["attachments"] = attachments
        data = self._call("POST", f"/{_q(account_id)}/chats/send", body=body)
        return {"chat_id": str(data.get("chat_id") or ""), "message_id": _message_id(data)}

    def get_user(self, account_id: str, user_id: str) -> dict:
        """A profile: a WhatsApp number (``<digits>@s.whatsapp.net``), a LinkedIn public id, or a
        provider id. 404 means no such user (for WhatsApp: the number is not on WhatsApp)."""
        data = self._call("GET", f"/{_q(account_id)}/users/{_q(user_id)}")
        spec = data.get("specifics") or {}
        return {"id": str(data.get("id") or ""), "name": str(data.get("display_name") or ""),
                "messaging_id": str(spec.get("messaging_identifier") or data.get("id") or "")}

    def download_attachment(self, account_id: str, chat_id: str, message_id: str, attachment_id: str,
                            max_bytes: int = MAX_DOWNLOAD_BYTES) -> tuple[bytes, str]:
        """A received file: (its bytes, the content type Unipile reports)."""
        return self._request("GET", f"/{_q(account_id)}/chats/{_q(chat_id)}/messages/{_q(message_id)}"
                                    f"/attachments/{_q(attachment_id)}", accept="*/*", max_bytes=max_bytes)

    def react(self, account_id: str, chat_id: str, message_id: str, reaction: str, *, remove: bool = False) -> None:
        """Add (or remove your own) emoji reaction on a message."""
        self._call("DELETE" if remove else "POST",
                   f"/{_q(account_id)}/chats/{_q(chat_id)}/messages/{_q(message_id)}/reactions",
                   body={"reaction": reaction})

    def mark_chat_read(self, account_id: str, chat_id: str) -> None:
        """Mark the whole chat as read (the other person may see read receipts)."""
        self._call("PATCH", f"/{_q(account_id)}/chats/{_q(chat_id)}", body={"read_status": True})

    # ── connecting apps (v2 hosted auth) ──
    def create_auth_link(self, *, redirect_uri: str, expires_on: float, account_id: str = "") -> str:
        """A Unipile page where the user connects an app (WhatsApp shows a QR or pairing code;
        Instagram and LinkedIn a login). With ``account_id`` it reconnects that account."""
        body: dict = {"expires_on": iso(expires_on), "redirect_uri": redirect_uri}
        if account_id:
            body["account_id"] = account_id
        else:
            body["providers"] = "*:MESSAGING"
        data = self._call("POST", "/auth/link", body=body)
        link = data.get("link") or data.get("url") or ""
        if not link:
            raise InboxError("Unipile did not return a connect link.", kind="unavailable")
        return str(link)

    # ── webhooks (v2: /webhooks/endpoints) ──
    def create_webhook(self, url: str, events: list[str], account_ids: list[str], description: str = "") -> dict:
        """Returns the endpoint, including its signing ``secret``."""
        body: dict = {"url": url, "trigger_events": events}
        if account_ids:  # omitted = every account of the Unipile application
            body["account_ids"] = account_ids
        if description:
            body["description"] = description[:255]
        return self._call("POST", "/webhooks/endpoints/", body=body)

    def list_webhooks(self) -> list[dict]:
        out: list[dict] = []
        for page in range(20):
            data = self._call("GET", "/webhooks/endpoints/", {"limit": "50", "offset": str(page * 50)})
            out.extend(data.get("data", []))
            if not data.get("has_more"):
                break
        return out

    def delete_webhook(self, endpoint_id: str) -> None:
        self._call("DELETE", f"/webhooks/endpoints/{_q(endpoint_id)}")
