"""Auto-drafts: when an urgent message arrives, a reply is drafted for you right away.

Runs in the background worker after each sync (the device that leads, with cloud sync). For each
chat with new messages from someone else, once their burst has settled:
  * it must be urgent by the same rules as triage (urgent words, a question, several messages,
    in a group only when you are mentioned);
  * skipped when you already replied, already have a draft for that chat, or the chat is muted,
    archived, read-only or snoozed;
  * one draft per urgent moment (until you reply, more messages do not make another one);
  * never for history: only for messages that arrive after it was switched on.
The draft is written by Hermes's language model with the chat's summary, what memory knows about
the person (on every app they use), open items, the latest messages and your writing style. It is
saved like any draft (marked as Hermes's) and you get an alert. Nothing is ever sent: you review
it and send it yourself. A detail only you know is left as a blank like [time]; such a draft
cannot be sent until the blank is filled. If you answer in the app yourself, the draft is
withdrawn. Bounded by ``auto_draft_per_day`` (0 = off) and memory's daily model-call cap.
"""

from __future__ import annotations

import re
from datetime import datetime

from . import triage as triage_mod
from .client import InboxError
from .context import MSG_MAX_CHARS, SETTLE_MAX_NEW, SETTLE_SECONDS
from .pacing import DAY, NETWORK_LABELS
from .privacy import is_sensitive, redact

PLACEHOLDER = re.compile(r"\[[^\[\]\n]{1,40}\]")
FAIL_LIMIT = 2           # model failures on one urgent moment before giving up on it
STALE_SECONDS = DAY      # an urgent message older than this is not drafted any more
DONE_KEEP = 500          # how many chats' "already drafted" marks are remembered

