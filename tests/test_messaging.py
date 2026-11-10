"""Messaging: files people send you, voice notes as text, replies that quote a message, reactions,
mark as read (opt-in), groups (mentions and members) and new Instagram chats by username."""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from conftest import KEY, make_inbox
from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend
from unified_inbox import data_control, sending
from unified_inbox.api import FileResult, InboxAPI
from unified_inbox.commands import build_command
from unified_inbox.inbox import REACTIONS_PER_HOUR
from unified_inbox.sync import SyncEngine
from unified_inbox.tools import build_handlers

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
VOICE = {"id": "att-voice", "type": "audio", "voice_note": True, "mimetype": "audio/ogg", "duration": 7}
PHOTO = {"id": "att-photo", "type": "img", "mimetype": "image/jpeg"}
PDF = {"id": "att-pdf", "type": "file", "mimetype": "application/pdf", "filename": "../../invoice March.pdf"}
NOTE = {"id": "att-note", "type": "file", "mimetype": "text/plain", "filename": "notes.txt"}


class Transcriber:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"success": True, "transcript": "Rent is due on Friday"}
        self.error = error
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def inbox(clock, tmp_path):
    return _inbox(clock, tmp_path)


def _inbox(clock, folder, config=None, transcriber="default"):
    backend = SampleBackend(clock)
    conn = sqlite3.connect(str(folder / "data.db"), check_same_thread=False)
    conn.execute("PRAGMA synchronous=OFF")  # a test database needs no crash safety; keeps the suite fast
    i = make_inbox(clock, {"unipile_account_id": SAMPLE_ACCOUNT_IDS, **(config or {})}, secrets=KEY,
                   backend=backend, conn=conn)
    i.backend = backend
    i.transcriber = Transcriber() if transcriber == "default" else transcriber
    i._local_now = lambda: datetime.fromtimestamp(clock())
    _sync(i)
    clock.advance(1)
    return i


def _sync(inbox):
    while SyncEngine(inbox).sync_once()["pending_chats"]:
        pass


def _receive(inbox, chat_id, text, **kw):
    msg = inbox.backend.receive(chat_id, text, **kw)
    _sync(inbox)
    return msg


def _tool(inbox, name, args):
    return json.loads(build_handlers(inbox)[name](args))


def _approved(inbox, tool, args):
    """What Hermes does: the pre_tool_call hook asks the user (who approves), then the tool runs."""
    directive = sending.pre_tool_call_hook(inbox)(tool_name=tool, args=args)
    return directive, _tool(inbox, tool, args)


# ─── 1. files people send you ────────────────────────────────────────────────

def test_a_file_is_downloaded_once_into_the_plugin_folder(inbox, tmp_path):
    inbox.backend.files["att-pdf"] = b"%PDF-1.4 march"
    msg = _receive(inbox, "wa-maya", "", attachments=[PDF])
    out = _tool(inbox, "inbox_attachment", {"action": "open", "message_id": msg["id"]})
    path = Path(out["path"])
    assert path.parent == tmp_path / "attachments" and path.read_bytes() == b"%PDF-1.4 march"
    assert path.name.endswith("-invoice March.pdf")
    assert out["filename"].endswith("invoice March.pdf") and ".." not in out["filename"]  # stays in the folder
    assert out["mimetype"] == "application/pdf" and out["size"] == 14 and "hint" in out
    calls = inbox.backend.calls.count("download")
    again = _tool(inbox, "inbox_attachment", {"action": "open", "message_id": msg["id"]})
    assert again["path"] == out["path"] and inbox.backend.calls.count("download") == calls  # reused


