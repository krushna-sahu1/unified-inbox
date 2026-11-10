"""``inbox_send`` and ``inbox_react``: Hermes sends a message (and files), or reacts to one, after
you approve it.

How the approval works (and why the model cannot skip it):
  1. Hermes calls ``inbox_send`` with who (a name, a phone number, a LinkedIn profile or an
     Instagram username), the text and optional files.
  2. Before the tool runs, this plugin's ``pre_tool_call`` hook works out exactly who and what
     (locally, no network), then returns ``{"action": "approve"}``. Hermes then shows you its
     own approval prompt (terminal, Desktop or your messaging app) with the person, the app,
     the text and the files. Nothing is sent unless you approve.
  3. Only after you approve does the tool run, and it checks the hook really asked (so a
     Hermes without the hook can never send unasked). Every safety limit still applies.
  If Hermes would approve without asking you (its "yolo" mode, approvals turned off, or an
  unattended run such as cron), the hook blocks the send instead.

If a name matches more than one chat, nothing is asked or sent: the tool returns the matches
and Hermes asks you which one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .client import InboxError
from .inbox import check_emoji, quote_view
from .pacing import NETWORK_LABELS

NETWORKS = ("whatsapp", "instagram", "linkedin")
MAX_FILES = 5
MAX_FILE_BYTES = 15 * 1024 * 1024        # WhatsApp media is limited to about 16 MB
MAX_TOTAL_BYTES = 20 * 1024 * 1024       # LinkedIn allows 20 MB per message
APPROVAL_TTL = 15 * 60                   # an approval request is valid this long
_PHONE = re.compile(r"^\+?[\d\s\-().]{7,22}$")
_LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#\s]+)", re.I)
_INSTAGRAM = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$", re.I)
_IG_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
TOOLS = ("inbox_send", "inbox_react")

_requests: dict[str, float] = {}          # args fingerprint -> when the hook asked
_targets: dict[str, str] = {}             # args fingerprint -> the chat or person the prompt named
_attempt_ids: dict[str, int] = {}         # args fingerprint -> its row in the activity log
_requests_lock = threading.Lock()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").casefold())).strip()


def _label(network: str) -> str:
    return NETWORK_LABELS.get(network, network)


def fingerprint(args: dict, tool: str = "inbox_send") -> str:
    """The exact request (which tool; who, what, which files, which app, which message) the
    approval was for."""
    keys = ("message_id", "emoji", "remove") if tool == "inbox_react" else (
        "to", "network", "text", "files", "chat_id", "reply_to")
    keep = {"tool": tool, **{k: args.get(k) for k in keys}}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


# ─── who ─────────────────────────────────────────────────────────────────────

def _accounts(inbox, network: str) -> list[dict]:
    return [a for a in inbox._known_accounts() if a["network"] == network]


def resolve(inbox, to: str, network: Optional[str] = None, chat_id: Optional[str] = None) -> dict:
    """Who ``to`` is. Returns one of:
      {"status": "chat", "chat": {...}}                       an existing conversation
      {"status": "new", "network", "account_id", "user_id", "name", "resolve_linkedin"?}
      {"status": "choose", "options": [...]}                   several matches: ask the user
      {"status": "not_found", "message": "..."}"""
    store = inbox.store()
    network = (network or "").lower().strip() or None
    if network and network not in NETWORKS:
        return {"status": "not_found", "message": f"Unknown app {network!r}: use whatsapp, instagram or linkedin."}
    if chat_id:
        chat = store.cached_chat(chat_id)
        return {"status": "chat", "chat": chat} if chat else {"status": "not_found", "message": "No such chat."}
    to = (to or "").strip()
    if not to:
        return {"status": "not_found", "message": "Say who to send to: a name, a phone number or a profile."}

    chat = store.cached_chat(to)  # a chat id
    if chat:
        return {"status": "chat", "chat": chat}

    m = _LINKEDIN.search(to)
    if m or (network == "linkedin" and to.startswith("in/")):
        public_id = m.group(1) if m else to[3:]
        return _new(inbox, "linkedin", public_id, f"linkedin.com/in/{public_id}", resolve_profile=True)

    if _PHONE.match(to) and network in (None, "whatsapp"):
        digits = re.sub(r"\D", "", to)
        if not 8 <= len(digits) <= 15:
            return {"status": "not_found", "message": f"{to!r} does not look like a full phone number."}
        if not to.startswith("+") and len(digits) <= 10:
            return {"status": "not_found", "message": f"Add the country code to {to!r}, e.g. +91 {to}."}
        for c in store.cached_chats(network="whatsapp", limit=5000):
            if c["id"].split("@")[0] == digits:
                return {"status": "chat", "chat": c}
        return _new(inbox, "whatsapp", f"{digits}@s.whatsapp.net", f"+{digits}")

    m = _INSTAGRAM.match(to)
    if m and network in (None, "instagram"):
        handle = m.group(1)
        for c in store.cached_chats(network="instagram", limit=5000):
            if _norm(c["name"]) == _norm(handle):
                return {"status": "chat", "chat": c}
        return _new(inbox, "instagram", handle, f"@{handle}", resolve_profile=True)

    handle = to[1:] if to.startswith("@") else to
    want = _norm(handle)
    chats = store.cached_chats(network=network, limit=5000)
    exact = [c for c in chats if _norm(c["name"]) == want]
    tokens = want.split()
    partial = [c for c in chats if c not in exact and tokens and
               all(any(w.startswith(t) for w in _norm(c["name"]).split()) for t in tokens)]
    found = exact + partial
    if not to.startswith("@"):
        found = [c for c in found if not c["is_group"]] or found  # people before groups
    if len(found) == 1:
        return {"status": "chat", "chat": found[0]}
    people = {(store.contact_for_chat(c["id"]) or {}).get("id") or c["id"] for c in found}
    if len(found) > 1 and len(people) == 1 and not network:
        # One person on several apps: where you talked last (the prompt names the app).
        return {"status": "chat", "chat": max(found, key=lambda c: c["ts"])}
    if len(found) > 1:
        return {"status": "choose", "options": [
            {"chat_id": c["id"], "name": c["name"], "network": c["network"], "app": _label(c["network"]),
             "group": c["is_group"], "last_message": (("You: " if c["last_from_me"] else "") + c["last_text"])[:80]}
            for c in found[:8]]}
    # A new Instagram chat needs an explicit @username (or a profile link, above): a plain name like
    # "Suraj" must never turn into some stranger's account called "suraj".
    if to.startswith("@") and _IG_HANDLE.match(handle) and network in (None, "instagram"):
        return _new(inbox, "instagram", handle, f"@{handle}", resolve_profile=True)
    hint = ("give their phone number with the country code (WhatsApp), their LinkedIn profile link or their"
            " Instagram @username")
    return {"status": "not_found", "message": f"No chat found for {to!r}. To message someone new, {hint}."}


def _new(inbox, network: str, user_id: str, name: str, **extra: Any) -> dict:
    accounts = _accounts(inbox, network)
    if not accounts:
        return {"status": "not_found", "message": f"No {_label(network)} account is connected."}
    if len(accounts) > 1:
        return {"status": "not_found", "message": f"You have {len(accounts)} {_label(network)} accounts connected;"
                                                  " open an existing chat with this person first."}
    return {"status": "new", "network": network, "account_id": accounts[0]["id"], "user_id": user_id,
            "name": name, **extra}


# ─── what ────────────────────────────────────────────────────────────────────

# Never attached, whoever asks: keys, passwords and login files (a message could trick the model
# into asking for one). Checked on the real file, after following links.
_SECRET_NAMES = {"auth.json", ".netrc", "_netrc", ".git-credentials", ".pgpass", "id_rsa", "id_dsa", "id_ecdsa",
                 "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"}
_SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store"}
_SECRET_SUFFIXES = (".pem", ".p12", ".pfx", ".kdbx")


def session_dir(inbox) -> Optional[str]:
    """The chat's folder, where relative file paths are read from (None outside a Hermes chat)."""
    get = getattr(inbox, "session_cwd", None)
    if get is None:
        return None
    try:
        return get() or None
    except Exception:  # noqa: BLE001 - no session here: paths are taken as given
        return None


