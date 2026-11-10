"""Storage for plans found in chats (calendar) and message translations. Mixed into Store.

Both stay on this machine (they are not part of the optional cloud copy): a plan or a
translation can always be found again from the messages themselves.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# v10: plans found in chats, translations of messages, and the original of a translated draft.
V10 = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    from_me INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0,
    approximate INTEGER NOT NULL DEFAULT 0,  -- "Thursday evening": the time is a guess
    phrase TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'suggested', -- suggested | added | dismissed
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(message_id)
);
CREATE INDEX IF NOT EXISTS plans_start ON plans(start_ts);
CREATE INDEX IF NOT EXISTS plans_chat ON plans(chat_id);
CREATE TABLE IF NOT EXISTS translations (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',          -- the language it was written in
    target TEXT NOT NULL,                     -- the language it was translated into
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS translations_chat ON translations(chat_id);
"""

_PLAN_COLS = ("id,chat_id,message_id,from_me,title,start_ts,end_ts,all_day,approximate,phrase,status,created_at,"
              "updated_at")


def _plan(r: tuple) -> dict:
    return {"id": r[0], "chat_id": r[1], "message_id": r[2], "from_me": bool(r[3]), "title": r[4],
            "start_ts": r[5], "end_ts": r[6], "all_day": bool(r[7]), "approximate": bool(r[8]), "phrase": r[9],
            "status": r[10], "created_at": r[11], "updated_at": r[12]}


class InsightStore:
    # ── plans ──
    def add_plan(self, *, chat_id: str, message_id: str, from_me: bool, title: str, start_ts: float, end_ts: float,
                 all_day: bool, approximate: bool, phrase: str, now: float) -> Optional[int]:
        """Save a plan found in a message; None if that message already has one."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO plans(chat_id,message_id,from_me,title,start_ts,end_ts,all_day,approximate,"
                "phrase,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'suggested',?,?)",
                (chat_id, message_id, int(from_me), title[:200], start_ts, end_ts, int(all_day), int(approximate),
                 phrase[:300], now, now))
            self._conn.commit()
            return int(cur.lastrowid) if cur.rowcount else None

    def plan(self, plan_id: Any) -> Optional[dict]:
        try:
            pid = int(plan_id)
        except (TypeError, ValueError):
            return None
        rows = self._q(f"SELECT {_PLAN_COLS} FROM plans WHERE id=?", (pid,))
        return _plan(rows[0]) if rows else None

    def plans(self, *, since: float, until: Optional[float] = None, chat_id: str = "",
              statuses: Iterable[str] = ("suggested", "added"), limit: int = 100) -> list[dict]:
        """Plans that end after ``since`` (and start before ``until``), soonest first."""
        statuses = list(statuses)
        sql = f"SELECT {_PLAN_COLS} FROM plans WHERE end_ts>? AND status IN ({','.join('?' * len(statuses))})"
        params: list = [since, *statuses]
        if until is not None:
            sql += " AND start_ts<?"
            params.append(until)
        if chat_id:
            sql += " AND chat_id=?"
            params.append(chat_id)
        return [_plan(r) for r in self._q(sql + " ORDER BY start_ts LIMIT ?", (*params, limit))]

    def set_plan(self, plan_id: int, now: float, **fields: Any) -> bool:
        allowed = {"status", "title", "start_ts", "end_ts", "all_day", "approximate"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return False
        sets = ", ".join(f"{k}=?" for k in cols)
        return self._x(f"UPDATE plans SET {sets}, updated_at=? WHERE id=?", (*cols.values(), now, plan_id)) > 0

    def prune_plans(self, before: float) -> int:
        """Plans that ended before ``before``, and suggestions whose message is gone."""
        with self._lock:
            n = self._conn.execute("DELETE FROM plans WHERE end_ts<?", (before,)).rowcount
            n += self._conn.execute("DELETE FROM plans WHERE status='suggested' AND message_id NOT IN"
                                    " (SELECT id FROM messages)").rowcount
            self._conn.commit()
        return n

    # ── translations ──
    def translations_for(self, message_ids: Iterable[str]) -> dict[str, dict]:
        ids = [i for i in message_ids if i]
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self._q(f"SELECT message_id, source, target, text FROM translations WHERE message_id IN"
                             f" ({','.join('?' * len(part))})", part):
                out[r[0]] = {"source": r[1], "target": r[2], "text": r[3]}
        return out

    def save_translations(self, chat_id: str, items: list[tuple[str, str, str, str]], now: float) -> None:
        """items: (message_id, source language, target language, text)."""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO translations(message_id,chat_id,source,target,text,created_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(message_id) DO UPDATE SET source=excluded.source, target=excluded.target,"
                " text=excluded.text, created_at=excluded.created_at",
                [(mid, chat_id, src, tgt, text, now) for mid, src, tgt, text in items])
            self._conn.commit()

    def prune_translations(self) -> int:
        """Translations of messages no longer kept (or edited since: translated again on demand)."""
        return self._x("DELETE FROM translations WHERE message_id NOT IN (SELECT id FROM messages)")

    def forget_chat_insights(self, chat_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM plans WHERE chat_id=?", (chat_id,))
            self._conn.execute("DELETE FROM translations WHERE chat_id=?", (chat_id,))
            self._conn.commit()

    def set_draft_original(self, draft_id: str, original: str) -> None:
        self._x("UPDATE drafts SET original_text=? WHERE id=?", (original, draft_id))
