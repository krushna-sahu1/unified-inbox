"""The HTTP API behind the Inbox page (web dashboard tab and Hermes Desktop).

Framework-free: ``InboxAPI.handle(method, path, query, body)`` returns ``(status, json)``.
``dashboard/plugin_api.py`` mounts it under ``/api/plugins/unified-inbox/`` with FastAPI, behind
Hermes's dashboard login; tests and the local preview call it directly.

Pages read from the local copy the background sync keeps, so they open instantly; "refresh"
asks Unipile, paced like everything else. Sending from the page is a person's action: the
request must carry the exact draft text the page showed, and every send rule still applies.
"""

from __future__ import annotations

import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .automation import describe_schedule, when_text
from .client import InboxError
from .autodraft import blanks
from .inbox import FRESH_SECONDS, Inbox, quote_view
from .pacing import DAY, HOUR, NETWORK_LABELS
from .privacy import is_sensitive

Handler = Callable[..., Any]
_STATUS = {"setup": 409, "input": 400, "not_found": 404, "paused": 429, "rate_limited": 429, "budget": 429,
           "bad_api_key": 401, "unavailable": 503, "embedding": 502, "memory": 502, "cloud": 503,
           "cloud_schema": 409, "cloud_auth": 401, "too_large": 413, "transcription": 502,
           "resource_not_found": 404, "voice": 502}
# Shown in the page itself; anything else is offered as a download, never rendered (a received
# HTML or SVG file must not run inside the dashboard).
_INLINE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "audio/", "video/")
DATA_MAX_BYTES = 15 * 1024 * 1024  # files/{mid}/{aid}/data (base64 JSON for Hermes Desktop)


class FileResult:
    """A file to stream back instead of JSON (the page's "show / play / download a file")."""

    def __init__(self, path: str, mimetype: str, filename: str):
        self.path, self.filename = path, filename
        self.inline = mimetype.startswith(_INLINE_TYPES)
        self.mimetype = mimetype if self.inline else "application/octet-stream"

    def headers(self) -> dict:
        from urllib.parse import quote

        disposition = "inline" if self.inline else "attachment"
        return {"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(self.filename)}",
                "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, max-age=3600"}
_NO_SETUP_NEEDED = {"overview", "setup", "changes", "cloud", "cloud_schema", "doctor", "export", "export_file",
                    "wipe"}  # pages that work before the plugin is connected
