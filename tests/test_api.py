"""The Inbox page's API (api.py), its FastAPI adapter, the connect link, and the built UI files."""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from conftest import KEY, make_inbox, make_sample_inbox
from unified_inbox import client as client_mod
from unified_inbox.api import InboxAPI
from unified_inbox.client import UnipileClient
from unified_inbox.sync import SyncEngine

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def api(clock):
    inbox = make_sample_inbox(clock)
    inbox._local_now = lambda: datetime.fromtimestamp(clock())
    engine = SyncEngine(inbox)
    while engine.sync_once()["pending_chats"]:
        pass
    clock.advance(1)
    a = InboxAPI(inbox, sync_once=engine.sync_once)
    a.call = lambda method, path, body=None, **q: a.handle(method, path, q, body)
    return a


def test_routing_and_errors(api):
    assert api.call("GET", "/nope")[0] == 404
    assert api.call("POST", "/overview")[0] == 405
    assert api.call("GET", "/chats/missing")[0] == 404
    status, out = api.call("GET", "/search")
    assert status == 400 and "search" in out["error"]


def test_unconfigured_page_points_to_setup(clock):
    a = InboxAPI(make_inbox(clock))
    status, out = a.handle("GET", "/overview")
    assert status == 200 and out["connected"] is False and out["setup"]["has_key"] is False
    assert a.handle("GET", "/today")[0] == 409  # setup needed


def test_overview_today_and_chat_lists(api):
    _, o = api.call("GET", "/overview")
    assert o["connected"] and o["counts"]["unread"] > 0 and o["counts"]["needs_reply"] > 0
    _, t = api.call("GET", "/today")
    assert any(x["chat_id"] == "wa-maya" and x["group"] == "urgent" for x in t["needs_reply"])
    _, everything = api.call("GET", "/chats")
    _, needs = api.call("GET", "/chats", filter="needs_reply")
    _, li = api.call("GET", "/chats", network="linkedin")
    assert len(needs["chats"]) < len(everything["chats"]) == 13
    assert {c["network"] for c in li["chats"]} == {"linkedin"}
    assert all(c["preview"] for c in everything["chats"])
    _, named = api.call("GET", "/chats", q="priya")
    assert [c["id"] for c in named["chats"]] == ["li-priya"]


def test_chat_detail(api):
    status, d = api.call("GET", "/chats/wa-maya")
    assert status == 200 and d["chat"]["name"] == "Maya Rivers" and d["messages"][-1]["text"] == "??"
    assert d["drafts"] == [] and d["watches"] == [] and "brief" in d


def test_send_needs_the_exact_text_shown(api):
    _, draft = api.call("POST", "/chats/wa-maya/drafts", {"text": "Yes, home at 8!"})
    did = draft["draft_id"]
    assert api.call("POST", f"/drafts/{did}/send", {"confirm": True, "text": "Yes, home at 9!"})[0] == 409
    assert api.call("POST", f"/drafts/{did}/send", {"text": "Yes, home at 8!"})[0] == 409
    assert api.inbox.backend.sent == []
    status, out = api.call("POST", f"/drafts/{did}/send", {"confirm": True, "text": "Yes, home at 8!"})
    assert status == 200 and out["sent"] is True and len(api.inbox.backend.sent) == 1
    assert api.call("POST", f"/drafts/{did}/send", {"confirm": True, "text": "Yes, home at 8!"})[0] == 409


def test_send_check_before_confirming(api):
    _, draft = api.call("POST", "/chats/wa-maya/drafts", {"text": "On my way"})
    status, out = api.call("GET", f"/drafts/{draft['draft_id']}/check")
    assert status == 200 and out["reasons"] == [] and out["sent_today"] == 0 and out["daily_limit"] > 0
    assert out["network_limit"] and out["sent_today_on_network"] == 0
    assert api.inbox.backend.sent == []  # checking never sends
    api.call("POST", f"/drafts/{draft['draft_id']}/send", {"confirm": True, "text": "On my way"})
    _, again = api.call("POST", "/chats/wa-maya/drafts", {"text": "On my way"})
    _, out = api.call("GET", f"/drafts/{again['draft_id']}/check")
    assert out["sent_today"] == 1 and out["reasons"]  # the same text again is a duplicate
    assert api.call("GET", "/drafts/nope/check")[0] == 404


def test_send_reports_safety_rules(api):
    api.inbox._get_config = lambda k, d=None: {"new_conversations_per_day": 0,
                                               "unipile_account_id": "mock-wa, mock-ig, mock-li"}.get(k, d)
    _, draft = api.call("POST", "/chats/wa-plumber/drafts", {"text": "Any slot?"})
    status, out = api.call("POST", f"/drafts/{draft['draft_id']}/send", {"confirm": True, "text": "Any slot?"})
    assert status == 200 and out["sent"] is False and "never messaged you" in out["reasons"][0]


