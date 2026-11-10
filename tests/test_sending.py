"""inbox_send: Hermes sends to a person after the user approves, on all three apps."""

import base64
import json

import pytest

from conftest import make_sample_inbox
from unified_inbox import sending
from unified_inbox.commands import build_command
from unified_inbox.sync import SyncEngine
from unified_inbox.tools import build_handlers


@pytest.fixture
def inbox(clock):
    i = make_sample_inbox(clock)
    while SyncEngine(i).sync_once()["pending_chats"]:
        pass
    clock.advance(1)
    calls = []

    def start_chat(account_id, user_id, text, attachments=None):
        calls.append(("start", account_id, user_id, text, attachments))
        return {"chat_id": user_id, "message_id": "m-new"}

    i.backend.start_chat = start_chat
    i.backend.get_user = lambda account_id, user_id: {"id": "ACo123", "name": "Priya", "messaging_id": "ACo123"}
    real = i.backend.send_message

    def send_message(account_id, chat_id, text, attachments=None):
        calls.append(("send", chat_id, text, attachments))
        return real(account_id, chat_id, text)

    i.backend.send_message = send_message
    i.backend.calls_out = calls
    return i


def approve_and_send(inbox, args, base_dir=None):
    """What Hermes does: run the pre_tool_call hook, show the prompt, then run the tool."""
    directive = sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args=args)
    out = json.loads(build_handlers(inbox)["inbox_send"](args))
    return directive, out


def test_the_user_is_asked_with_the_exact_person_text_and_app(inbox):
    directive, out = approve_and_send(inbox, {"to": "Maya", "text": "hello maya, on my way"})
    assert directive["action"] == "approve"
    assert directive["message"] == 'Send on WhatsApp to Maya Rivers: "hello maya, on my way"'
    assert out["sent"] is True and inbox.backend.calls_out[-1][:3] == ("send", "wa-maya", "hello maya, on my way")


def test_without_the_approval_prompt_nothing_is_sent(inbox):
    out = json.loads(build_handlers(inbox)["inbox_send"]({"to": "Maya", "text": "unasked"}))
    assert out["sent"] is False and "did not ask you to approve" in out["error"]
    assert inbox.backend.calls_out == []


def test_an_approval_is_for_one_exact_request(inbox):
    sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args={"to": "Maya", "text": "approved text"})
    out = json.loads(build_handlers(inbox)["inbox_send"]({"to": "Maya", "text": "a different text"}))
    assert out["sent"] is False and inbox.backend.calls_out == []
    # and one approval sends once
    approve_and_send(inbox, {"to": "Dev", "text": "Deck coming in 10 minutes"})
    again = json.loads(build_handlers(inbox)["inbox_send"]({"to": "Dev", "text": "Deck coming in 10 minutes"}))
    assert again["sent"] is False and len(inbox.backend.calls_out) == 1


def test_several_matches_ask_which_one_and_nothing_is_asked_or_sent(inbox, clock):
    inbox.store()._x("UPDATE chats SET name='Suraj Kumar' WHERE id='wa-dev'")
    inbox.store()._x("UPDATE chats SET name='Suraj Patel' WHERE id='li-marco'")
    directive, out = approve_and_send(inbox, {"to": "Suraj", "text": "hello suraj"})
    assert directive is None  # no prompt for an unclear person
    assert {o["chat_id"] for o in out["which_one"]} == {"wa-dev", "li-marco"} and "ask_user" in out
    directive, out = approve_and_send(inbox, {"to": "Suraj", "chat_id": "wa-dev", "text": "hello suraj"})
    assert directive["action"] == "approve" and out["sent"] is True
    clock.advance(200)  # the gap between sends
    directive, out = approve_and_send(inbox, {"to": "Suraj", "network": "linkedin", "text": "hi suraj"})
    assert "Suraj Patel" in directive["message"] and out["sent"] is True


def test_by_phone_number_existing_chat_or_new_conversation(inbox, clock):
    inbox.store().upsert_chats([{"id": "919876543210@s.whatsapp.net", "account_id": "mock-wa",
                                 "network": "whatsapp", "name": "Ravi", "ts": clock()}], clock())
    plan = sending.plan(inbox, {"to": "+91 98765 43210", "text": "hi"})
    assert plan["who"]["status"] == "chat" and plan["name"] == "Ravi"
    directive, out = approve_and_send(inbox, {"to": "+44 7700 900123", "text": "hello from hermes"})
    assert "(a new conversation)" in directive["message"]
    assert out["sent"] and out["new_conversation"]
    assert inbox.backend.calls_out[-1][:4] == ("start", "mock-wa", "447700900123@s.whatsapp.net", "hello from hermes")
    assert sending.plan(inbox, {"to": "9876543210", "text": "x"})["message"].startswith("Add the country code")