def resolve_files(paths: list, base: Optional[str] = None, data_dir: Optional[Path] = None) -> list[Path]:
    """The real files to attach, checked: they exist, are not secrets, and stay within the size
    limits. ``data_dir`` (the plugin's own folder: your whole inbox) is never attached either."""
    paths = [p for p in (paths or []) if str(p).strip()]
    if len(paths) > MAX_FILES:
        raise InboxError(f"At most {MAX_FILES} files per message.", kind="input")
    out, total = [], 0
    for raw in paths:
        path = Path(os.path.expanduser(str(raw).strip().strip('"')))
        if not path.is_absolute() and base:
            path = Path(base) / path
        if not path.is_file():
            raise InboxError(f"File not found: {raw}", kind="input")
        path = path.resolve()
        if _is_secret(path, data_dir):
            raise InboxError(f"{path.name} looks like a key, password or login file (or your inbox data), so it is"
                             " never attached.", kind="input")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise InboxError(f"{path.name} is {size // 1048576} MB; the limit is {MAX_FILE_BYTES // 1048576} MB.",
                             kind="input")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise InboxError(f"The files add up to more than {MAX_TOTAL_BYTES // 1048576} MB.", kind="input")
        out.append(path)
    return out


def _is_secret(path: Path, data_dir: Optional[Path]) -> bool:
    name = path.name.casefold()
    if name in _SECRET_NAMES or name == ".env" or name.startswith(".env.") or name.endswith(_SECRET_SUFFIXES):
        return True
    if any(part.casefold() in _SECRET_DIRS for part in path.parts[:-1]):
        return True
    if data_dir is not None:
        root = data_dir.resolve()
        try:
            path.relative_to(root / "attachments")
            return False  # a file someone sent you, opened here: fine to forward
        except ValueError:
            pass
        try:
            path.relative_to(root)
            return True  # the database, exports, the activity log
        except ValueError:
            pass
    return False


