"""Local side of the optional Supabase sync (schema v6).

SQLite stays the working database. When cloud sync is on, triggers record every change to the
synced tables in ``sync_outbox``; the cloud engine (cloud.py) pushes those rows to the user's
Supabase and pulls other devices' changes back, applying them with the triggers paused.

Rows need ids that mean the same thing on every device. Tables keyed by Unipile ids or text ids
use those; tables with local integer ids get a random ``gid`` (set by a trigger on insert).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class Spec:
    table: str
    key: str                     # the cross-device key column ("id", "chat_id" or "gid")
    cols: tuple[str, ...]        # columns that sync (the key first)
    watch: tuple[str, ...] = ()  # on UPDATE, only changes to these count (default: all synced columns)
    types: dict = field(default_factory=dict)  # Postgres types for non-text columns
    # Columns added in Supabase schema 2 (they are in ``cols`` too). A project still on schema 1
    # does not have them, so they are left out of what is sent there until its tables are updated.
    v2: tuple[str, ...] = ()

    @property
    def gid(self) -> bool:
        return self.key == "gid"


_R = "double precision"
_I = "bigint"
_T = "text not null default ''"

# Dependency order: rows are applied in this order when pulled.
SPECS: tuple[Spec, ...] = (
    Spec("accounts", "id", ("id", "network", "status", "paused_until", "pause_reason", "consecutive_429"),
         types={"paused_until": _R, "consecutive_429": _I}),
    Spec("chats", "id", ("id", "account_id", "network", "name", "unread", "ts", "is_group", "is_archived", "is_muted",
                         "is_readonly", "last_text", "last_from_me"),
         types={"unread": _I, "ts": _R, "is_group": _I, "is_archived": _I, "is_muted": _I, "is_readonly": _I,
                "last_from_me": _I}),
    Spec("messages", "id", ("id", "chat_id", "from_me", "sender", "text", "ts", "sender_id", "is_edited", "extra"),
         watch=("text", "extra", "is_edited", "sender"), types={"from_me": _I, "ts": _R, "is_edited": _I}),
    Spec("contacts", "id", ("id", "name", "created_at", "updated_at", "name_source"),
         types={"created_at": _R, "updated_at": _R, "name_source": "text not null default 'chat'"},
         v2=("name_source",)),
    Spec("contact_chats", "chat_id", ("chat_id", "contact_id", "network")),
    Spec("chat_context", "chat_id", ("chat_id", "summary", "memory_ts", "language", "updated_at"),
         watch=("summary", "memory_ts", "language"), types={"memory_ts": _R, "updated_at": _R}),
    Spec("chunks", "gid", ("gid", "chat_id", "start_ts", "end_ts", "msg_count", "text", "hash", "updated_at"),
         types={"start_ts": _R, "end_ts": _R, "msg_count": _I, "updated_at": _R}),
    Spec("drafts", "id", ("id", "chat_id", "account_id", "network", "chat_name", "text", "status", "error", "created_at",
                          "updated_at", "proposed_send_at", "reply_to", "origin", "original_text"),
         types={"created_at": _R, "updated_at": _R, "proposed_send_at": _R, "reply_to": _T, "origin": _T,
                "original_text": _T},
         v2=("reply_to", "origin", "original_text")),
    Spec("schedules", "id", ("id", "draft_id", "chat_id", "account_id", "network", "chat_name", "text", "send_at",
                             "status", "reason", "attempts", "message_id", "created_at", "updated_at"),
         types={"send_at": _R, "attempts": _I, "created_at": _R, "updated_at": _R}),
    Spec("send_log", "gid", ("gid", "draft_id", "chat_id", "account_id", "network", "text", "message_id", "sent_at",
                             "started"),
         types={"sent_at": _R, "started": "bigint not null default 0"}, v2=("started",)),
    Spec("facts", "gid", ("gid", "contact_id", "text", "category", "status", "source", "chat_id", "message_id",
                          "confidence", "created_at", "updated_at"),
         types={"confidence": _R, "created_at": _R, "updated_at": _R}),
    Spec("loops", "gid", ("gid", "chat_id", "kind", "text", "due_ts", "status", "message_id", "created_at", "updated_at",
                          "closed_at", "close_reason"),
         types={"due_ts": _R, "created_at": _R, "updated_at": _R, "closed_at": _R}),
    Spec("questions", "gid", ("gid", "kind", "text", "data", "status", "dedupe_key", "answer", "created_at",
                              "answered_at"), types={"created_at": _R, "answered_at": _R}),
    Spec("watches", "gid", ("gid", "kind", "chat_id", "network", "text", "until_ts", "anchor_ts", "nudge", "status",
                            "created_at", "fired_at", "fired_count"),
         types={"until_ts": _R, "anchor_ts": _R, "nudge": _I, "created_at": _R, "fired_at": _R, "fired_count": _I}),
    Spec("notifications", "gid", ("gid", "ts", "kind", "title", "body", "chat_id", "data", "read_at"),
         watch=("read_at",), types={"ts": _R, "read_at": _R}),
)
SPEC = {s.table: s for s in SPECS}

_V6 = """
CREATE TABLE IF NOT EXISTS sync_outbox (
    tbl TEXT NOT NULL, k TEXT NOT NULL, op TEXT NOT NULL, gid TEXT, ts REAL NOT NULL, PRIMARY KEY(tbl, k));
