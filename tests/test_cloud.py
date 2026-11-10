"""Optional Supabase sync: schema, push and pull, two devices, fail-closed sends, leases."""

import json
from datetime import datetime

import pytest

from conftest import make_inbox, make_sample_inbox
from fake_supabase import FakeSupabase
from unified_inbox.api import InboxAPI
from unified_inbox.client import InboxError
from unified_inbox.cloud import CloudClient, CloudSync, project_ref, schema_sql
from unified_inbox.cloud_store import SPECS
from unified_inbox.commands import build_command
from unified_inbox.sync import SyncEngine, SyncWorker
from unified_inbox.tools import build_handlers

URL = "https://abcdefghij.supabase.co"


def device(clock, supa, *, token=True, config=None):
    """One Hermes install with cloud sync pointed at the shared fake project."""
    inbox = make_sample_inbox(clock, {"supabase_url": URL, **(config or {})})
    secrets = {"UNIPILE_API_KEY": "test-key", "SUPABASE_SECRET_KEY": supa.key}
    if token:
        secrets["SUPABASE_ACCESS_TOKEN"] = supa.token
    inbox._get_secret = secrets.get
    inbox._local_now = lambda: datetime.fromtimestamp(clock())
    inbox.cloud = CloudSync(inbox, transport=supa)
    return inbox


def ask(answer):
    return lambda question, choices: answer


@pytest.fixture
def supa():
    return FakeSupabase()


@pytest.fixture
def leader(clock, supa):
    a = device(clock, supa)
    while SyncEngine(a).sync_once()["pending_chats"]:
        pass
    a.cloud.setup()
    a.cloud.sync()
    a.cloud.worker_role()
    return a


def test_schema_is_locked_down_and_complete():
    sql = schema_sql()
    for spec in SPECS:
        t = f"public.inbox_{spec.table}"
        assert f"create table if not exists {t}" in sql
        assert f"alter table {t} enable row level security;" in sql
        assert f"revoke all on table {t} from anon, authenticated;" in sql
        assert f"grant select, insert, update, delete on table {t} to service_role;" in sql
    assert "embedding extensions.vector" in sql and "create extension if not exists vector" in sql
    assert sql.index("function public.inbox_touch()") < sql.index("execute function public.inbox_touch()")
    for fn in ("inbox_pull", "inbox_acquire_lease", "inbox_release_lease", "inbox_cleanup"):
        assert f"revoke all on function public.{fn}(" in sql and f"grant execute on function public.{fn}(" in sql
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")


def test_headers_and_urls():
    seen = []

    def transport(method, url, headers, body):
        seen.append((method, url, headers, body))
        return 200, b"[]"

    CloudClient(URL, "sb_secret_abc", transport=transport).upsert("chats", [{"id": "c"}])
    method, url, headers, _ = seen[-1]
    assert url == f"{URL}/rest/v1/inbox_chats" and headers["apikey"] == "sb_secret_abc"
    assert "Authorization" not in headers and "merge-duplicates" in headers["Prefer"]
    assert "sb_secret_abc" not in url
    CloudClient(URL, "eyJlegacy", transport=transport).upsert("chats", [{"id": "c"}])
    assert seen[-1][2]["Authorization"] == "Bearer eyJlegacy"
    CloudClient(URL, "k", transport=transport).claim_schedule("s1", "dev_1")
    assert seen[-1][0] == "PATCH" and "id=eq.s1" in seen[-1][1] and "status=eq.scheduled" in seen[-1][1]
    assert project_ref(URL) == "abcdefghij" and project_ref("https://evil.example.com") == ""


def test_setup_with_a_token_creates_tables_and_copies_everything(leader, supa):
    assert supa.sql and "create table if not exists public.inbox_chats" in supa.sql[0]
    st = leader.cloud.status()
    assert st["active"] and st["pending_changes"] == 0 and st["role"] == "leader"
    assert len(supa.live("chats")) == 13 and len(supa.live("messages")) > 13
    assert all(r["device"] == st["device_id"] for r in supa.live("chats").values())


def test_setup_without_a_token_explains_the_sql_step(clock, supa):
    inbox = device(clock, supa, token=False)
    cmd = build_command(inbox, ask(None))
    assert "/inbox cloud sql" in cmd("cloud")
    assert "Run this once" in cmd("cloud sql")
    assert "not set up" in cmd("cloud setup")
    supa.installed = True  # the user ran the SQL in the editor
    assert "Cloud sync is on" in cmd("cloud setup")


def test_a_second_device_mirrors_the_first(leader, clock, supa):
    b = device(clock, supa)
    b.cloud.activate()
    b.cloud.sync()
    assert b.cloud.worker_role() == "follower"
    assert len(b.store().cached_chats(limit=100)) == 13
    assert b.store().cached_messages("wa-maya", 10)[-1]["text"] == "??"
    assert b.store().search_messages("plumber")  # searchable on the second device too
    assert b.backend.calls == []  # it never asked Unipile


