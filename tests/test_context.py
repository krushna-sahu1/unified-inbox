"""The context engine: privacy, chunks, embeddings, search by meaning, memory, questions, tools."""

import json
import re
import sqlite3

import pytest

from conftest import make_inbox, make_sample_inbox
from unified_inbox.client import InboxError
from unified_inbox.context import (CHUNK_MAX_MSGS, SETTLE_SECONDS, ContextEngine, build_chunks, parse_due)
from unified_inbox.embeddings import Embedder
from unified_inbox.privacy import is_sensitive, redact
from unified_inbox.store import SCHEMA_VERSION, Store
from unified_inbox.sync import SyncEngine, SyncWorker
from unified_inbox.tools import build_handlers

EMB = {"embedding_provider": "openai"}
EMB_KEY = {"UNIPILE_API_KEY": "test-key", "EMBEDDING_API_KEY": "emb-key"}

# A toy embedding: words that mean the same thing share a dimension, so "rent" finds "kiraya".
_SYNONYMS = [{"rent", "kiraya", "landlord", "makaan"}, {"plumber", "pipe", "leak"}, {"deck", "slides", "presentation"},
             {"concert", "tickets", "gig"}, {"job", "role", "recruiter", "interview"}]


def fake_vector(text):
    words = set(re.findall(r"\w+", text.lower()))
    vec = [float(len(words & group)) for group in _SYNONYMS] + [0.01]
    return vec


class FakeProvider:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        texts = body.get("input") or body.get("texts") or [r["content"]["parts"][0]["text"] for r in
                                                            body.get("requests", [])]
        vecs = [fake_vector(t) for t in texts]
        if "openai" in url or "voyage" in url:
            return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]}
        if "cohere" in url:
            return {"embeddings": {"float": vecs}}
        return {"embeddings": [{"values": v} for v in vecs]}


class FakeLlm:
    """Answers in order, or per chat: ``by_name={"Maya Rivers": answer}`` (matched in the prompt)."""

    def __init__(self, *answers, by_name=None):
        self.answers = list(answers)
        self.by_name = by_name or {}
        self.prompts = []

    def __call__(self, instructions, text, schema):
        self.prompts.append(text)
        for name, ans in self.by_name.items():
            if f"with {name}\n" in text:
                return ans
        a = self.answers.pop(0) if self.answers else _answer()
        if isinstance(a, Exception):
            raise a
        return a


def _answer(**kw):
    base = {"summary": "", "facts_new": [], "facts_replace": [], "loops_new": [], "loops_close": [],
            "questions": [], "language": "English"}
    base.update(kw)
    return base


def _inbox(clock, *, llm=None, emb=False, config=None, **kw):
    provider = FakeProvider()
    secrets = EMB_KEY if emb else {"UNIPILE_API_KEY": "test-key"}
    inbox = make_sample_inbox(clock, {**(EMB if emb else {}), **(config or {})}, **kw)
    inbox._get_secret = secrets.get
    inbox.context = ContextEngine(inbox, llm=llm, post=provider)
    inbox.provider = provider
    return inbox


def _ready(inbox, clock):
    SyncEngine(inbox).sync_once()
    clock.advance(SETTLE_SECONDS + 1)


# ─── privacy ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, hidden", [
    ("Your OTP is 482913. Do not share", "[code]"),
    ("482913 is your verification code", "[code]"),
    ("mera password hai abc@123", "[password]"),
    ("Card 4111 1111 1111 1111 exp 12/29", "[card number]"),
    ("cvv 123", "[cvv]"),
    ("A/c no 123456789012 IFSC SBIN0001234", "[account number]"),
    ("Aadhaar 1234 5678 9012", "[ID number]"),
    ("PAN ABCDE1234F", "[ID number]"),
    ("IBAN DE89370400440532013000", "[account number]"),
])
def test_secrets_are_redacted(text, hidden):
    assert hidden in redact(text) and is_sensitive(text)


