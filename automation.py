"""Automation: scheduled messages, watches, follow-ups, snoozes and alerts.

Runs inside the background worker (one process, after each sync), so everything here sees the
freshest cache and never adds Unipile reads of its own beyond what a send needs.

Scheduled messages
  The user approves the exact text, recipient and time with ``/inbox schedule`` (a person, not
  the model; the model can only propose a time). At that time every send rule is checked again:
    * if the person wrote again after it was scheduled, it is held and the user is asked;
    * short waits (the gap between sends, the hourly limits, a short account pause) are waited
      out within ``schedule_grace_minutes``;
    * anything else (daily limits, quiet hours, the 7-day reply window) fails with the reason;
    * if the machine was asleep past the grace window, it is never sent late: it is marked
      missed and the user is told.
  A schedule is claimed atomically before sending and is never sent twice. If a send was cut off
  half-way, it is marked unknown and the user is asked to check the app; it is never retried.

Watches (all end in an alert)
  reply     tell me when this person writes (once)
  keyword   tell me when a message contains this text (optionally in one chat or app)
  topic     tell me when someone writes about this (judged by the language model)
  no_reply  if this person has not replied by a time, remind me (and optionally draft a nudge)
  snooze    hide this chat from triage until a time, then remind me

Alerts are stored, shown by ``inbox_updates`` and the ``/inbox alerts`` command, and pushed to a
chat the user picked (``/inbox alerts here``) when Hermes allows plugin messages.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Optional

from .client import InboxError
from .pacing import DAY, NETWORK_LABELS, in_quiet_hours
from .privacy import redact
from .timeparse import WhenError, parse_when

MAX_AHEAD_DAYS = 30
MAX_SCHEDULED = 20
STALE_SENDING = 300        # a schedule stuck in "sending" this long was cut off mid-send
TOPIC_BATCH = 20
WATCH_KINDS = ("reply", "keyword", "topic", "no_reply", "snooze")
# Send-rule reasons that clear up by themselves within minutes.
_TRANSIENT = ("Too soon", "Hourly limit", "Already ", "This account is paused", "Could not read the chat: Waiting",
              "Could not read the chat: Unipile is rate-limiting", "Could not check the account")

TOPIC_INSTRUCTIONS = """Decide which new chat messages are about which watched topics. A message
matches a topic only if it is clearly about it (in any language: English, Hindi or Hinglish).
Return {"matches": [{"watch_id": <id>, "message_id": "<id>"}]}; usually empty."""
TOPIC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["matches"], "properties": {
    "matches": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                           "required": ["watch_id", "message_id"],
                                           "properties": {"watch_id": {"type": "integer"},
                                                          "message_id": {"type": "string"}}}}}}
NUDGE_INSTRUCTIONS = """Write a short, friendly follow-up message from the user ("me") to the other
person, who has not replied. Same language and tone as the chat (English, Hindi or Hinglish).
One or two sentences, no pressure, nothing invented. Return {"text": "..."}."""
NUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"text": {"type": "string"}}}


def when_text(ts: float) -> str:
    return time.strftime("%a %d %b %H:%M", time.localtime(ts))


class Automation:
    def __init__(self, inbox, push: Optional[Callable[[str], bool]] = None):
        self.inbox = inbox
        self.push = push  # delivers alert text to the user's chosen chat; set by register()

    # ── scheduling (called by /inbox schedule after the user confirms) ──
    def resolve_when(self, when: str) -> float:
        try:
            return parse_when(when, self.inbox._local_now()).timestamp()
        except WhenError as exc:
            raise InboxError(str(exc), kind="input") from None

    def check_schedule(self, draft: dict, send_at: float) -> list[str]:
        """Reasons this draft cannot be scheduled for ``send_at`` (empty = fine)."""
        store, s, now = self.inbox.store(), self.inbox.settings, self.inbox.clock()
        reasons = []
        if draft["status"] != "pending":
            reasons.append(f"Draft {draft['id']} is already {draft['status']}.")
        if store.schedule_for_draft(draft["id"]):
            reasons.append(f"Draft {draft['id']} is already scheduled. Cancel that first.")
        if send_at <= now + 30:
            reasons.append("That time is now or in the past. Use /inbox send to send now.")
        if send_at > now + MAX_AHEAD_DAYS * DAY:
            reasons.append(f"Schedules can be at most {MAX_AHEAD_DAYS} days ahead.")
        if len(store.schedules()) >= MAX_SCHEDULED:
            reasons.append(f"There are already {MAX_SCHEDULED} scheduled messages waiting.")
        if in_quiet_hours(s.quiet_hours, datetime.fromtimestamp(send_at)):
            reasons.append(f"That time is inside your quiet hours ({s.quiet_hours}).")
        inbound = [m["ts"] for m in store.cached_messages(draft["chat_id"], 50) if not m["from_me"]]
        if not inbound:
            reasons.append("They have never messaged you in this chat. unified-inbox only replies.")
        elif send_at - max(inbound) > s.reply_window_days * DAY:
            reasons.append(f"By then their last message will be older than {s.reply_window_days} days, so the"
                           " send would be blocked. Pick an earlier time.")
        return reasons

    def schedule(self, draft_id: str, send_at: float) -> dict:
        store = self.inbox.store()
        draft = store.get_draft(draft_id)
        if not draft:
            raise InboxError(f"No draft {draft_id!r}.", kind="not_found")
        reasons = self.check_schedule(draft, send_at)
        if reasons:
            raise InboxError(" ".join(reasons), kind="input")
        sid = store.add_schedule(draft=draft, send_at=send_at, now=self.inbox.clock())
        store.propose_send_at(draft["id"], None)
        return {"schedule_id": sid, "draft_id": draft["id"], "to": draft["chat_name"], "network": draft["network"],
                "send_at": send_at}

    def cancel(self, schedule_id: str) -> dict:
        store = self.inbox.store()
        sch = store.schedule(schedule_id)
        if not sch or sch["status"] != "scheduled":
            raise InboxError(f"No scheduled message {schedule_id!r}.", kind="not_found")
        store.set_schedule(sch["id"], self.inbox.clock(), status="cancelled", reason="cancelled by the user",
                           expect="scheduled")
        return {"cancelled": sch["id"], "draft_id": sch["draft_id"], "draft_kept": True}

    # ── watches ──
    def add_watch(self, kind: str, *, chat: Optional[dict] = None, text: str = "", until: Optional[float] = None,
                  network: str = "", nudge: bool = False) -> dict:
        inbox, store, now = self.inbox, self.inbox.store(), self.inbox.clock()
        if kind not in WATCH_KINDS:
            raise InboxError(f"Unknown watch type {kind!r}.", kind="input")
        if kind in ("reply", "no_reply", "snooze") and not chat:
            raise InboxError(f"A {kind.replace('_', ' ')} watch needs a chat or person.", kind="input")
        if kind in ("keyword", "topic") and len(text.strip()) < 2:
            raise InboxError("Say what to watch for.", kind="input")
        if kind == "topic" and not inbox.context.llm:
            raise InboxError("Topic watches need Hermes's language model. Use a keyword watch instead.", kind="input")
        if kind in ("no_reply", "snooze") and not until:
            raise InboxError("Say by when, e.g. 'in 2 days' or 'friday 9am'.", kind="input")
        if until is not None and until <= now:
            raise InboxError("That time is in the past.", kind="input")
        if until is not None and until > now + 90 * DAY:
            raise InboxError("Watches can run at most 90 days.", kind="input")
        anchor = now
        if kind == "no_reply" and chat:
            mine = [m["ts"] for m in store.cached_messages(chat["id"], 50) if m["from_me"]]
            anchor = max(mine) if mine else now  # replies after my last message count
        wid = store.add_watch(kind=kind, now=now, chat_id=chat["id"] if chat else "",
                              network=network or (chat["network"] if chat else ""), text=text.strip()[:200],
                              until_ts=until, anchor_ts=anchor, nudge=nudge)
        return self.watch_view(store.watch(wid))

    def watch_view(self, w: dict) -> dict:
        chat = self.inbox.store().cached_chat(w["chat_id"]) if w["chat_id"] else None
        view = {"watch_id": w["id"], "type": w["kind"]}
        if chat:
            view["name"], view["network"] = chat["name"], chat["network"]
        elif w["network"]:
            view["network"] = w["network"]
        if w["text"]:
            view["text"] = w["text"]
        if w["until_ts"]:
            view["until"] = when_text(w["until_ts"])
        if w["nudge"]:
            view["prepare_nudge_draft"] = True
        return view

    # ── the tick ──
    def tick(self) -> dict:
        """One automation step. Never raises for one item's failure."""
        out = {"sent": 0, "alerts": 0, "problems": []}
        inbox = self.inbox
        if not inbox.configured:
            return out
        store = inbox.store()
        before = store._q("SELECT COALESCE(MAX(id), 0) FROM notifications")[0][0]
        self._recover_stuck()
        out["sent"] = self._run_due(out["problems"])
        self._check_messages(out["problems"])
        self._check_times()
        out["alerts"] = store._q("SELECT COALESCE(MAX(id), 0) FROM notifications")[0][0] - before
        self._push()
        return out

    def next_due_in(self) -> Optional[float]:
        store, now = self.inbox.store(), self.inbox.clock()
        times = [t for t in (store.next_schedule_at(), store.next_watch_at()) if t is not None]
        return max(0.0, min(times) - now) if times else None

    # ── scheduled sends ──
    def _recover_stuck(self) -> None:
        store, now = self.inbox.store(), self.inbox.clock()
        for sch in store.schedules(statuses=("sending",)):
            if now - sch["updated_at"] > STALE_SENDING:
                store.set_schedule(sch["id"], now, status="unknown", expect="sending",
                                   reason="interrupted while sending")
                self.notify("schedule", f"Check {NETWORK_LABELS.get(sch['network'], sch['network'])}:"
                            f" message to {sch['chat_name']} may not have gone out",
                            f"Sending was interrupted. It was not retried. Message: \"{sch['text'][:200]}\"",
                            chat_id=sch["chat_id"], data={"schedule_id": sch["id"]})

    def _run_due(self, problems: list) -> int:
        inbox, store, s = self.inbox, self.inbox.store(), self.inbox.settings
        sent = 0
        for sch in store.due_schedules(inbox.clock()):
            now = inbox.clock()
            label = f"{sch['chat_name']} on {NETWORK_LABELS.get(sch['network'], sch['network'])}"
            draft = store.get_draft(sch["draft_id"])
            if not draft or draft["status"] != "pending":
                store.set_schedule(sch["id"], now, status="cancelled", expect="scheduled",
                                   reason=f"draft {draft['status'] if draft else 'deleted'}")
                continue
            if draft["text"] != sch["text"]:
                # You approved exact words. If the draft says something else now (changed on another
                # device, or by anything that got past the "scheduled drafts can't be edited" rule),
                # it does not go out on the old approval.
                store.set_schedule(sch["id"], now, status="failed", expect="scheduled",
                                   reason="the draft was changed after you scheduled it")
                self.notify("schedule", f"Not sent: scheduled message to {label}",
                            f"The draft's text changed after you scheduled it, so it was not sent. Read it, then"
                            f" /inbox send {draft['id']} or /inbox schedule {draft['id']} <time>.",
                            chat_id=sch["chat_id"], data={"schedule_id": sch["id"], "draft_id": draft["id"]})
                continue
            if now > sch["send_at"] + s.schedule_grace_minutes * 60:
                store.set_schedule(sch["id"], now, status="missed", expect="scheduled",
                                   reason="Hermes was not running at the scheduled time")
                self.notify("schedule", f"Missed: scheduled message to {label}",
                            f"It was due {when_text(sch['send_at'])}, when Hermes was not running, so it was not"
                            f" sent late. Draft {draft['id']} is still saved: /inbox send {draft['id']} or"
                            f" /inbox schedule {draft['id']} <new time>.", chat_id=sch["chat_id"],
                            data={"schedule_id": sch["id"], "draft_id": draft["id"]})
                continue
            chat = store.cached_chat(sch["chat_id"]) or {"id": sch["chat_id"], "account_id": sch["account_id"],
                                                         "name": sch["chat_name"]}
            try:
                msgs = inbox.messages(chat, limit=10, max_age=30)
            except InboxError as exc:
                store.set_schedule(sch["id"], now, status="scheduled", reason=f"waiting: {exc}", attempt=True)
                continue
            if any(not m["from_me"] and m["ts"] > sch["created_at"] for m in msgs):
                store.set_schedule(sch["id"], now, status="held", expect="scheduled",
                                   reason="they wrote again after you scheduled it")
                self.notify("schedule", f"Held: {sch['chat_name']} wrote again before your scheduled message",
                            f"Not sent, so it does not miss what they said. Read the chat, then"
                            f" /inbox send {draft['id']} or /inbox schedule {draft['id']} <time>.",
                            chat_id=sch["chat_id"], data={"schedule_id": sch["id"], "draft_id": draft["id"]})
                continue
            reasons = inbox.preflight(draft, max_age=30)
            if reasons:
                self._not_now(sch, draft, reasons, label)
                continue
            try:  # with cloud sync, claim it in Supabase first: only one device sends it
                claimed = inbox.cloud.claim_schedule(sch["id"])
            except InboxError as exc:
                store.set_schedule(sch["id"], now, status="scheduled", reason=f"waiting: Supabase unreachable ({exc})",
                                   attempt=True)
                continue
            if not claimed:
                continue  # another device has it, or it was cancelled there; the next pull shows which
            if not store.set_schedule(sch["id"], now, status="sending", expect="scheduled", attempt=True):
                continue  # claimed elsewhere
            try:
                result = inbox.send(draft["id"], via="schedule")
            except InboxError as exc:
                if exc.kind not in ("cloud", "paused"):
                    store.set_schedule(sch["id"], inbox.clock(), status="unknown", expect="sending", reason=str(exc))
                    self.notify("schedule", f"Check: message to {label} may not have gone out",
                                f"Error while sending ({exc}). It was not retried.", chat_id=sch["chat_id"],
                                data={"schedule_id": sch["id"]})
                    problems.append(str(exc))
                    continue
                # refused before anything was sent (Supabase unreachable, or another device sending)
                store.set_schedule(sch["id"], inbox.clock(), status="scheduled", expect="sending",
                                   reason=f"waiting: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - never resend: ask the user to check
                store.set_schedule(sch["id"], inbox.clock(), status="unknown", expect="sending", reason=str(exc))
                self.notify("schedule", f"Check: message to {label} may not have gone out",
                            f"Error while sending ({exc}). It was not retried.", chat_id=sch["chat_id"],
                            data={"schedule_id": sch["id"]})
                problems.append(str(exc))
                continue
            if result["sent"]:
                sent += 1
                store.set_schedule(sch["id"], inbox.clock(), status="sent", expect="sending", reason="")
                self.notify("schedule", f"Sent your scheduled message to {label}", f"\"{sch['text'][:200]}\"",
                            chat_id=sch["chat_id"], data={"schedule_id": sch["id"]})
            else:
                after = store.get_draft(draft["id"])
                if after and after["status"] == "failed":  # the send itself failed: never retried
                    store.set_schedule(sch["id"], inbox.clock(), status="failed", expect="sending",
                                       reason="; ".join(result["reasons"]))
                    self.notify("schedule", f"Not sent: scheduled message to {label}", "; ".join(result["reasons"]),
                                chat_id=sch["chat_id"], data={"schedule_id": sch["id"]})
                else:
                    store.set_schedule(sch["id"], inbox.clock(), status="scheduled", expect="sending",
                                       reason="; ".join(result["reasons"]))
                    self._not_now(store.schedule(sch["id"]), draft, result["reasons"], label)
        return sent

    def _not_now(self, sch: dict, draft: dict, reasons: list[str], label: str) -> None:
        """Wait out short blocks inside the grace window; fail on anything else."""
        store, now = self.inbox.store(), self.inbox.clock()
        if all(r.startswith(_TRANSIENT) for r in reasons):
            store.set_schedule(sch["id"], now, status="scheduled", reason="waiting: " + "; ".join(reasons),
                               attempt=True)
            return
        store.set_schedule(sch["id"], now, status="failed", expect="scheduled", reason="; ".join(reasons))
        self.notify("schedule", f"Not sent: scheduled message to {label}",
                    "; ".join(reasons) + f" Draft {draft['id']} is still saved.", chat_id=sch["chat_id"],
                    data={"schedule_id": sch["id"], "draft_id": draft["id"]})

    # ── watches on new messages ──
    def _check_messages(self, problems: list) -> None:
        store, now = self.inbox.store(), self.inbox.clock()
        cursor = store.get_meta("watch_cursor")
        if cursor is None:  # start from now: watches never fire on old events
            store.set_meta("watch_cursor", store.last_event_id())
            return
        raw = store.events_after(int(cursor), types=["message.new"], limit=500)
        events = [e for e in raw if not e["data"].get("from_me")]
        active = store.watches(kinds=["reply", "keyword", "topic", "no_reply"])
        topics = [w for w in active if w["kind"] == "topic"]
        topic_msgs = []
        for e in events:
            chat = store.cached_chat(e["chat_id"]) or {"name": "", "network": ""}
            text, who = e["data"].get("text", ""), e["data"].get("sender") or chat["name"]
            where = NETWORK_LABELS.get(chat["network"], chat["network"])
            for w in active:
                if w["status"] != "active":
                    continue
                if w["kind"] in ("reply", "no_reply") and w["chat_id"] == e["chat_id"]:
                    if w["kind"] == "no_reply" and (e["data"].get("ts") or e["ts"]) <= w["anchor_ts"]:
                        continue
                    title = (f"{who} replied ({where})" if w["kind"] == "reply" else
                             f"{who} replied ({where}), the follow-up you set is done")
                    self.notify("watch", title, redact(text)[:300], chat_id=e["chat_id"], data={"watch_id": w["id"]})
                    store.set_watch(w["id"], now, status="done", fired=True)
                    w["status"] = "done"
                elif w["kind"] == "keyword" and w["text"].casefold() in (text or "").casefold() and (
                        not w["chat_id"] or w["chat_id"] == e["chat_id"]) and (
                        not w["network"] or w["network"] == chat["network"]):
                    self.notify("watch", f"\"{w['text']}\" from {who} ({where})", redact(text)[:300],
                                chat_id=e["chat_id"], data={"watch_id": w["id"]})
                    store.set_watch(w["id"], now, fired=True)
            if topics:
                topic_msgs.append((e, who, where))
        if topics and topic_msgs:
            try:
                self._check_topics(topics, topic_msgs)
            except InboxError as exc:  # other watches already fired; never repeat them
                problems.append(str(exc))
        if raw:
            store.set_meta("watch_cursor", raw[-1]["id"])

    def _check_topics(self, topics: list[dict], msgs: list[tuple]) -> None:
        inbox, store, s = self.inbox, self.inbox.store(), self.inbox.settings
        day = inbox.context._day()
        for i in range(0, len(msgs), TOPIC_BATCH):
            if store.usage_today(day, "llm_calls") >= s.memory_llm_calls_per_day:
                raise InboxError("Topic watches paused: the daily model-call limit is reached.", kind="memory")
            batch = msgs[i:i + TOPIC_BATCH]
            lines = ["Watched topics:"] + [f"  #{w['id']}: {w['text']}" for w in topics] + ["New messages:"]
            lines += [f"  [{e['message_id']}] {who}: {redact(e['data'].get('text', ''))[:400]}" for e, who, _ in batch]
            store.add_usage(day, "llm_calls", 1)
            try:
                result = inbox.context.llm(TOPIC_INSTRUCTIONS, "\n".join(lines), TOPIC_SCHEMA) or {}
            except Exception as exc:  # noqa: BLE001
                raise InboxError(f"Topic watches: the language model failed ({exc})", kind="memory") from None
            by_msg = {e["message_id"]: (e, who, where) for e, who, where in batch}
            by_watch = {w["id"]: w for w in topics}
            for m in result.get("matches") or []:
                hit, w = by_msg.get(m.get("message_id")), by_watch.get(m.get("watch_id"))
                if hit and w:
                    e, who, where = hit
                    self.notify("watch", f"{who} wrote about \"{w['text']}\" ({where})",
                                redact(e["data"].get("text", ""))[:300], chat_id=e["chat_id"],
                                data={"watch_id": w["id"]})
                    store.set_watch(w["id"], inbox.clock(), fired=True)

    # ── watches on time ──
    def _check_times(self) -> None:
        store, now = self.inbox.store(), self.inbox.clock()
        for w in store.watches():
            if w["until_ts"] is None or w["until_ts"] > now:
                continue
            chat = store.cached_chat(w["chat_id"]) if w["chat_id"] else None
            name = chat["name"] if chat else ""
            where = NETWORK_LABELS.get(chat["network"], chat["network"]) if chat else ""
            if w["kind"] == "snooze":
                self.notify("reminder", f"Reminder: {name} ({where})", "The chat you snoozed is back.",
                            chat_id=w["chat_id"], data={"watch_id": w["id"]})
                store.set_watch(w["id"], now, status="done", fired=True)
            elif w["kind"] == "no_reply":
                body = f"No reply since {when_text(w['anchor_ts'])}."
                if w["nudge"]:
                    body += " " + self._nudge(w, chat)
                self.notify("reminder", f"{name} has not replied ({where})", body, chat_id=w["chat_id"],
                            data={"watch_id": w["id"]})
                store.set_watch(w["id"], now, status="done", fired=True)
            else:  # keyword / topic / reply watches simply end
                store.set_watch(w["id"], now, status="expired")

    def _nudge(self, w: dict, chat: Optional[dict]) -> str:
        """Prepare a follow-up draft (never sent). Returns a sentence for the alert."""
        inbox, store, s = self.inbox, self.inbox.store(), self.inbox.settings
        if not chat or not inbox.context.llm:
            return "Draft a follow-up when you are ready."
        if store.usage_today(inbox.context._day(), "llm_calls") >= s.memory_llm_calls_per_day:
            return "No draft prepared (daily model-call limit reached)."
        cc = store.chat_context(chat["id"])
        msgs = store.cached_messages(chat["id"], 8)
        lines = [f"Chat with {chat['name']} ({NETWORK_LABELS.get(chat['network'], chat['network'])}).",
                 f"Summary: {cc['summary'] or '(none)'}", f"Language: {cc['language'] or 'match the messages'}",
                 "Latest messages:"]
        lines += [f"  {'me' if m['from_me'] else chat['name']}: {redact(m['text'] or '')[:300]}" for m in msgs]
        lines += inbox.style.prompt_lines(chat)  # sound like you
        store.add_usage(inbox.context._day(), "llm_calls", 1)
        try:
            text = str((inbox.context.llm(NUDGE_INSTRUCTIONS, "\n".join(lines), NUDGE_SCHEMA) or {}).get("text") or "")
        except Exception:  # noqa: BLE001
            text = ""
        if not text.strip():
            return "Draft a follow-up when you are ready."
        try:
            draft = inbox.create_draft(chat["id"], text.strip()[:1000])
        except InboxError as exc:
            return f"Could not prepare a draft ({exc})."
        return f"Follow-up draft {draft['draft_id']} is ready: \"{draft['text'][:200]}\" Send with /inbox send" \
               f" {draft['draft_id']}."

    # ── alerts ──
    def notify(self, kind: str, title: str, body: str = "", *, chat_id: str = "", data: Optional[dict] = None) -> int:
        return self.inbox.store().add_notification(kind=kind, title=title, body=body, now=self.inbox.clock(),
                                                   chat_id=chat_id, data=data)

    def _push(self) -> None:
        """Send new alerts, batched into one message, to the chat the user chose."""
        if not self.push or not self.inbox.store().get_meta("alerts_session"):
            return
        store = self.inbox.store()
        pending = store.notifications(unread_only=True, unpushed_only=True, limit=20)
        if not pending:
            return
        lines = [f"- {n['title']}" + (f": {n['body']}" if n["body"] else "") for n in pending]
        text = ("[unified-inbox alerts] Tell me these in a few short lines, nothing else:\n" + "\n".join(lines))
        try:
            ok = bool(self.push(text))
        except Exception:  # noqa: BLE001 - the alerts stay unread and show in inbox_updates
            ok = False
        if ok:
            store.mark_notifications([n["id"] for n in pending], self.inbox.clock(), field="pushed_at")

    def alerts(self, *, mark_read: bool = True, limit: int = 20) -> list[dict]:
        store = self.inbox.store()
        items = store.notifications(unread_only=True, limit=limit)
        if mark_read and items:
            store.mark_notifications([n["id"] for n in items], self.inbox.clock())
        return [{"title": n["title"], "body": n["body"], "chat_id": n["chat_id"], "ts": n["ts"]} for n in items]


def describe_schedule(sch: dict) -> dict[str, Any]:
    out = {"schedule_id": sch["id"], "to": sch["chat_name"], "network": sch["network"], "text": sch["text"],
           "send_at": when_text(sch["send_at"]), "status": sch["status"], "draft_id": sch["draft_id"]}
    if sch["reason"]:
        out["note"] = sch["reason"]
    return out