def test_reading_a_chat_shows_files_and_their_ids(inbox):
    inbox.backend.files["att-photo"] = PNG
    msg = _receive(inbox, "wa-maya", "", attachments=[PHOTO, VOICE])
    read = _tool(inbox, "inbox_read_chat", {"chat_id": "wa-maya"})
    m = next(x for x in read["messages"] if x["message_id"] == msg["id"])
    assert [f["attachment_id"] for f in m["files"]] == ["att-photo", "att-voice"]
    assert [f["kind"] for f in m["files"]] == ["[photo]", "[voice note]"] and not m["files"][0]["downloaded"]
    with pytest.raises(Exception):  # several files: say which
        inbox.fetch_attachment(msg["id"])
    inbox.fetch_attachment(msg["id"], "att-photo")
    m = next(x for x in _tool(inbox, "inbox_read_chat", {"chat_id": "wa-maya"})["messages"]
             if x["message_id"] == msg["id"])
    assert m["files"][0]["downloaded"] is True and m["files"][1]["downloaded"] is False


def test_small_text_files_are_returned_as_text(inbox):
    inbox.backend.files["att-note"] = "milk, eggs, बिस्कुट".encode("utf-8")
    msg = _receive(inbox, "wa-mum", "", attachments=[NOTE])
    out = _tool(inbox, "inbox_attachment", {"action": "open", "message_id": msg["id"]})
    assert out["text"] == "milk, eggs, बिस्कुट"


@pytest.mark.parametrize("problem, setup, expected", [
    ("unknown message", lambda i: "nope", "No message"),
    ("no file", lambda i: _receive(i, "wa-maya", "just text")["id"], "no file"),
    ("expired", lambda i: _receive(i, "wa-maya", "", attachments=[dict(PHOTO, is_unavailable=True)])["id"],
     "no longer available"),
    ("too big", lambda i: _receive(i, "wa-maya", "", attachments=[dict(PHOTO, file_size=90 * 1024 * 1024)])["id"],
     "larger than"),
    ("gone at Unipile", lambda i: _receive(i, "wa-maya", "", attachments=[dict(PHOTO, id="att-gone")])["id"], "404"),
])
def test_file_problems_are_explained_and_nothing_is_kept(inbox, tmp_path, problem, setup, expected):
    mid = setup(inbox)
    calls = inbox.backend.calls.count("download")
    out = _tool(inbox, "inbox_attachment", {"action": "open", "message_id": mid})
    assert expected in out["error"], problem
    if problem == "too big":
        assert inbox.backend.calls.count("download") == calls  # refused before downloading
    assert not (tmp_path / "attachments").exists() or not any((tmp_path / "attachments").iterdir())


def test_a_file_that_turns_out_too_large_is_not_saved(inbox, tmp_path):
    inbox.backend.files["att-photo"] = PNG
    msg = _receive(inbox, "wa-maya", "", attachments=[PHOTO])
    real = inbox.backend.download_attachment
    inbox.backend.download_attachment = lambda *a, **k: real(*a[:4], max_bytes=10)
    assert "larger" in _tool(inbox, "inbox_attachment", {"action": "open", "message_id": msg["id"]})["error"]
    assert inbox.store().attachment_file(msg["id"], "att-photo") is None


def test_files_need_a_database_folder(clock):
    from conftest import make_sample_inbox

    i = make_sample_inbox(clock)  # in-memory database
    _sync(i)
    i.backend.files["att-photo"] = PNG
    msg = _receive(i, "wa-maya", "", attachments=[PHOTO])
    assert "no folder" in _tool(i, "inbox_attachment", {"action": "open", "message_id": msg["id"]})["error"]


def test_files_go_when_their_message_goes_and_after_30_days(inbox, tmp_path, clock):
    from unified_inbox.pacing import DAY

    inbox.backend.files.update({"att-photo": PNG, "att-voice": b"OggS voice"})
    gone = _receive(inbox, "wa-maya", "", attachments=[PHOTO])
    kept = _receive(inbox, "wa-mum", "", attachments=[VOICE])
    p1 = inbox.fetch_attachment(gone["id"])["path"]
    p2 = inbox.transcribe(kept["id"])["path"]
    inbox.store().delete_message(gone["id"])  # deleted for everyone (a webhook delivers it)
    assert not Path(p1).exists()
    assert inbox.store().attachment_file(gone["id"], "att-photo") is None
    clock.advance(31 * DAY)
    inbox.store().prune(clock(), retention_days=180)
    saved = inbox.store().attachment_file(kept["id"], "att-voice")
    assert saved["path"] == "" and saved["transcript"] == "Rent is due on Friday"  # the words stay
    assert not any((tmp_path / "attachments").iterdir())
    assert inbox.fetch_attachment(kept["id"])["path"] == p2  # fetched again on request