@pytest.mark.parametrize("text", ["call me at 98765 43210", "meet at 5:30, room 1204", "order #4567 shipped",
                                  "kal 10 baje milte hain", "4111 1111 1111 1112 is not a card", "नमस्ते दोस्त"])
def test_ordinary_text_is_left_alone(text):
    assert redact(text) == text and not is_sensitive(text)


# ─── chunks ──────────────────────────────────────────────────────────────────

def _m(i, ts, text="hello", me=False):
    return {"id": f"m{i}", "chat_id": "c1", "from_me": me, "sender": "" if me else "Ravi", "text": text, "ts": ts}


def test_chunks_split_on_gaps_and_size_and_carry_their_context():
    base = 1_800_000_000.0
    msgs = [_m(i, base + i * 60) for i in range(CHUNK_MAX_MSGS + 3)]
    msgs.append(_m(99, base + 10 * 3600, "OTP 123456 for login"))
    chunks = build_chunks(msgs, {"name": "Ravi", "network": "whatsapp", "is_group": False})
    assert [c["msg_count"] for c in chunks] == [CHUNK_MAX_MSGS, 3, 1]
    assert chunks[0]["text"].startswith("WhatsApp chat with Ravi, ")
    assert "Ravi: hello" in chunks[0]["text"]
    assert "123456" not in chunks[2]["text"] and "[code]" in chunks[2]["text"]


def test_new_messages_only_rebuild_the_tail_and_keep_earlier_vectors(clock):
    inbox = _inbox(clock, emb=True)
    _ready(inbox, clock)
    ctx, store = inbox.context, inbox.store()
    ctx.process_once()
    embedded_before = sum(len(c[2]["input"]) for c in inbox.provider.calls)
    n_chunks = store._q("SELECT COUNT(*) FROM chunks")[0][0]
    assert embedded_before == n_chunks > 0
    inbox.backend.receive("wa-maya", "And the rent is due on Friday")
    SyncEngine(inbox).sync_once()
    ctx.process_once()
    newly = sum(len(c[2]["input"]) for c in inbox.provider.calls) - embedded_before
    assert newly == 1  # only the changed window of Maya's chat
    assert store.chunks_without_vectors("openai:text-embedding-3-small", 10) == []


def test_edited_and_deleted_messages_are_reflected_in_search(clock):
    inbox = _inbox(clock, emb=True)
    _ready(inbox, clock)
    m = inbox.backend.receive("ig-arjun", "the landlord wants the rent")
    SyncEngine(inbox).sync_once()
    inbox.context.process_once()
    assert any(r["chat_id"] == "ig-arjun" for r in inbox.context.search_meaning("kiraya"))
    store = inbox.store()
    store.upsert_messages([{"id": m["id"], "chat_id": "ig-arjun", "from_me": False, "text": "never mind",
                            "ts": m["ts"], "deleted": True}], clock(), account_id="mock-ig", baseline=0)
    inbox.context.process_once()
    assert not any(r["chat_id"] == "ig-arjun" for r in inbox.context.search_meaning("kiraya"))


# ─── embeddings and search by meaning ────────────────────────────────────────

def test_search_finds_other_words_with_the_same_meaning(clock):
    inbox = _inbox(clock, emb=True)
    _ready(inbox, clock)
    inbox.backend.receive("wa-mum", "Beta, makaan ka kiraya bhej dena")
    SyncEngine(inbox).sync_once()
    inbox.context.process_once()
    out = json.loads(build_handlers(inbox)["inbox_search"]({"query": "rent"}))
    assert out["results"] == []  # no message has the word "rent"
    assert out["related"][0]["chat_id"] == "wa-mum" and "kiraya" in out["related"][0]["text"]
    assert out["related"][0]["from"]
    out = json.loads(build_handlers(inbox)["inbox_search"]({"query": "plumber"}))
    assert [r["chat_id"] for r in out["results"]] == ["wa-maya"]
    assert all(r["chat_id"] != "wa-maya" for r in out.get("related", []))  # not repeated


