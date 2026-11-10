"""``/inbox doctor``: one look at every moving part, each with a fix when something is off.

Every check is local except the Unipile one (a single paced call) and, with cloud sync on, one
Supabase call. Each result is ok (True), a problem (False) or a note (None, optional parts).
"""

from __future__ import annotations

from typing import Any, Optional

from .client import InboxError
from .pacing import DAY, NETWORK_LABELS, quiet_hours_problem
from .store import SCHEMA_VERSION


def _check(name: str, ok: Optional[bool], detail: str, fix: str = "") -> dict:
    out: dict[str, Any] = {"name": name, "ok": ok, "detail": detail}
    if fix and ok is not True:
        out["fix"] = fix
    return out


def run(inbox) -> list[dict]:
    store, s, now = inbox.store(), inbox.settings, inbox.clock()
    out: list[dict] = []

    if not inbox.configured:
        out.append(_check("Unipile API key", False, "Not set.",
                          "Paste it in Settings -> Plugins -> unified-inbox (Unipile dashboard -> API keys)."))
    else:
        out.append(_check("Unipile API key", True, "Set."))
        try:
            accounts = inbox.refresh_accounts(force=True)
            if not accounts:
                out.append(_check("Connected apps", False, "No WhatsApp, Instagram or LinkedIn account found.",
                                  "Connect one on the Inbox page: Setup -> Connect an app."))
            for a in accounts:
                label = NETWORK_LABELS.get(a["network"], a["network"])
                if a["status"] != "OK":
                    out.append(_check(label, False, f"Status {a['status'].lower()}.",
                                      "Reconnect it on the Inbox page: Setup -> Reconnect."))
                elif a["paused_until"] > now:
                    out.append(_check(label, None, f"Waiting {int((a['paused_until'] - now) // 60) + 1} min:"
                                                   f" {a['pause_reason']}."))
                else:
                    out.append(_check(label, True, "Connected."))
            missing = store.get_meta("missing_accounts", []) or []
            if missing:
                out.append(_check("Account filter", False, f"Not found in Unipile: {', '.join(missing)}.",
                                  "Fix or clear 'Only these Unipile accounts' in settings."))
        except InboxError as exc:
            out.append(_check("Unipile", False, str(exc),
                              "Check the API key and your connection." if exc.kind != "paused" else ""))

    integrity = store._conn.execute("PRAGMA quick_check").fetchone()[0]
    version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    row = store._conn.execute("PRAGMA database_list").fetchone()
    size = ""
    if row and row[2]:
        try:
            from pathlib import Path

            size = f", {sum(p.stat().st_size for p in Path(row[2]).parent.glob(Path(row[2]).name + '*')) / 1e6:.1f} MB"
        except OSError:
            size = ""
    out.append(_check("Database", integrity == "ok" and version == SCHEMA_VERSION,
                      f"Schema {version}, integrity {integrity}{size}.",
                      "Restart Hermes to finish the upgrade; if integrity is not ok, export your data and reset."))
    out.append(_check("Search index", True if store.fts else None,
                      "Full-text search on." if store.fts else "This SQLite has no full-text index; search is slower."))

    sync = inbox.sync_status()
    if not s.sync_enabled:
        out.append(_check("Background sync", None, "Off (sync_enabled)."))
    elif sync.get("last_ok_at"):
        late = now - float(sync["last_ok_at"]) > 3 * 900
        out.append(_check("Background sync", not late and not sync.get("last_error"),
                          f"Last checked {sync.get('minutes_since_last_sync', 0)} min ago."
                          + (f" {sync['last_error']}" if sync.get("last_error") else ""),
                          "Hermes must be running. Try /inbox sync to see the error."))
    elif inbox.configured:
        out.append(_check("Background sync", None, "Has not run yet; it starts within a minute of Hermes starting."))

    if s.quiet_hours:  # a setting that cannot be read is silently off: say so
        problem = quiet_hours_problem(s.quiet_hours)
        out.append(_check("Quiet hours", not problem, problem or f"No sends between {s.quiet_hours}.",
                          "In plugin settings, write quiet_hours like 23:00-07:00."))

    mem = inbox.memory_status()
    if not mem["enabled"]:
        out.append(_check("Memory", None, "Off (memory_enabled)."))
    elif not mem["language_model"]:
        out.append(_check("Memory", None, "Off: this Hermes gives plugins no language model."))
    else:
        out.append(_check("Memory", not mem.get("problems"), "On." + (" " + "; ".join(mem.get("problems", []))
                                                                      if mem.get("problems") else ""),
                          "See Settings -> Models -> Auxiliary -> Inbox memory."))
    drafter = getattr(inbox, "autodraft", None)
    if drafter is not None:
        state = store.get_meta("autodraft_state", {}) or {}
        if drafter.enabled():
            out.append(_check("Auto-drafts", not state.get("problems"),
                              f"On: up to {inbox.settings.auto_draft_per_day} a day for urgent chats."
                              + (" " + "; ".join(state["problems"]) if state.get("problems") else "")))
        else:
            out.append(_check("Auto-drafts", None, "Off (auto_draft_per_day is 0, memory is off, or this Hermes"
                                                   " gives plugins no language model)."))
    style = store.get_meta("style_profile", {}) or {}
    if inbox.settings.style_learning:
        out.append(_check("Writing style", True if style.get("guidelines") else None,
                          f"Learned from {style.get('messages', 0)} of your messages." if style.get("guidelines")
                          else style.get("note") or "Not learned yet; it learns in the background."))
    if getattr(inbox, "transcriber", None) is None:
        out.append(_check("Voice notes to text", None, "Off: this Hermes gives plugins no speech-to-text."))
    else:
        out.append(_check("Voice notes to text", True, "On request, with Hermes's speech-to-text (stt in"
                                                       " config.yaml)."))
    out.append(_check("Scam warnings", True if s.scam_shield else None,
                      "On: checked on this machine, no model involved." if s.scam_shield else "Off (scam_shield)."))
    cal = getattr(inbox, "calendar", None)
    if not s.plan_detection:
        out.append(_check("Plans for your calendar", None, "Off (plan_detection)."))
    elif cal is not None and cal.configured:
        cal.refresh()
        problem = cal.problem()
        events = (store.get_meta("calendar_state", {}) or {}).get("events")
        out.append(_check("Plans for your calendar", not problem,
                          problem or f"On; your calendar is connected ({events or 0} events in the next 60 days).",
                          "Copy the calendar's private iCal address again (Google Calendar: Settings -> your"
                          " calendar -> Secret address in iCal format)."))
    else:
        out.append(_check("Plans for your calendar", True, "On. Optional: add your calendar's private iCal address"
                                                           " so it can tell when you are busy."))
    if not inbox.context.llm:
        out.append(_check("Translation", None, "Off: this Hermes gives plugins no language model."))
    else:
        tr_state = store.get_meta("translate_state", {}) or {}
        recent_error = tr_state.get("error") if now - float(tr_state.get("at") or 0) < DAY else ""
        out.append(_check("Translation", not recent_error,
                          (f"Into {inbox.translator.my_language()}"
                           + (", new messages automatically." if s.auto_translate else ", when you ask."))
                          + (f" {recent_error}" if recent_error else "")))
    out.append(_check("Spoken brief", True if getattr(inbox.voice, "speaker", None) else None,
                      "On, with Hermes's text-to-speech (tts in config.yaml)." if getattr(inbox.voice, "speaker", None)
                      else "Text only: this Hermes gives plugins no text-to-speech."))
    insights = store.get_meta("insights_state", {}) or {}
    for name, label in (("shield", "Scam warnings"), ("planner", "Plans for your calendar"),
                        ("translator", "Translation"), ("radar", "People radar")):
        err = insights.get(name) or {}
        if err.get("error") and now - float(err.get("at") or 0) < DAY:
            out.append(_check(f"{label} (background)", False, err["error"], "Run /inbox doctor again after the next"
                                                                              " sync; report it if it stays."))
    out.append(_check("Search by meaning", True if mem["embeddings"] != "off" else None,
                      f"On ({mem['embeddings']}, index: {mem['vector_index']})." if mem["embeddings"] != "off"
                      else "Off (optional): pick an embedding provider in settings."))

    hook = store.get_meta("webhook") or {}
    if hook.get("id"):
        last = store.get_meta("webhook_last_event_at")
        out.append(_check("Instant updates", True if last else None,
                          f"Webhook {hook.get('url')}" + (f", last event {int((now - float(last)) // 60)} min ago."
                                                           if last else ", no events received yet."),
                          "Make sure the public address forwards to the receiver (webhook_port)."))

    cloud = inbox.cloud
    if cloud.configured:
        st = cloud.status()
        if not st["active"]:
            out.append(_check("Cloud sync", False, st["error"] or "Set, but the tables are not created yet.",
                              "Inbox page: Setup -> Cloud backup and sync."))
        else:
            out.append(_check("Cloud sync", not st["error"],
                              f"On ({st['role'] or 'starting'}), {st['pending_changes']} change(s) waiting."
                              + (f" {st['error']}" if st["error"] else ""),
                              "Check the Supabase project is running (free projects pause after a week unused)."))
            if st.get("update_available"):
                out.append(_check("Cloud tables", None, "Made by an earlier version. Syncing works, but reply quotes,"
                                  " \"drafted by Hermes\" marks and the new-conversation count are not shared between"
                                  " your devices yet.",
                                  "Run /inbox cloud setup (or the SQL from /inbox cloud sql, then /inbox cloud setup)."))
    return out


def format_text(checks: list[dict]) -> str:
    mark = {True: "ok ", False: "!! ", None: " - "}
    lines = []
    for c in checks:
        lines.append(f"{mark[c['ok']]} {c['name']}: {c['detail']}")
        if c.get("fix"):
            lines.append(f"     fix: {c['fix']}")
    bad = sum(1 for c in checks if c["ok"] is False)
    lines.append("All good." if not bad else f"{bad} thing(s) need attention.")
    return "\n".join(lines)