def test_export_lists_files_and_delete_everything_removes_them(inbox, tmp_path):
    inbox.backend.files["att-voice"] = b"OggS voice"
    msg = _receive(inbox, "wa-mum", "", attachments=[VOICE])
    inbox.transcribe(msg["id"])
    rows = data_control.export_all(inbox)["tables"]["attachment_files"]
    assert rows[0]["transcript"] == "Rent is due on Friday" and "content" not in rows[0]
    out = data_control.wipe(inbox, everywhere=False)
    assert out["deleted"]["downloaded_files"] == 1 and not (tmp_path / "attachments").exists()
    assert inbox.store()._q("SELECT COUNT(*) FROM attachment_files")[0][0] == 0


def test_a_file_someone_sent_can_be_forwarded_under_its_own_name(inbox):
    inbox.backend.files["att-pdf"] = b"%PDF-1.4 march"
    msg = _receive(inbox, "wa-maya", "", attachments=[PDF])
    path = inbox.fetch_attachment(msg["id"])["path"]
    inbox.backend.calls_out = []
    real = inbox.backend.send_message
    sent = []
    inbox.backend.send_message = lambda a, c, t, attachments=None, quote_id="": sent.append(attachments) or real(a, c, t)
    directive, out = _approved(inbox, "inbox_send", {"to": "Dev", "text": "Maya's invoice", "files": [path]})
    assert "invoice March.pdf (1 KB" in directive["message"] and out["sent"]
    assert sent[0][0]["filename"] == "invoice March.pdf"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_downloaded_files_are_readable_by_you_alone(inbox, tmp_path):
    import stat

    inbox.backend.files["att-photo"] = PNG
    msg = _receive(inbox, "wa-maya", "", attachments=[PHOTO])
    path = inbox.fetch_attachment(msg["id"])["path"]
    assert stat.S_IMODE((tmp_path / "attachments").stat().st_mode) == 0o700
    assert stat.S_IMODE(__import__("os").stat(path).st_mode) == 0o600


# ─── 2. voice notes as text ──────────────────────────────────────────────────

def test_a_voice_note_becomes_text_once_and_shows_everywhere(inbox):
    inbox.backend.files["att-voice"] = b"OggS voice"
    msg = _receive(inbox, "wa-maya", "", attachments=[VOICE])
    out = _tool(inbox, "inbox_attachment", {"action": "transcribe", "message_id": msg["id"]})
    assert out["transcript"] == "Rent is due on Friday" and inbox.transcriber.paths[0].endswith(".ogg")
    _tool(inbox, "inbox_attachment", {"action": "transcribe", "message_id": msg["id"]})
    assert len(inbox.transcriber.paths) == 1  # done once
    m = next(x for x in _tool(inbox, "inbox_read_chat", {"chat_id": "wa-maya"})["messages"]
             if x["message_id"] == msg["id"])
    assert m["files"][0]["transcript"] == "Rent is due on Friday"
    hits = _tool(inbox, "inbox_search", {"query": "rent is due"})["results"]
    assert hits and hits[0]["text"] == "[voice note] Rent is due on Friday"
    opened = _tool(inbox, "inbox_attachment", {"action": "open", "message_id": msg["id"]})
    assert opened["transcript"] == "Rent is due on Friday" and "hint" not in opened


