"""unified-inbox: one inbox for WhatsApp, Instagram and LinkedIn messages in Hermes (via Unipile).

The model can read, search, triage and draft. It sends only through ``inbox_send``, and only after
the user approves that exact message in Hermes's own approval prompt (see sending.py); a person can
also send a saved draft with the /inbox send slash command. A background sync (and, optionally, a
local webhook receiver) keeps the local cache current. Hermes modules are imported lazily, inside
functions, so the plugin also imports cleanly in the validator's bare interpreter.
"""

from __future__ import annotations

from typing import Any

PLUGIN_NAME = "unified-inbox"
TOOLSET = "unified_inbox"

DIGEST_PROMPT = (
    "Write my unified inbox digest for WhatsApp, Instagram and LinkedIn. Call inbox_status, then "
    "inbox_triage with hours={hours} and inbox_loops with show=waiting_on_you. List URGENT chats first, then "
    "UNANSWERED ones and promises due soon, each as one line: "
    "name, app, how long they've waited, and why it matters. Then list up to {drafts} of the most "
    "important chats and save a short, personal draft reply for each with inbox_draft_reply. "
    "Never say a message was sent: you cannot send. End with: 'Review drafts with /inbox drafts and "
    "send one with /inbox send <id>.' If an account is paused or the tools report an error, say so "
    "plainly. Keep the whole digest under 200 words."
)


def _open_db():
    """The plugin database, opened so the background sync thread can share it.

    Hermes's ``plugin_db`` decides where the file lives. The background sync, the webhook
    receiver and tool calls use one connection from several threads (the Store serialises
    them), so the file is reopened with ``check_same_thread=False``. A busy timeout covers a
    second Hermes process (CLI and gateway) writing at the same moment."""
    import sqlite3

    from plugins.plugin_storage import plugin_db

    conn = plugin_db(PLUGIN_NAME, "data.db")
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:  # noqa: BLE001 - not a plain sqlite3 connection: use it as given
        return conn
    if not path:  # in-memory: nothing to reopen
        return conn
    conn.close()
    shared = sqlite3.connect(path, check_same_thread=False, timeout=30)
    shared.execute("PRAGMA journal_mode=WAL")
    shared.execute("PRAGMA busy_timeout=30000")
    _owner_only(path)
    return shared


def _owner_only(path: str) -> None:
    """The database holds your messages: on macOS and Linux, make it readable by you alone.
    (Windows already limits a user's profile folder to that user.)"""
    import os

    if os.name == "nt":
        return
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except OSError:
            pass


EVENING_PROMPT = (
    "Write my evening inbox wrap-up for WhatsApp, Instagram and LinkedIn. Call inbox_updates, then inbox_loops "
    "with show=all, then inbox_schedule with action=list. In short lines: who wrote today and is still waiting on "
    "me, what I promised that is due tomorrow or overdue, who I am waiting on, and what is scheduled to go out "
    "tomorrow. Offer to draft replies for the top {drafts} waiting chats with inbox_draft_reply. Never say a "
    "message was sent unless inbox_updates reports it. Under 150 words."
)

WEEKLY_PROMPT = (
    "Write my weekly inbox review for WhatsApp, Instagram and LinkedIn. Call inbox_triage with hours=168, then "
    "inbox_loops with show=all, then inbox_radar. In short lines: people I have not replied to this week (longest "
    "wait first), promises still open, things I am waiting on, people I am drifting apart from, birthdays coming "
    "up, and my reply time. Suggest which to answer first. Under 220 words."
)

ALERTS_PROMPT = (
    "Check my inbox for anything I should know now. Call inbox_updates. If it has no alerts and no new messages "
    "that need me (skip chit-chat and newsletters), reply with exactly [SILENT] and nothing else. Otherwise list "
    "each in one short line: who, which app, what they need."
)

MEMORY_TASK = "unified_inbox_memory"