def test_search_without_the_extension_gives_the_same_answer(clock):
    inbox = _inbox(clock, emb=True)
    inbox.store().vec_index = False
    _ready(inbox, clock)
    inbox.backend.receive("wa-mum", "kiraya bhej dena")
    SyncEngine(inbox).sync_once()
    inbox.context.process_once()
    assert inbox.context.search_meaning("rent")[0]["chat_id"] == "wa-mum"


def test_without_a_provider_search_is_keyword_only(clock):
    inbox = _inbox(clock)
    _ready(inbox, clock)
    assert inbox.context.process_once()["embedded"] == 0
    out = json.loads(build_handlers(inbox)["inbox_search"]({"query": "plumber"}))
    assert out["results"] and "related" not in out


def test_switching_models_re_embeds(clock):
    inbox = _inbox(clock, emb=True)
    _ready(inbox, clock)
    inbox.context.process_once()
    store = inbox.store()
    assert store.get_meta("embedding_model").startswith("openai:")
    inbox._get_config = lambda k, d=None: {"embedding_provider": "voyage",
                                           "unipile_account_id": "mock-wa, mock-ig, mock-li"}.get(k, d)
    inbox.context.process_once()
    assert store.get_meta("embedding_model").startswith("voyage:voyage-4-lite:")
    assert store._q("SELECT COUNT(DISTINCT model) FROM chunk_vectors")[0][0] == 1


def test_embedding_daily_cap(clock):
    inbox = _inbox(clock, emb=True, config={"embedding_chars_per_day": 300})
    _ready(inbox, clock)
    out = inbox.context.process_once()
    assert out["backlog"] is True
    assert inbox.store().usage_today(inbox.context._day(), "embed_chars") <= 300


@pytest.mark.parametrize("provider, url, check", [
    ("openai", "https://api.openai.com/v1/embeddings",
     lambda h, b: h["Authorization"] == "Bearer k" and b["dimensions"] == 512 and b["input"] == ["hi"]),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
     lambda h, b: h["x-goog-api-key"] == "k" and b["requests"][0]["output_dimensionality"] == 512
     and b["requests"][0]["content"]["parts"][0]["text"] == "task: search result | query: hi"),
    ("cohere", "https://api.cohere.com/v2/embed",
     lambda h, b: b["input_type"] == "search_query" and b["output_dimension"] == 512 and b["texts"] == ["hi"]),
    ("voyage", "https://api.voyageai.com/v1/embeddings",
     lambda h, b: b["input_type"] == "query" and b["output_dimension"] == 512 and b["model"] == "voyage-4-lite"),
])
def test_provider_requests(provider, url, check):
    fake = FakeProvider()
    vecs = Embedder(provider, "k", post=fake).embed(["hi"], query=True)
    sent_url, headers, body = fake.calls[0]
    assert sent_url == url and check(headers, body) and len(vecs) == 1


def test_gemini_documents_use_the_document_prefix_and_legacy_task_type():
    fake = FakeProvider()
    Embedder("gemini", "k", post=fake).embed(["hello"])
    assert fake.calls[0][2]["requests"][0]["content"]["parts"][0]["text"] == "title: none | text: hello"
    Embedder("gemini", "k", "gemini-embedding-001", post=fake).embed(["hello"])
    assert fake.calls[1][2]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"


def test_bad_provider_answer_and_unknown_provider():
    with pytest.raises(InboxError):
        Embedder("openai", "k", post=lambda u, h, b: {"data": []}).embed(["a"])
    with pytest.raises(InboxError, match="Unknown embedding provider"):
        Embedder("local", "k")


# ─── memory ──────────────────────────────────────────────────────────────────