@pytest.mark.parametrize("transcriber, expected", [
    (None, "Speech-to-text is not available"),
    (Transcriber({"success": False, "error": "faster-whisper is not installed"}), "faster-whisper is not installed"),
    (Transcriber(error=RuntimeError("model crashed")), "model crashed"),
])
def test_voice_note_problems_are_explained(clock, tmp_path, transcriber, expected):
    inbox = _inbox(clock, tmp_path, transcriber=transcriber)
    inbox.backend.files["att-voice"] = b"OggS voice"
    msg = _receive(inbox, "wa-maya", "", attachments=[VOICE])
    out = _tool(inbox, "inbox_attachment", {"action": "transcribe", "message_id": msg["id"]})
    assert expected in out["error"] and out["kind"] == "transcription"
    assert inbox.store().attachment_file(msg["id"], "att-voice") is None or \
        inbox.store().attachment_file(msg["id"], "att-voice")["transcript"] == ""


def test_silence_and_non_audio(inbox):
    inbox.transcriber = Transcriber({"success": True, "transcript": "   "})
    inbox.backend.files.update({"att-voice": b"OggS", "att-photo": PNG})
    msg = _receive(inbox, "wa-maya", "", attachments=[VOICE])
    assert inbox.transcribe(msg["id"])["transcript"] == "(no speech recognised)"
    photo = _receive(inbox, "wa-maya", "", attachments=[PHOTO])
    assert "Only voice notes" in _tool(inbox, "inbox_attachment", {"action": "transcribe",
                                                                  "message_id": photo["id"]})["error"]


# ─── 3. replies that quote a message ─────────────────────────────────────────

def test_a_reply_quotes_the_message_and_the_prompt_says_so(inbox):
    their = inbox.store().cached_messages("wa-maya", 10)[-2]
    args = {"to": "Maya", "text": "Yes, I'll be home", "reply_to": their["id"]}
    directive, out = _approved(inbox, "inbox_send", args)
    assert directive["message"].startswith("Send on WhatsApp to Maya Rivers, replying to Maya Rivers's message")
    assert out["sent"] and inbox.backend.quotes == [("wa-maya", their["id"])]


def test_an_approval_without_the_reply_does_not_cover_one_with_it(inbox):
    their = inbox.store().cached_messages("wa-maya", 10)[-1]
    sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args={"to": "Maya", "text": "On my way"})
    out = _tool(inbox, "inbox_send", {"to": "Maya", "text": "On my way", "reply_to": their["id"]})
    assert out["sent"] is False and inbox.backend.quotes == []


def test_a_reply_must_be_to_a_message_of_that_chat(inbox):
    other = inbox.store().cached_messages("wa-mum", 1)[0]
    for args in ({"to": "Maya", "text": "hi", "reply_to": other["id"]},
                 {"to": "Maya", "text": "hi", "reply_to": "no-such-message"},
                 {"to": "+447700900123", "text": "hi", "reply_to": other["id"]}):
        directive, out = _approved(inbox, "inbox_send", args)
        assert directive is None and out.get("sent") is not True and "error" in out, args
    assert inbox.backend.sent == [] or all(not m["from_me"] for m in inbox.backend.sent)


def test_a_draft_reply_is_sent_quoted_after_confirming(inbox):
    their = inbox.store().cached_messages("wa-maya", 10)[-1]
    d = _tool(inbox, "inbox_draft_reply", {"chat_id": "wa-maya", "text": "Yes, home at 8!", "reply_to": their["id"]})
    assert d["replying_to"]["message_id"] == their["id"]
    asked = []
    out = build_command(inbox, lambda q, c: asked.append(q) or "send")(f"send {d['draft_id']}")
    assert out.startswith("Sent") and "Replying to: “??”" in asked[0]
    assert inbox.backend.quotes == [("wa-maya", their["id"])]


def test_a_reply_to_a_message_that_was_deleted_is_not_sent(inbox, clock):
    msg = _receive(inbox, "wa-maya", "Can you send the keys?")
    d = inbox.create_draft("wa-maya", "Sure, sending them now", reply_to=msg["id"])
    inbox.backend.delete(msg["id"])  # deleted for everyone on the phone,
    inbox.store().delete_message(msg["id"])  # and a webhook delivers that here
    clock.advance(1)
    out = build_command(inbox, lambda q, c: "send")(f"send {d['draft_id']}")
    assert "replies to is gone" in out and inbox.store().get_draft(d["draft_id"])["status"] == "pending"