def _make_llm(ctx: Any):
    """Memory's model call through Hermes (``ctx.llm``), on its own auxiliary task so the user can
    point it at a cheaper model in Settings -> Models -> Auxiliary. None if this Hermes has no
    plugin LLM access: memory is then off and everything else works."""
    llm = getattr(ctx, "llm", None)
    if llm is None or not hasattr(llm, "complete_structured"):
        return None
    task = None
    register_task = getattr(ctx, "register_auxiliary_task", None)
    if register_task:
        try:
            register_task(MEMORY_TASK, display_name="Inbox memory",
                          description="Summaries, facts about people and open items from your chats.",
                          defaults={"timeout": 90})
            task = MEMORY_TASK
        except Exception:  # noqa: BLE001 - older Hermes: fall back to the main model
            task = None

    def call(instructions: str, text: str, schema: dict):
        result = llm.complete_structured(instructions=instructions, input=[{"type": "text", "text": text}],
                                         json_schema=schema, schema_name="inbox_memory", temperature=0.1,
                                         max_tokens=1500, timeout=90, purpose="unified-inbox memory", task=task)
        return getattr(result, "parsed", None)

    return call


def _make_push(ctx: Any, inbox: Any):
    """Alerts into the chat the user picked with /inbox alerts here, through Hermes's gateway
    injection (the user must allow it in config.yaml). None if this Hermes cannot do it."""
    inject = getattr(ctx, "inject_message", None)
    if inject is None:
        return None

    def push(text: str) -> bool:
        key = inbox.store().get_meta("alerts_session")
        return bool(key) and bool(inject(text, role="user", session_key=key))

    return push


def _make_transcriber():
    """Voice notes to text with Hermes's own speech-to-text (the user's ``stt:`` setting: local
    Whisper by default, or Groq, OpenAI...). Imported when first used, so a Hermes without it
    only loses this one feature."""

    def transcribe(path: str) -> dict:
        try:
            from tools.transcription_tools import transcribe_audio  # Hermes's STT dispatcher
        except Exception as exc:  # noqa: BLE001 - not available in this Hermes
            return {"success": False, "error": f"this Hermes has no speech-to-text ({type(exc).__name__})"}
        return transcribe_audio(path, source="unified-inbox")

    return transcribe


def _make_speaker(ctx: Any):
    """Spoken briefs with Hermes's own text-to-speech tool (through ``ctx.dispatch_tool``, the
    public way for a plugin to use a built-in tool). None if this Hermes cannot do that."""
    dispatch = getattr(ctx, "dispatch_tool", None)
    if dispatch is None:
        return None
    from .voice import make_speaker

    return make_speaker(dispatch)


def _get_secret(name: str):
    from agent.secret_scope import get_secret

    return get_secret(name)


# Background threads of the current registration (a re-registration replaces them).
_RUNTIME: dict = {}


def _stop_background() -> None:
    server = _RUNTIME.pop("webhook_server", None)
    if server:
        server.stop()
    worker = _RUNTIME.pop("worker", None)
    if worker:
        worker.stop()
    _RUNTIME.pop("webhook_error", None)


def _unload() -> None:
    """Hermes unloads (or reloads) the plugin: stop the background threads and close the
    database, so nothing keeps the file open (Windows cannot delete or replace an open file)."""
    _stop_background()
    inbox = _RUNTIME.pop("inbox", None)
    if inbox is not None:
        inbox.close()


def _start_background(inbox: Any) -> dict:
    """Start the sync worker, and the webhook receiver when ``webhook_port`` is set. Both are
    daemon threads; the worker idles until the plugin is connected."""
    from .sync import SyncWorker
    from .webhooks import WebhookHandler, WebhookServer

    _stop_background()
    worker = SyncWorker(inbox)
    worker.start()
    _RUNTIME["worker"] = worker
    s = inbox.settings
    if s.webhook_port:
        server = WebhookServer(WebhookHandler(inbox, on_change=worker.wake), s.webhook_host or "127.0.0.1",
                               s.webhook_port)
        try:
            server.start()
            _RUNTIME["webhook_server"] = server
        except OSError as exc:  # port taken or not allowed: polling still works
            _RUNTIME["webhook_error"] = f"Could not listen on {s.webhook_host}:{s.webhook_port}: {exc}"
    return _RUNTIME