def test_follower_worker_leaves_unipile_to_the_leader(leader, clock, supa):
    b = device(clock, supa)
    b.cloud.activate()
    assert SyncWorker(b).run_once() == 30.0 and b.backend.calls == []
    assert SyncWorker(leader).run_once() > 30.0


def test_changes_flow_both_ways(leader, clock, supa):
    b = device(clock, supa)
    b.cloud.activate()
    b.cloud.sync()
    hb = build_handlers(b)
    d = json.loads(hb["inbox_draft_reply"]({"chat_id": "li-priya", "text": "Thursday 3-5pm works!"}))
    hb["inbox_memory"]({"action": "remember", "person": "Priya", "text": "Prefers afternoon calls"})
    b.cloud.push()
    leader.cloud.pull()
    assert leader.store().get_draft(d["draft_id"])["text"] == "Thursday 3-5pm works!"
    about = json.loads(build_handlers(leader)["inbox_memory"]({"action": "about", "chat_id": "li-priya"}))
    assert [f["fact"] for f in about["facts"]] == ["Prefers afternoon calls"]
    fact_id = about["facts"][0]["id"]
    build_handlers(leader)["inbox_memory"]({"action": "forget", "fact_id": fact_id})
    leader.cloud.push()
    b.cloud.pull()
    assert json.loads(hb["inbox_memory"]({"action": "about", "chat_id": "li-priya"}))["facts"] == []


def test_scheduled_on_one_device_sent_once_by_the_leader(leader, clock, supa):
    b = device(clock, supa)
    b.cloud.activate()
    b.cloud.sync()
    leader.automation.tick()
    clock.advance(1)
    d = json.loads(build_handlers(b)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    assert build_command(b, ask("schedule"))(f"schedule {d['draft_id']} in 1h").startswith("Scheduled")
    b.cloud.push()
    leader.cloud.pull()
    clock.advance(3605)
    leader.automation.tick()
    b.automation.tick()  # a follower's own tick must not send it again
    assert [m["text"] for m in leader.backend.sent] == ["Yes, home at 8!"]
    assert b.backend.sent == []
    leader.cloud.push()
    b.cloud.pull()
    sid = next(iter(supa.live("schedules")))
    assert b.store().schedule(sid)["status"] == "sent"


def test_a_schedule_cancelled_elsewhere_is_never_sent(leader, clock, supa):
    b = device(clock, supa)
    b.cloud.activate()
    b.cloud.sync()
    d = json.loads(build_handlers(leader)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "On my way!"}))
    build_command(leader, ask("schedule"))(f"schedule {d['draft_id']} in 1h")
    leader.cloud.push()
    b.cloud.pull()
    sid = b.store().schedules()[0]["id"]
    build_command(b, ask(None))(f"schedule cancel {sid}")
    b.cloud.push()  # the leader has not pulled yet when the time comes
    clock.advance(3605)
    leader.automation.tick()
    assert leader.backend.sent == []