def test_the_page_drafts_and_sends_a_reply(inbox):
    api = InboxAPI(inbox)
    their = inbox.store().cached_messages("li-priya", 10)[-1]
    status, d = api.handle("POST", "/chats/li-priya/drafts", body={"text": "Thursday 3pm works", "reply_to": their["id"]})
    assert status == 200 and d["replying_to"]["message_id"] == their["id"]
    row = next(x for x in api.handle("GET", "/drafts")[1]["drafts"] if x["id"] == d["draft_id"])
    assert row["replying_to"]["text"] == their["text"][:160]
    status, out = api.handle("POST", f"/drafts/{d['draft_id']}/send", body={"confirm": True, "text": "Thursday 3pm works"})
    assert status == 200 and out["sent"] and inbox.backend.quotes == [("li-priya", their["id"])]


# ─── 4. reactions ────────────────────────────────────────────────────────────

def test_a_reaction_is_asked_first_then_sent_and_logged(inbox):
    their = inbox.store().cached_messages("wa-maya", 10)[-1]
    directive, out = _approved(inbox, "inbox_react", {"message_id": their["id"], "emoji": "👍"})
    assert directive["action"] == "approve" and directive["message"].startswith("React 👍 to Maya Rivers's message")
    assert out["reacted"] and inbox.backend.reactions == [("wa-maya", their["id"], "👍", False)]
    assert inbox.store().attempts()[0]["text"] == "👍 reaction" and inbox.store().attempts()[0]["status"] == "sent"
    directive, out = _approved(inbox, "inbox_react", {"message_id": their["id"], "emoji": "👍", "remove": True})
    assert directive["message"].startswith("Remove your 👍 reaction") and out["removed"]


def test_no_reaction_without_the_prompt_or_with_a_bad_emoji(inbox):
    their = inbox.store().cached_messages("wa-maya", 10)[-1]
    assert _tool(inbox, "inbox_react", {"message_id": their["id"], "emoji": "👍"})["reacted"] is False
    for bad in ("", "ok", "👍 thanks", "<b>"):
        directive, out = _approved(inbox, "inbox_react", {"message_id": their["id"], "emoji": bad})
        assert directive is None and "one emoji" in out["error"], bad
    assert inbox.backend.reactions == []


def test_reactions_have_their_own_limits_and_quiet_hours(clock, tmp_path):
    inbox = _inbox(clock, tmp_path, {"quiet_hours": ""})
    msgs = inbox.store().cached_messages("wa-maya", 10)
    for i in range(REACTIONS_PER_HOUR):
        assert inbox.react(msgs[i % len(msgs)]["id"], "❤️")["reacted"]
    out = inbox.react(msgs[0]["id"], "😂")
    assert out["reacted"] is False and "Reaction limit" in out["reasons"][0]
    assert inbox.react(msgs[0]["id"], "❤️", remove=True)["reacted"]  # taking one back is always fine
    quiet = _inbox(clock, tmp_path / "q" if (tmp_path / "q").mkdir() is None else tmp_path,
                   {"quiet_hours": "00:00-23:59"})
    out = quiet.react(quiet.store().cached_messages("wa-maya", 1)[0]["id"], "👍")
    assert out["reacted"] is False and "Quiet hours" in out["reasons"][0]


def test_an_emoji_the_app_refuses_is_reported(inbox):
    inbox.backend.refused_emojis.add("🦄")
    their = inbox.store().cached_messages("ig-lena", 1)[0]
    _, out = _approved(inbox, "inbox_react", {"message_id": their["id"], "emoji": "🦄"})
    assert out["reacted"] is False and "not supported" in out["reasons"][0]
    assert inbox.store().attempts()[0]["status"] == "failed"