CREATE TABLE IF NOT EXISTS sync_flag (id INTEGER PRIMARY KEY CHECK (id = 1), applying INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO sync_flag(id, applying) VALUES (1, 0);
"""


def _local_key(spec: Spec) -> str:
    """The column the outbox uses to find the row locally."""
    return "id" if spec.gid else spec.key


class CloudStore:
    """Cloud-sync queries. Expects ``self._conn``, ``self._lock``, ``self._q``, ``self._x``, ``self._columns``."""

    _conn: sqlite3.Connection

    def _migrate_v6(self) -> None:
        c = self._conn
        c.executescript(_V6)
        for spec in SPECS:
            if spec.gid:
                self._add_column(spec.table, "gid", "TEXT")  # type: ignore[attr-defined]
                c.execute(f"UPDATE {spec.table} SET gid=lower(hex(randomblob(16))) WHERE gid IS NULL")
                c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {spec.table}_gid ON {spec.table}(gid)")
                c.execute(f"CREATE TRIGGER IF NOT EXISTS {spec.table}_gid_ai AFTER INSERT ON {spec.table}"
                          f" WHEN NEW.gid IS NULL BEGIN UPDATE {spec.table} SET gid=lower(hex(randomblob(16)))"
                          f" WHERE rowid=NEW.rowid; END;")

    # ── change capture ──
    def outbox_enabled(self) -> bool:
        return bool(self._q("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='sync_chats_ai'"))

    def enable_outbox(self, *, seed: bool) -> None:
        """Start recording changes. ``seed`` queues every existing row (the first full copy)."""
        with self._lock:
            c = self._conn
            for spec in SPECS:
                t, k = spec.table, _local_key(spec)
                gid_new = "NEW.gid" if spec.gid else "NULL"
                gid_old = "OLD.gid" if spec.gid else "NULL"
                when = "WHEN (SELECT applying FROM sync_flag WHERE id=1)=0"
                of = f" OF {', '.join(spec.watch)}" if spec.watch else ""
                # An upsert, not INSERT OR REPLACE: inside a trigger SQLite applies the outer
                # statement's conflict policy, which would turn REPLACE into an error when the
                # change itself came from an upsert.
                # ts is a counter, not a clock: a change made while a push is in flight always
                # gets a higher ts than the one the push read, so it is never dropped as "done".
                put = ("INSERT INTO sync_outbox(tbl,k,op,gid,ts) VALUES('{t}', {key}, '{op}', {gid},"
                       " (SELECT COALESCE(MAX(ts), 0) + 1 FROM sync_outbox))"
                       " ON CONFLICT(tbl, k) DO UPDATE SET op=excluded.op, gid=excluded.gid, ts=excluded.ts;")
                for suffix, event, key, op, gid in (("ai", "INSERT", f"NEW.{k}", "u", gid_new),
                                                    ("au", f"UPDATE{of}", f"NEW.{k}", "u", gid_new),
                                                    ("ad", "DELETE", f"OLD.{k}", "d", gid_old)):
                    c.execute(f"DROP TRIGGER IF EXISTS sync_{t}_{suffix}")
                    c.execute(f"CREATE TRIGGER sync_{t}_{suffix} AFTER {event} ON {t} {when} BEGIN "
                              + put.format(t=t, key=key, op=op, gid=gid) + " END;")
                if seed:
                    c.execute(f"INSERT INTO sync_outbox(tbl,k,op,gid,ts) SELECT '{t}', {k}, 'u',"
                              f" {'gid' if spec.gid else 'NULL'}, 0 FROM {t} WHERE true"
                              " ON CONFLICT(tbl, k) DO UPDATE SET op=excluded.op, ts=excluded.ts")
            c.commit()

    def disable_outbox(self) -> None:
        with self._lock:
            c = self._conn
            for spec in SPECS:
                for suffix in ("ai", "au", "ad"):
                    c.execute(f"DROP TRIGGER IF EXISTS sync_{spec.table}_{suffix}")
            c.execute("DELETE FROM sync_outbox")
            c.commit()

    def requeue_outbox(self, tables: Iterable[str]) -> None:
        """Queue every row of these tables to be sent again (after the Supabase tables gained
        columns, so what is already there gets them too)."""
        if not self.outbox_enabled():
            return
        with self._lock:
            c = self._conn
            for t in tables:
                spec = SPEC[t]
                c.execute(f"INSERT INTO sync_outbox(tbl,k,op,gid,ts) SELECT '{t}', {_local_key(spec)}, 'u',"
                          f" {'gid' if spec.gid else 'NULL'}, (SELECT COALESCE(MAX(ts), 0) + 1 FROM sync_outbox)"
                          f" FROM {t} WHERE true ON CONFLICT(tbl, k) DO UPDATE SET op=excluded.op, ts=excluded.ts")
            c.commit()

    def outbox_count(self) -> int:
        return int(self._q("SELECT COUNT(*) FROM sync_outbox")[0][0])

    def outbox_batch(self, table: str, limit: int) -> list[tuple[str, str, Optional[str], float]]:
        return [(r[0], r[1], r[2], r[3]) for r in
                self._q("SELECT k, op, gid, ts FROM sync_outbox WHERE tbl=? ORDER BY ts LIMIT ?", (table, limit))]

    def outbox_done(self, table: str, items: Iterable[tuple[str, float]]) -> None:
        """Forget pushed changes, unless the row changed again meanwhile (a newer ts)."""
        with self._lock:
            self._conn.executemany("DELETE FROM sync_outbox WHERE tbl=? AND k=? AND ts<=?",
                                   [(table, k, ts) for k, ts in items])
            self._conn.commit()

    def export_rows(self, table: str, keys: list[str]) -> list[dict]:
        """Current values of the synced columns for these local keys."""
        spec = SPEC[table]
        if not keys:
            return []
        cols = list(spec.cols)
        rows = self._q(f"SELECT {', '.join(cols)} FROM {table} WHERE {_local_key(spec)} IN"
                       f" ({','.join('?' * len(keys))})", keys)
        return [dict(zip(cols, r)) for r in rows]

    def chunk_vectors_for(self, gids: list[str]) -> dict[str, tuple[str, int, bytes]]:
        if not gids:
            return {}
        rows = self._q("SELECT ch.gid, v.model, v.dims, v.vec FROM chunks ch JOIN chunk_vectors v ON v.chunk_id=ch.id"
                       f" WHERE ch.gid IN ({','.join('?' * len(gids))})", gids)
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    # ── applying other devices' changes ──
    def apply_remote(self, table: str, rows: list[dict], now: float) -> int:
        """Write pulled rows (or delete tombstones) without re-queuing them for push. Messages go
        through ``apply_remote_messages`` instead. A row that clashes with a local one (e.g. the
        same question asked on two devices) is skipped."""
        spec = SPEC[table]
        n = 0
        with self._lock:
            c = self._conn
            c.execute("UPDATE sync_flag SET applying=1 WHERE id=1")
            try:
                for row in rows:
                    key = row.get(spec.key)
                    if key is None:
                        continue
                    if row.get("deleted"):
                        n += c.execute(f"DELETE FROM {table} WHERE {spec.key}=?", (key,)).rowcount
                        continue
                    values = {col: row.get(col) for col in spec.cols if col in row}
                    for col in spec.v2:  # written by a device that does not know this column yet
                        if values.get(col, 0) is None:
                            values.pop(col)
                    if table == "chats":  # its messages are pulled too: nothing to backfill
                        values["fetched_at"] = now
                        values["synced_ts"] = values.get("ts") or 0
                    cols = list(values)
                    updates = ", ".join(f"{col}=excluded.{col}" for col in cols if col != spec.key)
                    try:
                        c.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({','.join('?' * len(cols))})"
                                  f" ON CONFLICT({spec.key}) DO UPDATE SET {updates}", [values[col] for col in cols])
                        n += 1
                    except sqlite3.IntegrityError:
                        continue
            finally:
                c.execute("UPDATE sync_flag SET applying=0 WHERE id=1")
                c.commit()
        return n

    def apply_remote_messages(self, rows: list[dict], now: float, baseline_for) -> None:
        """Messages go through upsert_messages so search and new-message events work as usual."""
        gone = [r["id"] for r in rows if r.get("deleted") and r.get("id")]
        rows = [r for r in rows if not r.get("deleted") and r.get("chat_id")]
        by_account: dict[str, list[dict]] = {}
        for r in rows:
            chat = self.cached_chat(r["chat_id"])  # type: ignore[attr-defined]
            extra = r.get("extra")
            msg = {"id": r["id"], "chat_id": r["chat_id"], "from_me": bool(r.get("from_me")),
                   "sender": r.get("sender") or "", "text": r.get("text") or "", "ts": float(r.get("ts") or 0),
                   "sender_id": r.get("sender_id") or "", "edited": bool(r.get("is_edited")),
                   "extra": json.loads(extra) if isinstance(extra, str) and extra else (extra or {})}
            by_account.setdefault(chat["account_id"] if chat else "", []).append(msg)
        with self._lock:
            self._conn.execute("UPDATE sync_flag SET applying=1 WHERE id=1")
            try:
                for mid in gone:
                    self._conn.execute("DELETE FROM messages WHERE id=?", (mid,))
                for account_id, msgs in by_account.items():
                    self.upsert_messages(msgs, now, account_id=account_id,  # type: ignore[attr-defined]
                                         baseline=baseline_for(account_id) if account_id else None)
            finally:
                self._conn.execute("UPDATE sync_flag SET applying=0 WHERE id=1")
                self._conn.commit()

    def apply_remote_vectors(self, items: list[tuple[str, str, int, list[float]]]) -> None:
        """(chunk gid, model, dims, vector) from another device: no embedding call needed here."""
        ids = {}
        for gid, *_ in items:
            row = self._q("SELECT id FROM chunks WHERE gid=?", (gid,))
            if row:
                ids[gid] = int(row[0][0])
        by_model: dict[tuple[str, int], list] = {}
        for gid, model, dims, vec in items:
            if gid in ids and vec and len(vec) == dims:
                by_model.setdefault((model, dims), []).append((ids[gid], vec))
        for (model, dims), vecs in by_model.items():
            current = self.get_meta("embedding_model")  # type: ignore[attr-defined]
            if not current:
                self.use_embedding_model(model, dims)  # type: ignore[attr-defined]
            self.put_vectors(model, dims, vecs)  # type: ignore[attr-defined]

    def fact_by_gid(self, gid: str) -> Optional[dict]:
        rows = self._q("SELECT id FROM facts WHERE gid=?", (gid,))
        return self.fact(int(rows[0][0])) if rows else None  # type: ignore[attr-defined]

    def gid_of(self, table: str, local_id: int) -> Optional[str]:
        rows = self._q(f"SELECT gid FROM {table} WHERE id=?", (local_id,))
        return rows[0][0] if rows else None

    def device_id(self) -> str:
        dev = self.get_meta("device_id")  # type: ignore[attr-defined]
        if not dev:
            import uuid

            dev = "dev_" + uuid.uuid4().hex[:16]
            self.set_meta("device_id", dev)  # type: ignore[attr-defined]
        return dev


def remote_type(spec: Spec, col: str) -> str:
    return spec.types.get(col, "text")
