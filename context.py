"""The context engine: what Hermes knows about your conversations, kept small and current.

Built the way ChatGPT and Claude keep context light: a few always-useful facts per person
(like saved memories), a short rolling summary per chat (instead of re-reading history), a
list of open loops, and search by meaning that pulls details only when needed. Tools hand
Hermes a compact brief; nothing large is preloaded into the conversation.

``ContextEngine.process_once`` runs after each background sync cycle, entirely from the local
cache (it never calls Unipile):
  1. Chunks: new messages are grouped into short conversation windows (with the chat name and
     date on top, so each chunk makes sense on its own). Only the changed tail is rebuilt; an
     unchanged chunk keeps its embedding.
  2. Embeddings: chunks without a vector are embedded with the provider the user chose, within
     a daily character cap.
  3. Memory: for chats with new messages (once a burst of messages has settled), one small
     call to the language model updates the chat summary, facts about the person, and open
     loops (who is waiting on whom, promises with dates). Facts it is unsure of, and changes
     to earlier facts, become questions for the user instead of being saved. Group chats get
     a summary and loops but no personal facts. A daily call cap bounds the cost.

Secrets (one-time codes, passwords, card and account numbers, ID numbers) are redacted before
anything is sent to a model or provider, and never saved as facts.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .client import InboxError
from .embeddings import Embedder
from .memory_store import text_hash
from .pacing import DAY, NETWORK_LABELS
from .privacy import is_sensitive, redact

Llm = Callable[[str, str, dict], Optional[dict]]

CHUNK_GAP = 6 * 3600       # a quiet gap this long starts a new conversation window
CHUNK_MAX_MSGS = 12
CHUNK_MAX_CHARS = 1500
MSG_MAX_CHARS = 500
SETTLE_SECONDS = 120       # wait for a burst of messages to finish before reading it...
SETTLE_MAX_NEW = 10        # ...unless this many are already waiting
MEMORY_BATCH = 40          # messages per model call
MIN_MEANING_SCORE = 0.15   # cosine similarity floor for "related" results...
RELATIVE_MEANING_SCORE = 0.7  # ...and at least this share of the best match, so weak ones are dropped
AUTO_SAVE_CONFIDENCE = 0.7
AUTO_UPDATE_CONFIDENCE = 0.9
MAX_FACTS_IN_PROMPT = 25
FAILURE_LIMIT = 3          # after this many failed calls on the same messages, skip past them
FACT_CATEGORIES = ["relationship", "work", "personal", "preference", "plans", "contact", "context", "other"]
LOOP_KINDS = ["they_wait", "you_wait", "you_promised", "they_promised"]
_YES = {"yes", "y", "yeah", "yep", "yup", "haan", "ha", "han", "haa", "haan ji", "ji", "ji haan", "correct",
        "right", "sure", "ok", "okay", "true", "same", "same person", "हाँ", "हां"}
_NO = {"no", "n", "nope", "nah", "nahi", "nahin", "na", "wrong", "false", "not", "different", "नहीं"}

INSTRUCTIONS = """You maintain a private memory of one chat for the user (called "me").
Messages may be in English, Hindi or Hinglish (Hindi written in Latin letters). Read all of
them; write your output in English, quoting short original words when they matter.

Return JSON with:
- summary: the whole conversation so far in at most 80 words, updating the current summary with
  the new messages. Keep what still matters, drop what no longer does.
- facts_new: lasting facts about the other person that would help the user later (who they are,
  relationship to the user, work, family, preferences, important dates, plans). Not facts about
  the user, not one-off chit-chat, not anything already in the known facts. Never codes,
  passwords, card, bank or ID numbers. Leave empty for group chats. confidence 0-1: how sure you
  are the fact is true and lasting.
- facts_replace: known facts the new messages show are now different (give the fact id and the
  new text). Only clear changes.
- loops_new: things still open after these messages. kind: they_wait (they asked or need
  something from me and I have not answered), you_wait (I asked them and they have not
  answered), you_promised (I said I would do something), they_promised (they said they would).
  due: ISO date or date-time if a time is stated or clearly implied, else null. Resolve relative
  dates from today's date. In Hindi "kal" can mean yesterday or tomorrow and "parso" the day
  before or after: decide from context, and if you cannot, add a question instead of guessing.
  message_id: the message that opened it.
- loops_close: ids of open items the new messages settle (answered, done, cancelled), with a
  short reason.
