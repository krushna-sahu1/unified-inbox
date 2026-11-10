"""The user's control over their data: export everything, forget a person, delete everything.

Kept apart from the rest so the rules are easy to read:
  * Export writes every table the user owns as plain JSON (no embeddings, no secrets; files people
    sent are listed with their transcripts, not copied in).
  * "Forget" a person removes what memory derived about them (facts, summary, open items,
    conversation windows and embeddings, pending questions). Their messages stay: they are the
    inbox itself, and Unipile would bring them back on the next read anyway.
  * "Delete everything" empties the local database. Two things are kept on purpose, because
    removing them could let the plugin break your own safety limits: sends from the last
    7 days (the limits count them) and account pauses after a rate limit. With cloud sync on,
    the user chooses: delete only on this device (sync is turned off first, so the Supabase
    copy stays), or everywhere (the deletions sync to Supabase and to the other devices).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .client import InboxError
from .pacing import DAY

EXPORT_TABLES = ("chats", "messages", "drafts", "schedules", "send_log", "send_attempts", "attachment_files",
                 "contacts", "contact_chats", "facts", "loops", "questions", "chat_context", "watches",
                 "notifications", "accounts", "plans", "translations")
# Emptied by "delete everything". send_log and accounts are trimmed, not emptied (see above).
# send_attempts is the activity log (/inbox log): the limits read send_log, so it can all go.
WIPE_TABLES = ("messages", "chats", "drafts", "schedules", "facts", "contact_chats", "contacts", "loops",
               "questions", "chat_context", "watches", "notifications", "events", "webhook_seen", "send_attempts",
               "attachment_files", "plans", "translations")
KEEP_META = {"device_id", "next_send_allowed_at", "last_call_at", "cloud_active", "cloud_pull_since", "cloud_role",
             "cloud_schema_version",
             "cloud_state", "webhook", "alerts_session", "pruned_at",
             "reaction_times"}  # reactions have limits too: a reset must not hand out a fresh allowance
SAFETY_WINDOW = 7 * DAY
ACTIVITY_FILES = ("activity.log", "activity.log.1")


def export_all(inbox) -> dict:
    """Everything the user owns, as JSON-ready rows."""
    store = inbox.store()
    out: dict = {"app": "unified-inbox", "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "note": "Your inbox data. Embeddings and secrets are not included.", "tables": {}}
    for table in EXPORT_TABLES:
        cur = store._conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for key in ("extra", "data", "files"):
                if isinstance(row.get(key), str):
                    try:
                        row[key] = json.loads(row[key])
                    except ValueError:
                        pass
            row.pop("rid", None)
            rows.append(row)
        out["tables"][table] = rows
    out["counts"] = {t: len(rows) for t, rows in out["tables"].items()}
    style = store.get_meta("style_profile", {}) or {}
    if style:
        out["writing_style"] = style  # what was learned about how you write
    return out


def export_to_file(inbox, folder: Optional[Path] = None) -> Path:
    """Write the export next to the database (or into ``folder``). Returns the file."""
    folder = folder or _data_dir(inbox)
    if folder is None:
        raise InboxError("This database has no folder to write to. Export from the Inbox page instead.",
                         kind="input")
    folder = Path(folder) / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / time.strftime("unified-inbox-export-%Y%m%d-%H%M%S.json")
    data = json.dumps(export_all(inbox), ensure_ascii=False, indent=1)
    # It holds your messages: like the database, readable by you alone on macOS and Linux.
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        fh.write(data)
    return path


def _data_dir(inbox) -> Optional[Path]:
    row = inbox.store()._conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]).parent if row and row[2] else None


def _delete_activity_files(inbox) -> int:
    """The plain-text copy of the activity log (activity.log, and its previous part)."""
    folder = _data_dir(inbox)
    removed = 0
    for name in ACTIVITY_FILES if folder else ():
        try:
            (folder / name).unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def _delete_downloads(store) -> int:
    """Files people sent you that were opened here (the attachments folder)."""
    import shutil

    folder = store.files_dir()
    if folder is None or not folder.is_dir():
        return 0
    count = sum(1 for p in folder.iterdir() if p.is_file())
    shutil.rmtree(folder, ignore_errors=True)
    return count


def _delete_folders(store, names: tuple) -> int:
    """Spoken briefs and calendar files the plugin made (in its own folder only)."""
    import shutil

    base = store.data_dir()
    count = 0
    for name in names if base else ():
        folder = base / name
        if folder.is_dir():
            count += sum(1 for p in folder.iterdir() if p.is_file())
            shutil.rmtree(folder, ignore_errors=True)
    return count


def forget_person(inbox, chat_id: str) -> dict:
    """Remove everything memory derived about the person in this chat (all their chats if
    they were linked across apps)."""
    store, now = inbox.store(), inbox.clock()
    chat = store.cached_chat(chat_id)
    if not chat:
        raise InboxError("No such chat.", kind="not_found")
    contact = store.contact_for_chat(chat_id)
    chats = [c["chat_id"] for c in (store.contact(contact["id"]) or {}).get("chats", [])] if contact else [chat_id]
    facts = store.forget_contact(contact["id"]) if contact else 0
    for cid in chats:
        store.forget_chat_memory(cid)
        store.forget_chat_insights(cid)  # plans found in their chats, translations of their messages
    birthdays = dict(store.get_meta("radar_birthdays", {}) or {})
    if any(cid in birthdays for cid in chats):
        store.set_meta("radar_birthdays", {k: v for k, v in birthdays.items() if k not in chats})
    dropped = 0
    for q in store.questions(limit=1000):
        d = q["data"]
        if d.get("chat_id") in chats or (contact and d.get("contact_id") == contact["id"]) or \
                (contact and q["kind"] == "merge" and contact["id"] in (d.get("keep"), d.get("merge"))):
            store.close_question(q["id"], now, status="dismissed", answer="forgotten")
            dropped += 1
    return {"name": chat["name"], "chats": len(chats), "facts": facts, "questions": dropped}


def wipe(inbox, *, everywhere: bool) -> dict:
    """Delete everything (see the module notes for what is kept, and why)."""
    store, now = inbox.store(), inbox.clock()
    cloud = getattr(inbox, "cloud", None)
    syncing = bool(cloud and cloud.active)
    if syncing and not everywhere:
        cloud.deactivate()  # keep the Supabase copy: stop syncing before deleting here
    counts = {}
    with store._lock:
        c = store._conn
        ids = [r[0] for r in c.execute("SELECT id FROM chunks")]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            c.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({marks})", part)
            if store.vec_index and store._has_vec_table():
                c.executemany("DELETE FROM vec_chunks WHERE chunk_id=?", [(x,) for x in part])
        counts["chunks"] = c.execute("DELETE FROM chunks").rowcount
        for table in WIPE_TABLES:
            counts[table] = c.execute(f"DELETE FROM {table}").rowcount
        counts["send_log"] = c.execute("DELETE FROM send_log WHERE sent_at<?", (now - SAFETY_WINDOW,)).rowcount
        c.execute(f"DELETE FROM meta WHERE key NOT IN ({','.join('?' * len(KEEP_META))})", tuple(KEEP_META))
        c.execute("DELETE FROM usage")
        c.commit()
    counts["activity_files"] = _delete_activity_files(inbox)
    counts["downloaded_files"] = _delete_downloads(store)
    counts["voice_and_calendar_files"] = _delete_folders(store, ("voice", "calendar"))
    calendar = getattr(inbox, "calendar", None)
    if calendar is not None:
        calendar.forget()  # the copy of your calendar kept in memory
    pending = 0
    if syncing and everywhere:
        # The deletions sync like any change, so Supabase and your other devices delete too.
        # Send as much as possible now; the background worker finishes the rest.
        deadline = time.monotonic() + 60
        while store.outbox_count() and time.monotonic() < deadline:
            cloud.push()
        pending = store.outbox_count()
    return {"deleted": counts, "kept": "sends from the last 7 days and account pauses (your limits need them)",
            "supabase": ("deleted" if syncing and everywhere else "kept" if syncing else "not used"),
            "still_syncing": pending}
