"""Tool handlers. Each returns a JSON string and never raises."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .client import InboxError, describe_attachment
from .automation import describe_schedule, when_text
from .inbox import FRESH_SECONDS, Inbox
from .privacy import is_sensitive
from .autodraft import blanks
from .sending import react_tool, send_tool
from .shield import label as scam_label
from .shield import verdict as scam_verdict

_SHOW = {"waiting_on_you": ["they_wait", "you_promised"], "waiting_on_them": ["you_wait", "they_promised"],
         "promises": ["you_promised", "they_promised"], "all": None}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="minutes") if ts else ""


def _net(args: dict) -> Any:
    n = str(args.get("network") or "all").lower()
    return None if n == "all" else n


def _wrap(fn: Callable[[dict], Any]) -> Callable[..., str]:
    def handler(args: dict, **kwargs) -> str:
        try:
            return json.dumps(fn(args or {}), ensure_ascii=False)
        except InboxError as exc:
            return json.dumps({"error": str(exc), "kind": exc.kind})
        except Exception as exc:  # never raise into the agent loop
            return json.dumps({"error": f"unified-inbox failed: {type(exc).__name__}: {exc}"})
    return handler


def _local_ts(value: Any, what: str) -> float:
    """An ISO date-time (local time unless it says otherwise) as a timestamp."""
    try:
        dt = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        raise InboxError(f"Give {what} as an ISO date-time, e.g. 2026-11-12T16:00.", kind="input") from None
    return dt.timestamp()


def _plan_out(v: dict) -> dict:
    """A plan for the model: no raw timestamps."""
    out = {k: x for k, x in v.items() if k not in ("start_ts", "end_ts")}
    if out.get("busy_with"):
        out["busy_with"] = [{"when": b["when"], "what": b["title"], "from": b["source"]} for b in out["busy_with"]]
    return out


def warning_view(v: dict) -> dict:
    return {"level": v["level"], "says": scam_label(v), "reasons": v["reasons"], "advice": v["advice"]}


def message_view(m: dict, chat: dict, translation: Optional[dict] = None, warning: Optional[dict] = None) -> dict:
    """One message as the model sees it: who, what, when, its id, files, quote and reactions (and
    its translation and a scam warning, when it has them)."""
    out: dict = {"message_id": m["id"], "from": "me" if m["from_me"] else (m.get("sender") or chat.get("name") or ""),
                 "text": m["text"], "time": _iso(m["ts"])}
    extra = m.get("extra") or {}
    files = [{"attachment_id": a["id"], "kind": describe_attachment(a), "downloaded": a.get("downloaded", False),
              **({"transcript": a["transcript"]} if a.get("transcript") else {})}
             for a in extra.get("attachments", []) if a.get("id")]
    if files:
        out["files"] = files
    quoted = extra.get("quoted")
    if quoted:
        out["replying_to"] = {"message_id": quoted.get("id", ""), "text": quoted.get("text", "")[:160],
                              **({"from": quoted["sender"]} if quoted.get("sender") else {})}
    if extra.get("reactions"):
        out["reactions"] = {r["reaction"]: r["count"] for r in extra["reactions"]}
    if extra.get("mentioned") and not m["from_me"]:
        out["mentions_you"] = True
    if m.get("my_reactions"):
        out["your_reactions"] = m["my_reactions"]
    if translation:
        out["translation"] = {"from": translation["source"], "text": translation["text"]}
    if warning:
        out["warning"] = warning_view(warning)
    return out


def build_handlers(inbox: Inbox) -> dict[str, Callable[..., str]]:
    def status(args: dict) -> Any:
        return inbox.status()

    def list_chats(args: dict) -> Any:
        chats, problems = inbox.gather_chats(network=_net(args), unread_only=bool(args.get("unread_only")),
                                             limit=int(args.get("limit") or 20), max_age=FRESH_SECONDS)
        out = {"chats": [
            {"chat_id": c["id"], "name": c["name"], "network": c["network"], "unread": c["unread"],
             "last_activity": _iso(c["ts"])} for c in chats]}
        if problems:
            out["problems"] = problems
        return out

    def read_chat(args: dict) -> Any:
        chat = inbox.find_chat(str(args.get("chat_id") or ""))
        msgs = inbox.with_files(inbox.messages(chat, limit=int(args.get("limit") or 20), max_age=FRESH_SECONDS))
        store = inbox.store()
        translations = store.translations_for(m["id"] for m in msgs)
        warnings = inbox.shield.for_messages(chat, msgs)
        out = {"chat_id": chat["id"], "name": chat["name"], "network": chat["network"],
               "messages": [message_view(m, chat, translations.get(m["id"]), warnings.get(m["id"])) for m in msgs]}
        scam = inbox.shield.for_chat(chat, msgs)
        if scam:
            out["safety_warning"] = warning_view(scam)
        untranslated = inbox.translator.pending(msgs)
        if untranslated:
            out["not_translated"] = {"messages": len(untranslated),
                                     "languages": sorted({lang for _m, lang in untranslated if lang}),
                                     "how": "inbox_translate with action=chat"}
        if chat.get("is_group"):
            out["group"] = True
            out["members_seen"] = [m["name"] for m in inbox.members(chat["id"]) if m["name"]][:30]
            out["how_to_mention"] = "Write @ and their name in inbox_send text, e.g. @Priya."
        return out

    def search(args: dict) -> Any:
        query = str(args.get("query") or "").strip()
        if not query:
            raise InboxError("Give a query to search for.", kind="input")
        out = inbox.search(query, network=_net(args))
        for r in out["results"]:
            r["time"] = _iso(r.pop("ts"))
        for r in out.get("related", []):
            r["from"], r["to"] = _iso(r.pop("start_ts")), _iso(r.pop("end_ts"))
            r.pop("chunk_id", None)
        return out

    def _one_chat(args: dict) -> Any:
        """The chat ``args`` points at, or a dict asking which one (several matches)."""
        chat_id, person = str(args.get("chat_id") or "").strip(), str(args.get("person") or "").strip()
        if not chat_id and not person:
            raise InboxError("Give a chat_id or a person's name.", kind="input")
        inbox.client()  # setup check
        found = inbox.context.find(chat_id=chat_id, person=person)
        if not found and chat_id:
            found = [inbox.find_chat(chat_id)]
        if not found:
            raise InboxError(f"No chat found for {person or chat_id!r}. Try inbox_list_chats.", kind="not_found")
        if not chat_id and len(found) > 1 and found[0]["name"].casefold() != person.casefold():
            return {"which_one": [{"chat_id": c["id"], "name": c["name"], "network": c["network"],
                                   "last_activity": _iso(c["ts"])} for c in found[:6]]}
        return found[0]

    def context(args: dict) -> Any:
        chat = _one_chat(args)
        if "which_one" in chat:
            return chat
        try:
            recent = inbox.messages(chat, limit=6, max_age=FRESH_SECONDS)
        except InboxError:
            recent = inbox.store().cached_messages(chat["id"], 6)  # paused or offline: what we have
        out = inbox.context.brief(chat, recent=recent)
        for m in out.get("recent", []):
            m["time"] = _iso(m.pop("ts"))
        if not inbox.context.llm:
            out["note"] = "Memory (summaries, facts, open items) needs Hermes's language model; showing messages only."
        return out

    def loops(args: dict) -> Any:
        inbox.client()  # setup check
        store, now = inbox.store(), inbox.clock()
        if args.get("done"):
            if not store.close_loop(int(args["done"]), now, reason="marked done by the user"):
                raise InboxError(f"No open item {args['done']}.", kind="not_found")
        show = str(args.get("show") or "all")
        items = store.loops(kinds=_SHOW.get(show), limit=200)
        chats: dict = {}
        views = []
        for lp in items:
            chat = chats.setdefault(lp["chat_id"], store.cached_chat(lp["chat_id"]))
            views.append(inbox.context.loop_view(lp, chat, now))
        views.sort(key=lambda v: (not v.get("overdue", False), v.get("due") or "9999", -v["since_hours"]))
        out: dict = {"show": show, "items": views[:40], "more": max(0, len(views) - 40)}
        if args.get("done"):
            out["marked_done"] = int(args["done"])
        if not inbox.context.llm:
            out["note"] = "Open items come from memory, which needs Hermes's language model. Use inbox_triage."
        return out

    def memory(args: dict) -> Any:
        inbox.client()  # setup check
        store, now, ctx = inbox.store(), inbox.clock(), inbox.context
        action = str(args.get("action") or "")
        if action == "questions":
            qs = store.questions(limit=3)
            return {"questions": [{"question_id": q["id"], "question": q["text"]} for q in qs],
                    "total_open": store.count_questions(),
                    "how": "Ask one at a time when the user is free; record with action=answer."}
        if action == "answer":
            return ctx.answer(int(args.get("question_id") or 0), str(args.get("answer") or ""))
        if action == "dismiss":
            ctx.dismiss(int(args.get("question_id") or 0))
            return {"dismissed": int(args["question_id"])}
        if action == "style":
            prof = inbox.style.profile()
            return {"learned": bool(prof.get("guidelines")), "guidelines": prof.get("guidelines", []),
                    "from_messages": prof.get("messages", 0), **({"note": prof["note"]} if prof.get("note") else {}),
                    "learning": inbox.settings.style_learning}
        if action == "forget_style":
            inbox.style.forget()
            return {"forgotten": "writing style", "learning_again": inbox.settings.style_learning}
        if action == "forget" and args.get("fact_id"):
            if not store.delete_fact(int(args["fact_id"])):
                raise InboxError(f"No fact {args['fact_id']}.", kind="not_found")
            return {"forgotten": int(args["fact_id"])}
        chat = _one_chat(args)
        if "which_one" in chat:
            return chat
        if chat["is_group"]:
            raise InboxError("Memory keeps facts about people, not groups.", kind="input")
        if action == "link":
            other = inbox.context.find(chat_id=str(args.get("other_chat_id") or ""))
            if not other:
                raise InboxError("Give other_chat_id: the other chat of the same person.", kind="input")
            return {"linked": inbox.identity.link(chat["id"], other[0]["id"])}
        if action == "unlink":
            return inbox.identity.unlink(chat["id"])
        if action == "rename":
            return {"renamed": inbox.identity.rename(chat["id"], str(args.get("name") or ""))}
        contact = store.ensure_contact(chat_id=chat["id"], network=chat["network"], name=chat["name"], now=now)
        if action == "person":
            return inbox.identity.profile(contact["id"])
        if action == "remember":
            text = str(args.get("text") or "").strip()
            if not text:
                raise InboxError("Give the fact to remember in text.", kind="input")
            if is_sensitive(text):
                raise InboxError("That looks like a code, password or account number; it is never saved.",
                                 kind="input")
            fid = store.add_fact(contact_id=contact["id"], text=text[:300], category="other", status="active",
                                 source="user", now=now, chat_id=chat["id"])
            return {"remembered": {"id": fid, "about": chat["name"], "fact": text[:300]}}
        if action == "forget":
            return {"forgotten_facts": store.forget_contact(contact["id"]), "about": chat["name"]}
        if action == "about":
            return {"about": chat["name"], "facts": [{"id": f["id"], "fact": f["text"], "source": f["source"]}
                                                     for f in store.facts(contact["id"])],
                    "unconfirmed": len(store.facts(contact["id"], status="pending"))}
        raise InboxError(f"Unknown action {action!r}.", kind="input")

    def updates(args: dict) -> Any:
        out = inbox.updates()
        for c in out["chats"]:
            for m in c["messages"]:
                m["time"] = _iso(m.pop("ts"))
        for a in out.get("alerts", []):
            a["time"] = _iso(a.pop("ts"))
        return out

    def schedule(args: dict) -> Any:
        inbox.client()  # setup check
        store, auto = inbox.store(), inbox.automation
        action = str(args.get("action") or "")
        if action == "list":
            return {"scheduled": [describe_schedule(s) for s in store.schedules()],
                    "recent": [describe_schedule(s) for s in store.schedules(
                        statuses=("held", "failed", "missed", "unknown"), limit=10)]}
        if action == "cancel":
            return auto.cancel(str(args.get("schedule_id") or ""))
        if action == "propose":
            draft = store.get_draft(str(args.get("draft_id") or ""))
            if not draft:
                raise InboxError("No such draft. Save one with inbox_draft_reply first.", kind="not_found")
            send_at = auto.resolve_when(str(args.get("when") or ""))
            problems = auto.check_schedule(draft, send_at)
            if problems:
                return {"proposed": False, "problems": problems}
            store.propose_send_at(draft["id"], send_at)
            return {"proposed": True, "draft_id": draft["id"], "to": draft["chat_name"], "network": draft["network"],
                    "send_at": when_text(send_at),
                    "scheduled": False,
                    "how_to_confirm": f"Only the user can schedule it: they type /inbox schedule {draft['id']}"
                                      " and confirm."}
        raise InboxError(f"Unknown action {action!r}.", kind="input")

    def watch(args: dict) -> Any:
        inbox.client()  # setup check
        store, auto = inbox.store(), inbox.automation
        action = str(args.get("action") or "")
        if action == "list":
            return {"watches": [auto.watch_view(w) for w in store.watches()]}
        if action == "remove":
            w = store.watch(int(args.get("watch_id") or 0))
            if not w or w["status"] != "active":
                raise InboxError(f"No active watch {args.get('watch_id')}.", kind="not_found")
            store.set_watch(w["id"], inbox.clock(), status="removed")
            return {"removed": w["id"]}
        if action != "add":
            raise InboxError(f"Unknown action {action!r}.", kind="input")
        kind = str(args.get("type") or "")
        chat = None
        if args.get("chat_id") or args.get("person"):
            chat = _one_chat(args)
            if "which_one" in chat:
                return chat
        until = auto.resolve_when(str(args["until"])) if args.get("until") else None
        return {"added": auto.add_watch(kind, chat=chat, text=str(args.get("text") or ""), until=until,
                                        network=str(args.get("network") or ""), nudge=bool(args.get("nudge")))}

    def triage(args: dict) -> Any:
        return inbox.triage(hours=int(args.get("hours") or 24), max_age=FRESH_SECONDS)

    def draft(args: dict) -> Any:
        if args.get("draft_id"):
            return inbox.edit_draft(str(args["draft_id"]), str(args.get("text") or ""))
        return inbox.create_draft(str(args.get("chat_id") or ""), str(args.get("text") or ""),
                                  reply_to=str(args.get("reply_to") or ""))

    def attachment(args: dict) -> Any:
        inbox.client()  # setup check
        action = str(args.get("action") or "open")
        mid, aid = str(args.get("message_id") or ""), str(args.get("attachment_id") or "")
        if action == "transcribe":
            view = inbox.transcribe(mid, aid)
            return {k: view[k] for k in ("message_id", "attachment_id", "kind", "transcript") if k in view}
        if action != "open":
            raise InboxError(f"Unknown action {action!r}.", kind="input")
        view = inbox.fetch_attachment(mid, aid)
        text = inbox.read_attachment_text(view)
        if text is not None:
            view["text"] = text
        elif view["type"] == "audio":
            if "transcript" not in view:
                view["hint"] = "Use action=transcribe to read what is said."
        else:
            view["hint"] = "Open the file at path with your own file or image tools."
        return view

    def mark_read(args: dict) -> Any:
        return inbox.mark_read(str(args.get("chat_id") or ""))

    def safety(args: dict) -> Any:
        action = str(args.get("action") or "list")
        if action == "check":
            text = str(args.get("text") or "")
            if not text.strip():
                raise InboxError("Give the text to check.", kind="input")
            v = scam_verdict([text])
            if not v:
                return {"looks_like_a_scam": False,
                        "note": "No warning signs found. That alone does not prove it is safe."}
            return {"looks_like_a_scam": True, **warning_view(v)}
        if action in ("safe", "unsafe"):
            return inbox.shield.trust(str(args.get("chat_id") or ""), safe=action == "safe")
        if action != "list":
            raise InboxError(f"Unknown action {action!r}.", kind="input")
        if not inbox.shield.enabled():
            return {"enabled": False, "note": "Scam warnings are switched off (scam_shield)."}
        items = inbox.shield.flagged()
        for x in items:
            x["time"] = _iso(x.pop("ts"))
        return {"flagged": items, "looked_at": "the latest messages of chats active in the last 7 days"}

    def calendar(args: dict) -> Any:
        action = str(args.get("action") or "list")
        planner = inbox.planner
        if action == "add":
            return _plan_out(planner.add(args.get("plan_id")))
        if action == "dismiss":
            return planner.dismiss(args.get("plan_id"))
        if action == "free":
            start, end = _local_ts(args.get("start"), "start"), _local_ts(args.get("end"), "end")
            if end <= start:
                raise InboxError("The end must be after the start.", kind="input")
            out = planner.availability(start, end)
            out["busy_with"] = [{"when": b["when"], "what": b["title"], "from": b["source"]} for b in out["busy_with"]]
            if out["free"] is None:
                out["note"] = ("Nothing known for that time. Without a connected calendar only plans added here"
                               " count, so ask the user.")
            return out
        if action != "list":
            raise InboxError(f"Unknown action {action!r}.", kind="input")
        days = max(1, min(int(args.get("days") or 14), 60))
        out = {"plans": [_plan_out(v) for v in planner.upcoming(days=days, chat_id=str(args.get("chat_id") or ""))],
               "calendar_connected": inbox.calendar.configured}
        if not planner.enabled():
            out["note"] = "Finding plans in chats is switched off (plan_detection)."
        if inbox.calendar.configured and inbox.calendar.problem():
            out["calendar_problem"] = inbox.calendar.problem()
        return out

    def translate(args: dict) -> Any:
        action = str(args.get("action") or "")
        tr = inbox.translator
        if action == "chat":
            return tr.chat(str(args.get("chat_id") or ""), limit=int(args.get("limit") or 20))
        if action == "draft":
            return tr.translate_draft(str(args.get("draft_id") or ""), str(args.get("to") or ""))
        if action == "text":
            chat = inbox.find_chat(str(args.get("chat_id") or ""))
            out = tr.reply(chat, str(args.get("text") or ""), str(args.get("to") or ""))
            return dict(out, saved=False, how_to_save="inbox_draft_reply with this text (or action=draft on a draft)")
        raise InboxError("Say action=chat, draft or text.", kind="input")

    def radar(args: dict) -> Any:
        inbox.client()  # setup check
        out = inbox.radar.report()
        rt = out["your_reply_time"]
        for k in ("this_week_seconds", "four_weeks_before_seconds"):
            rt.pop(k, None)
        return out

    def brief(args: dict) -> Any:
        inbox.client()  # setup check
        out = inbox.voice.brief()
        out.pop("at", None)
        if args.get("spoken"):
            try:
                audio = inbox.voice.speak(out["say"])
                out.update(media_tag=audio["media_tag"], audio_file=audio["path"],
                           how="Put media_tag on its own line in your reply to send the audio.")
            except InboxError as exc:
                out["audio_problem"] = str(exc)
        return out

    def list_drafts(args: dict) -> Any:
        status_ = str(args.get("status") or "pending")
        drafts = inbox.store().list_drafts(status_)
        return {"status": status_, "drafts": [
            {"draft_id": d["id"], "to": d["chat_name"], "network": d["network"], "text": d["text"],
             "created": _iso(d["created_at"]), **({"error": d["error"]} if d["error"] else {}),
             **({"by_hermes": True} if d["origin"] == "auto" else {}),
             **({"in_your_words": d["original_text"]} if d.get("original_text") else {}),
             **({"blanks_to_fill": blanks(d["text"])} if d["origin"] == "auto" and blanks(d["text"]) else {})}
            for d in drafts],
            "how_to_send": "The user types /inbox send <draft_id> and confirms."}

    return {
        "inbox_status": _wrap(status),
        "inbox_list_chats": _wrap(list_chats),
        "inbox_read_chat": _wrap(read_chat),
        "inbox_search": _wrap(search),
        "inbox_triage": _wrap(triage),
        "inbox_updates": _wrap(updates),
        "inbox_context": _wrap(context),
        "inbox_loops": _wrap(loops),
        "inbox_memory": _wrap(memory),
        "inbox_schedule": _wrap(schedule),
        "inbox_watch": _wrap(watch),
        "inbox_send": _wrap(lambda args: send_tool(inbox, args)),  # relative paths: from the chat's folder
        "inbox_react": _wrap(lambda args: react_tool(inbox, args)),
        "inbox_attachment": _wrap(attachment),
        "inbox_mark_read": _wrap(mark_read),
        "inbox_draft_reply": _wrap(draft),
        "inbox_list_drafts": _wrap(list_drafts),
        "inbox_safety": _wrap(safety),
        "inbox_calendar": _wrap(calendar),
        "inbox_translate": _wrap(translate),
        "inbox_radar": _wrap(radar),
        "inbox_brief": _wrap(brief),
    }