def test_yolo_refuses_reactions_too(inbox, monkeypatch):
    monkeypatch.setattr(sending, "approval_bypassed", lambda: "Hermes is in yolo mode or approvals are turned off")
    their = inbox.store().cached_messages("wa-maya", 1)[0]
    directive = sending.pre_tool_call_hook(inbox)(tool_name="inbox_react",
                                                  args={"message_id": their["id"], "emoji": "👍"})
    assert directive["action"] == "block"


def test_the_page_reacts_only_when_confirmed(inbox):
    api = InboxAPI(inbox)
    their = inbox.store().cached_messages("wa-maya", 1)[0]
    assert api.handle("POST", f"/messages/{their['id']}/react", body={"emoji": "👍"})[0] == 409
    status, out = api.handle("POST", f"/messages/{their['id']}/react", body={"emoji": "👍", "confirm": True})
    assert status == 200 and out["reacted"] and inbox.store().attempts()[0]["via"] == "page"


# ─── 5. mark as read (opt-in) ────────────────────────────────────────────────

def test_nothing_is_marked_read_unless_asked(inbox):
    d = inbox.create_draft("wa-maya", "Yes, home at 8!")
    assert inbox.send(d["draft_id"])["sent"] and "read" not in inbox.backend.calls


def test_after_a_reply_when_turned_on(clock, tmp_path):
    inbox = _inbox(clock, tmp_path, {"mark_read_after_reply": True})
    d = inbox.create_draft("wa-maya", "Yes, home at 8!")
    out = inbox.send(d["draft_id"])
    assert out["sent"] and out["marked_read"] and "wa-maya" in inbox.backend.read_chats
    assert inbox.store().cached_chat("wa-maya")["unread"] == 0


def test_a_failed_read_receipt_does_not_undo_the_send(clock, tmp_path):
    from unified_inbox.client import InboxError

    inbox = _inbox(clock, tmp_path, {"mark_read_after_reply": True})

    def broken(*a):
        raise InboxError("Unipile 500: boom", status=500)

    inbox.backend.mark_chat_read = broken
    d = inbox.create_draft("wa-maya", "Yes, home at 8!")
    out = inbox.send(d["draft_id"])
    assert out["sent"] is True and out["marked_read"] is False and "boom" in out["mark_read_problem"]


def test_mark_read_from_the_tool_and_the_page(inbox):
    assert inbox.store().cached_chat("li-priya")["unread"] > 0
    out = _tool(inbox, "inbox_mark_read", {"chat_id": "li-priya"})
    assert out["marked_read"] and inbox.store().cached_chat("li-priya")["unread"] == 0
    status, out = InboxAPI(inbox).handle("POST", "/chats/wa-mum/read")
    assert status == 200 and "wa-mum" in inbox.backend.read_chats
    assert InboxAPI(inbox).handle("POST", "/chats/nope/read")[0] == 404


# ─── 6. groups ───────────────────────────────────────────────────────────────

def _group(inbox):
    inbox.backend.make_group("wa-bookclub")
    _receive(inbox, "wa-bookclub", "Who's bringing snacks?", sender="Sam Okafor", sender_id="111@s.whatsapp.net")
    _receive(inbox, "wa-bookclub", "Not me this time", sender="Sam", sender_id="222@s.whatsapp.net")
    inbox.store()._x("UPDATE chats SET name='Book club', is_group=1 WHERE id='wa-bookclub'")
    return inbox.store().cached_chat("wa-bookclub")


def test_a_group_shows_who_writes_and_the_prompt_says_group(inbox):
    _group(inbox)
    read = _tool(inbox, "inbox_read_chat", {"chat_id": "wa-bookclub"})
    assert read["group"] and read["members_seen"][:2] == ["Sam", "Sam Okafor"]
    directive, out = _approved(inbox, "inbox_send", {"to": "Book club", "text": "@Sam Okafor I'll bring chai, @Sam ok?"})
    assert directive["message"].startswith("Send on WhatsApp to the group Book club:")
    assert "@Sam Okafor I'll bring chai" in directive["message"]  # you approve what they will see
    sent = [m for m in inbox.backend.sent if m["from_me"]][-1]["text"]
    assert sent == "@111@s.whatsapp.net I'll bring chai, @222@s.whatsapp.net ok?"