- questions: at most 2 short questions to ask the user, only when something important is truly
  ambiguous (who "he" is, which day is meant). Usually empty.
- language: the main language of the chat (e.g. English, Hindi, Hinglish)."""

SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "facts_new", "facts_replace", "loops_new", "loops_close", "questions", "language"],
    "properties": {
        "summary": {"type": "string"},
        "facts_new": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "category", "confidence"],
            "properties": {"text": {"type": "string"}, "category": {"type": "string", "enum": FACT_CATEGORIES},
                           "confidence": {"type": "number"}}}},
        "facts_replace": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "text", "confidence"],
            "properties": {"id": {"type": "integer"}, "text": {"type": "string"}, "confidence": {"type": "number"}}}},
        "loops_new": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "text", "due", "message_id"],
            "properties": {"kind": {"type": "string", "enum": LOOP_KINDS}, "text": {"type": "string"},
                           "due": {"type": ["string", "null"]}, "message_id": {"type": "string"}}}},
        "loops_close": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "reason"],
            "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}}}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
    },
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").casefold())).strip()


def _similar(a: str, b: str) -> bool:
    na, nb = set(_norm(a).split()), set(_norm(b).split())
    if not na or not nb:
        return False
    return len(na & nb) / len(na | nb) >= 0.8


def build_chunks(msgs: list[dict], chat: dict) -> list[dict]:
    """Group messages (oldest first) into conversation windows: a new window after a quiet gap,
    or when one gets long. Each starts with the app, chat and date, so it reads on its own."""
    label = NETWORK_LABELS.get(chat.get("network") or "", chat.get("network") or "")
    kind = "group" if chat.get("is_group") else "chat"
    name = chat.get("name") or "unknown"
    groups: list[list[tuple[dict, str]]] = []
    cur: list[tuple[dict, str]] = []
    chars = 0
    for m in msgs:
        who = "Me" if m["from_me"] else (m.get("sender") or name)
        line = f"{datetime.fromtimestamp(m['ts']).strftime('%H:%M')} {who}: {redact(m.get('text') or '')[:MSG_MAX_CHARS]}"
        if cur and (m["ts"] - cur[-1][0]["ts"] > CHUNK_GAP or len(cur) >= CHUNK_MAX_MSGS
                    or chars + len(line) > CHUNK_MAX_CHARS):
            groups.append(cur)
            cur, chars = [], 0
        cur.append((m, line))
        chars += len(line)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        start, end = g[0][0]["ts"], g[-1][0]["ts"]
        d1, d2 = (datetime.fromtimestamp(t).strftime("%Y-%m-%d") for t in (start, end))
        header = f"{label} {kind} with {name}, {d1}" + (f" to {d2}" if d2 != d1 else "")
        out.append({"start_ts": start, "end_ts": end, "msg_count": len(g),
                    "text": header + "\n" + "\n".join(line for _, line in g)})
    return out


def parse_due(value: Any) -> Optional[float]:
    """ISO date or date-time (local) -> Unix time. A bare date means the end of that day."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        if len(v) == 10:
            d = datetime.fromisoformat(v)
            return (d + timedelta(hours=23, minutes=59)).timestamp()
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