def test_memory_saves_sure_facts_asks_about_unsure_ones_and_never_keeps_secrets(clock):
    llm = FakeLlm()
    inbox = _inbox(clock, llm=llm, config={"memory_chats_per_cycle": 20})
    _ready(inbox, clock)
    llm.by_name["Maya Rivers"] = _answer(
        summary="Maya needs me home at 8am tomorrow for the plumber.",
        facts_new=[{"text": "Maya is my flatmate", "category": "relationship", "confidence": 0.9},
                   {"text": "Maya might be moving out in March", "category": "plans", "confidence": 0.4},
                   {"text": "Maya's bank OTP is 482913", "category": "other", "confidence": 0.99}],
        loops_new=[{"kind": "they_wait", "text": "Confirm being home at 8am for the plumber",
                    "due": "2027-01-16T08:00", "message_id": "wa-maya-2"}])
    out = inbox.context.process_once()
    assert out["memorized"] >= 1
    store = inbox.store()
    contact = store.contact_for_chat("wa-maya")
    assert [f["text"] for f in store.facts(contact["id"])] == ["Maya is my flatmate"]
    assert [f["text"] for f in store.facts(contact["id"], status="pending")] == ["Maya might be moving out in March"]
    assert [q["kind"] for q in store.questions()] == ["fact"]
    assert store.chat_context("wa-maya")["summary"].startswith("Maya needs me home")
    loop = store.loops(chat_id="wa-maya")[0]
    assert loop["kind"] == "they_wait" and loop["due_ts"] == parse_due("2027-01-16T08:00")
    prompt = next(p for p in llm.prompts if "Maya Rivers" in p)
    assert "Today:" in prompt and "plumber" in prompt


def test_memory_waits_for_a_burst_to_settle(clock):
    llm = FakeLlm(*[_answer()] * 50)
    inbox = _inbox(clock, llm=llm)
    SyncEngine(inbox).sync_once()
    clock.advance(SETTLE_SECONDS + 1)
    while inbox.context.process_once()["memorized"]:
        pass
    calls = len(llm.prompts)
    inbox.backend.receive("li-priya", "One more thing")
    SyncEngine(inbox).sync_once()
    inbox.context.process_once()
    assert len(llm.prompts) == calls  # still typing, probably
    clock.advance(SETTLE_SECONDS + 1)
    inbox.context.process_once()
    assert len(llm.prompts) == calls + 1


def test_memory_respects_the_daily_call_limit(clock):
    llm = FakeLlm(*[_answer()] * 50)
    inbox = _inbox(clock, llm=llm, config={"memory_llm_calls_per_day": 2})
    _ready(inbox, clock)
    for _ in range(5):
        inbox.context.process_once()
    assert len(llm.prompts) == 2
    assert "daily model-call limit" in " ".join(inbox.store().get_meta("context_state")["problems"])


def test_changed_facts_are_confirmed_unless_clear(clock):
    llm = FakeLlm()
    inbox = _inbox(clock, llm=llm, config={"memory_chats_per_cycle": 20})
    _ready(inbox, clock)
    store = inbox.store()
    c = store.ensure_contact(chat_id="li-priya", network="linkedin", name="Priya", now=clock())
    old = store.add_fact(contact_id=c["id"], text="Priya works at Northwind", category="work", status="active",
                         source="auto", now=clock())
    mine = store.add_fact(contact_id=c["id"], text="Priya is vegetarian", category="preference", status="active",
                          source="user", now=clock())
    llm.by_name["Priya Nair (Recruiter, Northwind)"] = _answer(facts_replace=[
        {"id": old, "text": "Priya works at Contoso now", "confidence": 0.95},
        {"id": mine, "text": "Priya eats fish", "confidence": 0.95}])
    inbox.context.process_once()
    active = {f["text"] for f in store.facts(c["id"])}
    assert "Priya works at Contoso now" in active and "Priya works at Northwind" not in active  # clear change
    assert "Priya is vegetarian" in active  # what the user said is never overwritten silently
    q = next(q for q in store.questions() if q["kind"] == "update")
    assert "Priya eats fish" in q["text"]
    inbox.context.answer(q["id"], "haan")
    active = {f["text"] for f in store.facts(c["id"])}
    assert "Priya eats fish" in active and "Priya is vegetarian" not in active