INSTRUCTIONS = """You write one reply for the user ("me") to send in a chat. Write it exactly as the
user would: follow their writing guidelines and match their recent messages (length, tone,
language, greetings, emojis). Reply in the language the other person used (English, Hindi or
Hinglish in Latin letters). Answer what they asked or need, concretely and briefly. Never invent
facts, times, amounts, addresses or decisions only the user can make: put a short blank in square
brackets instead, like [time] or [yes/no]. Never include codes, passwords or account numbers.
Never promise money. In a group, address the person who mentioned the user. If no reply is
needed (an automated or promotional message, a one-time code, a plain "thanks" or "ok"), set
skip true with a short reason and leave text empty. Return JSON: text, skip, reason."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["text", "skip", "reason"],
          "properties": {"text": {"type": "string"}, "skip": {"type": "boolean"}, "reason": {"type": "string"}}}


def blanks(text: str) -> list[str]:
    """The [blanks] a draft still has (e.g. "[time]")."""
    return PLACEHOLDER.findall(text or "")


class AutoDrafter:
    def __init__(self, inbox):
        self.inbox = inbox

    def enabled(self) -> bool:
        inbox, s = self.inbox, self.inbox.settings
        return bool(inbox.context.llm) and s.memory_enabled and s.auto_draft_per_day > 0

    def run_once(self) -> dict:
        """Look at what arrived since last time and draft for chats that became urgent."""
        inbox = self.inbox
        out: dict = {"drafted": 0, "considered": 0, "problems": []}
        if not inbox.configured:
            return out
        store, now = inbox.store(), inbox.clock()
        self._withdraw_answered(now)
        cursor = store.get_meta("autodraft_cursor")
        if cursor is None or not self.enabled():
            # Start (again) from now: switching it on never drafts for history.
            store.set_meta("autodraft_cursor", store.last_event_id())
            store.set_meta("autodraft_pending", {})
            return out
        pending: dict = dict(store.get_meta("autodraft_pending", {}) or {})
        events = store.events_after(int(cursor), types=["message.new"], limit=1000)
        for e in events:
            if not e["data"].get("from_me") and e["chat_id"]:
                pending[e["chat_id"]] = max(float(pending.get(e["chat_id"], 0.0)), float(e["data"].get("ts") or e["ts"]))
        if events:
            store.set_meta("autodraft_cursor", events[-1]["id"])
        s = inbox.settings
        for chat_id, ts in sorted(pending.items(), key=lambda kv: kv[1]):
            if now - ts > STALE_SECONDS:
                pending.pop(chat_id)
                continue
            if not self._settled(chat_id, now):
                continue
            day = inbox.context._day()
            if store.usage_today(day, "auto_drafts") >= s.auto_draft_per_day or \
                    store.usage_today(day, "llm_calls") >= s.memory_llm_calls_per_day:
                out["problems"].append("Auto-drafts: daily limit reached; continuing tomorrow.")
                break
            pending.pop(chat_id)
            out["considered"] += 1
            try:
                if self.consider(chat_id, now):
                    out["drafted"] += 1
            except InboxError as exc:
                out["problems"].append(f"Auto-draft: {exc}")
                if self._failed(chat_id) < FAIL_LIMIT:
                    pending[chat_id] = ts  # try again next cycle
        store.set_meta("autodraft_pending", pending)
        if out["problems"]:
            store.set_meta("autodraft_state", {"at": now, "problems": out["problems"][:3]})
        return out

    def _settled(self, chat_id: str, now: float) -> bool:
        """Their burst of messages is over (or long enough to answer already)."""
        store = self.inbox.store()
        msgs = store.cached_messages(chat_id, SETTLE_MAX_NEW)
        tail = 0
        for m in reversed(msgs):
            if m["from_me"]:
                break
            tail += 1
        return tail >= SETTLE_MAX_NEW or now - store.newest_message_ts(chat_id) >= SETTLE_SECONDS

    def _failed(self, chat_id: str) -> int:
        store = self.inbox.store()
        fails = dict(store.get_meta("autodraft_fails", {}) or {})
        fails[chat_id] = int(fails.get(chat_id, 0)) + 1
        store.set_meta("autodraft_fails", dict(list(fails.items())[-200:]))
        return fails[chat_id]

    def consider(self, chat_id: str, now: float) -> bool:
        """Draft for this chat if it needs one now. True when a draft was saved."""
        inbox = self.inbox
        store, s = inbox.store(), inbox.settings
        chat = store.cached_chat(chat_id)
        if not chat or chat["is_muted"] or chat["is_archived"] or chat["is_readonly"]:
            return False
        if chat_id in store.snoozed_chat_ids(now):
            return False
        msgs = store.cached_messages(chat_id, 12)
        if not msgs or msgs[-1]["from_me"]:
            return False  # you already answered
        tail = []
        for m in reversed(msgs):
            if m["from_me"]:
                break
            tail.append(m)
        tail.reverse()
        episode = tail[0]["id"]
        done = dict(store.get_meta("autodraft_done", {}) or {})
        if done.get(chat_id) == episode:
            return False  # already drafted (or decided) for this urgent moment
        if store.pending_drafts_for_chat(chat_id):
            return False  # a draft is already waiting for you
        if now - tail[-1]["ts"] > STALE_SECONDS:
            return False
        if chat["is_group"] and not any((m.get("extra") or {}).get("mentioned") for m in tail):
            return False
        shield = getattr(inbox, "shield", None)
        if shield is not None and shield.for_chat(chat, msgs):
            done[chat_id] = episode  # never answer what looks like a scam (you got a warning instead)
            store.set_meta("autodraft_done", dict(list(done.items())[-DONE_KEEP:]))
            return False
        verdict = triage_mod.assess(chat, msgs, now=now, unanswered_hours=s.unanswered_hours)
        if not verdict["urgent"]:
            return False  # not marked done: more messages may still make it urgent
        result = self._write(chat, msgs, verdict["reasons"])
        done[chat_id] = episode
        store.set_meta("autodraft_done", dict(list(done.items())[-DONE_KEEP:]))
        fails = dict(store.get_meta("autodraft_fails", {}) or {})
        if fails.pop(chat_id, None) is not None:
            store.set_meta("autodraft_fails", fails)
        text = " ".join(str(result.get("text") or "").split()) if not result.get("skip") else ""
        if not text or is_sensitive(text):
            return False
        reply_to = ""
        if chat["is_group"]:  # answer the message that mentioned you
            reply_to = next((m["id"] for m in reversed(tail) if (m.get("extra") or {}).get("mentioned")), "")
        draft = inbox.create_draft(chat_id, text[:1000], reply_to=reply_to, origin="auto")
        store.add_usage(inbox.context._day(), "auto_drafts", 1)
        name = chat["name"] or "this chat"
        app = NETWORK_LABELS.get(chat["network"], chat["network"])
        blank = blanks(draft["text"])
        inbox.automation.notify(
            "draft", f"Draft ready for {name} ({app})",
            f"They wrote: \"{(tail[-1]['text'] or '')[:140]}\". Draft: \"{draft['text'][:200]}\"."
            + (f" Fill in {', '.join(blank)} first." if blank else "")
            + f" Send it with /inbox send {draft['draft_id']}.",
            chat_id=chat_id, data={"draft_id": draft["draft_id"]})
        return True

    def _write(self, chat: dict, msgs: list[dict], reasons: list[str]) -> dict:
        inbox = self.inbox
        store, ctx = inbox.store(), inbox.context
        store.add_usage(ctx._day(), "llm_calls", 1)
        prompt = "\n".join(self.prompt(chat, msgs, reasons))
        try:
            result = ctx.llm(INSTRUCTIONS, prompt, SCHEMA) if ctx.llm else None
        except Exception as exc:  # noqa: BLE001 - provider errors vary
            raise InboxError(f"the language model failed for {chat['name'] or chat['id']}: {exc}",
                             kind="memory") from None
        if not isinstance(result, dict):
            raise InboxError(f"the language model gave no usable draft for {chat['name'] or chat['id']}",
                             kind="memory")
        return result

    def prompt(self, chat: dict, msgs: list[dict], reasons: list[str]) -> list[str]:
        """Everything the model needs to answer as you would (secrets removed)."""
        inbox = self.inbox
        store = inbox.store()
        name = chat["name"] or "this contact"
        app = NETWORK_LABELS.get(chat["network"], chat["network"])
        cc = store.chat_context(chat["id"])
        lines = [f"Today: {inbox._local_now().strftime('%A %Y-%m-%d %H:%M')} (user's local time)",
                 f"Chat: {app} {'group' if chat['is_group'] else 'one-to-one chat'} with {name}",
                 f"Why it needs the user: {', '.join(reasons) or 'they are waiting'}",
                 f"Summary so far: {cc['summary'] or '(none)'}"]
        if cc["language"]:
            lines.append(f"Language of this chat: {cc['language']}")
        contact = store.contact_for_chat(chat["id"]) if not chat["is_group"] else None
        if contact:
            facts = store.facts(contact["id"], limit=12)
            if facts:
                lines.append(f"What the user knows about {name}:")
                lines += [f"  - {f['text']}" for f in facts]
            full = store.contact(contact["id"]) or {"chats": []}
            for other in full["chats"]:
                if other["chat_id"] == chat["id"]:
                    continue
                oc = store.chat_context(other["chat_id"])
                if oc["summary"]:
                    lines.append(f"Same person on {NETWORK_LABELS.get(other['network'], other['network'])}:"
                                 f" {oc['summary']}")
        loops = store.loops(chat_id=chat["id"])
        if loops:
            lines.append("Open items:")
            lines += [f"  - {lp['kind']}: {lp['text']}" for lp in loops[:6]]
        lines += inbox.style.prompt_lines(chat)
        planner = getattr(inbox, "planner", None)
        if planner is not None:
            lines += planner.for_prompt(chat["id"])
        lines.append("Latest messages, oldest first:")
        for m in msgs:
            who = "me" if m["from_me"] else (m.get("sender") or name)
            when = datetime.fromtimestamp(m["ts"]).strftime("%a %H:%M")
            lines.append(f"  {when} {who}: {redact(m['text'] or '')[:MSG_MAX_CHARS]}")
        return lines

    def _withdraw_answered(self, now: float) -> None:
        """A draft Hermes prepared is withdrawn once you answered that chat yourself (in the app)."""
        store = self.inbox.store()
        for d in store.list_drafts("pending", limit=200):
            if d["origin"] != "auto":
                continue
            mine = store.my_messages(limit=1, chat_id=d["chat_id"])
            if mine and mine[0]["ts"] > d["created_at"]:
                store.set_draft_status(d["id"], "discarded", now, "you replied in the app")
