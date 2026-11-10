"""Storage for the context engine (schema v4): contacts, facts, open loops, questions for the
user, rolling chat summaries, conversation chunks and their embeddings.

Mixed into ``store.Store``; uses its connection, lock and helpers.

Vectors are always kept as float32 blobs in ``chunk_vectors`` (the source of truth, portable to
other databases later). When the ``sqlite-vec`` extension loads, a ``vec_chunks`` index mirrors
them for fast nearest-neighbour search; otherwise search scans the blobs in Python, which is
fine for a personal inbox.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from array import array
from typing import Any, Iterable, Optional

_V4_TABLES = """
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_chats (     -- one-to-one chats that belong to a contact
    chat_id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    network TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS contact_chats_contact ON contact_chats(contact_id);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    status TEXT NOT NULL DEFAULT 'active',     -- active | pending (waiting for the user) | superseded
    source TEXT NOT NULL DEFAULT 'auto',       -- auto | user
    chat_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1,
    replaces INTEGER,                           -- for a pending update: the fact it would replace
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_contact ON facts(contact_id, status);
CREATE TABLE IF NOT EXISTS loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    kind TEXT NOT NULL,                        -- they_wait | you_wait | you_promised | they_promised
    text TEXT NOT NULL,
    due_ts REAL,
    status TEXT NOT NULL DEFAULT 'open',       -- open | done | dismissed
    message_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL,
    close_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS loops_open ON loops(status, kind, due_ts);
CREATE INDEX IF NOT EXISTS loops_chat ON loops(chat_id, status);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                        -- fact | update | merge | meaning
    text TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',       -- open | answered | dismissed
    dedupe_key TEXT UNIQUE,
    answer TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    answered_at REAL
);
CREATE TABLE IF NOT EXISTS chat_context (
    chat_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    memory_ts REAL NOT NULL DEFAULT 0,         -- newest message the memory has read
    chunk_ts REAL NOT NULL DEFAULT 0,          -- newest message the chunks cover
    language TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    msg_count INTEGER NOT NULL,
    text TEXT NOT NULL,
    hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_chat ON chunks(chat_id, start_ts);
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    dims INTEGER NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL,
                                  PRIMARY KEY(day, kind));