def test_groups_get_a_summary_but_no_personal_facts(clock):
    llm = FakeLlm()
    inbox = _inbox(clock, llm=llm, config={"memory_chats_per_cycle": 20})
    _ready(inbox, clock)
    store = inbox.store()
    store._x("UPDATE chats SET is_group=1 WHERE id='wa-bookclub'")
    llm.answers = [_answer(summary="Book club picks a date.",
                           facts_new=[{"text": "Sam likes thrillers", "category": "preference", "confidence": 1}])
                   for _ in range(20)]
    inbox.context.process_once()
    assert store.contact_for_chat("wa-bookclub") is None
    assert store.chat_context("wa-bookclub")["summary"] == "Book club picks a date."


def test_loops_close_and_dedupe(clock):
    llm = FakeLlm()
    inbox = _inbox(clock, llm=llm, config={"memory_chats_per_cycle": 20})
    _ready(inbox, clock)
    store = inbox.store()
    lid = store.add_loop(chat_id="wa-dev", kind="they_wait", text="Send Dev the updated deck", due_ts=None,
                         message_id="", now=clock())
    llm.answers = [_answer(loops_close=[{"id": lid, "reason": "sent it"}],
                           loops_new=[{"kind": "you_promised", "text": "Call Dev after the demo", "due": None,
                                       "message_id": "nope"}]) for _ in range(20)]
    inbox.context.process_once()
    assert store.loop(lid)["status"] == "done"
    assert [lp["text"] for lp in store.loops(chat_id="wa-dev")] == ["Call Dev after the demo"]


def test_model_failures_are_retried_then_skipped(clock):
    llm = FakeLlm(*[RuntimeError("provider down")] * 10)
    inbox = _inbox(clock, llm=llm, config={"memory_chats_per_cycle": 1})
    _ready(inbox, clock)
    first = inbox.store().chats_behind("memory_ts", since_ts=0, limit=1)[0]
    for _ in range(3):
        out = inbox.context.process_once()
        assert "provider down" in " ".join(out["problems"])
    assert first not in inbox.store().chats_behind("memory_ts", since_ts=0, limit=100)


def test_same_name_on_two_apps_is_asked_once(clock):
    inbox = _inbox(clock, llm=FakeLlm(*[_answer()] * 50), config={"memory_chats_per_cycle": 20})
    _ready(inbox, clock)
    store = inbox.store()
    store._x("UPDATE chats SET name='Priya Nair' WHERE id IN ('li-priya', 'wa-mum')")
    for _ in range(3):
        inbox.backend.receive("wa-mum", "hi")
        inbox.backend.receive("li-priya", "hello")
        SyncEngine(inbox).sync_once()
        store._x("UPDATE chats SET name='Priya Nair' WHERE id IN ('li-priya', 'wa-mum')")
        clock.advance(SETTLE_SECONDS + 1)
        inbox.context.process_once()
    merges = [q for q in store.questions() if q["kind"] == "merge"]
    assert len(merges) == 1 and "same person" in merges[0]["text"]
    inbox.context.answer(merges[0]["id"], "yes")
    assert store.contact_for_chat("li-priya")["id"] == store.contact_for_chat("wa-mum")["id"]