def test_new_conversations_have_a_daily_limit(clock):
    inbox = make_sample_inbox(clock, {"new_conversations_per_day": 1})
    SyncEngine(inbox).sync_once()
    inbox.backend.start_chat = lambda a, u, t, attachments=None: {"chat_id": u, "message_id": "x"}
    assert approve_and_send(inbox, {"to": "+447700900001", "text": "first new chat"})[1]["sent"]
    clock.advance(200)
    out = approve_and_send(inbox, {"to": "+447700900002", "text": "second new chat"})[1]
    assert out["sent"] is False and "New-conversation limit" in out["reasons"][0]


def test_linkedin_profile_link(inbox):
    directive, out = approve_and_send(inbox, {"to": "https://www.linkedin.com/in/priya-nair/", "text": "Hi Priya!"})
    assert "LinkedIn to linkedin.com/in/priya-nair" in directive["message"]
    assert out["sent"] and inbox.backend.calls_out[-1][:3] == ("start", "mock-li", "ACo123")


def test_instagram_existing_chat_or_a_new_one_by_username(inbox, clock):
    directive, out = approve_and_send(inbox, {"to": "@lena.makes", "text": "Yes, let's collab!"})
    assert directive and "Instagram to lena.makes" in directive["message"] and out["sent"]
    assert inbox.backend.calls_out[-1][0] == "send"  # her existing chat
    clock.advance(200)  # the gap between sends
    looked_up = []
    inbox.backend.get_user = lambda account_id, user_id: looked_up.append(user_id) or {
        "id": "ig-4242", "name": "Noor", "messaging_id": "ig-4242"}
    directive, out = approve_and_send(inbox, {"to": "@noor.studio", "text": "Loved your shop!"})
    assert directive["message"] == 'Send on Instagram to @noor.studio (a new conversation): "Loved your shop!"'
    assert out["sent"] and out["new_conversation"] and looked_up == ["noor.studio"]
    assert inbox.backend.calls_out[-1][:4] == ("start", "mock-ig", "ig-4242", "Loved your shop!")


def test_files_are_attached_and_named_in_the_prompt(inbox, tmp_path):
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    directive, out = approve_and_send(inbox, {"to": "Dev", "text": "Here it is", "files": [str(f)]})
    assert f"with 1 file(s): invoice.pdf (1 KB, in {f.resolve().parent})" in directive["message"]
    att = inbox.backend.calls_out[-1][3]
    assert out["sent"] and att[0]["filename"] == "invoice.pdf" and att[0]["content_type"] == "application/pdf"
    assert base64.b64decode(att[0]["content"]) == b"%PDF-1.4 test"
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (sending.MAX_FILE_BYTES + 1))
    _, out = approve_and_send(inbox, {"to": "Dev", "text": "x", "files": [str(big)]})
    assert out["kind"] == "input" and "limit" in out["error"]
    _, out = approve_and_send(inbox, {"to": "Dev", "text": "x", "files": [str(tmp_path / "missing.pdf")]})
    assert "not found" in out["error"]


def test_safety_limits_still_apply(inbox, clock):
    assert approve_and_send(inbox, {"to": "Maya", "text": "first"})[1]["sent"]
    out = approve_and_send(inbox, {"to": "Dev", "text": "second right away"})[1]
    assert out["sent"] is False and "Too soon" in out["reasons"][0] and "draft_id" in out


def test_no_prompt_when_hermes_would_approve_without_asking(inbox, monkeypatch):
    monkeypatch.setattr(sending, "approval_bypassed", lambda: "Hermes is in yolo mode (approvals skipped)")
    directive = sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args={"to": "Maya", "text": "hi"})
    assert directive["action"] == "block" and "yolo" in directive["message"]


def test_other_tools_are_left_alone(inbox):
    assert sending.pre_tool_call_hook(inbox)(tool_name="terminal", args={"command": "ls"}) is None


def test_the_log_shows_what_happened(inbox, clock):
    approve_and_send(inbox, {"to": "Maya", "text": "hello maya"})
    approve_and_send(inbox, {"to": "Dev", "text": "too soon"})
    sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args={"to": "Mum", "text": "declined one"})
    log = build_command(inbox, lambda q, c: None)("log")
    assert "SENT      WhatsApp -> Maya Rivers: \"hello maya\"" in log
    assert "BLOCKED" in log and "Too soon" in log
    assert "WAITING/DECLINED" in log and "declined one" in log
    attempts = inbox.store().attempts()
    assert [a["status"] for a in attempts] == ["asked", "blocked", "sent"]


def test_the_page_activity_lists_sends(inbox):
    from unified_inbox.api import InboxAPI

    approve_and_send(inbox, {"to": "Maya", "text": "hello maya"})
    items = InboxAPI(inbox).handle("GET", "/activity")[1]["items"]
    assert any(i["title"].startswith("Sent: to Maya Rivers (WhatsApp), via hermes") for i in items)


def test_activity_file(tmp_path, clock):
    import sqlite3

    from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend
    from conftest import make_inbox

    conn = sqlite3.connect(str(tmp_path / "data.db"), check_same_thread=False)
    i = make_inbox(clock, {"unipile_account_id": SAMPLE_ACCOUNT_IDS}, secrets={"UNIPILE_API_KEY": "k"},
                   backend=SampleBackend(clock), conn=conn)
    SyncEngine(i).sync_once()
    clock.advance(1)
    approve_and_send(i, {"to": "Maya", "text": "hello maya"})
    text = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "SENT    hermes   WhatsApp -> Maya Rivers: 'hello maya'" in text