def test_unknown_mentions_and_emails_stay_as_written(inbox):
    chat = _group(inbox)
    assert inbox.with_mentions(chat, "@Priya and mail sam@example.com") == "@Priya and mail sam@example.com"
    assert inbox.with_mentions(inbox.store().cached_chat("wa-maya"), "@Sam hi") == "@Sam hi"  # not a group


def test_a_mention_of_you_in_a_group_needs_a_reply(inbox):
    _group(inbox)
    api = InboxAPI(inbox)
    row = next(c for c in api.handle("GET", "/chats")[1]["chats"] if c["id"] == "wa-bookclub")
    assert row["needs_reply"] is False and row["mentions_you"] is False  # chatter is not for you
    _receive(inbox, "wa-bookclub", "@you can you host next week?", sender="Sam", sender_id="222@s.whatsapp.net",
             mentioned=True)
    row = next(c for c in api.handle("GET", "/chats")[1]["chats"] if c["id"] == "wa-bookclub")
    assert row["needs_reply"] and row["mentions_you"]
    tri = inbox.triage(hours=24)
    entry = next(a for a in tri["urgent"] + tri["unanswered"] + tri["recent_waiting"] if a["chat_id"] == "wa-bookclub")
    assert "mentioned you" in entry["reasons"]
    detail = api.handle("GET", "/chats/wa-bookclub")[1]
    assert {m["name"] for m in detail["members"]} == {"Sam", "Sam Okafor"}


# ─── 7. new Instagram chats by username ──────────────────────────────────────

def test_an_instagram_link_or_username_starts_a_new_chat(inbox):
    inbox.backend.users["noor.studio"] = {"id": "ig-4242", "name": "Noor", "messaging_id": "ig-4242"}
    starts = []
    inbox.backend.start_chat = lambda a, u, t, attachments=None: starts.append((a, u, t)) or {
        "chat_id": "ig-new", "message_id": "m1"}
    directive, out = _approved(inbox, "inbox_send", {"to": "https://www.instagram.com/noor.studio/", "text": "Hi Noor!"})
    assert directive["message"] == 'Send on Instagram to @noor.studio (a new conversation): "Hi Noor!"'
    assert out["sent"] and starts == [("mock-ig", "ig-4242", "Hi Noor!")]


def test_an_unknown_instagram_username_is_reported_and_not_counted(inbox):
    directive, out = _approved(inbox, "inbox_send", {"to": "@nobody.here", "text": "hello there"})
    assert directive and out["sent"] is False and "No Instagram account @nobody.here" in out["reasons"][0]
    assert inbox.store().recent_sends(0) == []


def test_usernames_only_go_to_instagram(inbox):
    out = sending.plan(inbox, {"to": "@nobody.here", "network": "whatsapp", "text": "hi"})
    assert out["status"] == "not_found"
    out = sending.plan(inbox, {"to": "suraj", "network": "instagram", "text": "hi"})  # a name, not a username
    assert out["status"] == "not_found" and "@username" in out["message"]
    inbox.store()._x("UPDATE accounts SET network='' WHERE network='instagram'")
    out = sending.plan(inbox, {"to": "@nobody.here", "text": "hi"})
    assert out["status"] == "not_found" and "No Instagram account is connected" in out["message"]


# ─── the page: files ─────────────────────────────────────────────────────────