def test_answers(clock):
    inbox = _inbox(clock)
    _ready(inbox, clock)
    store, ctx, now = inbox.store(), inbox.context, clock()
    c = store.ensure_contact(chat_id="ig-lena", network="instagram", name="lena.makes", now=now)
    f1 = store.add_fact(contact_id=c["id"], text="Lena runs a pottery studio", category="work", status="pending",
                        source="auto", now=now)
    q1 = store.add_question(kind="fact", text="?", data={"fact_id": f1}, dedupe_key="fact:1", now=now)
    assert ctx.answer(q1, "nahi")["result"] == "not saved" and store.fact(f1) is None
    f2 = store.add_fact(contact_id=c["id"], text="Lena lives in Goa", category="personal", status="pending",
                        source="auto", now=now)
    q2 = store.add_question(kind="fact", text="?", data={"fact_id": f2}, dedupe_key="fact:2", now=now)
    ctx.answer(q2, "Lena lives in Pune")
    assert store.fact(f2)["text"] == "Lena lives in Pune" and store.fact(f2)["status"] == "active"
    f3 = store.add_fact(contact_id=c["id"], text="x", category="other", status="pending", source="auto", now=now)
    q3 = store.add_question(kind="fact", text="?", data={"fact_id": f3}, dedupe_key="fact:3", now=now)
    with pytest.raises(InboxError, match="never|not be saved"):
        ctx.answer(q3, "her card is 4111 1111 1111 1111")
    q4 = store.add_question(kind="meaning", text="Which day is 'kal'?", data={"chat_id": "ig-lena",
                                                                             "contact_id": c["id"]},
                            dedupe_key="meaning:1", now=now)
    ctx.answer(q4, "tomorrow, Saturday")
    assert any("tomorrow, Saturday" in f["text"] for f in store.facts(c["id"]))
    with pytest.raises(InboxError):
        ctx.answer(q4, "again")
    assert store.add_question(kind="meaning", text="again", data={}, dedupe_key="meaning:1", now=now) is None


# ─── tools ───────────────────────────────────────────────────────────────────

def test_context_tool(clock):
    inbox = _inbox(clock, llm=FakeLlm(*[_answer(summary="Recruiter call this week.")] * 50),
                   config={"memory_chats_per_cycle": 20})
    _ready(inbox, clock)
    inbox.context.process_once()
    h = build_handlers(inbox)
    out = json.loads(h["inbox_context"]({"person": "priya"}))
    assert out["chat_id"] == "li-priya" and out["summary"] == "Recruiter call this week."
    assert out["recent"] and out["recent"][-1]["time"]
    assert json.loads(h["inbox_context"]({"person": "a"}))["which_one"]  # several names contain "a"
    assert json.loads(h["inbox_context"]({"person": "zzz"}))["kind"] == "not_found"
    assert json.loads(h["inbox_context"]({}))["kind"] == "input"
    no_llm = _inbox(clock)
    _ready(no_llm, clock)
    assert "needs Hermes's language model" in json.loads(build_handlers(no_llm)["inbox_context"](
        {"chat_id": "wa-maya"}))["note"]


def test_loops_tool(clock):
    inbox = _inbox(clock, llm=FakeLlm())
    _ready(inbox, clock)
    store, now = inbox.store(), clock()
    a = store.add_loop(chat_id="wa-dev", kind="they_wait", text="Send the deck", due_ts=now - 60, message_id="",
                       now=now)
    store.add_loop(chat_id="li-priya", kind="you_wait", text="Priya to confirm the slot", due_ts=None,
                   message_id="", now=now)
    store.add_loop(chat_id="ig-lena", kind="you_promised", text="Send Lena prices", due_ts=now + 3600,
                   message_id="", now=now)
    h = build_handlers(inbox)
    mine = json.loads(h["inbox_loops"]({"show": "waiting_on_you"}))["items"]
    assert [i["text"] for i in mine] == ["Send the deck", "Send Lena prices"] and mine[0]["overdue"] is True
    assert mine[0]["name"] == "Dev Patel (work)"
    assert [i["text"] for i in json.loads(h["inbox_loops"]({"show": "waiting_on_them"}))["items"]] == [
        "Priya to confirm the slot"]
    out = json.loads(h["inbox_loops"]({"done": a}))
    assert out["marked_done"] == a and a not in [i["id"] for i in out["items"]]
    assert json.loads(h["inbox_loops"]({"done": a}))["kind"] == "not_found"