class ContextEngine:
    def __init__(self, inbox, llm: Optional[Llm] = None, post: Optional[Callable] = None):
        self.inbox = inbox
        self.llm = llm
        self._post = post
        self._embedder: Optional[Embedder] = None
        self._embedder_key: tuple = ()
        self._query_cache: dict[str, list[float]] = {}

    # ── helpers ──
    def _day(self) -> str:
        return self.inbox._local_now().strftime("%Y-%m-%d")

    def embedder(self) -> Optional[Embedder]:
        s = self.inbox.settings
        provider = (s.embedding_provider or "").lower()
        if provider in ("", "none", "off"):
            return None
        key = (self.inbox._get_secret("EMBEDDING_API_KEY") or "").strip()
        if not key:
            return None
        ident = (provider, s.embedding_model, key)
        if ident != self._embedder_key:
            self._embedder = Embedder(provider, key, s.embedding_model, post=self._post)
            self._embedder_key = ident
            self._query_cache.clear()
        return self._embedder

    # ── one cycle ──
    def process_once(self) -> dict:
        inbox = self.inbox
        out: dict = {"chunked": 0, "embedded": 0, "memorized": 0, "questions": 0, "backlog": False, "problems": []}
        if not inbox.configured:
            out["skipped"] = "setup"
            return out
        s, store, now = inbox.settings, inbox.store(), inbox.clock()
        since = now - max(s.initial_sync_days, 1) * DAY
        q_before = store.count_questions()

        self._apply_edits()
        if s.memory_enabled:  # one person across apps: everyone gets a profile, likely matches are asked
            created = inbox.identity.ensure_people(since)
            out["questions_link"] = inbox.identity.suggest_links(force=bool(created))
        if s.style_learning and inbox.style.due():
            inbox.style.learn(use_model=bool(self.llm) and s.memory_enabled)
        behind = store.chats_behind("chunk_ts", since_ts=since, limit=51)
        for chat_id in behind[:50]:
            self.update_chunks(chat_id)
            out["chunked"] += 1
        out["backlog"] |= len(behind) > 50

        emb = self.embedder()
        if emb:
            try:
                out["embedded"], more = self.embed_pending(emb)
                out["backlog"] |= more
            except InboxError as exc:
                out["problems"].append(f"Embeddings: {exc}")

        if self.llm and s.memory_enabled:
            done = 0
            for chat_id in store.chats_behind("memory_ts", since_ts=since, limit=s.memory_chats_per_cycle * 4):
                if store.usage_today(self._day(), "llm_calls") >= s.memory_llm_calls_per_day:
                    out["problems"].append("Memory: daily model-call limit reached; continuing tomorrow.")
                    break
                if not self._settled(chat_id, now):
                    continue
                if done >= s.memory_chats_per_cycle:
                    out["backlog"] = True
                    break
                try:
                    if self.memorize(chat_id):
                        out["memorized"] += 1
                except InboxError as exc:
                    out["problems"].append(f"Memory: {exc}")
                done += 1
        out["questions"] = store.count_questions() - q_before
        store.set_meta("context_state", {"last_run_at": now, "problems": out["problems"][:5],
                                         "last": {k: v for k, v in out.items() if k != "problems"}})
        return out

    def _settled(self, chat_id: str, now: float) -> bool:
        store = self.inbox.store()
        cc = store.chat_context(chat_id)
        new = store.messages_between(chat_id, cc["memory_ts"], limit=SETTLE_MAX_NEW)
        return bool(new) and (len(new) >= SETTLE_MAX_NEW or now - new[-1]["ts"] >= SETTLE_SECONDS
                              or now - store.newest_message_ts(chat_id) >= SETTLE_SECONDS)

    # ── chunks ──
    def _apply_edits(self) -> None:
        """Rebuild the windows that contain edited or deleted messages."""
        store = self.inbox.store()
        cursor = int(store.get_meta("ctx_edit_cursor", 0) or 0)
        events = store.events_after(cursor, types=["message.update", "message.delete"], limit=500)
        for e in events:
            ts = e["data"].get("ts")
            if e["chat_id"] and ts:
                self.update_chunks(e["chat_id"], from_ts=float(ts))
        if events:
            store.set_meta("ctx_edit_cursor", events[-1]["id"])

    def update_chunks(self, chat_id: str, from_ts: Optional[float] = None) -> list[int]:
        """Rebuild the chat's windows from ``from_ts`` (default: where new messages start).
        Returns the ids of chunks that need an embedding."""
        store, now = self.inbox.store(), self.inbox.clock()
        cc = store.chat_context(chat_id)
        if from_ts is None:
            first_new = store.messages_between(chat_id, cc["chunk_ts"], limit=1)
            if not first_new:
                return []
            from_ts = first_new[0]["ts"]
        old = store.chunks_from(chat_id, from_ts)
        last = store.last_chunk(chat_id)
        if not old and last and from_ts - last["end_ts"] <= CHUNK_GAP and last["msg_count"] < CHUNK_MAX_MSGS:
            old = [last]  # new messages continue the last window
        start = min([c["start_ts"] for c in old] + [from_ts])
        msgs = store.messages_between(chat_id, start - 1e-6, limit=100000)
        chat = store.cached_chat(chat_id) or {"name": "", "network": "", "is_group": False}
        new_chunks = build_chunks(msgs, chat)
        todo = store.replace_chunks(chat_id, [c["id"] for c in old], new_chunks, now)
        newest = msgs[-1]["ts"] if msgs else cc["chunk_ts"]
        store.set_chat_context(chat_id, now, chunk_ts=max(newest, cc["chunk_ts"]))
        return todo

    def embed_pending(self, emb: Embedder, limit: int = 256) -> tuple[int, bool]:
        """Embed chunks that have no vector yet. Returns (count, more_waiting)."""
        store, s = self.inbox.store(), self.inbox.settings
        day = self._day()
        budget = s.embedding_chars_per_day - store.usage_today(day, "embed_chars")
        pending = store.chunks_without_vectors(emb.name, limit + 1)
        more = len(pending) > limit
        batch, chars = [], 0
        for ch in pending[:limit]:
            if chars + len(ch["text"]) > budget:
                more = True
                break
            batch.append(ch)
            chars += len(ch["text"])
        if not batch:
            return 0, more
        vecs = emb.embed([c["text"] for c in batch])
        store.add_usage(day, "embed_chars", chars)
        dims = len(vecs[0])
        if store.get_meta("embedding_model") != f"{emb.name}:{dims}":
            store.use_embedding_model(emb.name, dims)
        store.put_vectors(emb.name, dims, [(c["id"], v) for c, v in zip(batch, vecs)])
        return len(batch), more

    def search_meaning(self, query: str, *, k: int = 8, network: Optional[str] = None) -> list[dict]:
        """Chunks closest in meaning to ``query`` (empty without an embedding provider)."""
        emb = self.embedder()
        if not emb:
            return []
        store = self.inbox.store()
        model = store.get_meta("embedding_model") or ""
        if not model.startswith(emb.name + ":"):
            return []  # nothing embedded with this model yet
        vec = self._query_cache.get(query)
        if vec is None:
            vec = emb.embed([redact(query)], query=True)[0]
            self._query_cache[query] = vec
            if len(self._query_cache) > 200:
                self._query_cache.pop(next(iter(self._query_cache)))
        hits = store.nearest_chunks(vec, emb.name, k * 3 if network else k)
        best = hits[0][1] if hits else 0.0
        hits = [(cid, sc) for cid, sc in hits if sc >= max(MIN_MEANING_SCORE, best * RELATIVE_MEANING_SCORE)]
        chunks = store.chunks_by_ids([cid for cid, _ in hits])
        out = []
        for cid, score in hits:
            ch = chunks.get(cid)
            chat = store.cached_chat(ch["chat_id"]) if ch else None
            if not ch or (network and (not chat or chat["network"] != network)):
                continue
            out.append({"chunk_id": cid, "chat_id": ch["chat_id"], "chat_name": (chat or {}).get("name", ""),
                        "network": (chat or {}).get("network", ""), "start_ts": ch["start_ts"],
                        "end_ts": ch["end_ts"], "text": ch["text"], "score": round(score, 3)})
        return out[:k]

    # ── memory ──
    def memorize(self, chat_id: str) -> bool:
        """Read the chat's unread-by-memory messages (one batch) with the language model."""
        inbox = self.inbox
        store, now = inbox.store(), inbox.clock()
        chat = store.cached_chat(chat_id)
        cc = store.chat_context(chat_id)
        new = store.messages_between(chat_id, cc["memory_ts"], limit=MEMORY_BATCH)
        if not chat or not new:
            if new:
                store.set_chat_context(chat_id, now, memory_ts=new[-1]["ts"])
            return False
        contact = None if chat["is_group"] else store.ensure_contact(
            chat_id=chat_id, network=chat["network"], name=chat["name"], now=now)
        facts = store.facts(contact["id"], limit=MAX_FACTS_IN_PROMPT) if contact else []
        loops = store.loops(chat_id=chat_id)
        prompt = self._prompt(chat, cc["summary"], facts, loops, new)
        store.add_usage(self._day(), "llm_calls", 1)
        try:
            result = self.llm(INSTRUCTIONS, prompt, SCHEMA) if self.llm else None
        except Exception as exc:  # noqa: BLE001 - provider errors vary; recorded and retried
            result = None
            error = str(exc) or type(exc).__name__
        else:
            error = "no usable answer"
        if isinstance(result, dict):
            try:
                self._apply(result, chat, contact, facts, loops, new, now)
            except InboxError:
                raise
            except Exception as exc:  # noqa: BLE001 - an answer in the wrong shape (a list where a
                # number belongs...): counted like any failed call, so it is not retried for ever
                result, error = None, f"an answer in the wrong shape ({type(exc).__name__})"
        if not isinstance(result, dict):
            fails = int(store.get_meta(f"memory_fail:{chat_id}", 0) or 0) + 1
            if fails >= FAILURE_LIMIT:  # do not retry the same messages forever
                store.set_chat_context(chat_id, now, memory_ts=new[-1]["ts"])
                store.set_meta(f"memory_fail:{chat_id}", 0)
            else:
                store.set_meta(f"memory_fail:{chat_id}", fails)
            raise InboxError(f"the language model gave {error} for {chat['name'] or chat_id}", kind="memory")
        store.set_chat_context(chat_id, now, memory_ts=new[-1]["ts"],
                               summary=redact(str(result.get("summary") or cc["summary"]))[:800],
                               language=str(result.get("language") or cc["language"])[:40])
        store.set_meta(f"memory_fail:{chat_id}", 0)
        return True

    def _prompt(self, chat: dict, summary: str, facts: list[dict], loops: list[dict], new: list[dict]) -> str:
        local = self.inbox._local_now()
        name = chat["name"] or "this contact"
        lines = [f"Today: {local.strftime('%A %Y-%m-%d %H:%M')} (user's local time)",
                 f"Chat: {NETWORK_LABELS.get(chat['network'], chat['network'])}"
                 f" {'group' if chat['is_group'] else 'one-to-one chat'} with {name}",
                 f"Current summary: {summary or '(none yet)'}"]
        if not chat["is_group"]:
            lines.append(f"Known facts about {name}:")
            lines += [f"  #{f['id']} {f['text']}" for f in facts] or ["  (none)"]
        lines.append("Open items:")
        lines += [f"  #{lp['id']} {lp['kind']}: {lp['text']}"
                  + (f" (due {datetime.fromtimestamp(lp['due_ts']).strftime('%Y-%m-%d %H:%M')})" if lp["due_ts"] else "")
                  for lp in loops] or ["  (none)"]
        lines.append("New messages, oldest first:")
        for m in new:
            who = "me" if m["from_me"] else (m["sender"] or name)
            when = datetime.fromtimestamp(m["ts"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  [{m['id']}] {when} {who}: {redact(m['text'] or '')[:MSG_MAX_CHARS]}")
        return "\n".join(lines)

    def _apply(self, r: dict, chat: dict, contact: Optional[dict], facts: list[dict], loops: list[dict],
               new: list[dict], now: float) -> None:
        store = self.inbox.store()
        name = chat["name"] or "this contact"
        last_msg = new[-1]["id"] if new else ""
        if contact:
            known = {f["id"]: f for f in facts}
            pending = store.facts(contact["id"], status="pending")
            for f in r.get("facts_new") or []:
                text = str(f.get("text") or "").strip()[:300]
                if not text or is_sensitive(text):
                    continue
                if any(_similar(text, k["text"]) for k in [*known.values(), *pending]):
                    continue
                conf = float(f.get("confidence") or 0)
                category = f.get("category") if f.get("category") in FACT_CATEGORIES else "other"
                if conf >= AUTO_SAVE_CONFIDENCE:
                    fid = store.add_fact(contact_id=contact["id"], text=text, category=category, status="active",
                                         source="auto", now=now, chat_id=chat["id"], message_id=last_msg,
                                         confidence=conf)
                    known[fid] = store.fact(fid)
                else:  # unsure: ask instead of guessing
                    fid = store.add_fact(contact_id=contact["id"], text=text, category=category, status="pending",
                                         source="auto", now=now, chat_id=chat["id"], message_id=last_msg,
                                         confidence=conf)
                    store.add_question(kind="fact", text=f"About {name}: \"{text}\". Is that right?",
                                       data={"fact_id": fid, "fact_gid": store.gid_of("facts", fid)},
                                       dedupe_key=f"fact:{store.gid_of('facts', fid)}", now=now)
            for f in r.get("facts_replace") or []:
                old = known.get(f.get("id"))
                text = str(f.get("text") or "").strip()[:300]
                if not old or not text or is_sensitive(text) or _similar(text, old["text"]):
                    continue
                conf = float(f.get("confidence") or 0)
                if conf >= AUTO_UPDATE_CONFIDENCE and old["source"] != "user":
                    store.set_fact(old["id"], now, status="superseded")
                    store.add_fact(contact_id=contact["id"], text=text, category=old["category"], status="active",
                                   source="auto", now=now, chat_id=chat["id"], message_id=last_msg, confidence=conf)
                else:  # a change to what we knew is worth a check (always, for what the user told us)
                    fid = store.add_fact(contact_id=contact["id"], text=text, category=old["category"],
                                         status="pending", source="auto", now=now, chat_id=chat["id"],
                                         message_id=last_msg, confidence=conf, replaces=old["id"])
                    store.add_question(kind="update",
                                       text=f"About {name}, I had \"{old['text']}\". Now it looks like \"{text}\"."
                                            " Should I update it?",
                                       data={"fact_id": fid, "old_id": old["id"],
                                             "fact_gid": store.gid_of("facts", fid),
                                             "old_gid": store.gid_of("facts", old["id"])},
                                       dedupe_key=f"update:{store.gid_of('facts', fid)}", now=now)
        open_ids = {lp["id"] for lp in loops}
        for c in r.get("loops_close") or []:
            if c.get("id") in open_ids:
                store.close_loop(int(c["id"]), now, reason=str(c.get("reason") or "")[:200])
        msg_ids = {m["id"] for m in new}
        current = store.loops(chat_id=chat["id"])
        for lp in r.get("loops_new") or []:
            text = redact(str(lp.get("text") or "").strip())[:300]
            kind = lp.get("kind")
            if not text or kind not in LOOP_KINDS or any(_similar(text, o["text"]) for o in current):
                continue
            mid = lp.get("message_id") if lp.get("message_id") in msg_ids else last_msg
            lid = store.add_loop(chat_id=chat["id"], kind=kind, text=text, due_ts=parse_due(lp.get("due")),
                                 message_id=mid, now=now)
            current.append(store.loop(lid))
        for q in (r.get("questions") or [])[:2]:
            q = str(q or "").strip()[:300]
            if q and not is_sensitive(q):
                store.add_question(kind="meaning", text=f"In your chat with {name}: {q}",
                                   data={"chat_id": chat["id"], "contact_id": contact["id"] if contact else ""},
                                   dedupe_key=f"meaning:{chat['id']}:{text_hash(_norm(q))[:16]}", now=now)

    # ── answering questions ──
    def answer(self, question_id: int, answer: str) -> dict:
        store, now = self.inbox.store(), self.inbox.clock()
        q = store.question(int(question_id))
        if not q or q["status"] != "open":
            raise InboxError(f"No open question {question_id}.", kind="not_found")
        raw = (answer or "").strip()
        a = _norm(raw)
        yes, no = a in _YES, a in _NO
        d = q["data"]
        result = "noted"
        if q["kind"] in ("fact", "update"):
            fact = self._fact_ref(d, "fact")
            if not fact:
                result = "that fact no longer exists"
            elif no:
                store.delete_fact(fact["id"])
                result = "not saved"
            else:
                if not yes:
                    if is_sensitive(raw):
                        raise InboxError("That looks like a code, password or account number; it will not be"
                                         " saved.", kind="input")
                    store.set_fact(fact["id"], now, text=raw[:300], source="user")
                store.set_fact(fact["id"], now, status="active")
                old = self._fact_ref(d, "old") if q["kind"] == "update" else None
                if old:
                    store.set_fact(old["id"], now, status="superseded")
                result = "saved"
        elif q["kind"] == "merge":
            if not yes and not no:
                raise InboxError("Answer yes or no: is it the same person?", kind="input")
            result = self.inbox.identity.answer_link(q, yes)
        else:  # meaning: keep the user's clarification
            if raw and not is_sensitive(raw):
                if d.get("contact_id"):
                    store.add_fact(contact_id=d["contact_id"], text=f"{q['text']} - {raw}"[:300], category="context",
                                   status="active", source="user", now=now, chat_id=d.get("chat_id", ""))
                elif d.get("chat_id"):
                    cc = store.chat_context(d["chat_id"])
                    store.set_chat_context(d["chat_id"], now, summary=(cc["summary"] + f" Note: {raw}")[:800])
                result = "saved"
        store.close_question(q["id"], now, status="answered", answer=raw)
        return {"question_id": q["id"], "result": result}

    def dismiss(self, question_id: int) -> None:
        store, now = self.inbox.store(), self.inbox.clock()
        q = store.question(int(question_id))
        if not q or q["status"] != "open":
            raise InboxError(f"No open question {question_id}.", kind="not_found")
        if q["kind"] in ("fact", "update"):
            fact = self._fact_ref(q["data"], "fact")
            if fact:
                store.delete_fact(fact["id"])
        store.close_question(q["id"], now, status="dismissed")

    def _fact_ref(self, data: dict, which: str) -> Optional[dict]:
        """The fact a question points at: by its cross-device id when there is one (the question
        may have been asked on another device), else by the local id."""
        store = self.inbox.store()
        gid = data.get(f"{which}_gid")
        if gid:
            return store.fact_by_gid(gid)
        local = data.get(f"{which}_id")
        return store.fact(int(local)) if local else None

    # ── what tools show ──
    def find(self, *, chat_id: str = "", person: str = "") -> list[dict]:
        """Chats matching ``chat_id`` or a person's name (best first)."""
        store = self.inbox.store()
        if chat_id:
            chat = store.cached_chat(chat_id)
            return [chat] if chat else []
        want = _norm(person)
        if not want:
            return []
        chats = store.cached_chats(limit=5000)
        exact = [c for c in chats if _norm(c["name"]) == want]
        partial = [c for c in chats if c not in exact and want in _norm(c["name"])]
        return exact + partial

    def brief(self, chat: dict, *, recent: Optional[list[dict]] = None) -> dict:
        """Everything worth knowing about one chat, compact: who, what, what's open."""
        store, now = self.inbox.store(), self.inbox.clock()
        contact = store.contact_for_chat(chat["id"])
        out: dict = {"chat_id": chat["id"], "name": chat["name"], "network": chat["network"],
                     "group": chat["is_group"]}
        if contact:
            full = store.contact(contact["id"]) or {}
            out["person"] = contact["name"]
            others = [c for c in full.get("chats", []) if c["chat_id"] != chat["id"]]
            if others:
                also = []
                for c in others:
                    other_chat = store.cached_chat(c["chat_id"])
                    item = {"chat_id": c["chat_id"], "network": c["network"]}
                    if other_chat:
                        item["last_activity_ts"] = other_chat["ts"]
                        item["last_from_me"] = other_chat["last_from_me"]
                    oc = store.chat_context(c["chat_id"])
                    if oc["summary"]:
                        item["summary"] = oc["summary"]
                    open_items = store.loops(chat_id=c["chat_id"])
                    if open_items:
                        item["open"] = [self.loop_view(lp, other_chat, now) for lp in open_items[:4]]
                    also.append(item)
                out["also_on"] = also
            out["about"] = [{"id": f["id"], "fact": f["text"]} for f in store.facts(contact["id"], limit=12)]
        cc = store.chat_context(chat["id"])
        if cc["summary"]:
            out["summary"] = cc["summary"]
        if cc["language"]:
            out["language"] = cc["language"]
        loops = store.loops(chat_id=chat["id"])
        if loops:
            out["open"] = [self.loop_view(lp, chat, now) for lp in loops[:8]]
        if recent:
            out["recent"] = [{"from": "me" if m["from_me"] else (m["sender"] or chat["name"]),
                              "text": (m["text"] or "")[:300], "ts": m["ts"]} for m in recent[-6:]]
        pending = store.count_questions()
        if pending:
            out["memory_questions_waiting"] = pending
        style = self.inbox.style.for_chat(chat)
        if style:
            out["your_style"] = style
        inbox = self.inbox
        scam = inbox.shield.for_chat(chat) if getattr(inbox, "shield", None) else None
        if scam:
            out["safety_warning"] = {"level": scam["level"], "reasons": scam["reasons"], "advice": scam["advice"]}
        planner = getattr(inbox, "planner", None)
        if planner is not None:
            plans = planner.upcoming(days=30, chat_id=chat["id"])
            if plans:
                out["plans"] = [{k: v for k, v in p.items() if k not in ("start_ts", "end_ts")} for p in plans[:4]]
        return out

    @staticmethod
    def loop_view(lp: dict, chat: Optional[dict], now: float) -> dict:
        view = {"id": lp["id"], "kind": lp["kind"], "text": lp["text"],
                "since_hours": round((now - lp["created_at"]) / 3600, 1)}
        if chat:
            view.update(chat_id=chat["id"], name=chat["name"], network=chat["network"])
        if lp["due_ts"]:
            view["due"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(lp["due_ts"]))
            view["due_ts"] = lp["due_ts"]
            view["overdue"] = lp["due_ts"] < now
        return view