_STORED = re.compile(r"^[0-9a-f]{12}-(.+)$")


def display_name(path: Path) -> str:
    """The file's own name: a file someone sent you is kept as "<12 hex>-<name>"; forward it as <name>."""
    if path.parent.name == "attachments":
        m = _STORED.match(path.name)
        if m:
            return m.group(1)
    return path.name


def load_files(paths: list, base: Optional[str] = None, data_dir: Optional[Path] = None) -> list[dict]:
    """Read files to attach: [{filename, content (base64), content_type, size}]."""
    out = []
    for path in resolve_files(paths, base, data_dir):
        data = path.read_bytes()
        name = display_name(path)
        out.append({"filename": name, "content": base64.b64encode(data).decode("ascii"),
                    "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                    "size": len(data)})
    return out


def _file_summary(paths: list) -> list[str]:
    """How files are named in the approval prompt and the log: name, size and folder."""
    names = []
    for raw in paths or []:
        path = Path(raw)
        try:
            names.append(f"{display_name(path)} ({max(1, path.stat().st_size // 1024)} KB, in {path.parent})")
        except OSError:
            names.append(display_name(path))
    return names


def _data_dir(inbox) -> Optional[Path]:
    try:
        row = inbox.store()._conn.execute("PRAGMA database_list").fetchone()
    except Exception:  # noqa: BLE001
        return None
    return Path(row[2]).parent if row and row[2] else None


def plan(inbox, args: dict, base_dir: Optional[str] = None) -> dict:
    """Everything about a send request, worked out locally (used by the approval hook and the
    tool, so the prompt names exactly the files that would be sent)."""
    if not inbox.configured:
        return {"status": "error", "message": "unified-inbox is not connected."}
    text = str(args.get("text") or "").strip()
    raw_files = [str(f) for f in (args.get("files") or []) if str(f).strip()]
    if not text and not raw_files:
        return {"status": "error", "message": "Nothing to send: give a text or a file."}
    if len(text) > 4000:
        return {"status": "error", "message": "Messages are limited to 4000 characters."}
    try:
        files = [str(f) for f in resolve_files(raw_files, base_dir or session_dir(inbox), _data_dir(inbox))]
    except InboxError as exc:
        return {"status": "error", "message": str(exc)}
    who = resolve(inbox, str(args.get("to") or ""), args.get("network"), args.get("chat_id"))
    if who["status"] in ("choose", "not_found"):
        return who
    quoted = None
    if who["status"] == "chat":
        chat = who["chat"]
        name, network = chat["name"] or "this chat", chat["network"]
        if args.get("reply_to"):
            try:
                quoted = inbox.quoted_message(chat["id"], str(args["reply_to"]))
            except InboxError as exc:
                return {"status": "error", "message": str(exc)}
    else:
        name, network = who["name"], who["network"]
        if args.get("reply_to"):
            return {"status": "error", "message": "A reply needs an existing chat: there is no message to reply to"
                                                  " in a new conversation."}
    if who["status"] == "chat":
        risk = inbox.shield.reply_risk(who["chat"]["id"], text)
        if risk:
            return {"status": "error", "message": risk}
    group = who["status"] == "chat" and who["chat"].get("is_group")
    summary = f"Send on {_label(network)} to {'the group ' if group else ''}{name}"
    if who["status"] == "new":
        summary += " (a new conversation)"
    if quoted:
        q = quote_view(quoted)
        whose = "your" if q["from"] == "me" else q["from"] + "'s"
        summary += f", replying to {whose} message \"{q['text'][:80]}\""
    summary += f': "{text}"' if text else ""
    names = _file_summary(files)
    if names:
        summary += f" with {len(names)} file(s): " + ", ".join(names)
    scam = inbox.shield.for_chat(who["chat"]) if who["status"] == "chat" else None
    if scam:
        summary = (f"CAREFUL: this chat looks like a scam ({scam['reasons'][0]}). {scam['advice']}\n" + summary)
    return {"status": "ok", "who": who, "text": text, "files": files, "name": name, "network": network,
            "summary": summary, "reply_to": quoted["id"] if quoted else ""}


def plan_reaction(inbox, args: dict) -> dict:
    """A reaction request, worked out locally (for the approval hook and the tool)."""
    if not inbox.configured:
        return {"status": "error", "message": "unified-inbox is not connected."}
    try:
        emoji = check_emoji(str(args.get("emoji") or ""))
    except InboxError as exc:
        return {"status": "error", "message": str(exc)}
    store = inbox.store()
    msg = store.get_message(str(args.get("message_id") or "").strip())
    if not msg:
        return {"status": "error", "message": "No such message in the local copy. Read the chat first."}
    chat = store.cached_chat(msg["chat_id"])
    if not chat:
        return {"status": "error", "message": "The chat of that message is no longer in the local copy."}
    q = quote_view(msg)
    whose = "your" if q["from"] == "me" else q["from"] + "'s"
    verb = f"Remove your {emoji} reaction from" if args.get("remove") else f"React {emoji} to"
    summary = f'{verb} {whose} message on {_label(chat["network"])} in {chat["name"] or "this chat"}: "{q["text"][:120]}"'
    return {"status": "ok", "summary": summary, "network": chat["network"], "name": chat["name"],
            "chat_id": chat["id"], "emoji": emoji}


# ─── the approval ────────────────────────────────────────────────────────────

def approval_bypassed() -> str:
    """Why Hermes would approve without asking you right now ('' when it will ask).

    Hermes's public ``is_approval_bypass_active`` (yolo, approvals off) is checked first, so a
    future Hermes that renames the finer-grained checks below still refuses in those modes.
    Those finer checks add unattended turns and runs with no one present (cron, single query)."""
    try:
        from tools import approval as ap  # Hermes internals: present inside Hermes only
    except Exception:  # noqa: BLE001 - outside Hermes (tests): nothing to bypass
        return ""
    try:
        if ap.is_approval_bypass_active():
            return "Hermes is in yolo mode or approvals are turned off"
    except Exception:  # noqa: BLE001 - an older or newer Hermes: the checks below decide
        pass
    try:
        if ap._yolo_active():
            return "Hermes is in yolo mode (approvals skipped)"
        if ap.approval_context._get_approval_mode() == "off":
            return "Hermes approvals are turned off"
        _cb, is_cli, is_gateway, is_ask = ap._presence(None)
        if not (is_cli or is_gateway or is_ask):
            return "no one is here to approve it (an unattended run)"
    except Exception:  # noqa: BLE001 - a newer Hermes: let its gate decide
        return ""
    return ""


def pre_tool_call_hook(inbox):
    """The ``pre_tool_call`` hook: ask a human before every ``inbox_send``."""

    def hook(tool_name: str = "", args: Optional[dict] = None, **_kw: Any) -> Optional[dict]:
        if tool_name not in TOOLS:
            return None
        args = args or {}
        try:
            p = plan_reaction(inbox, args) if tool_name == "inbox_react" else plan(inbox, args)
        except Exception:  # noqa: BLE001 - the tool reports the problem itself; nothing is sent
            return None
        if p["status"] != "ok":
            return None  # the tool answers with the choices or the problem; nothing to approve
        bypass = approval_bypassed()
        if bypass:
            return {"action": "block", "message": f"Not sent: {bypass}, so I can't ask you first. Save it as a draft"
                                                  " (inbox_draft_reply) and send it with /inbox send."}
        key = fingerprint(args, tool_name)
        now = time.time()
        with _requests_lock:
            for k in [k for k, t in _requests.items() if now - t > APPROVAL_TTL]:
                _requests.pop(k, None)
                _attempt_ids.pop(k, None)  # asked, then declined or never answered
                _targets.pop(k, None)
            _requests[key] = now
            _targets[key] = _approved_target(p, tool_name)
        try:
            if tool_name == "inbox_react":
                what = ("removed " if args.get("remove") else "") + f"{p['emoji']} reaction"
                aid = inbox.store().log_attempt(via="hermes", network=p["network"], to_name=p["name"],
                                                target=p["chat_id"], text=what, files=[], status="asked",
                                                now=inbox.clock(), detail="waiting for your approval")
            else:
                aid = inbox.store().log_attempt(via="hermes", network=p["network"], to_name=p["name"],
                                                target=_target(p), text=p["text"], files=_file_summary(p["files"]),
                                                status="asked", now=inbox.clock(),
                                                detail="waiting for your approval")
            with _requests_lock:
                _attempt_ids[key] = aid
        except Exception:  # noqa: BLE001 - logging must never stop the approval
            pass
        # a fresh rule key every time: "always allow" can never approve a different message
        return {"action": "approve", "message": p["summary"], "rule_key": f"{tool_name}:{key[:16]}:{int(now)}"}

    return hook



def _target(p: dict) -> str:
    return p["who"]["chat"]["id"] if p["who"]["status"] == "chat" else p["who"]["user_id"]


def _approved_target(p: dict, tool: str = "inbox_send") -> str:
    """Exactly where an approved request goes: the app and the chat (or the new person)."""
    if tool == "inbox_react":
        return f"{p['network']}|{p['chat_id']}"
    return f"{p['network']}|{p['who']['status']}|{_target(p)}"


_CHANGED = -1  # _was_approved: you approved it, but for a different chat than it would go to now


def _was_approved(args: dict, tool: str = "inbox_send", target: Optional[str] = None) -> Optional[int]:
    """The hook asked for exactly this request, recently (Hermes only runs the tool after you
    approve). Returns the activity-log id, None if it never asked, or ``_CHANGED`` when the name
    now points at a different chat than the one the prompt showed (``target``: where it would go
    now). An approval is used once."""
    key = fingerprint(args, tool)
    with _requests_lock:
        asked = _requests.pop(key, None)
        attempt = _attempt_ids.pop(key, 0)
        shown = _targets.pop(key, None)
    if asked is None or time.time() - asked > APPROVAL_TTL:
        return None
    if target is not None and shown is not None and shown != target:
        return _CHANGED
    return attempt


# ─── the tool ────────────────────────────────────────────────────────────────

def _profile_resolver(inbox, account_id: str, network: str, public_id: str):
    """A LinkedIn profile name or an Instagram username -> the user id Unipile messages (one paced
    read, at send time)."""
    def resolve_id() -> str:
        try:
            profile = inbox._call(account_id, lambda: inbox.client().get_user(account_id, public_id))
        except InboxError as exc:
            if exc.status == 404:
                where = "@" + public_id if network == "instagram" else "linkedin.com/in/" + public_id
                raise InboxError(f"No {_label(network)} account {where} was found.", status=404,
                                 kind="not_found") from None
            raise
        return profile["id"] or public_id
    return resolve_id


def send_tool(inbox, args: dict, base_dir: Optional[str] = None) -> dict:
    p = plan(inbox, args, base_dir)
    if p["status"] == "choose":
        return {"sent": False, "which_one": p["options"],
                "ask_user": "Several chats match. Ask the user which one, then call inbox_send again with its chat_id."}
    if p["status"] != "ok":
        raise InboxError(p["message"], kind="not_found" if p["status"] == "not_found" else "input")
    attempt = _was_approved(args, target=_approved_target(p))
    if attempt is None:
        return {"sent": False, "error": "Not sent: Hermes did not ask you to approve this, so it was not sent."
                                        " Update Hermes, or save a draft and send it with /inbox send."}
    if attempt == _CHANGED:
        # e.g. the same person wrote on another app while the prompt was open: never send
        # somewhere other than where the approval said
        return {"sent": False, "error": f"Not sent: while you were deciding, {p['name']!r} came to mean a different"
                                        " chat than the one you approved. Call inbox_send again so the user sees"
                                        " and approves where it goes now."}
    attempt = attempt or None
    files = load_files(p["files"], data_dir=_data_dir(inbox))  # already resolved and checked by plan
    attachments = [{k: f[k] for k in ("filename", "content", "content_type")} for f in files]
    who = p["who"]
    if who["status"] == "chat":
        chat = who["chat"]
        draft = inbox.store().add_draft(chat_id=chat["id"], account_id=chat["account_id"], network=chat["network"],
                                        chat_name=chat["name"], text=p["text"], now=inbox.clock(),
                                        reply_to=p["reply_to"])
        out = inbox.send(draft, attachments=attachments, via="hermes", attempt_id=attempt)
        if not out["sent"]:
            out["draft_id"] = draft
    else:
        resolver = (_profile_resolver(inbox, who["account_id"], who["network"], who["user_id"])
                    if who.get("resolve_profile") else None)
        out = inbox.start_conversation(account_id=who["account_id"], network=who["network"], user_id=who["user_id"],
                                       name=who["name"], text=p["text"], attachments=attachments, via="hermes",
                                       attempt_id=attempt, resolve=resolver)
    out["files"] = [f["filename"] for f in files]
    if out.get("sent"):
        inbox.automation.notify("send", f"Sent to {p['name']} on {_label(p['network'])}",
                                (p["text"] or "") + (f" (+{len(files)} file(s))" if files else ""),
                                chat_id=out.get("chat_id") or (who["chat"]["id"] if who["status"] == "chat" else ""))
    return out


def react_tool(inbox, args: dict) -> dict:
    """``inbox_react``: runs only after the user approved this exact reaction (see the hook)."""
    p = plan_reaction(inbox, args)
    if p["status"] != "ok":
        raise InboxError(p["message"], kind="input")
    attempt = _was_approved(args, "inbox_react", target=_approved_target(p, "inbox_react"))
    if attempt is None or attempt == _CHANGED:
        return {"reacted": False, "error": "Not done: Hermes did not ask you to approve this reaction."}
    return inbox.react(str(args.get("message_id") or "").strip(), p["emoji"], remove=bool(args.get("remove")),
                       via="hermes", attempt_id=attempt or None)