def test_memory_tool(clock):
    inbox = _inbox(clock, llm=FakeLlm())
    _ready(inbox, clock)
    h = build_handlers(inbox)
    saved = json.loads(h["inbox_memory"]({"action": "remember", "person": "Mum", "text": "Mum's birthday is 4 June"}))
    assert saved["remembered"]["about"] == "Mum"
    assert json.loads(h["inbox_memory"]({"action": "remember", "person": "Mum",
                                         "text": "password: hunter2"}))["kind"] == "input"
    about = json.loads(h["inbox_memory"]({"action": "about", "chat_id": "wa-mum"}))
    assert [f["fact"] for f in about["facts"]] == ["Mum's birthday is 4 June"] and about["facts"][0]["source"] == "user"
    assert json.loads(h["inbox_memory"]({"action": "questions"}))["total_open"] == 0
    fid = about["facts"][0]["id"]
    assert json.loads(h["inbox_memory"]({"action": "forget", "fact_id": fid}))["forgotten"] == fid
    h["inbox_memory"]({"action": "remember", "person": "Mum", "text": "Likes jasmine tea"})
    assert json.loads(h["inbox_memory"]({"action": "forget", "person": "Mum"}))["forgotten_facts"] == 1
    assert json.loads(h["inbox_memory"]({"action": "questions"}))["questions"] == []


def test_status_and_unconfigured(clock):
    inbox = _inbox(clock, llm=FakeLlm(), emb=True)
    st = json.loads(build_handlers(inbox)["inbox_status"]({}))["memory"]
    assert st["language_model"] is True and st["embeddings"] == "openai:text-embedding-3-small"
    bare = make_inbox(clock)
    assert ContextEngine(bare).process_once()["skipped"] == "setup"
    for tool in ("inbox_context", "inbox_loops", "inbox_memory"):
        assert json.loads(build_handlers(bare)[tool]({"action": "about", "person": "x"}))["kind"] == "setup"


# ─── wiring ──────────────────────────────────────────────────────────────────

def test_worker_runs_memory_after_sync_and_survives_its_errors(clock):
    llm = FakeLlm(*[_answer(summary="ok")] * 50)
    inbox = _inbox(clock, llm=llm)
    worker = SyncWorker(inbox)
    worker.run_once()
    clock.advance(SETTLE_SECONDS + 1)
    worker.run_once()
    assert llm.prompts
    inbox.context.process_once = lambda: (_ for _ in ()).throw(RuntimeError("broken memory"))
    assert worker.run_once() > 60  # the sync still ran
    assert "broken memory" in inbox.store().get_meta("context_state")["problems"][0]


def test_hermes_llm_adapter():
    import unified_inbox

    seen = {}

    class Result:
        parsed = {"summary": "s"}

    class Llm:
        def complete_structured(self, **kw):
            seen.update(kw)
            return Result()

    class Ctx:
        llm = Llm()
        tasks = []

        def register_auxiliary_task(self, name, **kw):
            self.tasks.append(name)

    ctx = Ctx()
    call = unified_inbox._make_llm(ctx)
    assert call("do it", "text here", {"type": "object"}) == {"summary": "s"}
    assert seen["input"] == [{"type": "text", "text": "text here"}] and seen["task"] == unified_inbox.MEMORY_TASK
    assert ctx.tasks == [unified_inbox.MEMORY_TASK]
    assert unified_inbox._make_llm(object()) is None


def test_v3_database_gains_memory_tables():
    conn = sqlite3.connect(":memory:")
    store = Store(conn)
    conn.execute("PRAGMA user_version=3")
    conn.execute("DROP TABLE facts")
    Store(conn)
    assert store._has_table("facts") and conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_secrets_never_reach_the_model_or_the_embedding_provider(clock):
    llm = FakeLlm()
    inbox = _inbox(clock, llm=llm, emb=True, config={"memory_chats_per_cycle": 20})
    _ready(inbox, clock)
    inbox.backend.receive("wa-mum", "Beta OTP 482913 hai, aur card 4111 1111 1111 1111")
    SyncEngine(inbox).sync_once()
    clock.advance(SETTLE_SECONDS + 1)
    while inbox.context.process_once()["memorized"]:
        pass
    sent = " ".join(llm.prompts) + json.dumps([c[2] for c in inbox.provider.calls])
    assert "482913" not in sent and "4111 1111" not in sent and "[code]" in sent
    assert inbox.store().search_messages("482913")  # the inbox itself still shows the message
