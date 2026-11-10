"""SQLite storage: drafts, the send log, a read cache and per-account pacing state.

The schema is versioned with ``PRAGMA user_version``; every migration is idempotent, so a
half-applied upgrade simply runs again. Message search uses an FTS5 trigram index when the
SQLite build has it (case-insensitive substring search that works for any script, including
Hindi and Hinglish) and falls back to LIKE otherwise.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from .automation_store import AutomationStore
from .cloud_store import CloudStore
from .insight_store import V10, InsightStore
from .memory_store import MemoryStore
from .pacing import DAY, HOUR, AccountGate, SendRecord

SCHEMA_VERSION = 10
LOG_RETENTION_DAYS = 90  # send log and finished drafts; must stay above the 7-day duplicate window
EVENT_RETENTION_DAYS = 30
FILE_RETENTION_DAYS = 30  # downloaded files; their transcripts stay with the message

_V1 = """
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    network TEXT NOT NULL,
    chat_name TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | discarded | failed
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    network TEXT NOT NULL,
    text TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    sent_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS send_log_sent_at ON send_log(sent_at);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    network TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    unread INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    from_me INTEGER NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, ts);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    network TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OK',
    paused_until REAL NOT NULL DEFAULT 0,
    pause_reason TEXT NOT NULL DEFAULT '',
    consecutive_429 INTEGER NOT NULL DEFAULT 0,
    seen_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS api_calls (ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# v2: messages get a stable INTEGER rowid (so the search index can never drift, even after a
# VACUUM), chats remember when their messages were last fetched and how many were asked for
# (replacing one meta row per chat), and the hot queries get indexes.
_V2_MESSAGES = """
CREATE TABLE messages_v2 (
    rid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    from_me INTEGER NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    fetched_at REAL NOT NULL
);
INSERT INTO messages_v2(id, chat_id, from_me, sender, text, ts, fetched_at)
    SELECT id, chat_id, from_me, sender, text, ts, fetched_at FROM messages ORDER BY ts;
DROP TABLE messages;
ALTER TABLE messages_v2 RENAME TO messages;
"""

_V2_INDEXES = """
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS chats_account_ts ON chats(account_id, ts);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status, created_at);
CREATE INDEX IF NOT EXISTS api_calls_ts ON api_calls(ts);
"""

_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='rid', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages
WHEN old.text IS NOT new.text BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rid, new.text);
END;
"""

# v3: richer chats and messages (groups, attachments, replies, reactions, edits), an append-only
# event log that later features read (notifications, memory, automations), and leases so only
# one Hermes process runs the background sync.
_V3_COLUMNS = [
    ("chats", "is_group", "INTEGER NOT NULL DEFAULT 0"),
    ("chats", "is_archived", "INTEGER NOT NULL DEFAULT 0"),
    ("chats", "is_muted", "INTEGER NOT NULL DEFAULT 0"),
    ("chats", "is_readonly", "INTEGER NOT NULL DEFAULT 0"),
    ("chats", "last_text", "TEXT NOT NULL DEFAULT ''"),
    ("chats", "last_from_me", "INTEGER NOT NULL DEFAULT 0"),
    ("chats", "synced_ts", "REAL NOT NULL DEFAULT 0"),      # newest message time the sync has stored
    ("chats", "needs_refresh", "INTEGER NOT NULL DEFAULT 0"),  # e.g. first seen through a webhook
    ("messages", "sender_id", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "is_edited", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "extra", "TEXT NOT NULL DEFAULT '{}'"),
]

_V3_TABLES = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,            -- message.new | message.update | message.delete | chat.update | account.status
    account_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS webhook_seen (id TEXT PRIMARY KEY, ts REAL NOT NULL);  -- redelivery guard
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, until REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chats_sync ON chats(account_id, synced_ts, ts);
"""

# v7: a log of every send attempt (who, what, how it went), shown by /inbox log and the page.
_V7 = """
CREATE TABLE IF NOT EXISTS send_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    via TEXT NOT NULL,                      -- hermes | command | page | schedule
    network TEXT NOT NULL DEFAULT '',
    to_name TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',        -- chat id, or the person's id for a new conversation
    text TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,                   -- asked | sent | failed | blocked | unsure
    detail TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS send_attempts_ts ON send_attempts(ts);
"""

# v8: received files downloaded on demand (and voice-note transcripts), and drafts that reply to
# (quote) a specific message.
_V8 = """
CREATE TABLE IF NOT EXISTS attachment_files (
    message_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',          -- '' once the file was cleaned up (it can be fetched again)
    filename TEXT NOT NULL DEFAULT '',
    mimetype TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL DEFAULT '',
    transcribed_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, attachment_id)
);
CREATE INDEX IF NOT EXISTS attachment_files_chat ON attachment_files(chat_id);
CREATE INDEX IF NOT EXISTS attachment_files_fetched ON attachment_files(fetched_at);
"""

_CHAT_COLS = ("id,account_id,network,name,unread,ts,fetched_at,msgs_fetched_at,msgs_fetched_n,"
              "is_group,is_archived,is_muted,is_readonly,last_text,last_from_me,synced_ts,needs_refresh")
