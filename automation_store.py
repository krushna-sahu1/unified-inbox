"""Storage for automation (schema v5): scheduled messages, watches and alerts.

Mixed into ``store.Store``; uses its connection, lock and helpers.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Optional

_V5_TABLES = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    network TEXT NOT NULL,
    chat_name TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,                       -- exactly what the user approved
    send_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | sending | sent | held | failed | missed | cancelled | unknown
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    message_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules(status, send_at);
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                       -- reply | keyword | topic | no_reply | snooze
    chat_id TEXT NOT NULL DEFAULT '',
    network TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    until_ts REAL,                            -- no_reply / snooze: when it fires; others: when it ends
    anchor_ts REAL NOT NULL DEFAULT 0,        -- no_reply: replies after this count
    nudge INTEGER NOT NULL DEFAULT 0,         -- no_reply: prepare a follow-up draft
    status TEXT NOT NULL DEFAULT 'active',    -- active | done | removed | expired
    created_at REAL NOT NULL,
    fired_at REAL,
    fired_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS watches_active ON watches(status, kind, chat_id);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    read_at REAL,
    pushed_at REAL
);
CREATE INDEX IF NOT EXISTS notifications_unread ON notifications(read_at, id);
"""

_SCHEDULE_COLS = ("id,draft_id,chat_id,account_id,network,chat_name,text,send_at,status,reason,attempts,message_id,"
                  "created_at,updated_at")
_WATCH_COLS = "id,kind,chat_id,network,text,until_ts,anchor_ts,nudge,status,created_at,fired_at,fired_count"