def test_schedule_from_the_page(api):
    _, draft = api.call("POST", "/chats/li-priya/drafts", {"text": "Thursday 3-5pm works!"})
    did = draft["draft_id"]
    _, parsed = api.call("GET", "/parse-when", q="tomorrow 9am", draft_id=did)
    assert parsed["ok"] and parsed["problems"] == [] and "09:00" in parsed["text"]
    assert api.call("GET", "/parse-when", q="at 6")[1]["ok"] is False
    assert api.call("POST", f"/drafts/{did}/schedule", {"ts": parsed["ts"], "text": "other", "confirm": True})[0] == 409
    status, out = api.call("POST", f"/drafts/{did}/schedule", {"ts": parsed["ts"], "text": "Thursday 3-5pm works!",
                                                               "confirm": True})
    assert status == 200 and out["schedule_id"]
    _, listed = api.call("GET", "/scheduled")
    assert listed["scheduled"][0]["draft_id"] == did
    _, drafts = api.call("GET", "/drafts")
    assert drafts["drafts"][0]["schedule"]["schedule_id"] == out["schedule_id"]
    assert api.call("DELETE", f"/drafts/{did}")[0] == 200  # discarding also cancels the schedule
    assert api.call("GET", "/scheduled")[1]["scheduled"] == []


def test_loops_watches_alerts_activity(api):
    store, now = api.inbox.store(), api.inbox.clock()
    lid = store.add_loop(chat_id="wa-dev", kind="you_promised", text="Send the deck", due_ts=now - 60, message_id="",
                         now=now)
    _, loops = api.call("GET", "/loops", show="waiting_on_you")
    assert loops["items"][0]["overdue"] and loops["items"][0]["due_ts"]
    assert api.call("POST", f"/loops/{lid}/done")[0] == 200
    assert api.call("POST", f"/loops/{lid}/done")[0] == 404
    status, w = api.call("POST", "/watches", {"type": "reply", "chat_id": "li-priya"})
    assert status == 200 and w["type"] == "reply"
    assert api.call("POST", "/watches", {"type": "snooze", "chat_id": "li-priya"})[0] == 400  # needs a time
    assert len(api.call("GET", "/watches")[1]["watches"]) == 1
    assert api.call("DELETE", f"/watches/{w['watch_id']}")[0] == 200
    api.inbox.automation.notify("watch", "Priya replied")
    _, ch = api.call("GET", "/changes", since=0, alerts_since=0)
    assert ch["unread_alerts"] == 1 and ch["alerts"][0]["title"] == "Priya replied"
    assert api.call("GET", "/changes", alerts_since=ch["alerts"][0]["id"])[1]["alerts"] == []
    item = next(i for i in api.call("GET", "/activity")[1]["items"] if i["title"] == "Priya replied")
    assert item["read"] is False and item["id"] == ch["alerts"][0]["id"]
    api.call("POST", "/alerts/read", {"ids": [item["id"]]})
    assert api.call("GET", "/alerts")[1]["alerts"] == []
    assert any(i["title"] == "Priya replied" and i["read"] for i in api.call("GET", "/activity")[1]["items"])


def test_memory_from_the_page(api):
    status, f = api.call("POST", "/people/wa-mum/facts", {"text": "Birthday is 4 June"})
    assert status == 200
    assert api.call("POST", "/people/wa-mum/facts", {"text": "PIN code is 4590"})[0] == 400
    _, p = api.call("GET", "/people/wa-mum")
    assert [x["text"] for x in p["facts"]] == ["Birthday is 4 June"]
    assert api.call("GET", "/people")[1]["people"][0]["facts"] == 1
    assert api.call("DELETE", f"/facts/{f['id']}")[0] == 200
    store, now = api.inbox.store(), api.inbox.clock()
    c = store.contact_for_chat("wa-mum")
    fid = store.add_fact(contact_id=c["id"], text="Visiting on the 20th", category="plans", status="pending",
                         source="auto", now=now)
    qid = store.add_question(kind="fact", text="?", data={"fact_id": fid}, dedupe_key="x", now=now)
    assert api.call("GET", "/questions")[1]["questions"][0]["id"] == qid
    assert api.call("POST", f"/questions/{qid}", {"answer": "yes"})[1]["result"] == "saved"


def test_search_setup_and_sync(api):
    _, s = api.call("GET", "/search", q="plumber")
    assert s["results"][0]["chat_id"] == "wa-maya"
    _, setup = api.call("GET", "/setup")
    assert setup["has_key"] and len(setup["accounts"]) == 3 and setup["limits"]["daily"] == 20
    assert api.call("POST", "/sync")[1]["accounts"] == 3