_MSG_COLS = "id,chat_id,from_me,sender,text,ts,sender_id,is_edited,extra"
_FILE_COLS = "message_id,attachment_id,chat_id,path,filename,mimetype,size,fetched_at,transcript,transcribed_at"
_DRAFT_COLS = "id,chat_id,account_id,network,chat_name,text,status,error,created_at,reply_to,origin,original_text"


class Store(MemoryStore, AutomationStore, CloudStore, InsightStore):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()
        with self._lock:
            self._migrate()
            self.fts = self._has_table("messages_fts")
            self._load_vec()

    def close(self) -> None:
        """Close the database connection (when the plugin is unloaded)."""
        with self._lock:
            self._conn.close()

    # ── schema ──
    def _has_table(self, name: str) -> bool:
        return bool(self._conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _add_column(self, table: str, name: str, decl: str) -> None:
        if name not in self._columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _migrate(self) -> None:
        """Each step is idempotent, so an upgrade interrupted half-way simply runs again."""
        c = self._conn
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if version < 2:
            self._to_v2()
        if version < 3:
            for table, name, decl in _V3_COLUMNS:
                self._add_column(table, name, decl)
            c.executescript(_V3_TABLES)
            c.execute("UPDATE chats SET synced_ts=ts WHERE synced_ts=0")  # cached chats need no backfill
        if version < 4:
            self._migrate_v4()
        if version < 5:
            self._migrate_v5()
        if version < 6:
            self._migrate_v6()
        if version < 7:
            self._add_column("send_log", "started", "INTEGER NOT NULL DEFAULT 0")
            c.executescript(_V7)
        if version < 8:
            c.executescript(_V8)
            self._add_column("drafts", "reply_to", "TEXT NOT NULL DEFAULT ''")
        if version < 9:  # who wrote a draft; whether a person's name was chosen by you or merged
            self._add_column("drafts", "origin", "TEXT NOT NULL DEFAULT ''")  # '' (you or Hermes on request) | auto
            self._add_column("contacts", "name_source", "TEXT NOT NULL DEFAULT 'chat'")  # chat | merged | user
        if version < 10:  # plans found in chats, translations, and what a translated draft said in your words
            c.executescript(V10)
            self._add_column("drafts", "original_text", "TEXT NOT NULL DEFAULT ''")
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.commit()

    def _to_v2(self) -> None:
        c = self._conn
        c.executescript(_V1)
        if "rid" not in self._columns("messages"):
            c.executescript("BEGIN;" + _V2_MESSAGES + "COMMIT;")
        if "msgs_fetched_at" not in self._columns("chats"):
            c.execute("ALTER TABLE chats ADD COLUMN msgs_fetched_at REAL NOT NULL DEFAULT 0")
        if "msgs_fetched_n" not in self._columns("chats"):
            c.execute("ALTER TABLE chats ADD COLUMN msgs_fetched_n INTEGER NOT NULL DEFAULT 0")
        rows = c.execute("SELECT key, value FROM meta WHERE key LIKE 'msgs_at:%'").fetchall()
        c.executemany("UPDATE chats SET msgs_fetched_at=?, msgs_fetched_n=20 WHERE id=?",  # v1 read 20
                      [(float(json.loads(v)), k[len("msgs_at:"):]) for k, v in rows])
        c.execute("DELETE FROM meta WHERE key LIKE 'msgs_at:%'")
        c.executescript(_V2_INDEXES)
        try:
            c.executescript(_FTS)
            # Always rebuild while upgrading, so an upgrade interrupted half-way still ends with a
            # complete index. One-time cost, proportional to the cached messages.
            c.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError:  # SQLite built without FTS5 or the trigram tokenizer
            pass

    # ── helpers ──
    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _x(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.rowcount

    # ── meta ──
    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_meta(self, key: str, value: Any) -> None:
        self._x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    # ── drafts ──
    def add_draft(self, *, chat_id: str, account_id: str, network: str, chat_name: str,
                  text: str, now: float, reply_to: str = "", origin: str = "") -> str:
        """``reply_to``: the id of the message this draft answers (sent as a quoted reply).
        ``origin``: "auto" for a draft Hermes prepared by itself."""
        draft_id = uuid.uuid4().hex[:6]
        self._x("INSERT INTO drafts(id,chat_id,account_id,network,chat_name,text,created_at,updated_at,reply_to,"
                "origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (draft_id, chat_id, account_id, network, chat_name, text, now, now, reply_to or "", origin or ""))
        return draft_id

    def set_draft_text(self, draft_id: str, text: str, now: float, original: str = "") -> bool:
        """New text for a waiting draft. ``original``: what it says in your own language when the
        text is a translation (an edit by hand clears it: it would no longer match)."""
        return self._x("UPDATE drafts SET text=?, original_text=?, updated_at=? WHERE id=? AND status='pending'",
                       (text, original, now, draft_id)) > 0

    def pending_drafts_for_chat(self, chat_id: str) -> list[dict]:
        rows = self._q(f"SELECT {_DRAFT_COLS} FROM drafts WHERE chat_id=? AND status='pending' ORDER BY created_at",
                       (chat_id,))
        return [_draft(r) for r in rows]

    def sent_texts_by_plugin(self, since: float) -> set:
        """Texts this plugin sent (the send log): style learning leaves them out, so Hermes's own
        drafts never teach it how you write."""
        return {r[0] for r in self._q("SELECT text FROM send_log WHERE sent_at>=?", (since,))}

    def my_messages(self, *, limit: int = 400, chat_id: str = "", network: str = "") -> list[dict]:
        """Messages you wrote (newest first), with their chat's app."""
        sql = ("SELECT m.id, m.chat_id, m.text, m.ts, c.network, c.is_group FROM messages m JOIN chats c"
               " ON c.id=m.chat_id WHERE m.from_me=1 AND m.text!=''")
        params: list = []
        if chat_id:
            sql += " AND m.chat_id=?"
            params.append(chat_id)
        if network:
            sql += " AND c.network=?"
            params.append(network)
        rows = self._q(sql + " ORDER BY m.ts DESC LIMIT ?", params + [limit])
        return [{"id": r[0], "chat_id": r[1], "text": r[2], "ts": r[3], "network": r[4], "is_group": bool(r[5])}
                for r in rows]

    def question_exists(self, dedupe_key: str) -> bool:
        return bool(self._q("SELECT 1 FROM questions WHERE dedupe_key=?", (dedupe_key,)))

    def get_draft(self, draft_id: str) -> Optional[dict]:
        rows = self._q(f"SELECT {_DRAFT_COLS} FROM drafts WHERE id=?", (draft_id.strip().lower(),))
        return _draft(rows[0]) if rows else None

    def list_drafts(self, status: str = "pending", limit: int = 20) -> list[dict]:
        rows = self._q(f"SELECT {_DRAFT_COLS} FROM drafts WHERE status=? ORDER BY created_at DESC LIMIT ?",
                       (status, limit))
        return [_draft(r) for r in rows]

    def count_drafts(self, status: str = "pending") -> int:
        return self._q("SELECT COUNT(*) FROM drafts WHERE status=?", (status,))[0][0]

    def set_draft_status(self, draft_id: str, status: str, now: float, error: str = "") -> None:
        self._x("UPDATE drafts SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now, draft_id))

    # ── send log ──
    def log_send(self, *, draft_id: str, chat_id: str, account_id: str, network: str, text: str,
                 message_id: str, now: float, started: bool = False) -> None:
        self._x("INSERT INTO send_log(draft_id,chat_id,account_id,network,text,message_id,sent_at,started)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (draft_id, chat_id, account_id, network, text, message_id, now, int(started)))

    def recent_sends(self, since: float) -> list[SendRecord]:
        rows = self._q("SELECT chat_id,network,text,sent_at,started FROM send_log WHERE sent_at>=? ORDER BY sent_at",
                       (since,))
        return [SendRecord(chat_id=r[0], network=r[1], text=r[2], sent_at=r[3], started=bool(r[4])) for r in rows]

    # ── send attempts (the activity log) ──
    def log_attempt(self, *, via: str, network: str, to_name: str, target: str, text: str, files: list,
                    status: str, now: float, detail: str = "", message_id: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO send_attempts(ts,via,network,to_name,target,text,files,status,detail,message_id,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now, via, network, to_name, target, text[:2000], json.dumps(files), status, detail[:1000],
                 message_id, now))
            self._conn.commit()
            return int(cur.lastrowid)

    def update_attempt(self, attempt_id: int, now: float, *, status: str, detail: str = "",
                       message_id: str = "", target: str = "") -> None:
        sets = "status=?, detail=?, message_id=?, updated_at=?" + (", target=?" if target else "")
        params = [status, detail[:1000], message_id, now] + ([target] if target else []) + [attempt_id]
        self._x(f"UPDATE send_attempts SET {sets} WHERE id=?", params)

    def attempts(self, limit: int = 50) -> list[dict]:
        rows = self._q("SELECT id,ts,via,network,to_name,target,text,files,status,detail,message_id,updated_at"
                       " FROM send_attempts ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": r[0], "ts": r[1], "via": r[2], "network": r[3], "to": r[4], "target": r[5], "text": r[6],
                 "files": json.loads(r[7] or "[]"), "status": r[8], "detail": r[9], "message_id": r[10],
                 "updated_at": r[11]} for r in rows]

    def chat_members(self, chat_id: str, limit: int = 200) -> list[dict]:
        """People seen writing in a chat (others, not you): [{id, name}], most recent first. A group's
        own member list is not read from Unipile; who wrote recently is who can be mentioned."""
        rows = self._q("SELECT sender_id, sender, MAX(ts) FROM messages WHERE chat_id=? AND from_me=0"
                       " AND sender_id!='' GROUP BY sender_id ORDER BY MAX(ts) DESC LIMIT ?", (chat_id, limit))
        return [{"id": r[0], "name": r[1] or ""} for r in rows]

    def set_chat_unread(self, chat_id: str, unread: int) -> None:
        self._x("UPDATE chats SET unread=? WHERE id=?", (unread, chat_id))

    def group_chats_mentioning_you(self, since: float) -> set:
        """Group chats where someone mentioned you after your last message there (since ``since``)."""
        rows = self._q(
            "SELECT DISTINCT m.chat_id FROM messages m JOIN chats c ON c.id=m.chat_id"
            " WHERE c.is_group=1 AND m.from_me=0 AND m.ts>=? AND m.extra LIKE '%\"mentioned\": true%'"
            " AND m.ts > COALESCE((SELECT MAX(ts) FROM messages WHERE chat_id=m.chat_id AND from_me=1), 0)",
            (since,))
        return {r[0] for r in rows}

    # ── received files (downloaded on demand) and voice-note transcripts ──
    def data_dir(self) -> Optional[Path]:
        """The folder of the database file (None for an in-memory database)."""
        row = self._conn.execute("PRAGMA database_list").fetchone()
        return Path(row[2]).parent if row and row[2] else None

    def files_dir(self) -> Optional[Path]:
        folder = self.data_dir()
        return folder / "attachments" if folder else None

    def attachment_file(self, message_id: str, attachment_id: str) -> Optional[dict]:
        rows = self._q(f"SELECT {_FILE_COLS} FROM attachment_files WHERE message_id=? AND attachment_id=?",
                       (message_id, attachment_id))
        return _file(rows[0]) if rows else None

    def save_attachment_file(self, *, message_id: str, attachment_id: str, chat_id: str, path: str,
                             filename: str, mimetype: str, size: int, now: float) -> None:
        self._x("INSERT INTO attachment_files(message_id,attachment_id,chat_id,path,filename,mimetype,size,"
                "fetched_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(message_id,attachment_id) DO UPDATE SET"
                " path=excluded.path, filename=excluded.filename, mimetype=excluded.mimetype,"
                " size=excluded.size, fetched_at=excluded.fetched_at",
                (message_id, attachment_id, chat_id, path, filename, mimetype, size, now))

    def save_transcript(self, message_id: str, attachment_id: str, transcript: str, now: float) -> None:
        self._x("UPDATE attachment_files SET transcript=?, transcribed_at=? WHERE message_id=? AND attachment_id=?",
                (transcript, now, message_id, attachment_id))

    def files_for_messages(self, message_ids: Iterable[str]) -> dict:
        """{(message_id, attachment_id): file row} for these messages."""
        ids = list(dict.fromkeys(message_ids))
        out: dict = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self._q(f"SELECT {_FILE_COLS} FROM attachment_files WHERE message_id IN"
                             f" ({','.join('?' * len(part))})", part):
                f = _file(r)
                out[(f["message_id"], f["attachment_id"])] = f
        return out

    def search_transcripts(self, query: str, network: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Voice notes whose transcript contains ``query`` (case-insensitive), newest first."""
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql = ("SELECT m.chat_id, c.name, c.network, m.from_me, m.sender, f.transcript, m.ts, m.id"
               " FROM attachment_files f JOIN messages m ON m.id=f.message_id JOIN chats c ON c.id=m.chat_id"
               " WHERE f.transcript LIKE ? ESCAPE '\\'")
        params: list = [like]
        if network:
            sql += " AND c.network=?"
            params.append(network)
        rows = self._q(sql + " ORDER BY m.ts DESC LIMIT ?", params + [limit])
        return [{"chat_id": r[0], "chat_name": r[1], "network": r[2], "from_me": bool(r[3]), "sender": r[4],
                 "text": "[voice note] " + r[5], "ts": r[6], "message_id": r[7]} for r in rows]

    def _drop_files_locked(self, message_ids: list) -> None:
        """Forget downloaded files (and their transcripts) of messages that are gone. Caller holds the lock."""
        for i in range(0, len(message_ids), 500):
            part = message_ids[i:i + 500]
            marks = ",".join("?" * len(part))
            paths = [r[0] for r in self._conn.execute(
                f"SELECT path FROM attachment_files WHERE message_id IN ({marks}) AND path!=''", part)]
            self._conn.execute(f"DELETE FROM attachment_files WHERE message_id IN ({marks})", part)
            _unlink_files(paths, self.files_dir())

    def prune_files(self, now: float) -> int:
        """Delete downloaded files older than FILE_RETENTION_DAYS (the transcript stays, the file can
        be fetched again), and every file whose message is gone. Returns the files deleted."""
        cutoff = now - FILE_RETENTION_DAYS * DAY
        with self._lock:
            c = self._conn
            orphans = [r[0] for r in c.execute(
                "SELECT message_id FROM attachment_files WHERE message_id NOT IN (SELECT id FROM messages)")]
            old = [r[0] for r in c.execute("SELECT path FROM attachment_files WHERE path!='' AND fetched_at<?",
                                           (cutoff,))]
            c.execute("UPDATE attachment_files SET path='' WHERE path!='' AND fetched_at<?", (cutoff,))
            removed = _unlink_files(old, self.files_dir())
            if orphans:
                before = c.execute("SELECT COUNT(*) FROM attachment_files WHERE path!=''").fetchone()[0]
                self._drop_files_locked(orphans)
                removed += before - c.execute("SELECT COUNT(*) FROM attachment_files WHERE path!=''").fetchone()[0]
            c.commit()
        return removed

    # ── chat cache ──
    def upsert_chats(self, chats: Iterable[dict], now: float) -> None:
        """Store chats as Unipile reports them. ``ts`` only moves forward: a message the cache
        already holds can be newer than a slightly stale chat list."""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO chats(id,account_id,network,name,unread,ts,fetched_at,is_group,is_archived,"
                "is_muted,is_readonly,last_text,last_from_me,needs_refresh) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, unread=excluded.unread,"
                " ts=MAX(chats.ts, excluded.ts), fetched_at=excluded.fetched_at, is_group=excluded.is_group,"
                " is_archived=excluded.is_archived, is_muted=excluded.is_muted, is_readonly=excluded.is_readonly,"
                # a list without a preview never erases the one we have
                " last_text=CASE WHEN excluded.last_text!='' AND excluded.ts>=chats.ts THEN excluded.last_text"
                " ELSE chats.last_text END,"
                " last_from_me=CASE WHEN excluded.last_text!='' AND excluded.ts>=chats.ts THEN excluded.last_from_me"
                " ELSE chats.last_from_me END, needs_refresh=0,"
                # More unread at the same time stamp: a message arrived in the same second as the last
                # one synced (WhatsApp times are whole seconds, e.g. a photo album). Move the sync mark
                # just below, so the next cycle reads that chat once (the read overlaps by a second).
                " synced_ts=CASE WHEN excluded.ts>0 AND excluded.ts=chats.ts AND chats.synced_ts>=chats.ts"
                " AND excluded.unread>chats.unread THEN chats.ts-0.001 ELSE chats.synced_ts END",
                [(c["id"], c["account_id"], c["network"], c.get("name") or "", int(c.get("unread") or 0),
                  float(c.get("ts") or 0), now, int(bool(c.get("is_group"))), int(bool(c.get("is_archived"))),
                  int(bool(c.get("is_muted"))), int(bool(c.get("is_readonly"))), c.get("last_text") or "",
                  int(bool(c.get("last_from_me")))) for c in chats])
            self._conn.commit()

    def chats_needing_sync(self, account_id: str, limit: int) -> list[dict]:
        """Chats whose latest activity is newer than what the sync has stored, newest first."""
        rows = self._q(f"SELECT {_CHAT_COLS} FROM chats WHERE account_id=? AND (ts>synced_ts OR needs_refresh=1)"
                       " ORDER BY ts DESC LIMIT ?", (account_id, limit))
        return [_chat(r) for r in rows]

    def mark_chat_synced(self, chat_id: str, synced_ts: float) -> None:
        self._x("UPDATE chats SET synced_ts=MAX(synced_ts, ?) WHERE id=?", (synced_ts, chat_id))

    def ensure_chat(self, *, chat_id: str, account_id: str, network: str, now: float) -> bool:
        """Create a placeholder for a chat first seen through a webhook. True if it was new;
        the sync then fetches its name and details."""
        return self._x("INSERT OR IGNORE INTO chats(id,account_id,network,name,ts,fetched_at,needs_refresh)"
                       " VALUES(?,?,?,'',0,?,1)", (chat_id, account_id, network, now)) > 0

    def note_chat_message(self, msg: dict, *, inbound_new: bool) -> None:
        """Move a chat's preview, time and unread count forward for a message received live."""
        self._x("UPDATE chats SET ts=MAX(ts, ?), last_text=CASE WHEN ?>=ts THEN ? ELSE last_text END,"
                " last_from_me=CASE WHEN ?>=ts THEN ? ELSE last_from_me END,"
                " unread=CASE WHEN ? THEN unread+1 WHEN ? THEN 0 ELSE unread END WHERE id=?",
                (msg["ts"], msg["ts"], msg["text"], msg["ts"], int(msg["from_me"]), int(inbound_new),
                 int(msg["from_me"]), msg["chat_id"]))

    def set_chat_preview(self, msg: dict) -> None:
        """Show ``msg`` as the chat's latest message if nothing newer is known."""
        self._x("UPDATE chats SET last_text=?, last_from_me=?, ts=MAX(ts, ?) WHERE id=? AND ts<=?",
                (msg["text"], int(msg["from_me"]), msg["ts"], msg["chat_id"], msg["ts"]))

    def delete_chat(self, chat_id: str) -> None:
        self.forget_chat_memory(chat_id)
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            self._conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            self._conn.commit()

    def cached_chat(self, chat_id: str) -> Optional[dict]:
        rows = self._q(f"SELECT {_CHAT_COLS} FROM chats WHERE id=?", (chat_id,))
        return _chat(rows[0]) if rows else None

    def cached_chats(self, *, account_id: Optional[str] = None, network: Optional[str] = None,
                     unread_only: bool = False, limit: int = 50) -> list[dict]:
        where, params = [], []
        if account_id:
            where.append("account_id=?")
            params.append(account_id)
        if network:
            where.append("network=?")
            params.append(network)
        if unread_only:
            where.append("unread>0")
        sql = f"SELECT {_CHAT_COLS} FROM chats" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._q(sql + " ORDER BY ts DESC LIMIT ?", (*params, limit))
        return [_chat(r) for r in rows]

    def chats_by_ids(self, ids: list[str]) -> list[dict]:
        """Cached chats in the order of ``ids``; ids no longer cached are skipped."""
        if not ids:
            return []
        rows = self._q(f"SELECT {_CHAT_COLS} FROM chats WHERE id IN ({','.join('?' * len(ids))})", ids)
        by_id = {r[0]: _chat(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def set_messages_fetched(self, chat_id: str, now: float, n: int) -> None:
        """Record a read of the latest ``n`` messages (n=0 marks the cache stale)."""
        self._x("UPDATE chats SET msgs_fetched_at=?, msgs_fetched_n=? WHERE id=?", (now, n, chat_id))

    # ── message cache ──
    def upsert_messages(self, messages: Iterable[dict], now: float, *, account_id: str = "",
                        baseline: Optional[float] = None) -> dict:
        """Store messages; drop the ones flagged ``deleted``. Returns the ids that were new,
        edited and deleted. With ``baseline`` (the time the sync started watching this account),
        new messages newer than it and every edit or deletion are also logged as events, so
        history fetched for the first time never looks like a burst of new messages."""
        msgs = [m for m in messages if m.get("id")]
        out: dict = {"new": [], "edited": [], "deleted": []}
        if not msgs:
            return out
        with self._lock:
            c = self._conn
            ids = [m["id"] for m in msgs]
            known = dict(c.execute(f"SELECT id, text FROM messages WHERE id IN ({','.join('?' * len(ids))})",
                                   ids).fetchall())
            keep = [m for m in msgs if not m.get("deleted")]
            gone = [m for m in msgs if m.get("deleted") and m["id"] in known]
            c.executemany(
                "INSERT INTO messages(id,chat_id,from_me,sender,text,ts,fetched_at,sender_id,is_edited,extra)"
                " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,"
                " fetched_at=excluded.fetched_at, is_edited=MAX(messages.is_edited, excluded.is_edited),"
                " extra=excluded.extra, sender=CASE WHEN excluded.sender!='' THEN excluded.sender"
                " ELSE messages.sender END",
                [(m["id"], m["chat_id"], int(bool(m["from_me"])), m.get("sender") or "", m.get("text") or "",
                  float(m["ts"]), now, m.get("sender_id") or "", int(bool(m.get("edited"))),
                  json.dumps(m.get("extra") or {}, ensure_ascii=False)) for m in keep])
            if gone:
                c.executemany("DELETE FROM messages WHERE id=?", [(m["id"],) for m in gone])
                self._drop_files_locked([m["id"] for m in gone])
            for m in keep:
                if m["id"] not in known:
                    out["new"].append(m["id"])
                elif known[m["id"]] != (m.get("text") or ""):
                    out["edited"].append(m["id"])
            out["deleted"] = [m["id"] for m in gone]
            if baseline is not None:
                by_id = {m["id"]: m for m in msgs}
                rows = []
                for mid in out["new"]:
                    m = by_id[mid]
                    if m["ts"] > baseline:
                        rows.append((now, "message.new", account_id, m["chat_id"], mid, _event_data(m)))
                for kind, key in (("message.update", "edited"), ("message.delete", "deleted")):
                    for mid in out[key]:
                        m = by_id[mid]
                        rows.append((now, kind, account_id, m["chat_id"], mid, _event_data(m)))
                c.executemany("INSERT INTO events(ts,type,account_id,chat_id,message_id,data) VALUES(?,?,?,?,?,?)",
                              rows)
            c.commit()
        return out

    def delete_message(self, message_id: str) -> bool:
        with self._lock:
            gone = self._conn.execute("DELETE FROM messages WHERE id=?", (message_id,)).rowcount > 0
            if gone:
                self._drop_files_locked([message_id])
            self._conn.commit()
        return gone

    def get_message(self, message_id: str) -> Optional[dict]:
        rows = self._q(f"SELECT {_MSG_COLS} FROM messages WHERE id=?", (message_id,))
        return _message(rows[0]) if rows else None

    def set_message_extra(self, message_id: str, extra: dict) -> None:
        self._x("UPDATE messages SET extra=? WHERE id=?", (json.dumps(extra, ensure_ascii=False), message_id))

    def cached_messages(self, chat_id: str, limit: int) -> list[dict]:
        rows = self._q(f"SELECT {_MSG_COLS} FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
                       (chat_id, limit))
        return [_message(r) for r in reversed(rows)]

    def newest_message_ts(self, chat_id: str) -> float:
        return float(self._q("SELECT COALESCE(MAX(ts), 0) FROM messages WHERE chat_id=?", (chat_id,))[0][0])

    def search_messages(self, query: str, network: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Case-insensitive substring search, newest first."""
        cols = "m.chat_id, c.name, c.network, m.from_me, m.sender, m.text, m.ts"
        params: list[Any]
        if self.fts and len(query) >= 3:  # the trigram index needs at least 3 characters
            sql = (f"SELECT {cols} FROM messages_fts f JOIN messages m ON m.rid=f.rowid"
                   " LEFT JOIN chats c ON c.id=m.chat_id WHERE messages_fts MATCH ?")
            params = ['"' + query.replace('"', '""') + '"']
        else:
            sql = (f"SELECT {cols} FROM messages m LEFT JOIN chats c ON c.id=m.chat_id"
                   " WHERE m.text LIKE ? ESCAPE '\\'")
            params = ["%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"]
        if network:
            sql += " AND c.network=?"
            params.append(network)
        rows = self._q(sql + " ORDER BY m.ts DESC LIMIT ?", (*params, limit))
        return [{"chat_id": r[0], "chat_name": r[1] or "", "network": r[2] or "", "from_me": bool(r[3]),
                 "sender": r[4], "text": r[5], "ts": r[6]} for r in rows]

    def cached_message_count(self) -> int:
        return self._q("SELECT COUNT(*) FROM messages")[0][0]

    # ── accounts / pacing state ──
    def account(self, account_id: str) -> dict:
        rows = self._q("SELECT id,network,status,paused_until,pause_reason,consecutive_429,seen_at"
                       " FROM accounts WHERE id=?", (account_id,))
        if not rows:
            return {"id": account_id, "network": "", "status": "OK", "paused_until": 0.0, "pause_reason": "",
                    "consecutive_429": 0, "seen_at": 0.0}
        r = rows[0]
        return {"id": r[0], "network": r[1], "status": r[2], "paused_until": r[3], "pause_reason": r[4],
                "consecutive_429": r[5], "seen_at": r[6]}

    def save_account(self, a: dict) -> None:
        self._x("INSERT INTO accounts(id,network,status,paused_until,pause_reason,consecutive_429,"
                "seen_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET network=excluded.network,"
                " status=excluded.status, paused_until=excluded.paused_until, pause_reason=excluded.pause_reason,"
                " consecutive_429=excluded.consecutive_429,"
                " seen_at=excluded.seen_at",
                (a["id"], a.get("network", ""), a.get("status", "OK"), a.get("paused_until", 0.0),
                 a.get("pause_reason", ""), a.get("consecutive_429", 0), a.get("seen_at", 0.0)))

    def accounts(self) -> list[dict]:
        return [self.account(r[0]) for r in self._q("SELECT id FROM accounts ORDER BY network")]

    def gate(self, account_id: str) -> AccountGate:
        a, g = self.account(account_id), self.account("*")
        paused_until = max(a["paused_until"], g["paused_until"])
        reason = a["pause_reason"] if a["paused_until"] >= g["paused_until"] else g["pause_reason"]
        return AccountGate(paused_until=paused_until, pause_reason=reason)

    # ── API call budget ──
    def record_api_call(self, now: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM api_calls WHERE ts<?", (now - HOUR,))
            self._conn.execute("INSERT INTO api_calls(ts) VALUES(?)", (now,))
            self._conn.commit()

    def api_calls_last_hour(self, now: float) -> int:
        return self._q("SELECT COUNT(*) FROM api_calls WHERE ts>=?", (now - HOUR,))[0][0]

    # ── event log ──
    def add_event(self, *, type: str, now: float, account_id: str = "", chat_id: str = "", message_id: str = "",
                  data: Optional[dict] = None) -> None:
        self._x("INSERT INTO events(ts,type,account_id,chat_id,message_id,data) VALUES(?,?,?,?,?,?)",
                (now, type, account_id, chat_id, message_id, json.dumps(data or {}, ensure_ascii=False)))

    def first_delivery(self, webhook_event_id: str, now: float) -> bool:
        """True the first time a webhook event id is seen; False for a redelivery."""
        return self._x("INSERT OR IGNORE INTO webhook_seen(id, ts) VALUES(?, ?)", (webhook_event_id, now)) > 0

    def forget_delivery(self, webhook_event_id: str) -> None:
        """A delivery that could not be applied: its redelivery must not look like a duplicate."""
        self._x("DELETE FROM webhook_seen WHERE id=?", (webhook_event_id,))

    def events_after(self, last_id: int, *, types: Optional[Iterable[str]] = None, limit: int = 200) -> list[dict]:
        sql = "SELECT id,ts,type,account_id,chat_id,message_id,data FROM events WHERE id>?"
        params: list[Any] = [last_id]
        if types:
            types = list(types)
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params += types
        rows = self._q(sql + " ORDER BY id LIMIT ?", (*params, limit))
        return [{"id": r[0], "ts": r[1], "type": r[2], "account_id": r[3], "chat_id": r[4], "message_id": r[5],
                 "data": json.loads(r[6] or "{}")} for r in rows]

    def last_event_id(self) -> int:
        return int(self._q("SELECT COALESCE(MAX(id), 0) FROM events")[0][0])

    # ── leases (one background worker across processes) ──
    def acquire_lease(self, name: str, owner: str, now: float, ttl: float) -> bool:
        """Take or renew ``name`` for ``owner`` unless another owner holds an unexpired lease."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO leases(name,owner,until) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET"
                " owner=excluded.owner, until=excluded.until WHERE leases.owner=excluded.owner OR leases.until<?",
                (name, owner, now + ttl, now))
            self._conn.commit()
            row = self._conn.execute("SELECT owner FROM leases WHERE name=?", (name,)).fetchone()
        return bool(row and row[0] == owner)

    def release_lease(self, name: str, owner: str) -> None:
        self._x("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    # ── housekeeping ──
    def prune(self, now: float, retention_days: int) -> dict:
        """Drop cached chats and messages older than ``retention_days`` and finished drafts,
        send-log and activity-log rows older than LOG_RETENTION_DAYS. Pending drafts and their
        chats are kept."""
        cache_cutoff = now - retention_days * DAY
        log_cutoff = now - LOG_RETENTION_DAYS * DAY
        with self._lock:
            c = self._conn
            out = {
                "messages": c.execute("DELETE FROM messages WHERE ts<?", (cache_cutoff,)).rowcount,
                "chats": c.execute("DELETE FROM chats WHERE ts<? AND id NOT IN"
                                   " (SELECT chat_id FROM drafts WHERE status='pending')", (cache_cutoff,)).rowcount,
                "drafts": c.execute("DELETE FROM drafts WHERE status!='pending' AND updated_at<?",
                                    (log_cutoff,)).rowcount,
                "send_log": c.execute("DELETE FROM send_log WHERE sent_at<?", (log_cutoff,)).rowcount,
                "send_attempts": c.execute("DELETE FROM send_attempts WHERE ts<?", (log_cutoff,)).rowcount,
            }
            out["events"] = c.execute("DELETE FROM events WHERE ts<?", (now - EVENT_RETENTION_DAYS * DAY,)).rowcount
            c.execute("DELETE FROM webhook_seen WHERE ts<?", (now - EVENT_RETENTION_DAYS * DAY,))
            old_chunks = [r[0] for r in c.execute("SELECT id FROM chunks WHERE end_ts<?", (cache_cutoff,))]
            for i in range(0, len(old_chunks), 500):
                part = old_chunks[i:i + 500]
                marks = ",".join("?" * len(part))
                c.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({marks})", part)
                if self.vec_index and self._has_vec_table():
                    c.executemany("DELETE FROM vec_chunks WHERE chunk_id=?", [(x,) for x in part])
                c.execute(f"DELETE FROM chunks WHERE id IN ({marks})", part)
            out["chunks"] = len(old_chunks)
            c.execute("DELETE FROM loops WHERE status!='open' AND updated_at<?", (log_cutoff,))
            c.execute("DELETE FROM questions WHERE status!='open' AND answered_at<?", (log_cutoff,))
            c.execute("DELETE FROM usage WHERE day<?", (_day_before(now, LOG_RETENTION_DAYS),))
            c.execute("DELETE FROM notifications WHERE ts<?", (log_cutoff,))
            c.execute("DELETE FROM watches WHERE status!='active' AND created_at<?", (log_cutoff,))
            c.execute("DELETE FROM schedules WHERE status IN ('sent','cancelled','failed','missed','held','unknown')"
                      " AND updated_at<?", (log_cutoff,))
            c.execute("DELETE FROM api_calls WHERE ts<?", (now - HOUR,))
            c.commit()
        out["files"] = self.prune_files(now)
        out["plans"] = self.prune_plans(now - LOG_RETENTION_DAYS * DAY)
        out["translations"] = self.prune_translations()
        return out

    def maybe_prune(self, now: float, retention_days: int) -> Optional[dict]:
        """Prune at most once a day."""
        if now - float(self.get_meta("pruned_at", 0.0)) < DAY:
            return None
        out = self.prune(now, retention_days)
        self.set_meta("pruned_at", now)
        return out


def _draft(r: tuple) -> dict:
    return {"id": r[0], "chat_id": r[1], "account_id": r[2], "network": r[3], "chat_name": r[4],
            "text": r[5], "status": r[6], "error": r[7], "created_at": r[8], "reply_to": r[9] or "",
            "origin": r[10] or "", "original_text": r[11] or ""}


def _chat(r: tuple) -> dict:
    return {"id": r[0], "account_id": r[1], "network": r[2], "name": r[3], "unread": r[4], "ts": r[5],
            "fetched_at": r[6], "msgs_fetched_at": r[7], "msgs_fetched_n": r[8], "is_group": bool(r[9]),
            "is_archived": bool(r[10]), "is_muted": bool(r[11]), "is_readonly": bool(r[12]), "last_text": r[13],
            "last_from_me": bool(r[14]), "synced_ts": r[15], "needs_refresh": bool(r[16])}


def _message(r: tuple) -> dict:
    out = {"id": r[0], "chat_id": r[1], "from_me": bool(r[2]), "sender": r[3], "text": r[4], "ts": r[5],
           "sender_id": r[6], "edited": bool(r[7])}
    extra = json.loads(r[8] or "{}")
    if extra:
        out["extra"] = extra
    return out


def _file(r: tuple) -> dict:
    return {"message_id": r[0], "attachment_id": r[1], "chat_id": r[2], "path": r[3], "filename": r[4],
            "mimetype": r[5], "size": r[6], "fetched_at": r[7], "transcript": r[8], "transcribed_at": r[9]}


def _unlink_files(paths: Iterable[str], folder: Optional[Path]) -> int:
    """Delete these downloaded files, but only inside the plugin's own attachments folder."""
    if folder is None:
        return 0
    root = folder.resolve()
    removed = 0
    for raw in paths:
        try:
            path = Path(raw).resolve()
            path.relative_to(root)
            path.unlink()
            removed += 1
        except (ValueError, FileNotFoundError):
            pass
        except OSError:
            pass  # in use or not allowed: the daily cleanup tries again
    return removed


def _event_data(m: dict) -> str:
    """The part of a message an event carries (JSON): enough to notify without another lookup."""
    return json.dumps({"from_me": bool(m.get("from_me")), "sender": m.get("sender") or "",
                       "text": (m.get("text") or "")[:500], "ts": m.get("ts")}, ensure_ascii=False)


def _day_before(now: float, days: int) -> str:
    import time as _time
    return _time.strftime("%Y-%m-%d", _time.gmtime(now - days * DAY))