_SHOW = {"waiting_on_you": ["they_wait", "you_promised"], "waiting_on_them": ["you_wait", "they_promised"],
         "promises": ["you_promised", "they_promised"], "all": None}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "plan").strip("-")[:60] or "plan"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class InboxAPI:
    def __init__(self, inbox: Inbox, sync_once: Optional[Callable[[], dict]] = None):
        self.inbox = inbox
        self._sync_once = sync_once
        self._routes: list[tuple[str, re.Pattern, Handler]] = []
        for method, pattern, fn in [
            ("GET", "/overview", self.overview), ("GET", "/today", self.today), ("GET", "/changes", self.changes),
            ("GET", "/chats", self.chats), ("GET", "/chats/{id}", self.chat), ("POST", "/chats/{id}/drafts", self.draft),
            ("GET", "/drafts", self.drafts), ("DELETE", "/drafts/{id}", self.discard),
            ("POST", "/chats/{id}/read", self.mark_read),
            ("POST", "/messages/{id}/react", self.react), ("GET", "/files/{id}/{aid}", self.file),
            ("POST", "/files/{id}/{aid}/open", self.open_file), ("GET", "/files/{id}/{aid}/data", self.file_data),
            ("POST", "/files/{id}/{aid}/save", self.save_file),
            ("POST", "/files/{id}/{aid}/transcribe", self.transcribe),
            ("POST", "/drafts/{id}/send", self.send), ("GET", "/drafts/{id}/check", self.check), ("POST", "/drafts/{id}/schedule", self.schedule),
            ("GET", "/parse-when", self.parse_when), ("GET", "/scheduled", self.scheduled),
            ("DELETE", "/scheduled/{id}", self.unschedule), ("GET", "/loops", self.loops),
            ("POST", "/loops/{id}/done", self.loop_done), ("GET", "/watches", self.watches),
            ("POST", "/watches", self.add_watch), ("DELETE", "/watches/{id}", self.remove_watch),
            ("GET", "/alerts", self.alerts), ("POST", "/alerts/read", self.read_alerts),
            ("GET", "/activity", self.activity), ("GET", "/questions", self.questions),
            ("POST", "/questions/{id}", self.answer), ("POST", "/questions/{id}/dismiss", self.dismiss),
            ("GET", "/people", self.people), ("GET", "/people/{id}", self.person),
            ("POST", "/people/{id}/facts", self.add_fact), ("DELETE", "/facts/{id}", self.forget_fact),
            ("POST", "/people/{id}/link", self.link_person), ("POST", "/people/{id}/unlink", self.unlink_person),
            ("POST", "/people/{id}/rename", self.rename_person), ("POST", "/drafts/{id}/text", self.edit_draft),
            ("GET", "/style", self.style), ("POST", "/style/relearn", self.relearn_style),
            ("DELETE", "/style", self.forget_style),
            ("GET", "/search", self.search), ("GET", "/setup", self.setup), ("POST", "/setup/connect", self.connect),
            ("POST", "/sync", self.sync), ("GET", "/cloud", self.cloud), ("GET", "/cloud/schema", self.cloud_schema),
            ("POST", "/cloud/setup", self.cloud_setup), ("POST", "/cloud/sync", self.cloud_sync),
            ("GET", "/doctor", self.doctor), ("GET", "/privacy/export", self.export),
            ("POST", "/privacy/export-file", self.export_file),
            ("POST", "/privacy/forget/{id}", self.forget), ("POST", "/privacy/wipe", self.wipe),
            ("GET", "/safety", self.safety), ("POST", "/safety/check", self.safety_check),
            ("POST", "/chats/{id}/safe", self.mark_safe),
            ("GET", "/plans", self.plans), ("POST", "/plans/{id}/add", self.add_plan),
            ("POST", "/plans/{id}/dismiss", self.dismiss_plan), ("GET", "/plans/{id}/ics", self.plan_ics),
            ("GET", "/calendar", self.calendar_status), ("POST", "/calendar/refresh", self.calendar_refresh),
            ("POST", "/chats/{id}/translate", self.translate_chat), ("POST", "/drafts/{id}/translate", self.translate_draft),
            ("POST", "/translate", self.translate_text),
            ("GET", "/radar", self.radar),
            ("GET", "/voice/brief", self.voice_brief), ("POST", "/voice/brief/audio", self.voice_brief_audio),
            ("GET", "/voice/audio/{id}", self.voice_audio), ("POST", "/voice/dictate", self.voice_dictate),
        ]:
            regex = re.compile("^" + pattern.replace("{id}", r"(?P<id>[^/]+)").replace("{aid}", r"(?P<aid>[^/]+)")
                               + "$")
            self._routes.append((method, regex, fn))

    # ── dispatch ──
    def handle(self, method: str, path: str, query: Optional[dict] = None, body: Any = None) -> tuple[int, Any]:
        path = "/" + (path or "").strip("/")
        query = {k: v for k, v in (query or {}).items()}
        body = body if isinstance(body, dict) else {}
        for m, regex, fn in self._routes:
            match = regex.match(path)
            if match and m == method.upper():
                try:
                    if fn.__name__ not in _NO_SETUP_NEEDED:
                        self.inbox.client()  # clear "connect first" error instead of a half-empty page
                    args = [match.group(g) for g in ("id", "aid") if g in regex.groupindex]
                    result = fn(*args, q=query, b=body)
                    if method.upper() != "GET":
                        self.inbox.cloud.push_quietly()  # other devices see the change right away
                    return 200, result
                except ApiError as exc:
                    return exc.status, {"error": str(exc)}
                except InboxError as exc:
                    return _STATUS.get(exc.kind, 400), {"error": str(exc), "kind": exc.kind}
                except Exception as exc:  # noqa: BLE001 - never leak a traceback to the page
                    return 500, {"error": f"unified-inbox failed: {type(exc).__name__}: {exc}"}
        if any(regex.match(path) for _, regex, _ in self._routes):
            return 405, {"error": "method not allowed"}
        return 404, {"error": "not found"}

    # ── helpers ──
    def _chat(self, chat_id: str) -> dict:
        chat = self.inbox.store().cached_chat(chat_id)
        if not chat:
            raise ApiError(404, "No such chat.")
        return chat

    def _chat_row(self, c: dict, snoozed: set, loops_by_chat: dict, now: float,
                  mentioned: frozenset = frozenset()) -> dict:
        waiting = not c["last_from_me"] and bool(c["last_text"]) and now - c["ts"] < 7 * DAY and not c["is_muted"]
        scam = None
        if not c["last_from_me"] and now - c["ts"] < 2 * DAY:  # only a recent message from them can be one
            v = self.inbox.shield.for_chat(c)
            scam = {"level": v["level"], "reason": v["reasons"][0]} if v else None
        return {"id": c["id"], "name": c["name"] or "Unknown contact", "network": c["network"],
                "unread": c["unread"], "ts": c["ts"], "preview": c["last_text"][:160], "from_me": c["last_from_me"],
                "group": c["is_group"], "muted": c["is_muted"], "archived": c["is_archived"],
                "readonly": c["is_readonly"],  # e.g. an announcements channel: no reply box
                "snoozed": c["id"] in snoozed, "open_items": loops_by_chat.get(c["id"], 0),
                "mentions_you": c["id"] in mentioned, "scam": scam,
                "needs_reply": waiting and not scam and (not c["is_group"] or c["id"] in mentioned)}

    def _loop_rows(self, items: list[dict]) -> list[dict]:
        store, now = self.inbox.store(), self.inbox.clock()
        cache: dict = {}
        rows = []
        for lp in items:
            chat = cache.setdefault(lp["chat_id"], store.cached_chat(lp["chat_id"]))
            rows.append(self.inbox.context.loop_view(lp, chat, now) | {"due_ts": lp["due_ts"]})  # also when not due
        rows.sort(key=lambda r: (not r.get("overdue", False), r["due_ts"] or 9e18, -r["since_hours"]))
        return rows

    # ── overview ──
    def overview(self, q: dict, b: dict) -> dict:
        inbox = self.inbox
        if not inbox.configured:
            return {"connected": False, "setup": self.setup(q, b)}
        store, now = inbox.store(), inbox.clock()
        chats = store.cached_chats(limit=500)
        snoozed = store.snoozed_chat_ids(now)
        mentioned = store.group_chats_mentioning_you(now - 7 * DAY)
        return {"connected": True,
                "counts": {"unread": sum(1 for c in chats if c["unread"] and c["id"] not in snoozed),
                           "needs_reply": sum(1 for c in chats if self._chat_row(c, snoozed, {}, now, mentioned)
                                              ["needs_reply"] and c["id"] not in snoozed),
                           "drafts": store.count_drafts("pending"), "scheduled": len(store.schedules()),
                           "alerts": len(store.notifications(unread_only=True, limit=1000)),
                           "questions": store.count_questions(), "watches": len(store.watches())},
                "sync": inbox.sync_status(), "memory": inbox.memory_status(),
                "last_event_id": store.last_event_id()}

    def changes(self, q: dict, b: dict) -> dict:
        """Cheap poll for the page: has anything happened since ``since`` (an event id)?"""
        store = self.inbox.store()
        since = _int(q.get("since"))
        last = store.last_event_id()
        alerts = store.notifications(unread_only=True, limit=20)
        return {"last_event_id": last, "changed": last > since, "unread_alerts": len(alerts),
                "alerts": [{"id": a["id"], "title": a["title"], "body": a["body"], "chat_id": a["chat_id"]}
                           for a in alerts if a["id"] > _int(q.get("alerts_since"))]}

    def today(self, q: dict, b: dict) -> dict:
        inbox = self.inbox
        store, now = inbox.store(), inbox.clock()
        try:
            triage = inbox.triage(hours=48, max_age=FRESH_SECONDS)
        except InboxError as exc:
            triage = {"urgent": [], "unanswered": [], "problems": [str(exc)]}
        loops = store.loops(limit=300)
        day_end = time.mktime(time.localtime(now)[:3] + (23, 59, 59, 0, 0, -1))
        return {
            "needs_reply": [dict(a, group="urgent") for a in triage.get("urgent", [])]
            + [dict(a, group="unanswered") for a in triage.get("unanswered", [])],
            "waiting_on_you": self._loop_rows([lp for lp in loops if lp["kind"] in _SHOW["waiting_on_you"]])[:15],
            "waiting_on_them": self._loop_rows([lp for lp in loops if lp["kind"] in _SHOW["waiting_on_them"]])[:10],
            "scheduled_today": [describe_schedule(s) | {"send_ts": s["send_at"]} for s in store.schedules()
                                if s["send_at"] <= day_end],
            "questions": [self._question_row(x) for x in store.questions(limit=3)],
            "alerts": [{"id": a["id"], "title": a["title"], "body": a["body"], "chat_id": a["chat_id"], "ts": a["ts"]}
                       for a in store.notifications(unread_only=True, limit=10)],
            "problems": triage.get("problems", []),
        }

    # ── chats ──
    def chats(self, q: dict, b: dict) -> dict:
        inbox = self.inbox
        store, now = inbox.store(), inbox.clock()
        network = q.get("network") or None
        if q.get("refresh"):
            inbox.gather_chats(network=network, limit=100, max_age=FRESH_SECONDS)
        snoozed = store.snoozed_chat_ids(now)
        mentioned = store.group_chats_mentioning_you(now - 7 * DAY)
        loops_by_chat: dict = {}
        for lp in store.loops(limit=1000):
            loops_by_chat[lp["chat_id"]] = loops_by_chat.get(lp["chat_id"], 0) + 1
        waiting_on_them = {lp["chat_id"] for lp in store.loops(kinds=_SHOW["waiting_on_them"], limit=1000)}
        rows = [self._chat_row(c, snoozed, loops_by_chat, now, mentioned)
                for c in store.cached_chats(network=network, limit=_int(q.get("limit"), 200))]
        f = q.get("filter") or "all"
        if f == "needs_reply":
            rows = [r for r in rows if r["needs_reply"] and not r["snoozed"]]
        elif f == "unread":
            rows = [r for r in rows if r["unread"] and not r["snoozed"]]
        elif f == "waiting_on_them":
            rows = [r for r in rows if r["id"] in waiting_on_them]
        elif f == "snoozed":
            rows = [r for r in rows if r["snoozed"]]
        text = (q.get("q") or "").strip().casefold()
        if text:
            rows = [r for r in rows if text in r["name"].casefold()]
        return {"chats": rows, "filter": f}

    def chat(self, chat_id: str, q: dict, b: dict) -> dict:
        inbox = self.inbox
        store = inbox.store()
        chat = self._chat(chat_id)
        problem = ""
        limit = max(1, min(_int(q.get("limit"), 40), inbox.settings.read_page_size))
        try:
            msgs = inbox.messages(chat, limit=limit, max_age=None if q.get("refresh") else 120)
        except InboxError as exc:
            msgs, problem = store.cached_messages(chat_id, limit), str(exc)
        msgs = inbox.with_files(msgs)
        brief = inbox.context.brief(chat)
        now = inbox.clock()
        translations = store.translations_for(m["id"] for m in msgs)
        warnings = inbox.shield.for_messages(chat, msgs)
        out = {"chat": self._chat_row(chat, store.snoozed_chat_ids(now), {}, now,
                                      store.group_chats_mentioning_you(now - 7 * DAY)),
               "brief": brief, "problem": problem,
               "messages": [{"id": m["id"], "from_me": m["from_me"], "sender": m.get("sender") or "",
                             "text": m["text"], "ts": m["ts"], "edited": m.get("edited", False),
                             "extra": m.get("extra") or {}, "my_reactions": m.get("my_reactions", []),
                             "translation": translations.get(m["id"]),
                             "warning": ({"level": warnings[m["id"]]["level"], "reasons": warnings[m["id"]]["reasons"],
                                          "advice": warnings[m["id"]]["advice"]} if m["id"] in warnings else None)}
                            for m in msgs],
               "untranslated": len(inbox.translator.pending(msgs)), "can_translate": inbox.translator.available(),
               "chat_language": inbox.translator.chat_language(chat),
               "drafts": [self._draft_row(d) for d in store.list_drafts("pending", limit=50) if d["chat_id"] == chat_id],
               "watches": [inbox.automation.watch_view(w) for w in store.watches(chat_id=chat_id)],
               "memory": bool(inbox.context.llm), "mark_read_after_reply": inbox.settings.mark_read_after_reply,
               "can_transcribe": inbox.transcriber is not None}
        if chat["is_group"]:
            out["members"] = inbox.members(chat_id)
        return out

    def _draft_row(self, d: dict) -> dict:
        store = self.inbox.store()
        row = d | {"schedule": (describe_schedule(s) if (s := store.schedule_for_draft(d["id"])) else None)}
        quoted = store.get_message(d["reply_to"]) if d.get("reply_to") else None
        row["replying_to"] = quote_view(quoted) if quoted else ({"message_id": d["reply_to"], "missing": True}
                                                                if d.get("reply_to") else None)
        chat = store.cached_chat(d["chat_id"])
        row["is_group"] = bool(chat and chat["is_group"])
        row["by_hermes"] = d.get("origin") == "auto"
        row["blanks"] = blanks(d["text"]) if row["by_hermes"] else []
        row["your_words"] = d.get("original_text") or None  # a translated draft: what it says, as you wrote it
        risk = self.inbox.shield.reply_risk(d["chat_id"], d["text"])
        row["blocked_as_scam"] = risk or None
        return row

    def edit_draft(self, draft_id: str, q: dict, b: dict) -> dict:
        out = self.inbox.edit_draft(draft_id, str(b.get("text") or ""))
        return out | {"draft": self._draft_row(self.inbox.store().get_draft(draft_id))}

    # ── one person across apps ──
    def link_person(self, chat_id: str, q: dict, b: dict) -> dict:
        self._chat(chat_id)
        return self.inbox.identity.link(chat_id, str(b.get("other_chat_id") or ""))

    def unlink_person(self, chat_id: str, q: dict, b: dict) -> dict:
        self._chat(chat_id)
        return self.inbox.identity.unlink(chat_id)

    def rename_person(self, chat_id: str, q: dict, b: dict) -> dict:
        self._chat(chat_id)
        return self.inbox.identity.rename(chat_id, str(b.get("name") or ""))

    # ── how you write ──
    def style(self, q: dict, b: dict) -> dict:
        prof = self.inbox.style.profile()
        return {"learning": self.inbox.settings.style_learning, "learned": bool(prof.get("guidelines")),
                "guidelines": prof.get("guidelines", []), "messages": prof.get("messages", 0),
                "updated_at": prof.get("updated_at"), "source": prof.get("source", ""), "note": prof.get("note", ""),
                "by_app": {k: v.get("messages", 0) for k, v in (prof.get("by_app") or {}).items()}}

    def relearn_style(self, q: dict, b: dict) -> dict:
        if not self.inbox.settings.style_learning:
            raise ApiError(409, "Learning how you write is off (style_learning).")
        self.inbox.style.learn(use_model=bool(self.inbox.context.llm) and self.inbox.settings.memory_enabled)
        return self.style(q, b)

    def forget_style(self, q: dict, b: dict) -> dict:
        self.inbox.style.forget()
        return self.style(q, b)

    # ── drafts and sending ──
    def draft(self, chat_id: str, q: dict, b: dict) -> dict:
        self._chat(chat_id)
        return self.inbox.create_draft(chat_id, str(b.get("text") or ""), reply_to=str(b.get("reply_to") or ""))

    def drafts(self, q: dict, b: dict) -> dict:
        store = self.inbox.store()
        return {"drafts": [self._draft_row(d) for d in store.list_drafts("pending", limit=100)]}

    def discard(self, draft_id: str, q: dict, b: dict) -> dict:
        store = self.inbox.store()
        sch = store.schedule_for_draft(draft_id)
        if sch:
            self.inbox.automation.cancel(sch["id"])
        self.inbox.discard(draft_id)
        return {"discarded": draft_id}

    def _confirmed_draft(self, draft_id: str, b: dict) -> dict:
        draft = self.inbox.store().get_draft(draft_id)
        if not draft:
            raise ApiError(404, "No such draft.")
        if not b.get("confirm") or b.get("text") != draft["text"]:
            raise ApiError(409, "Confirm the exact text shown before sending.")
        return draft

    def send(self, draft_id: str, q: dict, b: dict) -> dict:
        draft = self._confirmed_draft(draft_id, b)
        if draft["status"] != "pending":
            raise ApiError(409, f"This draft is already {draft['status']}.")
        reasons = self.inbox.preflight(draft)
        if reasons:
            return {"sent": False, "reasons": reasons}
        return self.inbox.send(draft["id"], via="page")

    def check(self, draft_id: str, q: dict, b: dict) -> dict:
        """What the confirm card shows before you press Send: every reason it would be blocked right
        now (empty = fine) and how many sends today count toward the limits. Sends nothing; reads
        the chat only if the saved copy is older than two minutes, and does not refresh accounts."""
        inbox = self.inbox
        draft = inbox.store().get_draft(draft_id)
        if not draft:
            raise ApiError(404, "No such draft.")
        s, now = inbox.settings, inbox.clock()
        local = time.localtime(now)
        midnight = time.mktime(local[:3] + (0, 0, 0, 0, 0, -1))
        today = [r for r in inbox.store().recent_sends(midnight)]
        return {"reasons": inbox.preflight(draft, max_age=120, check_accounts=False),
                "sent_today": len(today), "daily_limit": s.daily_send_limit,
                "sent_today_on_network": sum(1 for r in today if r.network == draft["network"]),
                "network_limit": getattr(s, "daily_limit_" + draft["network"], None),
                "quiet_hours": s.quiet_hours or ""}

    def mark_read(self, chat_id: str, q: dict, b: dict) -> dict:
        self._chat(chat_id)
        return self.inbox.mark_read(chat_id)

    def react(self, message_id: str, q: dict, b: dict) -> dict:
        """A person's reaction from the page: the request must carry the emoji the page showed and
        confirm, like a send."""
        if not b.get("confirm"):
            raise ApiError(409, "Confirm the reaction first.")
        return self.inbox.react(message_id, str(b.get("emoji") or ""), remove=bool(b.get("remove")), via="page")

    def file(self, message_id: str, attachment_id: str, q: dict, b: dict) -> "FileResult":
        """The file itself (downloaded first if needed): shown inline only for photos, audio and video."""
        view = self.inbox.fetch_attachment(message_id, attachment_id)
        return FileResult(view["path"], view["mimetype"], view["filename"])

    def open_file(self, message_id: str, attachment_id: str, q: dict, b: dict) -> dict:
        view = self.inbox.fetch_attachment(message_id, attachment_id)
        out = {k: view[k] for k in ("message_id", "attachment_id", "kind", "type", "filename", "mimetype", "size")}
        out["url"] = f"files/{message_id}/{attachment_id}"
        out["inline"] = FileResult(view["path"], view["mimetype"], view["filename"]).inline
        if view.get("transcript"):
            out["transcript"] = view["transcript"]
        return out

    def file_data(self, message_id: str, attachment_id: str, q: dict, b: dict) -> dict:
        """A photo, audio or video as base64 JSON, for hosts that can only read JSON (Hermes
        Desktop). Other types are never returned this way: save them instead."""
        import base64

        view = self.inbox.fetch_attachment(message_id, attachment_id)
        if not FileResult(view["path"], view["mimetype"], view["filename"]).inline:
            raise ApiError(415, "Only photos, audio and video can be shown here. Save the file instead.")
        if view["size"] > DATA_MAX_BYTES:
            raise InboxError(f"This file is larger than {DATA_MAX_BYTES // 1048576} MB: save it instead.",
                             kind="too_large")
        with open(view["path"], "rb") as fh:
            data = fh.read()
        return {"mimetype": view["mimetype"], "filename": view["filename"], "size": len(data),
                "data": base64.b64encode(data).decode("ascii")}

    def save_file(self, message_id: str, attachment_id: str, q: dict, b: dict) -> dict:
        """A copy of a received file in the plugin's "saved" folder, to open or reveal from the app."""
        return {"path": str(self.inbox.save_attachment_copy(message_id, attachment_id))}

    def transcribe(self, message_id: str, attachment_id: str, q: dict, b: dict) -> dict:
        view = self.inbox.transcribe(message_id, attachment_id)
        return {"message_id": message_id, "attachment_id": attachment_id, "transcript": view.get("transcript", "")}

    def parse_when(self, q: dict, b: dict) -> dict:
        try:
            ts = self.inbox.automation.resolve_when(q.get("q") or "")
        except InboxError as exc:
            return {"ok": False, "error": str(exc)}
        out = {"ok": True, "ts": ts, "text": time.strftime("%A %d %B, %H:%M", time.localtime(ts))}
        draft = self.inbox.store().get_draft(q.get("draft_id") or "") if q.get("draft_id") else None
        if draft:
            out["problems"] = self.inbox.automation.check_schedule(draft, ts)
        return out

    def schedule(self, draft_id: str, q: dict, b: dict) -> dict:
        draft = self._confirmed_draft(draft_id, b)
        auto = self.inbox.automation
        try:
            ts = float(b["ts"]) if b.get("ts") else auto.resolve_when(str(b.get("when") or ""))
        except (TypeError, ValueError):
            raise ApiError(400, "That time can't be read. Pick the time again.") from None
        if ts != ts or ts in (float("inf"), float("-inf")):
            raise ApiError(400, "That time can't be read. Pick the time again.")
        out = auto.schedule(draft["id"], ts)
        return out | {"send_at_text": when_text(out["send_at"])}

    def scheduled(self, q: dict, b: dict) -> dict:
        store = self.inbox.store()
        return {"scheduled": [describe_schedule(s) | {"send_ts": s["send_at"]} for s in store.schedules()],
                "recent": [describe_schedule(s) | {"send_ts": s["send_at"]} for s in store.schedules(
                    statuses=("sent", "held", "failed", "missed", "unknown", "cancelled"), limit=30)]}

    def unschedule(self, schedule_id: str, q: dict, b: dict) -> dict:
        return self.inbox.automation.cancel(schedule_id)

    # ── open items, watches, alerts ──
    def loops(self, q: dict, b: dict) -> dict:
        show = q.get("show") or "all"
        return {"items": self._loop_rows(self.inbox.store().loops(kinds=_SHOW.get(show), limit=300)), "show": show}

    def loop_done(self, loop_id: str, q: dict, b: dict) -> dict:
        if not self.inbox.store().close_loop(_int(loop_id), self.inbox.clock(), reason="marked done by the user"):
            raise ApiError(404, "No such open item.")
        return {"done": _int(loop_id)}

    def watches(self, q: dict, b: dict) -> dict:
        auto = self.inbox.automation
        return {"watches": [auto.watch_view(w) for w in self.inbox.store().watches()]}

    def add_watch(self, q: dict, b: dict) -> dict:
        auto = self.inbox.automation
        chat = self._chat(str(b["chat_id"])) if b.get("chat_id") else None
        until = auto.resolve_when(str(b["until"])) if b.get("until") else None
        return auto.add_watch(str(b.get("type") or ""), chat=chat, text=str(b.get("text") or ""), until=until,
                              network=str(b.get("network") or ""), nudge=bool(b.get("nudge")))

    def remove_watch(self, watch_id: str, q: dict, b: dict) -> dict:
        store = self.inbox.store()
        w = store.watch(_int(watch_id))
        if not w or w["status"] != "active":
            raise ApiError(404, "No such watch.")
        store.set_watch(w["id"], self.inbox.clock(), status="removed")
        return {"removed": w["id"]}

    def alerts(self, q: dict, b: dict) -> dict:
        items = self.inbox.store().notifications(unread_only=not q.get("all"), limit=_int(q.get("limit"), 50))
        return {"alerts": [{"id": a["id"], "kind": a["kind"], "title": a["title"], "body": a["body"],
                            "chat_id": a["chat_id"], "ts": a["ts"], "read": a["read_at"] is not None}
                           for a in reversed(items)]}

    def read_alerts(self, q: dict, b: dict) -> dict:
        store = self.inbox.store()
        ids = [_int(i) for i in (b.get("ids") or [])] or [a["id"] for a in store.notifications(limit=1000)]
        store.mark_notifications(ids, self.inbox.clock())
        return {"read": len(ids)}

    def activity(self, q: dict, b: dict) -> dict:
        """One time-ordered feed: alerts, sends and scheduled-message results."""
        store = self.inbox.store()
        items = [{"ts": a["ts"], "kind": "alert", "title": a["title"], "body": a["body"], "chat_id": a["chat_id"],
                  "id": a["id"], "read": a["read_at"] is not None}
                 for a in store.notifications(unread_only=False, limit=100)]
        for r in store._q("SELECT chat_id, network, text, sent_at FROM send_log ORDER BY sent_at DESC LIMIT 50"):
            chat = store.cached_chat(r[0])
            items.append({"ts": r[3], "kind": "sent", "chat_id": r[0], "body": r[2],
                          "title": f"You sent to {chat['name'] if chat else 'a chat'}"
                                   f" ({NETWORK_LABELS.get(r[1], r[1])})"})
        for a in store.attempts(limit=100):
            if a["status"] == "sent" and a["via"] in ("command", "page"):
                continue  # already listed from the send log
            word = {"sent": "Sent", "failed": "Failed", "blocked": "Blocked", "unsure": "Check the app",
                    "asked": "Waiting for approval (or declined)"}.get(a["status"], a["status"])
            items.append({"ts": a["ts"], "kind": "send", "chat_id": a["target"] if "@" in a["target"] else "",
                          "title": f"{word}: to {a['to']} ({NETWORK_LABELS.get(a['network'], a['network'])}),"
                                   f" via {a['via']}",
                          "body": a["text"] + (f" (+{len(a['files'])} file(s))" if a["files"] else "")
                                  + (f" | {a['detail']}" if a["detail"] and a["status"] != "sent" else "")})
        items.sort(key=lambda x: x["ts"], reverse=True)
        return {"items": items[:120]}

    # ── memory ──
    def questions(self, q: dict, b: dict) -> dict:
        return {"questions": [self._question_row(x) for x in self.inbox.store().questions(limit=50)]}

    def _question_row(self, x: dict) -> dict:
        """A memory question; a "same person?" one also names its two chats (app, name)."""
        row = {"id": x["id"], "text": x["text"], "kind": x["kind"]}
        if x["kind"] == "merge":
            store = self.inbox.store()
            d = x.get("data") or {}
            ids = [d.get("keep_chat"), d.get("merge_chat")]
            if not all(ids):  # an older question names two people: one chat of each
                ids = [((store.contact(d.get(k, "")) or {}).get("chats") or [{}])[0].get("chat_id") for k in
                       ("keep", "merge")]
            chats = [store.cached_chat(i) for i in ids if i]
            if len(chats) == 2 and all(chats):
                row["chats"] = [{"chat_id": c["id"], "network": c["network"], "name": c["name"]} for c in chats]
        return row

    def answer(self, question_id: str, q: dict, b: dict) -> dict:
        return self.inbox.context.answer(_int(question_id), str(b.get("answer") or ""))

    def dismiss(self, question_id: str, q: dict, b: dict) -> dict:
        self.inbox.context.dismiss(_int(question_id))
        return {"dismissed": _int(question_id)}

    def people(self, q: dict, b: dict) -> dict:
        store = self.inbox.store()
        out = []
        for c in store.contacts():
            facts = store.facts(c["id"], limit=200)
            chats = [store.cached_chat(x["chat_id"]) for x in c["chats"]]
            chats = [x for x in chats if x]
            if not chats:
                continue
            chats.sort(key=lambda x: -x["ts"])
            out.append({"id": c["id"], "name": c["name"], "facts": len(facts), "linked": len(chats) > 1,
                        "chats": [{"chat_id": x["id"], "network": x["network"], "name": x["name"]} for x in chats],
                        "last_ts": chats[0]["ts"]})
        out.sort(key=lambda p: p["last_ts"], reverse=True)
        return {"people": out}

    def person(self, chat_id: str, q: dict, b: dict) -> dict:
        store = self.inbox.store()
        chat = self._chat(chat_id)
        contact = store.contact_for_chat(chat_id)
        if not contact:
            return {"name": chat["name"], "facts": [], "pending": 0, "chats": [{"chat_id": chat["id"],
                    "network": chat["network"], "name": chat["name"], "last_activity": chat["ts"]}], "linked": False}
        profile = self.inbox.identity.profile(contact["id"])
        return {"name": contact["name"], "person_id": contact["id"],
                "facts": [{"id": f["id"], "text": f["text"], "source": f["source"], "category": f["category"]}
                          for f in store.facts(contact["id"], limit=200)],
                "pending": len(store.facts(contact["id"], status="pending")),
                "chats": profile["chats"], "linked": len(profile["chats"]) > 1}

    def add_fact(self, chat_id: str, q: dict, b: dict) -> dict:
        store, now = self.inbox.store(), self.inbox.clock()
        chat = self._chat(chat_id)
        text = str(b.get("text") or "").strip()
        if not text:
            raise ApiError(400, "Write the fact first.")
        if is_sensitive(text):
            raise ApiError(400, "That looks like a code, password or account number. It is never saved.")
        if chat["is_group"]:
            raise ApiError(400, "Memory keeps facts about people, not groups.")
        contact = store.ensure_contact(chat_id=chat_id, network=chat["network"], name=chat["name"], now=now)
        fid = store.add_fact(contact_id=contact["id"], text=text[:300], category="other", status="active",
                             source="user", now=now, chat_id=chat_id)
        return {"id": fid, "text": text[:300]}

    def forget_fact(self, fact_id: str, q: dict, b: dict) -> dict:
        if not self.inbox.store().delete_fact(_int(fact_id)):
            raise ApiError(404, "No such fact.")
        return {"forgotten": _int(fact_id)}

    def search(self, q: dict, b: dict) -> dict:
        text = (q.get("q") or "").strip()
        if not text:
            raise ApiError(400, "Type something to search for.")
        return self.inbox.search(text, network=q.get("network") or None, limit=30)

    # ── setup ──
    def setup(self, q: dict, b: dict) -> dict:
        inbox = self.inbox
        out: dict = {"has_key": inbox.configured, "accounts": [], "filter": inbox.account_ids(), "problem": ""}
        if not inbox.configured:
            return out
        try:
            accounts = inbox.refresh_accounts(force=bool(q.get("refresh")))
        except InboxError as exc:
            accounts, out["problem"] = inbox._known_accounts(), str(exc)
        now = inbox.clock()
        out["accounts"] = [{"id": a["id"], "network": a["network"], "status": a["status"],
                            "label": NETWORK_LABELS.get(a["network"], a["network"]),
                            "paused_minutes": int((a["paused_until"] - now) // 60) + 1 if a["paused_until"] > now else 0,
                            "pause_reason": a["pause_reason"]} for a in accounts]
        out["not_found"] = inbox.store().get_meta("missing_accounts", []) or []
        out["memory"] = inbox.memory_status()
        out["sync"] = inbox.sync_status()
        out["cloud"] = inbox.cloud.status()
        s = inbox.settings
        out["limits"] = {"daily": s.daily_send_limit, "hourly": s.hourly_send_limit,
                         "whatsapp": s.daily_limit_whatsapp, "instagram": s.daily_limit_instagram,
                         "linkedin": s.daily_limit_linkedin, "gap_seconds": s.min_send_gap_seconds,
                         "reply_window_days": s.reply_window_days, "quiet_hours": s.quiet_hours or "off",
                         "mark_read_after_reply": s.mark_read_after_reply}
        out["auto_drafts"] = {"per_day": s.auto_draft_per_day, "on": inbox.autodraft.enabled(),
                              "today": inbox.store().usage_today(inbox.context._day(), "auto_drafts")}
        out["style"] = self.style(q, b)
        out["safety"] = {"scam_shield": s.scam_shield}
        out["calendar"] = self.calendar_status(q, b)
        out["translation"] = {"my_language": inbox.translator.my_language(), "also_read": s.also_read,
                              "auto": s.auto_translate, "available": inbox.translator.available()}
        out["voice"] = {"speech_to_text": inbox.transcriber is not None, "text_to_speech": inbox.voice.speaker is not None}
        out["people_radar"] = s.people_radar
        return out

    # ── scam warnings ──
    def safety(self, q: dict, b: dict) -> dict:
        shield = self.inbox.shield
        return {"enabled": shield.enabled(), "flagged": shield.flagged(days=_int(q.get("days"), 7))}

    def safety_check(self, q: dict, b: dict) -> dict:
        from .shield import verdict

        text = str(b.get("text") or "")
        if not text.strip():
            raise ApiError(400, "Paste the message to check.")
        v = verdict([text[:5000]])
        return {"looks_like_a_scam": bool(v), **({k: v[k] for k in ("level", "reasons", "advice")} if v else {})}

    def mark_safe(self, chat_id: str, q: dict, b: dict) -> dict:
        self._chat(chat_id)
        return self.inbox.shield.trust(chat_id, safe=b.get("safe", True) is not False)

    # ── plans and your calendar ──
    def plans(self, q: dict, b: dict) -> dict:
        planner = self.inbox.planner
        days = max(1, min(_int(q.get("days"), 14), 60))
        return {"enabled": planner.enabled(), "calendar": self.calendar_status(q, b),
                "plans": planner.upcoming(days=days, chat_id=q.get("chat_id") or "",
                                          include_dismissed=bool(q.get("dismissed")))}

    def add_plan(self, plan_id: str, q: dict, b: dict) -> dict:
        planner = self.inbox.planner
        view = planner.add(plan_id)
        plan = self.inbox.store().plan(plan_id)
        from .plans import ics_text

        view["ics"] = {"filename": f"{_slug(plan['title'])}.ics",
                       "text": ics_text(plan, self.inbox.clock(), planner._description(plan, view))}
        view.pop("ics_file", None)
        return view

    def dismiss_plan(self, plan_id: str, q: dict, b: dict) -> dict:
        return self.inbox.planner.dismiss(plan_id)

    def plan_ics(self, plan_id: str, q: dict, b: dict) -> "FileResult":
        planner = self.inbox.planner
        plan = self.inbox.store().plan(plan_id)
        if not plan:
            raise ApiError(404, "No such plan.")
        path = planner.ics_file(plan, planner._description(plan, planner.view(plan)))
        if path is None:
            raise ApiError(503, "No folder to write the calendar file to.")
        return FileResult(str(path), "text/calendar", f"{_slug(plan['title'])}.ics")

    def calendar_status(self, q: dict, b: dict) -> dict:
        cal = self.inbox.calendar
        state = self.inbox.store().get_meta("calendar_state", {}) or {}
        return {"connected": cal.configured, "problem": cal.problem() if cal.configured else "",
                "checked_at": state.get("at"), "events": state.get("events")}

    def calendar_refresh(self, q: dict, b: dict) -> dict:
        self.inbox.calendar.refresh(force=True)
        return self.calendar_status(q, b)

    # ── translation ──
    def translate_chat(self, chat_id: str, q: dict, b: dict) -> dict:
        self._chat(chat_id)
        return self.inbox.translator.chat(chat_id, limit=_int(b.get("limit"), 20))

    def translate_draft(self, draft_id: str, q: dict, b: dict) -> dict:
        out = self.inbox.translator.translate_draft(draft_id, str(b.get("to") or ""))
        draft = self.inbox.store().get_draft(out["draft_id"])
        return out | {"draft": self._draft_row(draft)}

    def translate_text(self, q: dict, b: dict) -> dict:
        chat = self._chat(str(b.get("chat_id") or ""))
        return self.inbox.translator.reply(chat, str(b.get("text") or ""), str(b.get("to") or ""))

    # ── people radar ──
    def radar(self, q: dict, b: dict) -> dict:
        return self.inbox.radar.report()

    # ── voice ──
    def voice_brief(self, q: dict, b: dict) -> dict:
        return self.inbox.voice.brief() | {"can_speak": self.inbox.voice.speaker is not None}

    def voice_brief_audio(self, q: dict, b: dict) -> dict:
        """The brief (or the text the page shows) as audio: its address, and the audio itself as base64
        for hosts that only read JSON (Hermes Desktop)."""
        import base64

        voice = self.inbox.voice
        text = str(b.get("text") or "").strip() or voice.brief()["say"]
        audio = voice.speak(text)
        path = Path(audio["path"])
        data = path.read_bytes()
        mimetype = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
        return {"text": text, "file": audio["file"], "url": f"voice/audio/{audio['file']}", "mimetype": mimetype,
                "data": base64.b64encode(data).decode("ascii") if len(data) <= DATA_MAX_BYTES else None}

    def voice_audio(self, name: str, q: dict, b: dict) -> "FileResult":
        path = self.inbox.voice.audio_file(name)
        return FileResult(str(path), mimetypes.guess_type(path.name)[0] or "audio/mpeg", path.name)

    def voice_dictate(self, q: dict, b: dict) -> dict:
        return self.inbox.voice.dictate(str(b.get("audio") or ""), str(b.get("mimetype") or ""))

    def connect(self, q: dict, b: dict) -> dict:
        """A Unipile link where the user connects (or reconnects) an app."""
        inbox = self.inbox
        redirect = str(b.get("redirect_uri") or "https://dashboard.unipile.com")
        if not redirect.startswith(("https://", "http://")):
            raise ApiError(400, "redirect_uri must be a web address.")
        account_id = str(b.get("account_id") or "")
        link = inbox._call("*", lambda: inbox.client().create_auth_link(
            redirect_uri=redirect, expires_on=inbox.clock() + HOUR, account_id=account_id))
        inbox.store().set_meta("accounts_at", 0.0)  # look for the new account on the next read
        return {"link": link}

    # ── cloud sync (optional Supabase) ──
    def cloud(self, q: dict, b: dict) -> dict:
        return self.inbox.cloud.status()

    def cloud_schema(self, q: dict, b: dict) -> dict:
        from .cloud import schema_sql

        return {"sql": schema_sql()}

    def cloud_setup(self, q: dict, b: dict) -> dict:
        cloud = self.inbox.cloud
        return cloud.setup() if b.get("create_tables") else cloud.activate()

    def cloud_sync(self, q: dict, b: dict) -> dict:
        cloud = self.inbox.cloud
        if not cloud.active:
            cloud.activate()
        return cloud.sync() | {"status": cloud.status()}

    # ── health and your data ──
    def doctor(self, q: dict, b: dict) -> dict:
        from . import health

        return {"checks": health.run(self.inbox)}

    def export(self, q: dict, b: dict) -> dict:
        from . import data_control

        return data_control.export_all(self.inbox)

    def export_file(self, q: dict, b: dict) -> dict:
        from . import data_control

        return {"path": str(data_control.export_to_file(self.inbox))}

    def forget(self, chat_id: str, q: dict, b: dict) -> dict:
        from . import data_control

        if not b.get("confirm"):
            raise ApiError(409, "Confirm first.")
        return data_control.forget_person(self.inbox, chat_id)

    def wipe(self, q: dict, b: dict) -> dict:
        from . import data_control

        if b.get("confirm") != "DELETE":
            raise ApiError(409, "Type DELETE to confirm.")
        scope = b.get("scope") or "device"
        if scope not in ("device", "everywhere"):
            raise ApiError(400, "scope is device or everywhere.")
        if scope == "everywhere" and not self.inbox.cloud.active:
            raise ApiError(400, "Cloud sync is off: there is only this device to delete from.")
        return data_control.wipe(self.inbox, everywhere=scope == "everywhere")

    def sync(self, q: dict, b: dict) -> dict:
        if not self._sync_once:
            raise ApiError(503, "Sync is not available here.")
        return self._sync_once()