def test_connect_link(clock):
    class Backend:
        def __init__(self):
            self.args = None

        def create_auth_link(self, **kw):
            self.args = kw
            return "https://account.unipile.com/link/abc"

    backend = Backend()
    a = InboxAPI(make_inbox(clock, secrets=KEY, backend=backend))
    status, out = a.handle("POST", "/setup/connect", {}, {"redirect_uri": "https://my.host/inbox"})
    assert status == 200 and out["link"].endswith("/abc")
    assert backend.args["redirect_uri"] == "https://my.host/inbox" and backend.args["account_id"] == ""
    assert a.handle("POST", "/setup/connect", {}, {"redirect_uri": "javascript:alert(1)"})[0] == 400


def test_client_auth_link_request(monkeypatch):
    seen = []

    class Resp:
        def __init__(self):
            self.headers = {}

        def read(self):
            return json.dumps({"object": "HostedAuthLink", "link": "https://x/y"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(client_mod.request, "urlopen", lambda req, timeout=None: seen.append(req) or Resp())
    assert UnipileClient("k").create_auth_link(redirect_uri="https://r", expires_on=1_800_000_000) == "https://x/y"
    body = json.loads(seen[0].data)
    assert seen[0].full_url == "https://api.unipile.com/v2/auth/link"
    assert body == {"expires_on": "2027-01-15T08:00:00.000Z", "redirect_uri": "https://r", "providers": "*:MESSAGING"}
    UnipileClient("k").create_auth_link(redirect_uri="https://r", expires_on=1_800_000_000, account_id="acc_1")
    assert json.loads(seen[1].data)["account_id"] == "acc_1" and "providers" not in json.loads(seen[1].data)


def test_sends_are_one_at_a_time_across_processes(api, clock):
    store = api.inbox.store()
    assert store.acquire_lease("send", "another-process", clock(), 120)
    _, draft = api.call("POST", "/chats/wa-maya/drafts", {"text": "Yes, home at 8!"})
    status, out = api.call("POST", f"/drafts/{draft['draft_id']}/send", {"confirm": True, "text": "Yes, home at 8!"})
    assert status == 429 and "Another Hermes window" in out["error"] and api.inbox.backend.sent == []
    store.release_lease("send", "another-process")
    assert api.call("POST", f"/drafts/{draft['draft_id']}/send", {"confirm": True, "text": "Yes, home at 8!"})[1]["sent"]


def test_fastapi_adapter(api, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import importlib.util

    spec = importlib.util.spec_from_file_location("inbox_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_api", api)
    app = fastapi.FastAPI()
    app.include_router(module.router, prefix="/api/plugins/unified-inbox")
    http = TestClient(app)
    assert http.get("/api/plugins/unified-inbox/overview").json()["connected"] is True
    r = http.post("/api/plugins/unified-inbox/chats/wa-maya/drafts", json={"text": "On my way"})
    assert r.status_code == 200 and r.json()["draft_id"]
    assert http.post("/api/plugins/unified-inbox/chats/wa-maya/drafts", content=b"not json").status_code == 400
    assert http.delete("/api/plugins/unified-inbox/watches/999").status_code == 404



def test_the_page_follows_a_plugin_reload(clock, monkeypatch):
    """After Hermes reloads the plugin (the old inbox is closed), the page uses the new one."""
    pytest.importorskip("fastapi")
    import importlib.util

    import unified_inbox
    from conftest import make_sample_inbox

    spec = importlib.util.spec_from_file_location("inbox_plugin_api_reload", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old, new = make_sample_inbox(clock), make_sample_inbox(clock)
    monkeypatch.setitem(unified_inbox._RUNTIME, "inbox", old)
    assert module.get_api().inbox is old and module.get_api().inbox is old  # built once, then reused
    old.close()
    monkeypatch.setitem(unified_inbox._RUNTIME, "inbox", new)
    assert module.get_api().inbox is new
    assert module.get_api().handle("GET", "/overview")[1]["connected"] is True


# ─── the built UI ────────────────────────────────────────────────────────────

def test_built_ui_is_up_to_date():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_ui.py"), "--check"], capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_built_ui_is_valid_javascript():
    web = subprocess.run(["node", "--check", str(ROOT / "dashboard" / "dist" / "index.js")], capture_output=True,
                         text=True)
    assert web.returncode == 0, web.stderr
    desktop = subprocess.run(["node", "--input-type=module", "--check"],
                             input=(ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8"),
                             capture_output=True, text=True, encoding="utf-8")
    assert desktop.returncode == 0, desktop.stderr


def test_dashboard_manifest():
    m = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert m["name"] == "unified-inbox" and m["entry"] == "dist/index.js" and m["api"] == "plugin_api.py"
    assert (ROOT / "dashboard" / m["entry"]).exists() and (ROOT / "dashboard" / m["api"]).exists()
    assert m["tab"]["path"] == "/inbox"