"""

_FACT_COLS = "id,contact_id,text,category,status,source,chat_id,message_id,confidence,replaces,created_at,updated_at"
_LOOP_COLS = "id,chat_id,kind,text,due_ts,status,message_id,created_at,updated_at,closed_at,close_reason"
_CHUNK_COLS = "id,chat_id,start_ts,end_ts,msg_count,text,hash"


def pack(vec: Iterable[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class MemoryStore:
    """Context-engine queries. Expects ``self._conn``, ``self._lock``, ``self._q``, ``self._x``."""

    _conn: sqlite3.Connection
    vec_index: bool = False  # sqlite-vec loaded

    def _migrate_v4(self) -> None:
        self._conn.executescript(_V4_TABLES)

    def _load_vec(self) -> None:
        """Load sqlite-vec when installed. Optional: everything works without it."""
        try:
            import sqlite_vec  # type: ignore[import-not-found]

            self._conn.enable_load_extension(True)
            try:
                sqlite_vec.load(self._conn)
            finally:
                self._conn.enable_load_extension(False)
            self.vec_index = True
        except Exception:  # noqa: BLE001 - not installed, or extensions disabled in this SQLite build
            self.vec_index = False

    # ── usage caps ──
    def usage_today(self, day: str, kind: str) -> int:
        rows = self._q("SELECT amount FROM usage WHERE day=? AND kind=?", (day, kind))
        return int(rows[0][0]) if rows else 0

    def add_usage(self, day: str, kind: str, amount: int) -> None:
        self._x("INSERT INTO usage(day,kind,amount) VALUES(?,?,?) ON CONFLICT(day,kind)"
                " DO UPDATE SET amount=amount+excluded.amount", (day, kind, amount))

    # ── contacts ──
    def contact_for_chat(self, chat_id: str) -> Optional[dict]:
        rows = self._q("SELECT c.id, c.name FROM contact_chats cc JOIN contacts c ON c.id=cc.contact_id"
                       " WHERE cc.chat_id=?", (chat_id,))
        return {"id": rows[0][0], "name": rows[0][1]} if rows else None

    def ensure_contact(self, *, chat_id: str, network: str, name: str, now: float) -> dict:
        """The contact of a one-to-one chat, created on first sight (one contact per chat until
        the user confirms two chats are the same person). A contact follows its chat's name,
        unless it spans several chats or you named it yourself."""
        found = self.contact_for_chat(chat_id)
        if found:
            if name and found["name"] != name:
                n = self._x("UPDATE contacts SET name=?, updated_at=? WHERE id=? AND name_source='chat' AND"
                            " (SELECT COUNT(*) FROM contact_chats WHERE contact_id=?)=1",
                            (name, now, found["id"], found["id"]))
                if n:
                    found["name"] = name
            return found
        cid = "ct_" + text_hash(chat_id)[:12]
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO contacts(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                               (cid, name, now, now))
            self._conn.execute("INSERT OR REPLACE INTO contact_chats(chat_id,contact_id,network) VALUES(?,?,?)",
                               (chat_id, cid, network))
            self._conn.commit()
        return {"id": cid, "name": name}

    def contact(self, contact_id: str) -> Optional[dict]:
        rows = self._q("SELECT id, name FROM contacts WHERE id=?", (contact_id,))
        if not rows:
            return None
        chats = self._q("SELECT chat_id, network FROM contact_chats WHERE contact_id=?", (contact_id,))
        return {"id": rows[0][0], "name": rows[0][1], "chats": [{"chat_id": r[0], "network": r[1]} for r in chats]}

    def contacts(self) -> list[dict]:
        rows = self._q("SELECT c.id, c.name, cc.chat_id, cc.network FROM contacts c"
                       " JOIN contact_chats cc ON cc.contact_id=c.id ORDER BY c.id")
        out: dict[str, dict] = {}
        for cid, name, chat_id, network in rows:
            out.setdefault(cid, {"id": cid, "name": name, "chats": []})["chats"].append(
                {"chat_id": chat_id, "network": network})
        return list(out.values())

    def merge_contacts(self, keep: str, gone: str, now: float) -> None:
        """Make ``gone`` part of ``keep``: its chats and facts move over."""
        if keep == gone:
            return
        with self._lock:
            c = self._conn
            c.execute("UPDATE contact_chats SET contact_id=? WHERE contact_id=?", (keep, gone))
            c.execute("UPDATE facts SET contact_id=? WHERE contact_id=?", (keep, gone))
            c.execute("DELETE FROM contacts WHERE id=?", (gone,))
            c.execute("UPDATE contacts SET updated_at=? WHERE id=?", (now, keep))
            c.commit()

    def set_contact_name(self, contact_id: str, name: str, now: float, *, source: str) -> None:
        """``source``: "merged" (picked when chats were linked) or "user" (you named them)."""
        self._x("UPDATE contacts SET name=?, name_source=?, updated_at=? WHERE id=?", (name, source, now, contact_id))

    def contact_name_source(self, contact_id: str) -> str:
        rows = self._q("SELECT name_source FROM contacts WHERE id=?", (contact_id,))
        return rows[0][0] if rows else ""

    def split_chat(self, chat_id: str, name: str, now: float) -> Optional[str]:
        """Take a chat out of a person it was linked to: it becomes its own person again, with
        the facts learned from that chat. Returns the new contact id (None if it was alone)."""
        current = self.contact_for_chat(chat_id)
        if not current:
            return None
        with self._lock:
            c = self._conn
            if c.execute("SELECT COUNT(*) FROM contact_chats WHERE contact_id=?", (current["id"],)).fetchone()[0] < 2:
                return None
            new_id = "ct_" + text_hash(f"{chat_id}:split:{now}")[:12]
            c.execute("INSERT INTO contacts(id,name,created_at,updated_at) VALUES(?,?,?,?)", (new_id, name, now, now))
            c.execute("UPDATE contact_chats SET contact_id=? WHERE chat_id=?", (new_id, chat_id))
            c.execute("UPDATE facts SET contact_id=? WHERE contact_id=? AND chat_id=?", (new_id, current["id"], chat_id))
            c.execute("UPDATE contacts SET updated_at=? WHERE id=?", (now, current["id"]))
            c.commit()
        return new_id

    def forget_contact(self, contact_id: str) -> int:
        """Delete everything remembered about a contact (facts and summaries of its chats). The
        messages themselves stay in the inbox cache."""
        with self._lock:
            c = self._conn
            chats = [r[0] for r in c.execute("SELECT chat_id FROM contact_chats WHERE contact_id=?", (contact_id,))]
            n = c.execute("DELETE FROM facts WHERE contact_id=?", (contact_id,)).rowcount
            for chat_id in chats:
                c.execute("UPDATE chat_context SET summary='' WHERE chat_id=?", (chat_id,))
            c.commit()
        return n

    # ── facts ──
    def facts(self, contact_id: str, *, status: str = "active", limit: int = 50) -> list[dict]:
        rows = self._q(f"SELECT {_FACT_COLS} FROM facts WHERE contact_id=? AND status=?"
                       " ORDER BY updated_at DESC LIMIT ?", (contact_id, status, limit))
        return [_fact(r) for r in rows]

    def fact(self, fact_id: int) -> Optional[dict]:
        rows = self._q(f"SELECT {_FACT_COLS} FROM facts WHERE id=?", (fact_id,))
        return _fact(rows[0]) if rows else None

    def add_fact(self, *, contact_id: str, text: str, category: str, status: str, source: str, now: float,
                 chat_id: str = "", message_id: str = "", confidence: float = 1.0,
                 replaces: Optional[int] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts(contact_id,text,category,status,source,chat_id,message_id,confidence,replaces,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (contact_id, text, category, status, source, chat_id, message_id, confidence, replaces, now, now))
            self._conn.commit()
            return int(cur.lastrowid)

    def set_fact(self, fact_id: int, now: float, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("text", "status", "category", "confidence", "source")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        self._x(f"UPDATE facts SET {sets}, updated_at=? WHERE id=?", (*allowed.values(), now, fact_id))

    def delete_fact(self, fact_id: int) -> bool:
        return self._x("DELETE FROM facts WHERE id=?", (fact_id,)) > 0

    # ── open loops ──
    def loops(self, *, chat_id: Optional[str] = None, kinds: Optional[Iterable[str]] = None,
              status: str = "open", limit: int = 100) -> list[dict]:
        sql, params = f"SELECT {_LOOP_COLS} FROM loops WHERE status=?", [status]
        if chat_id:
            sql += " AND chat_id=?"
            params.append(chat_id)
        if kinds:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY CASE WHEN due_ts IS NULL THEN 1 ELSE 0 END, due_ts, created_at LIMIT ?"
        return [_loop(r) for r in self._q(sql, (*params, limit))]

    def loop(self, loop_id: int) -> Optional[dict]:
        rows = self._q(f"SELECT {_LOOP_COLS} FROM loops WHERE id=?", (loop_id,))
        return _loop(rows[0]) if rows else None

    def add_loop(self, *, chat_id: str, kind: str, text: str, due_ts: Optional[float], message_id: str,
                 now: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO loops(chat_id,kind,text,due_ts,message_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (chat_id, kind, text, due_ts, message_id, now, now))
            self._conn.commit()
            return int(cur.lastrowid)

    def close_loop(self, loop_id: int, now: float, *, status: str = "done", reason: str = "") -> bool:
        return self._x("UPDATE loops SET status=?, closed_at=?, updated_at=?, close_reason=? WHERE id=? AND"
                       " status='open'", (status, now, now, reason[:200], loop_id)) > 0

    # ── questions for the user ──
    def add_question(self, *, kind: str, text: str, data: dict, dedupe_key: str, now: float) -> Optional[int]:
        """None when the same question was asked before (answered or not)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO questions(kind,text,data,dedupe_key,created_at) VALUES(?,?,?,?,?)",
                (kind, text, json.dumps(data, ensure_ascii=False), dedupe_key, now))
            self._conn.commit()
            return int(cur.lastrowid) if cur.rowcount else None

    def questions(self, *, status: str = "open", limit: int = 20) -> list[dict]:
        rows = self._q("SELECT id,kind,text,data,status,answer,created_at FROM questions WHERE status=?"
                       " ORDER BY created_at LIMIT ?", (status, limit))
        return [_question(r) for r in rows]

    def question(self, question_id: int) -> Optional[dict]:
        rows = self._q("SELECT id,kind,text,data,status,answer,created_at FROM questions WHERE id=?", (question_id,))
        return _question(rows[0]) if rows else None

    def close_question(self, question_id: int, now: float, *, status: str, answer: str = "") -> None:
        self._x("UPDATE questions SET status=?, answer=?, answered_at=? WHERE id=?",
                (status, answer[:500], now, question_id))

    def count_questions(self) -> int:
        return int(self._q("SELECT COUNT(*) FROM questions WHERE status='open'")[0][0])

    # ── chat context ──
    def chat_context(self, chat_id: str) -> dict:
        rows = self._q("SELECT summary, memory_ts, chunk_ts, language, updated_at FROM chat_context WHERE chat_id=?",
                       (chat_id,))
        if not rows:
            return {"chat_id": chat_id, "summary": "", "memory_ts": 0.0, "chunk_ts": 0.0, "language": "",
                    "updated_at": 0.0}
        r = rows[0]
        return {"chat_id": chat_id, "summary": r[0], "memory_ts": r[1], "chunk_ts": r[2], "language": r[3],
                "updated_at": r[4]}

    def set_chat_context(self, chat_id: str, now: float, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("summary", "memory_ts", "chunk_ts", "language")}
        cols = ", ".join(allowed)
        marks = ", ".join("?" * len(allowed))
        updates = ", ".join(f"{k}=excluded.{k}" for k in allowed)
        self._x(f"INSERT INTO chat_context(chat_id, {cols}, updated_at) VALUES(?, {marks}, ?)"
                f" ON CONFLICT(chat_id) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
                (chat_id, *allowed.values(), now))

    def chats_behind(self, column: str, *, since_ts: float, limit: int) -> list[str]:
        """Chats with messages newer than what ``column`` (memory_ts or chunk_ts) has covered,
        most recently active first. Only chats active after ``since_ts``."""
        assert column in ("memory_ts", "chunk_ts")
        rows = self._q(
            f"SELECT m.chat_id, MAX(m.ts) AS newest FROM messages m LEFT JOIN chat_context x ON x.chat_id=m.chat_id"
            f" GROUP BY m.chat_id HAVING newest > COALESCE(MAX(x.{column}), 0) AND newest >= ?"
            " ORDER BY newest DESC LIMIT ?", (since_ts, limit))
        return [r[0] for r in rows]

    def messages_between(self, chat_id: str, after_ts: float, *, limit: int) -> list[dict]:
        """Messages newer than ``after_ts``, oldest first (at most ``limit``, the oldest ones)."""
        rows = self._q("SELECT id,chat_id,from_me,sender,text,ts FROM messages WHERE chat_id=? AND ts>?"
                       " ORDER BY ts LIMIT ?", (chat_id, after_ts, limit))
        return [{"id": r[0], "chat_id": r[1], "from_me": bool(r[2]), "sender": r[3], "text": r[4], "ts": r[5]}
                for r in rows]

    # ── chunks and vectors ──
    def chunks_from(self, chat_id: str, ts: float) -> list[dict]:
        """Chunks that end at or after ``ts`` (the ones a change at ``ts`` can affect)."""
        rows = self._q(f"SELECT {_CHUNK_COLS} FROM chunks WHERE chat_id=? AND end_ts>=? ORDER BY start_ts",
                       (chat_id, ts))
        return [_chunk(r) for r in rows]

    def last_chunk(self, chat_id: str) -> Optional[dict]:
        rows = self._q(f"SELECT {_CHUNK_COLS} FROM chunks WHERE chat_id=? ORDER BY start_ts DESC LIMIT 1", (chat_id,))
        return _chunk(rows[0]) if rows else None

    def replace_chunks(self, chat_id: str, old_ids: list[int], new: list[dict], now: float) -> list[int]:
        """Swap ``old_ids`` for ``new`` chunks. A new chunk with the same text as an old one keeps
        its vector (no new embedding). Returns the ids of new chunks that need embedding."""
        with self._lock:
            c = self._conn
            kept: dict[str, tuple] = {}
            if old_ids:
                marks = ",".join("?" * len(old_ids))
                for h, model, dims, vec in c.execute(
                        f"SELECT ch.hash, v.model, v.dims, v.vec FROM chunks ch JOIN chunk_vectors v ON v.chunk_id=ch.id"
                        f" WHERE ch.id IN ({marks})", old_ids):
                    kept[h] = (model, dims, vec)
                c.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({marks})", old_ids)
                if self.vec_index and self._has_vec_table():
                    c.executemany("DELETE FROM vec_chunks WHERE chunk_id=?", [(i,) for i in old_ids])
                c.execute(f"DELETE FROM chunks WHERE id IN ({marks})", old_ids)
            todo = []
            for ch in new:
                h = text_hash(ch["text"])
                cur = c.execute("INSERT INTO chunks(chat_id,start_ts,end_ts,msg_count,text,hash,updated_at)"
                                " VALUES(?,?,?,?,?,?,?)",
                                (chat_id, ch["start_ts"], ch["end_ts"], ch["msg_count"], ch["text"], h, now))
                cid = int(cur.lastrowid)
                if h in kept:
                    model, dims, vec = kept.pop(h)
                    self._put_vector(cid, model, dims, vec)
                else:
                    todo.append(cid)
            c.commit()
        return todo

    def chunks_without_vectors(self, model: str, limit: int) -> list[dict]:
        rows = self._q(f"SELECT {_CHUNK_COLS} FROM chunks ch WHERE NOT EXISTS (SELECT 1 FROM chunk_vectors v"
                       " WHERE v.chunk_id=ch.id AND v.model=?) ORDER BY end_ts DESC LIMIT ?", (model, limit))
        return [_chunk(r) for r in rows]

    def chunks_by_ids(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        rows = self._q(f"SELECT {_CHUNK_COLS} FROM chunks WHERE id IN ({','.join('?' * len(ids))})", ids)
        return {r[0]: _chunk(r) for r in rows}

    def chunk_at(self, chat_id: str, ts: float) -> Optional[int]:
        rows = self._q("SELECT id FROM chunks WHERE chat_id=? AND start_ts<=? AND end_ts>=? LIMIT 1", (chat_id, ts, ts))
        return int(rows[0][0]) if rows else None

    def _has_vec_table(self) -> bool:
        return bool(self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_chunks'").fetchone())

    def _vec_dims(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='vec_dims'").fetchone()
        return int(json.loads(row[0])) if row else 0

    def _put_vector(self, chunk_id: int, model: str, dims: int, blob: bytes) -> None:
        c = self._conn
        c.execute("INSERT OR REPLACE INTO chunk_vectors(chunk_id,model,dims,vec) VALUES(?,?,?,?)",
                  (chunk_id, model, dims, blob))
        if self.vec_index and self._has_vec_table() and self._vec_dims() == dims:
            c.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (chunk_id,))
            c.execute("INSERT INTO vec_chunks(chunk_id, embedding) VALUES(?, ?)", (chunk_id, blob))

    def put_vectors(self, model: str, dims: int, items: list[tuple[int, list[float]]]) -> None:
        with self._lock:
            for chunk_id, vec in items:
                self._put_vector(chunk_id, model, dims, pack(normalize(vec)))
            self._conn.commit()

    def use_embedding_model(self, model: str, dims: int) -> None:
        """Switching models (or sizes) makes old vectors meaningless: drop them, and rebuild the
        fast index at the new size. Chunks are re-embedded gradually."""
        with self._lock:
            c = self._conn
            c.execute("DELETE FROM chunk_vectors WHERE model!=? OR dims!=?", (model, dims))
            if self.vec_index and (not self._has_vec_table() or self._vec_dims() != dims):
                c.execute("DROP TABLE IF EXISTS vec_chunks")
                c.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(chunk_id integer primary key,"
                          f" embedding float[{int(dims)}] distance_metric=cosine)")
                c.execute("INSERT INTO meta(key,value) VALUES('vec_dims',?) ON CONFLICT(key)"
                          " DO UPDATE SET value=excluded.value", (json.dumps(dims),))
                c.execute("INSERT INTO vec_chunks(chunk_id, embedding) SELECT chunk_id, vec FROM chunk_vectors")
            c.execute("INSERT INTO meta(key,value) VALUES('embedding_model',?) ON CONFLICT(key)"
                      " DO UPDATE SET value=excluded.value", (json.dumps(f"{model}:{dims}"),))
            c.commit()

    def nearest_chunks(self, query: list[float], model: str, k: int) -> list[tuple[int, float]]:
        """(chunk_id, cosine similarity), best first."""
        q = normalize(query)
        dims = len(q)
        if self.vec_index and self._has_vec_table() and self._vec_dims() == dims:
            rows = self._q("SELECT chunk_id, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ?",
                           (pack(q), k))
            return [(int(r[0]), 1.0 - float(r[1])) for r in rows]
        scored = []
        for chunk_id, blob in self._q("SELECT chunk_id, vec FROM chunk_vectors WHERE model=? AND dims=?",
                                      (model, dims)):
            v = unpack(blob)
            scored.append((int(chunk_id), sum(a * b for a, b in zip(q, v))))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def forget_chat_memory(self, chat_id: str) -> None:
        """Drop derived context for a chat (used when a chat is deleted)."""
        with self._lock:
            c = self._conn
            ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE chat_id=?", (chat_id,))]
            if ids:
                marks = ",".join("?" * len(ids))
                c.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({marks})", ids)
                if self.vec_index and self._has_vec_table():
                    c.executemany("DELETE FROM vec_chunks WHERE chunk_id=?", [(i,) for i in ids])
                c.execute(f"DELETE FROM chunks WHERE id IN ({marks})", ids)
            c.execute("DELETE FROM chat_context WHERE chat_id=?", (chat_id,))
            c.execute("DELETE FROM loops WHERE chat_id=?", (chat_id,))
            c.commit()


def _fact(r: tuple) -> dict:
    return {"id": r[0], "contact_id": r[1], "text": r[2], "category": r[3], "status": r[4], "source": r[5],
            "chat_id": r[6], "message_id": r[7], "confidence": r[8], "replaces": r[9], "created_at": r[10],
            "updated_at": r[11]}


def _loop(r: tuple) -> dict:
    return {"id": r[0], "chat_id": r[1], "kind": r[2], "text": r[3], "due_ts": r[4], "status": r[5],
            "message_id": r[6], "created_at": r[7], "updated_at": r[8], "closed_at": r[9], "close_reason": r[10]}


def _question(r: tuple) -> dict:
    return {"id": r[0], "kind": r[1], "text": r[2], "data": json.loads(r[3] or "{}"), "status": r[4],
            "answer": r[5], "created_at": r[6]}


def _chunk(r: tuple) -> dict:
    return {"id": r[0], "chat_id": r[1], "start_ts": r[2], "end_ts": r[3], "msg_count": r[4], "text": r[5],
            "hash": r[6]}