def test_the_page_shows_photos_but_only_downloads_documents(inbox):
    inbox.backend.files.update({"att-photo": PNG, "att-pdf": b"%PDF", "att-svg": b"<svg onload=alert(1)>"})
    msg = _receive(inbox, "wa-maya", "", attachments=[PHOTO, PDF, {"id": "att-svg", "type": "img",
                                                                    "mimetype": "image/svg+xml"}])
    api = InboxAPI(inbox)
    status, photo = api.handle("GET", f"/files/{msg['id']}/att-photo")
    assert status == 200 and isinstance(photo, FileResult) and photo.inline and photo.mimetype == "image/jpeg"
    for aid in ("att-pdf", "att-svg"):  # never rendered inside the dashboard
        _, f = api.handle("GET", f"/files/{msg['id']}/{aid}")
        assert not f.inline and f.mimetype == "application/octet-stream"
        assert f.headers()["Content-Disposition"].startswith("attachment;")
        assert f.headers()["X-Content-Type-Options"] == "nosniff"
    status, opened = api.handle("POST", f"/files/{msg['id']}/att-photo/open")
    assert status == 200 and opened["url"] == f"files/{msg['id']}/att-photo" and opened["inline"]
    assert api.handle("GET", f"/files/{msg['id']}/att-missing")[0] == 404


def test_the_page_transcribes(inbox):
    inbox.backend.files["att-voice"] = b"OggS"
    msg = _receive(inbox, "wa-maya", "", attachments=[VOICE])
    status, out = InboxAPI(inbox).handle("POST", f"/files/{msg['id']}/att-voice/transcribe")
    assert status == 200 and out["transcript"] == "Rent is due on Friday"
    detail = InboxAPI(inbox).handle("GET", "/chats/wa-maya")[1]
    assert detail["can_transcribe"] is True
    m = next(x for x in detail["messages"] if x["id"] == msg["id"])
    assert m["extra"]["attachments"][0]["transcript"] == "Rent is due on Friday"


def test_fastapi_streams_files_with_safe_headers(inbox, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import importlib.util
    from pathlib import Path

    from fastapi.testclient import TestClient

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("inbox_plugin_api_files", root / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_api", InboxAPI(inbox))
    monkeypatch.setattr(module, "get_api", lambda: module._api)
    inbox.backend.files.update({"att-photo": PNG, "att-pdf": b"%PDF-1.4"})
    msg = _receive(inbox, "wa-maya", "", attachments=[PHOTO, PDF])
    app = fastapi.FastAPI()
    app.include_router(module.router, prefix="/api/plugins/unified-inbox")
    with TestClient(app) as http:
        r = http.get(f"/api/plugins/unified-inbox/files/{msg['id']}/att-photo")
        assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/jpeg"
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["content-disposition"].startswith("inline")
        r = http.get(f"/api/plugins/unified-inbox/files/{msg['id']}/att-pdf")
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["content-disposition"].startswith("attachment;")


def test_desktop_reads_media_as_json_and_saves_any_file(inbox, tmp_path, monkeypatch):
    import base64

    from unified_inbox import api as api_mod

    inbox.backend.files.update({"att-photo": PNG, "att-pdf": b"%PDF-1.4"})
    msg = _receive(inbox, "wa-maya", "", attachments=[PHOTO, PDF])
    api = InboxAPI(inbox)
    status, out = api.handle("GET", f"/files/{msg['id']}/att-photo/data")
    assert status == 200 and base64.b64decode(out["data"]) == PNG and out["mimetype"] == "image/jpeg"
    status, out = api.handle("GET", f"/files/{msg['id']}/att-pdf/data")
    assert status == 415 and "data" not in out  # documents are never handed over as JSON
    monkeypatch.setattr(api_mod, "DATA_MAX_BYTES", 10)
    status, out = api.handle("GET", f"/files/{msg['id']}/att-photo/data")
    assert status == 413 and out["kind"] == "too_large"
    status, saved = api.handle("POST", f"/files/{msg['id']}/att-pdf/save")
    path = Path(saved["path"])
    assert status == 200 and path.parent == tmp_path / "saved" and path.name == "invoice March.pdf"
    assert path.read_bytes() == b"%PDF-1.4"
    assert api.handle("POST", f"/files/{msg['id']}/att-pdf/save")[1]["path"] == saved["path"]  # no duplicates
    path.write_bytes(b"changed by you")
    again = Path(api.handle("POST", f"/files/{msg['id']}/att-pdf/save")[1]["path"])
    assert again.name == "invoice March (2).pdf" and path.read_bytes() == b"changed by you"  # yours is kept

