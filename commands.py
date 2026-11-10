"""The /inbox slash command. A person sends a saved draft with /inbox send <id> and picks "Send"
in the confirmation (Hermes itself sends only through inbox_send, after the user approves it in
Hermes's own approval prompt: see sending.py)."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .autodraft import blanks
from .client import InboxError
from . import data_control, health
from .inbox import SETUP_MESSAGE, Inbox
from .sync import SyncEngine
from .webhooks import PATH as WEBHOOK_PATH
from .webhooks import remove_webhook, setup_webhook
from .pacing import DAY, NETWORK_LABELS, quiet_hours_problem

USAGE = """/inbox — your WhatsApp, Instagram and LinkedIn inbox
  /inbox status          connected accounts, today's sends vs limits
  /inbox drafts          drafts waiting for your approval
  /inbox send <id>       review one draft and send it (asks you to confirm)
  /inbox discard <id>    delete a draft
  /inbox schedule <id> [when]   schedule a draft (asks you to confirm); cancel <id>
  /inbox scheduled       scheduled messages
  /inbox brief           what you missed, in a few lines
  /inbox radar           who is waiting longest, who you are drifting from, birthdays
  /inbox plans           plans found in your chats (add them to your calendar from the Inbox page)
  /inbox scams           chats that look like a scam
  /inbox alerts          unread alerts; "here" sends new alerts to this chat, "off" stops that
  /inbox sync            check for new messages now
  /inbox cloud           Supabase sync: status | setup | sync | sql
  /inbox log             what Hermes sent, to whom, and whether it worked
  /inbox doctor          check every part and say how to fix what is wrong
  /inbox export          save all your data to a JSON file
  /inbox forget <name>   remove what memory knows about a person (asks first)
  /inbox reset           delete all inbox data (asks first)
  /inbox webhook         real-time updates: status | setup <public https URL> | remove
  /inbox limits          the safety limits in force"""

_CONFIRM = [
    {"value": "send", "label": "Send this message", "description": "Sends exactly the text shown, once."},
    {"value": "cancel", "label": "Cancel", "description": "Nothing is sent. The draft stays saved."},
]


def build_command(inbox: Inbox, ask: Callable[[str, list], Optional[str]],
                  runtime: Optional[dict] = None) -> Callable[..., str]:
    runtime = runtime if runtime is not None else {}

    def handle(raw_args: str = "", session_key: Optional[str] = None, **_kwargs: Any) -> str:
        parts = (raw_args or "").strip().split()
        sub = parts[0].lower() if parts else "status"
        arg = parts[1] if len(parts) > 1 else ""
        try:
            if sub in ("help", "-h", "--help"):
                return USAGE
            if sub == "status":
                return _status(inbox, runtime)
            if sub == "sync":
                return _sync(inbox, runtime)
            if sub == "schedule":
                if arg.lower() == "cancel":
                    return _cancel_schedule(inbox, parts[2] if len(parts) > 2 else "")
                return _schedule(inbox, ask, arg, " ".join(parts[2:]))
            if sub == "scheduled":
                return _scheduled(inbox)
            if sub == "brief":
                inbox.client()  # setup check
                return inbox.voice.brief()["say"]
            if sub == "radar":
                inbox.client()
                return _radar(inbox)
            if sub == "plans":
                return _plans(inbox)
            if sub in ("scams", "safety"):
                return _scams(inbox)
            if sub == "alerts":
                return _alerts(inbox, arg.lower(), session_key)
            if sub == "cloud":
                return _cloud(inbox, arg.lower())
            if sub in ("log", "activity"):
                return _log(inbox)
            if sub == "doctor":
                return health.format_text(health.run(inbox))
            if sub == "export":
                return f"Saved to {data_control.export_to_file(inbox)}. It holds your messages: keep it private."
            if sub == "forget":
                return _forget(inbox, ask, " ".join(parts[1:]))
            if sub == "reset":
                return _reset(inbox, ask)
            if sub == "webhook":
                return _webhook(inbox, runtime, arg.lower(), parts[2] if len(parts) > 2 else "")
            if sub == "drafts":
                return _drafts(inbox)
            if sub == "limits":
                return _limits(inbox)
            if sub == "discard":
                if not arg:
                    return "Usage: /inbox discard <draft id>"
                inbox.discard(arg)
                return f"Draft {arg} discarded."
            if sub == "send":
                if not arg:
                    return "Usage: /inbox send <draft id>  (see /inbox drafts)"
                return _send(inbox, ask, arg)
            return USAGE
        except InboxError as exc:
            return f"unified-inbox: {exc}"

    return handle


def _send(inbox: Inbox, ask: Callable[[str, list], Optional[str]], draft_id: str) -> str:
    store = inbox.store()
    draft = store.get_draft(draft_id)
    if not draft:
        return f"No draft {draft_id!r}. See /inbox drafts."
    if draft["status"] != "pending":
        return f"Draft {draft['id']} is already {draft['status']}."
    reasons = inbox.preflight(draft)
    if reasons:
        return "Not sent:\n" + "\n".join(f"  • {r}" for r in reasons)
    # Cheap local count; status() would also re-check accounts, which preflight just did.
    limit = inbox.settings.daily_send_limit
    left = limit - len(store.recent_sends(inbox.clock() - DAY))
    quoted = store.get_message(draft["reply_to"]) if draft.get("reply_to") else None
    reply = f"Replying to: “{(quoted.get('text') or '')[:120]}”\n\n" if quoted else ""
    words = f"\n\nIn your words: “{draft['original_text']}”" if draft.get("original_text") else ""
    question = (f"Send to {draft['chat_name']} on {_label(draft['network'])}?\n\n{reply}"
                f"“{draft['text']}”{words}\n\n"
                f"Sends left in the next 24 hours after this: {max(left - 1, 0)} of {limit}.")
    answer = ask(question, _CONFIRM)
    if answer is None:
        return ("Not sent: there was no answer to the confirmation (it timed out, was cancelled, or this"
                f" surface cannot ask). Draft {draft['id']} is still saved.")
    if answer != "send":
        return f"Not sent. Draft {draft['id']} is still saved."
    result = inbox.send(draft["id"])
    if not result["sent"]:
        return "Not sent:\n" + "\n".join(f"  • {r}" for r in result["reasons"])
    return (f"Sent to {result['to']} on {_label(result['network'])}. "
            f"Next send allowed in {result['next_send_in_seconds']}s.")


def _radar(inbox: Inbox) -> str:
    rep = inbox.radar.report()
    lines = [rep["summary"]]
    for w in rep["waiting_longest"][:5]:
        lines.append(f"  • waiting: {w['name']} ({_label(w['network'])}), {w['waiting_days']:g} days")
    for d in rep["drifting"][:3]:
        lines.append(f"  • drifting: {d['name']}, quiet {d['quiet_days']} days (you talked every"
                     f" ~{d['used_to_talk_every_days']} days)")
    for b in rep["birthdays"][:3]:
        lines.append(f"  • birthday: {b['name']} on {b['date']}")
    return "\n".join(lines)


def _plans(inbox: Inbox) -> str:
    plans = inbox.planner.upcoming(days=14)
    if not plans:
        return "No plans found in your chats for the next two weeks."
    lines = ["Plans found in your chats (add them from the Inbox page, or ask Hermes):"]
    for p in plans[:10]:
        state = {"added": "in your calendar", "suggested": "not added yet"}.get(p["status"], p["status"])
        clash = " - clashes with something else" if p.get("free") is False else ""
        lines.append(f"  • {p['title']}, {p['when']} ({state}){clash}")
    return "\n".join(lines)


def _scams(inbox: Inbox) -> str:
    if not inbox.shield.enabled():
        return "Scam warnings are switched off (scam_shield)."
    items = inbox.shield.flagged()
    if not items:
        return "No chat looks like a scam right now."
    lines = ["These chats look like a scam:"]
    for x in items[:10]:
        lines.append(f"  • {x['name'] or 'Unknown'} ({_label(x['network'])}): {'; '.join(x['reasons'][:2])}."
                     f" {x['advice']}")
    return "\n".join(lines)


def _label(network: str) -> str:
    return NETWORK_LABELS.get(network, network.title())


_SCHEDULE_CONFIRM = [
    {"value": "schedule", "label": "Schedule this message",
     "description": "It sends by itself at that time, once, if every safety rule still passes."},
    {"value": "cancel", "label": "Cancel", "description": "Nothing is scheduled. The draft stays saved."},
]


def _schedule(inbox: Inbox, ask: Callable[[str, list], Optional[str]], draft_id: str, when: str) -> str:
    if not draft_id:
        return "Usage: /inbox schedule <draft id> <when>, e.g. /inbox schedule a1b2c3 tomorrow 9am"
    inbox.client()  # setup check
    store, auto = inbox.store(), inbox.automation
    draft = store.get_draft(draft_id)
    if not draft:
        return f"No draft {draft_id!r}. See /inbox drafts."
    send_at = auto.resolve_when(when) if when else store.proposed_send_at(draft["id"])
    if not send_at:
        return f"When? e.g. /inbox schedule {draft['id']} tomorrow 9am"
    problems = auto.check_schedule(draft, send_at)
    if problems:
        return "Not scheduled:\n" + "\n".join(f"  • {p}" for p in problems)
    when_text = time.strftime("%A %d %B at %H:%M", time.localtime(send_at))
    question = (f"Schedule to {draft['chat_name']} on {_label(draft['network'])} for {when_text}?\n\n"
                f"“{draft['text']}”\n\n"
                "It is held, not sent, if they write again before then. You can cancel any time with"
                " /inbox schedule cancel <id>.")
    answer = ask(question, _SCHEDULE_CONFIRM)
    if answer != "schedule":
        return f"Not scheduled. Draft {draft['id']} is still saved."
    out = auto.schedule(draft["id"], send_at)
    return (f"Scheduled ({out['schedule_id']}): to {out['to']} on {_label(out['network'])}, {when_text}."
            f" Cancel with /inbox schedule cancel {out['schedule_id']}.")


def _cancel_schedule(inbox: Inbox, schedule_id: str) -> str:
    if not schedule_id:
        return "Usage: /inbox schedule cancel <id>  (see /inbox scheduled)"
    out = inbox.automation.cancel(schedule_id)
    return f"Cancelled {out['cancelled']}. Draft {out['draft_id']} is still saved."


def _scheduled(inbox: Inbox) -> str:
    items = inbox.store().schedules()
    if not items:
        return "Nothing scheduled. Schedule a draft with /inbox schedule <draft id> <when>."
    lines = ["Scheduled messages:"]
    for s in items:
        when = time.strftime("%a %d %b %H:%M", time.localtime(s["send_at"]))
        text = s["text"] if len(s["text"]) <= 80 else s["text"][:77] + "..."
        note = f" ({s['reason']})" if s["reason"] else ""
        lines.append(f"  [{s['id']}] {when} to {s['chat_name']} ({_label(s['network'])}): {text}{note}")
    return "\n".join(lines)


def _alerts(inbox: Inbox, action: str, session_key: Optional[str]) -> str:
    store = inbox.store()
    if action == "here":
        key = session_key or _session_key_from_env()
        if not key:
            return "This chat cannot receive alerts (no session). Open it from a messaging app or Hermes Desktop."
        store.set_meta("alerts_session", key)
        return ("New alerts will be sent to this chat. Hermes must allow it once: set"
                " plugins.entries.unified-inbox.allow_gateway_injection: true in config.yaml. /inbox alerts off"
                " stops it.")
    if action == "off":
        store.set_meta("alerts_session", "")
        return "Alerts will no longer be sent to a chat. They still show in /inbox alerts and inbox_updates."
    items = inbox.automation.alerts()
    if not items:
        return "No new alerts."
    return "\n".join(["Alerts:"] + [f"  • {a['title']}" + (f": {a['body']}" if a["body"] else "") for a in items])


def _session_key_from_env() -> Optional[str]:
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]

        return get_session_env("HERMES_SESSION_KEY") or None
    except Exception:  # noqa: BLE001 - not inside a Hermes gateway session
        return None


_STATUS_WORD = {"sent": "SENT", "failed": "FAILED", "blocked": "BLOCKED", "unsure": "CHECK APP",
                "asked": "WAITING/DECLINED"}


def _log(inbox: Inbox) -> str:
    items = inbox.store().attempts(limit=20)
    if not items:
        return "Nothing sent yet."
    lines = ["Recent sends (newest first):"]
    for a in items:
        when = time.strftime("%d %b %H:%M", time.localtime(a["ts"]))
        files = f" +{len(a['files'])} file(s)" if a["files"] else ""
        text = a["text"] if len(a["text"]) <= 60 else a["text"][:57] + "..."
        lines.append(f"  {when}  {_STATUS_WORD.get(a['status'], a['status'].upper()):9} {_label(a['network'])}"
                     f" -> {a['to']}: \"{text}\"{files}  (via {a['via']})")
        if a["detail"] and a["status"] != "sent":
            lines.append(f"                 {a['detail'][:160]}")
    path = _activity_path(inbox)
    if path:
        lines.append(f"Full log: {path}")
    return "\n".join(lines)


def _activity_path(inbox: Inbox) -> str:
    row = inbox.store()._conn.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        return ""
    import os

    return os.path.join(os.path.dirname(row[2]), "activity.log")


def _forget(inbox: Inbox, ask: Callable[[str, list], Optional[str]], who: str) -> str:
    if not who:
        return "Usage: /inbox forget <name>"
    found = inbox.context.find(person=who)
    if not found:
        return f"No chat found for {who!r}."
    if len(found) > 1 and found[0]["name"].casefold() != who.casefold():
        return "Which one? " + ", ".join(f"{c['name']} ({_label(c['network'])})" for c in found[:6])
    chat = found[0]
    answer = ask(f"Forget what memory knows about {chat['name']}? Facts, the chat summary, open items and the"
                 " search index for their chats are deleted. Messages stay in the inbox.",
                 [{"value": "forget", "label": "Forget", "description": "Cannot be undone."},
                  {"value": "cancel", "label": "Cancel", "description": "Nothing changes."}])
    if answer != "forget":
        return "Nothing was forgotten."
    out = data_control.forget_person(inbox, chat["id"])
    return f"Forgot {out['facts']} fact(s) and the summaries, open items and search index for {out['name']}."


def _reset(inbox: Inbox, ask: Callable[[str, list], Optional[str]]) -> str:
    choices = [{"value": "device", "label": "Delete on this device",
                "description": "Your Supabase copy (if any) is kept; cloud sync stops on this device."},
               {"value": "cancel", "label": "Cancel", "description": "Nothing is deleted."}]
    if inbox.cloud.active:
        choices.insert(1, {"value": "everywhere", "label": "Delete everywhere",
                           "description": "Here, in your Supabase project and on your other devices."})
    answer = ask("Delete all inbox data? Chats, messages, drafts, scheduled messages, memory, watches and alerts"
                 " are removed. Sends from the last 7 days and account pauses are kept so your limits stay"
                 " correct. This cannot be undone; /inbox export first if you want a copy.", choices)
    if answer not in ("device", "everywhere"):
        return "Nothing was deleted."
    out = data_control.wipe(inbox, everywhere=answer == "everywhere")
    tail = {"deleted": " Supabase and your other devices follow.", "kept": " Your Supabase copy was kept.",
            "not used": ""}[out["supabase"]]
    return f"Deleted. Kept: {out['kept']}.{tail}"


def _cloud(inbox: Inbox, action: str) -> str:
    cloud = inbox.cloud
    if not cloud.configured:
        return ("Cloud sync is off. To keep a synced copy in your own Supabase: create a free project at"
                " supabase.com, then add its Project URL (supabase_url) and secret key in Settings -> Plugins ->"
                " unified-inbox.")
    if action == "sql":
        from .cloud import schema_sql

        return "Run this once in your Supabase SQL editor, then /inbox cloud setup:\n\n" + schema_sql()
    if action == "setup":
        st = cloud.setup() if cloud.status()["can_create_tables"] else cloud.activate()
        return f"Cloud sync is on. {st['pending_changes']} change(s) are being copied up."
    if action == "sync":
        r = cloud.sync()
        return f"Synced with Supabase: {r['pushed']} sent, {r['pulled']} received."
    st = cloud.status()
    if not st["active"]:
        editor = st.get("sql_editor_url", "the SQL editor")
        how = ("/inbox cloud setup creates the tables" if st["can_create_tables"] else
               f"run the SQL from /inbox cloud sql in {editor}, then /inbox cloud setup")
        return f"Supabase is set but not syncing yet: {how}." + (f"\n{st['error']}" if st["error"] else "")
    roles = {"leader": "This device runs the background work.",
             "follower": "Another device runs the background work; this one mirrors it."}
    when = f"Last synced {int((inbox.clock() - st['last_pull_at']) // 60)} min ago." if st["last_pull_at"] else ""
    update = ("The Supabase tables are from an earlier version: run /inbox cloud setup"
              + ("" if st["can_create_tables"] else " after running the SQL from /inbox cloud sql")
              + " to also share reply quotes and the new-conversation count between devices."
              ) if st.get("update_available") else ""
    lines = [f"Cloud sync: on ({st['url']}).", roles.get(st["role"], ""), when,
             f"{st['pending_changes']} change(s) waiting to go up." if st["pending_changes"] else "", update,
             st["error"]]
    return "\n".join(x for x in lines if x)


def _sync(inbox: Inbox, runtime: dict) -> str:
    if not inbox.configured:
        return SETUP_MESSAGE
    worker = runtime.get("worker")
    engine = worker.engine if worker else SyncEngine(inbox)
    r = engine.sync_once()
    lines = [f"Checked {r['accounts']} account(s): {r['new_messages']} new message(s),"
             f" {r['chats_synced']} chat(s) updated."]
    if r["pending_chats"]:
        lines.append(f"{r['pending_chats']} more chat(s) will catch up in the next runs.")
    if r["budget_limited"]:
        lines.append("Stopped early to keep Unipile calls for your own requests (hourly budget).")
    lines += [f"  {p}" for p in r["problems"]]
    return "\n".join(lines)


def _webhook(inbox: Inbox, runtime: dict, action: str, url: str) -> str:
    if not inbox.configured:
        return SETUP_MESSAGE
    if action == "setup":
        if not url:
            return f"Usage: /inbox webhook setup https://your.domain{WEBHOOK_PATH}"
        hook = setup_webhook(inbox, url)
        tail = ("" if runtime.get("webhook_server") else
                "\nThe local receiver is off: set webhook_port in the plugin settings and restart Hermes,"
                " then make the address above forward to it.")
        return f"Webhook registered with Unipile ({hook['id']}) for {hook['url']}.{tail}"
    if action == "remove":
        if remove_webhook(inbox):
            return "Webhook removed. New messages are picked up by polling."
        return "No webhook is set up."
    if action not in ("", "status"):
        return "Usage: /inbox webhook [status | setup <public https URL> | remove]"
    return "\n".join(_webhook_lines(inbox, runtime)) or "No webhook is set up. New messages arrive by polling."


def _webhook_lines(inbox: Inbox, runtime: dict) -> list[str]:
    sync = inbox.sync_status()
    lines = []
    if sync.get("webhook"):
        last = sync.get("webhook_last_event_at")
        ago = f"last event {int((inbox.clock() - float(last)) // 60)} min ago" if last else "no events received yet"
        lines.append(f"Webhook: {sync.get('webhook_url')} ({ago})")
    server = runtime.get("webhook_server")
    if server:
        host, port = server.address
        lines.append(f"Receiver listening on {host}:{port}{WEBHOOK_PATH}")
    elif runtime.get("webhook_error"):
        lines.append(f"Receiver: {runtime['webhook_error']}")
    return lines


def _status(inbox: Inbox, runtime: Optional[dict] = None) -> str:
    st = inbox.status()
    if not st["connected"]:
        return SETUP_MESSAGE
    lines = ["Connected to Unipile."]
    for a in st["accounts"]:
        extra = ""
        if a.get("paused_for_min"):
            extra += f", waiting {a['paused_for_min']} min ({a['pause_reason']})"
        lines.append(f"  {_label(a['network'])}: {a['status']}{extra}")
    for missing in st.get("accounts_not_found", []):
        lines.append(f"  {missing}: not found in Unipile. Check the account ID in settings.")
    if st["accounts_error"]:
        lines.append(f"  Accounts: {st['accounts_error']}")
    s24, s1 = st["sent_last_24h"], st["sent_last_hour"]
    lines.append(f"Sent in 24 h: {s24['total']}/{s24['limit']} (WhatsApp {s24['whatsapp']}/{s24['whatsapp_limit']},"
                 f" Instagram {s24['instagram']}/{s24['instagram_limit']},"
                 f" LinkedIn {s24['linkedin']}/{s24['linkedin_limit']}); last hour {s1['total']}/{s1['limit']}")
    if st["next_send_in_seconds"]:
        lines.append(f"Next send allowed in {st['next_send_in_seconds']}s")
    u = st["unipile_calls_last_hour"]
    if u["used"]:
        lines.append(f"Unipile calls in the last hour: {u['used']}/{u['limit']}")
    lines.append(f"Pending drafts: {st['pending_drafts']} (/inbox drafts)")
    sync = st["sync"]
    note = f" ({sync['last_error']})" if sync.get("last_error") else ""
    if not sync["enabled"]:
        lines.append("Background sync: off")
    elif sync.get("minutes_since_last_sync") is not None:
        lines.append(f"Background sync: last checked {sync['minutes_since_last_sync']} min ago{note}")
    else:
        lines.append(f"Background sync: starting{note}")
    mem = st["memory"]
    if not mem["enabled"] or not mem["language_model"]:
        lines.append("Memory: off" + ("" if mem["enabled"] else " (memory_enabled)"))
    else:
        extra = f", {mem['open_questions']} question(s) for you" if mem["open_questions"] else ""
        lines.append(f"Memory: on; search by meaning: {mem['embeddings']}{extra}")
    lines += _webhook_lines(inbox, runtime or {})
    return "\n".join(lines)


def _drafts(inbox: Inbox) -> str:
    drafts = inbox.store().list_drafts("pending")
    if not drafts:
        return "No drafts waiting. Ask Hermes to draft a reply to a chat."
    lines = ["Drafts waiting for you (send one with /inbox send <id>):"]
    for d in drafts:
        text = d["text"] if len(d["text"]) <= 120 else d["text"][:117] + "..."
        mark = ""
        if d["origin"] == "auto":
            blank = blanks(d["text"])
            mark = " (drafted by Hermes" + (f"; fill in {', '.join(blank)} first" if blank else "") + ")"
        lines.append(f"  [{d['id']}] to {d['chat_name']} ({d['network']}){mark}: {text}")
    return "\n".join(lines)


def _limits(inbox: Inbox) -> str:
    s = inbox.settings
    if s.new_conversations_per_day:
        starting = (f"  • New conversations (people who have not written to you in {s.reply_window_days} days, or"
                    f" ever): at most {s.new_conversations_per_day} a day. Replies to recent chats are not counted")
    else:
        starting = f"  • Replies only: no new chats; they must have written within {s.reply_window_days} days"
    quiet = quiet_hours_problem(s.quiet_hours)
    return "\n".join([
        "Safety limits (stricter values allowed, looser ones are capped):",
        starting,
        "  • One message per approval: you confirm each one (/inbox send, the Inbox page, or Hermes's approval"
        " prompt). No bulk sending. A scheduled message is one you confirmed, checked again when it is due",
        f"  • {s.daily_send_limit} sends per 24 h (WhatsApp {s.daily_limit_whatsapp}, Instagram"
        f" {s.daily_limit_instagram}, LinkedIn {s.daily_limit_linkedin}); {s.hourly_send_limit} per hour; {s.per_chat_hourly_limit} per chat per hour",
        f"  • {s.min_send_gap_seconds}-{s.min_send_gap_seconds * 2}s random gap between sends",
        f"  • No same or near-same text to different people within {s.duplicate_window_hours} h"
        f" (messages of {s.duplicate_min_chars}+ characters)",
        f"  • Quiet hours: {s.quiet_hours or 'off'}" + (f"  (!! {quiet})" if quiet else ""),
        f"  • Reading: {s.read_page_size} items per request, at most {s.max_pages} pages,"
        f" {s.min_call_gap_seconds:g}-{s.max_call_gap_seconds:g}s between calls,"
        f" {s.api_calls_per_hour} calls per hour; "
        + ("a chat is marked as read after you reply (mark_read_after_reply)" if s.mark_read_after_reply
           else "never marks messages as read unless you ask"),
        "  • Errors: a rate limit is never retried; the account waits 1-30 min (1 h after 3 in a row)"
        " and you get a clear message; disconnected or restricted"
        " accounts pause; sends are never retried automatically",
    ])