def register(ctx: Any) -> None:
    from . import schemas
    from .commands import build_command
    from .inbox import Inbox
    from .tools import build_handlers

    _unload()  # a re-registration replaces the previous one completely
    inbox = Inbox(get_config=ctx.get_config, get_secret=_get_secret, open_db=_open_db, llm=_make_llm(ctx),
                  transcriber=_make_transcriber(), speaker=_make_speaker(ctx))
    inbox.automation.push = _make_push(ctx, inbox)
    runtime = _start_background(inbox)
    runtime["inbox"] = inbox  # the dashboard / Desktop API (dashboard/plugin_api.py) uses this one
    on_unload = getattr(ctx, "on_unload", None)
    if on_unload:
        on_unload(_unload)
    handlers = build_handlers(inbox)
    for schema in schemas.ALL:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handlers[schema["name"]],
                          description=schema["description"], emoji="📥")

    # Every inbox_send and inbox_react goes through Hermes's own approval prompt first (see sending.py).
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook:
        from .sending import pre_tool_call_hook

        register_hook("pre_tool_call", pre_tool_call_hook(inbox))
    session_cwd = getattr(ctx, "session_cwd", None)
    if session_cwd:
        inbox.session_cwd = session_cwd  # relative file paths are read from the chat's folder

    register_skill = getattr(ctx, "register_skill", None)
    if register_skill:  # how to use these tools well; loaded by Hermes only when relevant
        from pathlib import Path

        register_skill("inbox", str(Path(__file__).parent / "skills" / "inbox" / "SKILL.md"))

    ctx.register_command("inbox", handler=build_command(inbox, ctx.ask_choice, runtime),
                         description="Unified WhatsApp, Instagram and LinkedIn inbox: status, drafts, send <id>",
                         args_hint="[status|drafts|send <id>|discard <id>|sync|webhook|limits]")

    ctx.register_automation_blueprint(
        "daily-digest",
        title="Inbox daily digest",
        description="Every day, flag urgent and unanswered WhatsApp, Instagram and LinkedIn chats and draft replies"
                    " for you to approve. Never sends anything.",
        category="productivity",
        schedule_template="{minute} {hour} * * *",
        prompt_template=DIGEST_PROMPT,
        slots=[
            {"name": "time", "type": "time", "label": "What time?", "default": "09:00"},
            {"name": "hours", "type": "enum", "label": "Look back how far?", "default": "24",
             "options": ["12", "24", "48"]},
            {"name": "drafts", "type": "enum", "label": "Draft replies for how many chats?", "default": "3",
             "options": ["0", "3", "5"]},
            {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
             "options": ["origin", "local"], "strict": False},
        ],
        tags=["inbox", "whatsapp", "instagram", "linkedin", "daily"],
    )
    deliver = {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
               "options": ["origin", "local"], "strict": False}
    ctx.register_automation_blueprint(
        "evening-wrap",
        title="Inbox evening wrap-up",
        description="Every evening: who is still waiting on you, promises due tomorrow, and what is scheduled."
                    " Never sends anything.",
        category="productivity",
        schedule_template="{minute} {hour} * * *",
        prompt_template=EVENING_PROMPT,
        slots=[{"name": "time", "type": "time", "label": "What time?", "default": "20:00"},
               {"name": "drafts", "type": "enum", "label": "Offer drafts for how many chats?", "default": "3",
                "options": ["0", "3", "5"]}, deliver],
        tags=["inbox", "whatsapp", "instagram", "linkedin", "evening"],
    )
    ctx.register_automation_blueprint(
        "weekly-review",
        title="Inbox weekly review",
        description="Once a week: who you have not replied to, open promises, and who deserves a follow-up.",
        category="productivity",
        schedule_template="{minute} {hour} * * {day}",
        prompt_template=WEEKLY_PROMPT,
        slots=[{"name": "time", "type": "time", "label": "What time?", "default": "10:00"},
               {"name": "day", "type": "enum", "label": "Which day?", "default": "sun",
                "options": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}, deliver],
        tags=["inbox", "whatsapp", "instagram", "linkedin", "weekly"],
    )
    ctx.register_automation_blueprint(
        "inbox-alerts",
        title="Inbox alerts",
        description="Every few minutes, tell me about new messages that need me and alerts I set. Stays silent"
                    " when there is nothing. Each check uses one model call.",
        category="productivity",
        schedule_template="*/{every} * * * *",
        prompt_template=ALERTS_PROMPT,
        slots=[{"name": "every", "type": "enum", "label": "Check every how many minutes?", "default": "15",
                "options": ["5", "10", "15", "30"]}, deliver],
        tags=["inbox", "alerts", "whatsapp", "instagram", "linkedin"],
    )