@pytest.mark.parametrize("parts", [("id_rsa",), (".env",), (".env.local",), ("auth.json",), ("server.pem",),
                                   (".ssh", "config"), (".aws", "credentials"), ("vault.kdbx",)])
def test_keys_passwords_and_login_files_are_never_attached(inbox, tmp_path, parts):
    secret = tmp_path.joinpath(*parts)
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("TOPSECRET", encoding="utf-8")
    directive, out = approve_and_send(inbox, {"to": "Dev", "text": "here", "files": [str(secret)]})
    assert directive is None  # nothing to approve: the request itself is refused
    assert out["kind"] == "input" and "never attached" in out["error"]
    assert inbox.backend.calls_out == []


def test_a_link_to_a_secret_is_refused_too(inbox, tmp_path):
    secret = tmp_path / ".ssh" / "id_ed25519"
    secret.parent.mkdir()
    secret.write_text("TOPSECRET", encoding="utf-8")
    link = tmp_path / "holiday.jpg"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("this system does not allow symlinks")
    _, out = approve_and_send(inbox, {"to": "Dev", "text": "here", "files": [str(link)]})
    assert "never attached" in out["error"] and inbox.backend.calls_out == []


def test_the_inbox_database_folder_is_never_attached(tmp_path, clock):
    import sqlite3

    from conftest import make_inbox
    from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend

    conn = sqlite3.connect(str(tmp_path / "data.db"), check_same_thread=False)
    backend = SampleBackend(clock)
    i = make_inbox(clock, {"unipile_account_id": SAMPLE_ACCOUNT_IDS}, secrets={"UNIPILE_API_KEY": "k"},
                   backend=backend, conn=conn)
    SyncEngine(i).sync_once()
    clock.advance(1)
    _, out = approve_and_send(i, {"to": "Maya", "text": "my whole inbox", "files": [str(tmp_path / "data.db")]})
    assert "never attached" in out["error"] and backend.sent == []


def test_relative_paths_come_from_the_chat_folder_in_the_prompt_and_the_send(inbox, tmp_path, monkeypatch):
    chat_folder = tmp_path / "chat"
    chat_folder.mkdir()
    (chat_folder / "report.pdf").write_bytes(b"%PDF the right one")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "report.pdf").write_bytes(b"%PDF the wrong one, much longer " * 100)
    monkeypatch.chdir(elsewhere)  # the process folder is not the chat's folder
    inbox.session_cwd = lambda: str(chat_folder)
    directive, out = approve_and_send(inbox, {"to": "Dev", "text": "the report", "files": ["report.pdf"]})
    assert f"in {chat_folder.resolve()}" in directive["message"]  # the prompt names the file that is sent
    att = inbox.backend.calls_out[-1][3]
    assert out["sent"] and base64.b64decode(att[0]["content"]) == b"%PDF the right one"


def test_a_declined_request_is_forgotten_after_a_while(inbox, monkeypatch):
    args = {"to": "Maya", "text": "declined"}
    sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args=args)  # asked; the user says no
    later = sending.time.time() + sending.APPROVAL_TTL + 1
    monkeypatch.setattr(sending.time, "time", lambda: later)
    sending.pre_tool_call_hook(inbox)(tool_name="inbox_send", args={"to": "Dev", "text": "another"})
    assert sending.fingerprint(args) not in sending._requests
    assert sending.fingerprint(args) not in sending._attempt_ids
    out = json.loads(build_handlers(inbox)["inbox_send"](args))
    assert out["sent"] is False and inbox.backend.calls_out == []


def _fake_hermes_approval(monkeypatch, **attrs):
    import sys
    import types

    tools_pkg = types.ModuleType("tools")
    approval = types.ModuleType("tools.approval")
    for name, value in attrs.items():
        setattr(approval, name, value)
    tools_pkg.approval = approval
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)


def test_yolo_or_approvals_off_refuse_even_if_hermes_internals_change(monkeypatch):
    # A Hermes whose private checks were renamed: the public bypass check still refuses.
    _fake_hermes_approval(monkeypatch, is_approval_bypass_active=lambda: True)
    assert "yolo" in sending.approval_bypassed()


def test_unattended_runs_refuse(monkeypatch):
    import types

    ctx = types.SimpleNamespace(_get_approval_mode=lambda: "manual")
    _fake_hermes_approval(monkeypatch, is_approval_bypass_active=lambda: False, _yolo_active=lambda: False,
                          approval_context=ctx, _presence=lambda cb: (None, False, False, False))
    assert "unattended" in sending.approval_bypassed()
    _fake_hermes_approval(monkeypatch, is_approval_bypass_active=lambda: False, _yolo_active=lambda: False,
                          approval_context=ctx, _presence=lambda cb: (None, True, False, False))
    assert sending.approval_bypassed() == ""  # someone at the terminal: Hermes asks them