def test_send_limits_count_every_device(leader, clock, supa):
    b = device(clock, supa, config={"daily_limit_whatsapp": 1})
    b.cloud.activate()
    b.cloud.sync()
    d1 = json.loads(build_handlers(leader)["inbox_draft_reply"]({"chat_id": "wa-dev", "text": "Deck coming in 10!"}))
    assert leader.send(d1["draft_id"])["sent"]
    clock.advance(300)
    d2 = json.loads(build_handlers(b)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    out = b.send(d2["draft_id"])
    assert out["sent"] is False and "WhatsApp daily limit" in " ".join(out["reasons"])


def test_sends_fail_closed_when_supabase_is_unreachable(leader, clock, supa):
    d = json.loads(build_handlers(leader)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    supa.down = True
    with pytest.raises(InboxError, match="can't be reached") as info:
        leader.send(d["draft_id"])
    assert info.value.kind == "cloud" and leader.backend.sent == []
    assert leader.store().get_draft(d["draft_id"])["status"] == "pending"
    out = build_command(leader, ask("send"))(f"send {d['draft_id']}")
    assert "can't be reached" in out and leader.backend.sent == []
    supa.down = False
    clock.advance(120)
    assert leader.send(d["draft_id"])["sent"]


def test_scheduled_sends_wait_while_supabase_is_down(leader, clock, supa):
    d = json.loads(build_handlers(leader)["inbox_draft_reply"]({"chat_id": "wa-maya", "text": "Yes, home at 8!"}))
    build_command(leader, ask("schedule"))(f"schedule {d['draft_id']} in 1h")
    supa.down = True
    clock.advance(3605)
    leader.automation.tick()
    sch = leader.store().schedules()[0]
    assert sch["status"] == "scheduled" and "Supabase" in sch["reason"] and leader.backend.sent == []
    supa.down = False
    clock.advance(60)
    leader.automation.tick()
    assert [m["text"] for m in leader.backend.sent] == ["Yes, home at 8!"]


def test_leadership_moves_when_the_leader_goes_quiet(leader, clock, supa):
    b = device(clock, supa)
    b.cloud.activate()
    assert b.cloud.worker_role() == "follower"
    supa.lease_now += 1000  # the leader stopped renewing
    assert b.cloud.worker_role() == "leader"
    assert leader.cloud.worker_role() == "follower"


def test_embeddings_travel_with_the_chunks(leader, clock, supa):
    store = leader.store()
    for chat_id in store.chats_behind("chunk_ts", since_ts=0, limit=50):
        leader.context.update_chunks(chat_id)
    ids = [r[0] for r in store._q("SELECT id FROM chunks")]
    store.use_embedding_model("openai:test", 3)
    store.put_vectors("openai:test", 3, [(i, [1.0, 0.0, float(i % 3)]) for i in ids])
    leader.cloud.push()
    remote = supa.live("chunks")
    assert remote and all(r["embedding"].startswith("[") and r["model"] == "openai:test" for r in remote.values())
    b = device(clock, supa)
    b.cloud.activate()
    b.cloud.sync()
    assert b.store()._q("SELECT COUNT(*) FROM chunk_vectors")[0][0] == len(ids)
    assert b.store().get_meta("embedding_model") == "openai:test:3"
    assert b.store().nearest_chunks([1.0, 0.0, 0.0], "openai:test", 3)


def test_questions_answered_on_another_device_hit_the_right_fact(leader, clock, supa):
    store, now = leader.store(), clock()
    c = store.ensure_contact(chat_id="wa-mum", network="whatsapp", name="Mum", now=now)
    store.add_fact(contact_id=c["id"], text="unrelated", category="other", status="active", source="auto", now=now)
    fid = store.add_fact(contact_id=c["id"], text="Visiting on the 20th", category="plans", status="pending",
                         source="auto", now=now)
    store.add_question(kind="fact", text="Visiting on the 20th?",
                       data={"fact_id": fid, "fact_gid": store.gid_of("facts", fid)}, dedupe_key="q1", now=now)
    leader.cloud.push()
    b = device(clock, supa)
    b.cloud.activate()
    b.cloud.sync()
    q = b.store().questions()[0]
    b.context.answer(q["id"], "yes")
    b.cloud.push()
    leader.cloud.pull()
    assert store.fact(fid)["status"] == "active"


def test_turning_cloud_off_stops_recording(leader, clock, supa):
    leader._get_config = lambda k, d=None: {"unipile_account_id": "mock-wa, mock-ig, mock-li"}.get(k, d)
    assert leader.cloud.ensure() is False
    store = leader.store()
    assert not store.outbox_enabled() and store.outbox_count() == 0
    store.add_draft(chat_id="wa-maya", account_id="mock-wa", network="whatsapp", chat_name="", text="x", now=clock())
    assert store.outbox_count() == 0


def test_page_and_status(leader, clock, supa):
    api = InboxAPI(leader)
    status, st = api.handle("GET", "/cloud")
    assert status == 200 and st["active"] and st["sql_editor_url"].endswith("/abcdefghij/sql/new")
    assert "inbox_chats" in api.handle("GET", "/cloud/schema")[1]["sql"]
    assert api.handle("POST", "/cloud/sync", {}, {})[0] == 200
    assert api.handle("GET", "/setup")[1]["cloud"]["active"]
    assert "Cloud sync: on" in build_command(leader, ask(None))("cloud")
    unconfigured = InboxAPI(make_inbox(clock))
    assert unconfigured.handle("GET", "/cloud")[1]["configured"] is False


def test_page_writes_are_shared_right_away(leader, clock, supa):
    api = InboxAPI(leader)
    _, d = api.handle("POST", "/chats/wa-maya/drafts", {}, {"text": "Shared at once"})
    assert d["draft_id"] in supa.live("drafts")


def test_a_change_during_a_push_is_not_lost(leader, clock, supa):
    store = leader.store()
    d = store.add_draft(chat_id="wa-maya", account_id="mock-wa", network="whatsapp", chat_name="Maya", text="v1",
                        now=clock())
    batch = store.outbox_batch("drafts", 10)
    store._x("UPDATE drafts SET text='v2' WHERE id=?", (d,))  # edited while that batch is "in flight"
    store.outbox_done("drafts", [(k, ts) for k, _, _, ts in batch])
    assert [k for k, *_ in store.outbox_batch("drafts", 10)] == [d]  # still queued
    leader.cloud.push()
    assert supa.live("drafts")[d]["text"] == "v2"
