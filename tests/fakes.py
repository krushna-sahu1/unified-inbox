"""Test-only stand-in for UnipileClient, backed by tests/fixtures/sample_chats.json.

The plugin itself has no sample mode: users always connect real accounts. These fictional
chats exist only so the whole read → triage → draft → send flow can be tested offline.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable, Optional

from unified_inbox.client import InboxError

SAMPLE_FILE = Path(__file__).resolve().parent / "fixtures" / "sample_chats.json"
SAMPLE_ACCOUNT_IDS = "mock-wa, mock-ig, mock-li"


class SampleBackend:
    """Same method signatures and return shapes as UnipileClient. Times in the fixture are
    relative ("minutes_ago"), so the chats always look fresh. Sends are kept in memory."""

    def __init__(self, clock: Callable[[], float], path: Path = SAMPLE_FILE):
        self._clock = clock
        self._data = json.loads(path.read_text(encoding="utf-8"))
        self._loaded_at = clock()
        self.sent: list[dict] = []  # sends and test-injected messages
        self.calls: list[str] = []
        self.calls_detail: list[tuple] = []
        self.groups: set[str] = set()       # chats that are groups (make_group)
        self.read_chats: set[str] = set()   # chats marked read
        self.files: dict[str, bytes] = {}   # attachment id -> bytes served by download_attachment
        self.reactions: list[tuple] = []    # (chat_id, message_id, emoji, removed)
        self.refused_emojis: set[str] = set()
        self.quotes: list[tuple] = []       # (chat_id, quote_id) of sends that quoted a message
        self.users: dict[str, dict] = {}    # get_user answers by identifier

    def _at(self, minutes_ago: float) -> float:
        return self._loaded_at - float(minutes_ago) * 60

    def list_accounts(self) -> list[dict]:
        self.calls.append("accounts")
        return [{"id": a["id"], "network": a["network"], "status": a.get("status", "OK"),
                 "connected_at": self._at(a.get("connected_hours_ago", 720) * 60)}
                for a in self._data["accounts"]]

    def _messages(self, chat: dict) -> list[dict]:
        msgs = [{"id": f"{chat['id']}-{i}", "chat_id": chat["id"], "from_me": bool(m.get("from_me")),
                 "sender": "you" if m.get("from_me") else chat["name"], "text": m["text"],
                 "ts": self._at(m["minutes_ago"])} for i, m in enumerate(chat["messages"])]
        msgs += [dict(m) for m in self.sent if m["chat_id"] == chat["id"]]
        return sorted(msgs, key=lambda m: m["ts"])

    def _chat(self, account_id: str, chat_id: str) -> dict:
        for c in self._data["chats"]:
            if c["id"] == chat_id and c["account_id"] == account_id:
                return c
        raise InboxError("Chat not found.", status=404, kind="resource_not_found")

    def list_chats(self, account_id: str, *, limit: int, cursor: Optional[str] = None,
                   unread_only: bool = False) -> tuple[list[dict], Optional[str]]:
        self.calls.append("chats")
        networks = {a["id"]: a["network"] for a in self._data["accounts"]}
        chats = []
        for c in self._data["chats"]:
            if c["account_id"] != account_id:
                continue
            msgs = self._messages(c)
            unread = 0
            for m in reversed(msgs):
                if m["from_me"] or c["id"] in self.read_chats:
                    break
                unread += 1
            if unread_only and not unread:
                continue
            chats.append({"id": c["id"], "account_id": account_id, "network": networks[account_id],
                          "name": c["name"], "unread": unread, "ts": msgs[-1]["ts"] if msgs else 0.0,
                          "is_group": c["id"] in self.groups})
        chats.sort(key=lambda c: c["ts"], reverse=True)
        start = int(cursor or 0)
        page = chats[start:start + limit]
        nxt = str(start + limit) if start + limit < len(chats) else None
        return page, nxt

    def list_messages(self, account_id: str, chat_id: str, *, limit: int) -> list[dict]:
        self.calls.append("messages")
        return [m for m in self._messages(self._chat(account_id, chat_id)) if not m.get("deleted")][-limit:]

    def list_messages_page(self, account_id: str, chat_id: str, *, limit: int, after: Optional[float] = None,
                           cursor: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
        self.calls.append("messages")
        self.calls_detail.append(("messages", chat_id, after))
        msgs = [m for m in self._messages(self._chat(account_id, chat_id)) if after is None or m["ts"] > after]
        msgs.reverse()  # newest first, like Unipile
        start = int(cursor or 0)
        nxt = str(start + limit) if start + limit < len(msgs) else None
        return msgs[start:start + limit], nxt

    def get_chat(self, account_id: str, chat_id: str) -> Optional[dict]:
        self.calls.append("chat")
        for c in self.list_chats(account_id, limit=1000)[0]:
            if c["id"] == chat_id:
                self.calls.pop()  # the inner list_chats is not a separate request
                return c
        return None

    # ── test helpers: things happening on the phone ──
    def receive(self, chat_id: str, text: str, *, from_me: bool = False, sender: str = "", sender_id: str = "",
                attachments: Optional[list] = None, mentioned: bool = False, quoted: Optional[dict] = None) -> dict:
        """A new message arrives in ``chat_id`` now (optionally with files, a quote or a mention)."""
        chat = next(c for c in self._data["chats"] if c["id"] == chat_id)
        mid = f"in-{uuid.uuid4().hex[:8]}"
        extra: dict = {}
        if attachments:
            extra["attachments"] = attachments
        if mentioned:
            extra["mentioned"] = True
        if quoted:
            extra["quoted"] = quoted
        msg = {"id": mid, "chat_id": chat_id, "from_me": from_me,
               "sender": "you" if from_me else (sender or chat["name"]), "sender_id": sender_id,
               "text": text, "ts": self._clock(), "extra": extra}
        self.sent.append(msg)
        return msg

    def make_group(self, chat_id: str) -> None:
        self.groups.add(chat_id)

    def download_attachment(self, account_id: str, chat_id: str, message_id: str, attachment_id: str,
                            max_bytes: int = 25 * 1024 * 1024) -> tuple:
        self.calls.append("download")
        self._chat(account_id, chat_id)
        if attachment_id not in self.files:
            raise InboxError("Unipile 404: Attachment not found", status=404, kind="resource_not_found")
        data = self.files[attachment_id]
        if len(data) > max_bytes:
            raise InboxError("The file is larger than the limit: open it in the app.", kind="too_large")
        return data, "application/octet-stream"

    def react(self, account_id: str, chat_id: str, message_id: str, reaction: str, *, remove: bool = False) -> None:
        self.calls.append("react")
        self._chat(account_id, chat_id)
        if reaction in self.refused_emojis:
            raise InboxError("Unipile 400: This reaction is not supported", status=400, kind="invalid_parameters")
        self.reactions.append((chat_id, message_id, reaction, remove))

    def mark_chat_read(self, account_id: str, chat_id: str) -> None:
        self.calls.append("read")
        self._chat(account_id, chat_id)
        self.read_chats.add(chat_id)

    def get_user(self, account_id: str, user_id: str) -> dict:
        self.calls.append("user")
        if user_id not in self.users:
            raise InboxError("Unipile 404: User not found", status=404, kind="resource_not_found")
        return self.users[user_id]

    def delete(self, message_id: str) -> None:
        for m in self.sent:
            if m["id"] == message_id:
                m["deleted"] = True

    def send_message(self, account_id: str, chat_id: str, text: str, attachments: Optional[list] = None,
                     quote_id: str = "") -> str:
        self.calls.append("send")
        self._chat(account_id, chat_id)
        if quote_id:
            self.quotes.append((chat_id, quote_id))
        mid = "sent-" + uuid.uuid4().hex[:8]
        self.sent.append({"id": mid, "chat_id": chat_id, "from_me": True, "sender": "you", "text": text,
                          "ts": self._clock()})
        return mid