class AutomationStore:
    """Automation queries. Expects ``self._conn``, ``self._lock``, ``self._q``, ``self._x``."""

    def _migrate_v5(self) -> None:
        self._conn.executescript(_V5_TABLES)
        self._add_column("drafts", "proposed_send_at", "REAL")  # type: ignore[attr-defined]

    # ── scheduled messages ──
    def add_schedule(self, *, draft: dict, send_at: float, now: float) -> str:
        sid = uuid.uuid4().hex[:6]
        self._x("INSERT INTO schedules(id,draft_id,chat_id,account_id,network,chat_name,text,send_at,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sid, draft["id"], draft["chat_id"], draft["account_id"], draft["network"], draft["chat_name"],
                 draft["text"], send_at, now, now))
        return sid

    def schedule(self, schedule_id: str) -> Optional[dict]:
        rows = self._q(f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE id=?", ((schedule_id or "").strip().lower(),))
        return _schedule(rows[0]) if rows else None

    def schedules(self, *, statuses: Iterable[str] = ("scheduled",), limit: int = 50) -> list[dict]:
        st = list(statuses)
        rows = self._q(f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE status IN ({','.join('?' * len(st))})"
                       " ORDER BY send_at LIMIT ?", (*st, limit))
        return [_schedule(r) for r in rows]

    def schedule_for_draft(self, draft_id: str) -> Optional[dict]:
        rows = self._q(f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE draft_id=? AND status IN ('scheduled','sending')",
                       (draft_id,))
        return _schedule(rows[0]) if rows else None

    def due_schedules(self, now: float) -> list[dict]:
        rows = self._q(f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE status='scheduled' AND send_at<=?"
                       " ORDER BY send_at", (now,))
        return [_schedule(r) for r in rows]

    def set_schedule(self, schedule_id: str, now: float, *, status: str, reason: str = "",
                     message_id: Optional[str] = None, attempt: bool = False, expect: Optional[str] = None) -> bool:
        """Update a schedule; with ``expect``, only if its status is still that (an atomic claim)."""
        sql = "UPDATE schedules SET status=?, reason=?, updated_at=?, attempts=attempts+?"
        params: list[Any] = [status, reason[:500], now, int(attempt)]
        if message_id is not None:
            sql += ", message_id=?"
            params.append(message_id)
        sql += " WHERE id=?"
        params.append(schedule_id)
        if expect:
            sql += " AND status=?"
            params.append(expect)
        return self._x(sql, params) > 0

    def next_schedule_at(self) -> Optional[float]:
        row = self._q("SELECT MIN(send_at) FROM schedules WHERE status='scheduled'")[0][0]
        return float(row) if row is not None else None

    def propose_send_at(self, draft_id: str, ts: Optional[float]) -> None:
        self._x("UPDATE drafts SET proposed_send_at=? WHERE id=?", (ts, draft_id))

    def proposed_send_at(self, draft_id: str) -> Optional[float]:
        rows = self._q("SELECT proposed_send_at FROM drafts WHERE id=?", (draft_id,))
        return float(rows[0][0]) if rows and rows[0][0] is not None else None

    # ── watches ──
    def add_watch(self, *, kind: str, now: float, chat_id: str = "", network: str = "", text: str = "",
                  until_ts: Optional[float] = None, anchor_ts: float = 0.0, nudge: bool = False) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO watches(kind,chat_id,network,text,until_ts,anchor_ts,nudge,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)", (kind, chat_id, network, text, until_ts, anchor_ts, int(nudge), now))
            self._conn.commit()
            return int(cur.lastrowid)

    def watches(self, *, kinds: Optional[Iterable[str]] = None, chat_id: Optional[str] = None,
                status: str = "active") -> list[dict]:
        sql, params = f"SELECT {_WATCH_COLS} FROM watches WHERE status=?", [status]
        if kinds:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        if chat_id:
            sql += " AND chat_id=?"
            params.append(chat_id)
        return [_watch(r) for r in self._q(sql + " ORDER BY id", params)]

    def watch(self, watch_id: int) -> Optional[dict]:
        rows = self._q(f"SELECT {_WATCH_COLS} FROM watches WHERE id=?", (watch_id,))
        return _watch(rows[0]) if rows else None

    def set_watch(self, watch_id: int, now: float, *, status: Optional[str] = None, fired: bool = False) -> None:
        sets, params = ["fired_count=fired_count+?"], [int(fired)]
        if fired:
            sets.append("fired_at=?")
            params.append(now)
        if status:
            sets.append("status=?")
            params.append(status)
        self._x(f"UPDATE watches SET {', '.join(sets)} WHERE id=?", (*params, watch_id))

    def next_watch_at(self) -> Optional[float]:
        row = self._q("SELECT MIN(until_ts) FROM watches WHERE status='active' AND until_ts IS NOT NULL")[0][0]
        return float(row) if row is not None else None

    def snoozed_chat_ids(self, now: float) -> set[str]:
        return {r[0] for r in self._q("SELECT chat_id FROM watches WHERE kind='snooze' AND status='active'"
                                      " AND until_ts>?", (now,))}

    # ── alerts ──
    def add_notification(self, *, kind: str, title: str, body: str, now: float, chat_id: str = "",
                         data: Optional[dict] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO notifications(ts,kind,title,body,chat_id,data) VALUES(?,?,?,?,?,?)",
                (now, kind, title[:200], body[:1000], chat_id, json.dumps(data or {}, ensure_ascii=False)))
            self._conn.commit()
            return int(cur.lastrowid)

    def notifications(self, *, unread_only: bool = True, unpushed_only: bool = False, limit: int = 50) -> list[dict]:
        sql = "SELECT id,ts,kind,title,body,chat_id,data,read_at,pushed_at FROM notifications WHERE 1=1"
        if unread_only:
            sql += " AND read_at IS NULL"
        if unpushed_only:
            sql += " AND pushed_at IS NULL"
        rows = self._q(sql + " ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": r[0], "ts": r[1], "kind": r[2], "title": r[3], "body": r[4], "chat_id": r[5],
                 "data": json.loads(r[6] or "{}"), "read_at": r[7], "pushed_at": r[8]} for r in reversed(rows)]

    def mark_notifications(self, ids: list[int], now: float, *, field: str = "read_at") -> None:
        assert field in ("read_at", "pushed_at")
        if ids:
            self._x(f"UPDATE notifications SET {field}=? WHERE id IN ({','.join('?' * len(ids))})", (now, *ids))


def _schedule(r: tuple) -> dict:
    return {"id": r[0], "draft_id": r[1], "chat_id": r[2], "account_id": r[3], "network": r[4], "chat_name": r[5],
            "text": r[6], "send_at": r[7], "status": r[8], "reason": r[9], "attempts": r[10], "message_id": r[11],
            "created_at": r[12], "updated_at": r[13]}


def _watch(r: tuple) -> dict:
    return {"id": r[0], "kind": r[1], "chat_id": r[2], "network": r[3], "text": r[4], "until_ts": r[5],
            "anchor_ts": r[6], "nudge": bool(r[7]), "status": r[8], "created_at": r[9], "fired_at": r[10],
            "fired_count": r[11]}
