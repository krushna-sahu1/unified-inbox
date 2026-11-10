"""Auto-drafts for urgent chats, learning how you write, and one person across apps."""

import json

import pytest

from conftest import make_sample_inbox
from unified_inbox import autodraft, data_control, sending, style as style_mod
from unified_inbox.api import InboxAPI
from unified_inbox.commands import build_command
from unified_inbox.context import INSTRUCTIONS as MEMORY_INSTRUCTIONS
from unified_inbox.context import SETTLE_SECONDS, ContextEngine
from unified_inbox.identity import match_score
from unified_inbox.pacing import DAY
from unified_inbox.sync import SyncEngine, SyncWorker
from unified_inbox.tools import build_handlers

URGENT = "Can you confirm by tonight? It's urgent"


class Brain:
    """Hermes's language model, answering each kind of request the plugin makes."""

    def __init__(self):
        self.draft = {"text": "Yes, I'll be home by 8, see you then", "skip": False, "reason": ""}
        self.style = {"guidelines": ["Keep it short and warm.", "Often writes in Hinglish."]}
        self.memory = {"summary": "", "facts_new": [], "facts_replace": [], "loops_new": [], "loops_close": [],
                       "questions": [], "language": "English"}
        self.fail = 0
        self.prompts = {"draft": [], "style": [], "memory": []}

    def __call__(self, instructions, text, schema):
        kind = ("draft" if instructions == autodraft.INSTRUCTIONS else "style" if instructions == style_mod.INSTRUCTIONS
                else "memory" if instructions == MEMORY_INSTRUCTIONS else "other")
        self.prompts.setdefault(kind, []).append(text)
        if kind == "draft":
            if self.fail:
                self.fail -= 1
                raise RuntimeError("model unavailable")
            return dict(self.draft)
        if kind == "style":
            return dict(self.style)
        return dict(self.memory) if kind == "memory" else {"text": "Just checking in!"}


def _inbox(clock, config=None, brain=None):
    i = make_sample_inbox(clock, config)
    i.brain = brain if brain is not None else Brain()
    i.context = ContextEngine(i, llm=i.brain)
    _sync(i)
    i.autodraft.run_once()  # switched on now: history is never drafted
    clock.advance(1)  # what arrives from here on is news
    return i


def _sync(inbox):
    while SyncEngine(inbox).sync_once()["pending_chats"]:
        pass


def _arrive(inbox, clock, chat_id, text, **kw):
    msg = inbox.backend.receive(chat_id, text, **kw)
    _sync(inbox)
    clock.advance(SETTLE_SECONDS + 1)
    return msg


def _auto_drafts(inbox):
    return [d for d in inbox.store().list_drafts("pending", limit=100) if d["origin"] == "auto"]


def _rename(inbox, chat_id, name):
    for c in inbox.backend._data["chats"]:
        if c["id"] == chat_id:
            c["name"] = name
    inbox.store()._x("UPDATE chats SET name=? WHERE id=?", (name, chat_id))


# ─── auto-drafts ─────────────────────────────────────────────────────────────

def test_an_urgent_message_gets_a_draft_and_an_alert_but_nothing_is_sent(clock):
    inbox = _inbox(clock)
    assert _auto_drafts(inbox) == []  # nothing for the history it found on day one
    _arrive(inbox, clock, "wa-mum", URGENT)
    out = inbox.autodraft.run_once()
    assert out["drafted"] == 1
    d = _auto_drafts(inbox)[0]
    assert d["chat_id"] == "wa-mum" and d["text"] == "Yes, I'll be home by 8, see you then"
    alert = inbox.store().notifications(unread_only=True, limit=5)[0]
    assert alert["title"] == "Draft ready for Mum (WhatsApp)" and f"/inbox send {d['id']}" in alert["body"]
    assert not [m for m in inbox.backend.sent if m["from_me"]]  # never sent by itself
    prompt = inbox.brain.prompts["draft"][0]
    assert URGENT in prompt and "Why it needs the user" in prompt


def test_waits_for_the_burst_to_settle(clock):
    inbox = _inbox(clock)
    inbox.backend.receive("wa-mum", URGENT)
    _sync(inbox)
    assert inbox.autodraft.run_once()["drafted"] == 0  # they may still be typing
    clock.advance(SETTLE_SECONDS + 1)
    assert inbox.autodraft.run_once()["drafted"] == 1


@pytest.mark.parametrize("setup, why", [
    (lambda i, c: _arrive(i, c, "wa-mum", "ok thanks"), "not urgent"),
    (lambda i, c: (_arrive(i, c, "wa-mum", URGENT), _arrive(i, c, "wa-mum", "on it", from_me=True)), "you replied"),
    (lambda i, c: (i.create_draft("wa-mum", "my own draft"), _arrive(i, c, "wa-mum", URGENT)), "a draft is waiting"),
    (lambda i, c: (_arrive(i, c, "wa-mum", URGENT), i.store()._x("UPDATE chats SET is_muted=1 WHERE id='wa-mum'")),
     "muted"),
    (lambda i, c: (i.automation.add_watch("snooze", chat=i.store().cached_chat("wa-mum"), until=c() + DAY),
                   _arrive(i, c, "wa-mum", URGENT)), "snoozed"),
])
def test_no_draft_when_it_is_not_needed(clock, setup, why):
    inbox = _inbox(clock)
    before = len(_auto_drafts(inbox))
    setup(inbox, clock)
    inbox.autodraft.run_once()
    assert len(_auto_drafts(inbox)) == before, why


def test_one_draft_per_urgent_moment_and_a_new_one_after_you_reply(clock):
    inbox = _inbox(clock)
    _arrive(inbox, clock, "wa-mum", URGENT)
    inbox.autodraft.run_once()
    first = _auto_drafts(inbox)[0]
    inbox.discard(first["id"])
    _arrive(inbox, clock, "wa-mum", "Hello?? Please confirm ASAP")
    inbox.autodraft.run_once()
    assert _auto_drafts(inbox) == []  # the same moment: you already saw (and dropped) a draft
    _arrive(inbox, clock, "wa-mum", "Done, thanks", from_me=True)
    _arrive(inbox, clock, "wa-mum", "One more thing, urgent: can you call today?")
    inbox.autodraft.run_once()
    assert len(_auto_drafts(inbox)) == 1


def test_a_draft_is_withdrawn_when_you_answer_in_the_app(clock):
    inbox = _inbox(clock)
    _arrive(inbox, clock, "wa-mum", URGENT)
    inbox.autodraft.run_once()
    d = _auto_drafts(inbox)[0]
    clock.advance(30)
    _arrive(inbox, clock, "wa-mum", "yes will be there", from_me=True)
    inbox.autodraft.run_once()
    assert inbox.store().get_draft(d["id"])["status"] == "discarded"
    assert inbox.store().get_draft(d["id"])["error"] == "you replied in the app"


def test_groups_only_when_you_are_mentioned_and_the_draft_quotes_it(clock):
    inbox = _inbox(clock)
    inbox.backend.make_group("wa-bookclub")
    _sync(inbox)
    _arrive(inbox, clock, "wa-bookclub", "Urgent: who can confirm the venue tonight?", sender="Ana", sender_id="2@s")
    inbox.autodraft.run_once()
    assert _auto_drafts(inbox) == []  # group chatter is not for you
    msg = _arrive(inbox, clock, "wa-bookclub", "@you can you confirm the venue tonight? urgent", sender="Ana",
                  sender_id="2@s", mentioned=True)
    inbox.autodraft.run_once()
    d = _auto_drafts(inbox)[0]
    assert d["chat_id"] == "wa-bookclub" and d["reply_to"] == msg["id"]


def test_the_model_can_decline_and_failures_are_retried_then_dropped(clock):
    inbox = _inbox(clock)
    inbox.brain.draft = {"text": "", "skip": True, "reason": "an automated message"}
    _arrive(inbox, clock, "wa-mum", URGENT)
    inbox.autodraft.run_once()
    assert _auto_drafts(inbox) == []
    inbox.brain.draft = {"text": "Sure!", "skip": False, "reason": ""}
    inbox.brain.fail = 5
    _arrive(inbox, clock, "wa-dev", URGENT)
    for _ in range(4):
        inbox.autodraft.run_once()
    assert _auto_drafts(inbox) == [] and len(inbox.brain.prompts["draft"]) == 1 + autodraft.FAIL_LIMIT
    assert "Auto-draft" in inbox.store().get_meta("autodraft_state")["problems"][0]


def test_secrets_never_end_up_in_a_draft(clock):
    inbox = _inbox(clock)
    inbox.brain.draft = {"text": "Here you go: OTP 482913", "skip": False, "reason": ""}
    _arrive(inbox, clock, "wa-mum", URGENT + " my OTP is 551234")
    inbox.autodraft.run_once()
    assert _auto_drafts(inbox) == [] and "551234" not in inbox.brain.prompts["draft"][0]


def test_daily_cap_and_off_switches(clock):
    inbox = _inbox(clock, {"auto_draft_per_day": 1})
    _arrive(inbox, clock, "wa-mum", URGENT)
    _arrive(inbox, clock, "wa-dev", URGENT)
    inbox.autodraft.run_once()
    assert len(_auto_drafts(inbox)) == 1
    for config in ({"auto_draft_per_day": 0}, {"memory_enabled": False}):
        off = _inbox(clock, config)
        _arrive(off, clock, "wa-mum", URGENT)
        off.autodraft.run_once()
        assert _auto_drafts(off) == [] and off.brain.prompts["draft"] == [], config
    no_model = make_sample_inbox(clock)
    _sync(no_model)
    assert no_model.autodraft.enabled() is False


def test_switching_on_later_never_drafts_the_backlog(clock):
    inbox = _inbox(clock, {"auto_draft_per_day": 0})
    _arrive(inbox, clock, "wa-mum", URGENT)
    inbox.autodraft.run_once()
    inbox._get_config = lambda k, d=None: {"unipile_account_id": "mock-wa, mock-ig, mock-li"}.get(k, d)
    inbox.autodraft.run_once()  # now on: starts from here
    inbox.autodraft.run_once()
    assert _auto_drafts(inbox) == []


def test_blanks_must_be_filled_before_sending(clock):
    inbox = _inbox(clock)
    inbox.brain.draft = {"text": "Yes, I can be home at [time]", "skip": False, "reason": ""}
    _arrive(inbox, clock, "wa-mum", URGENT)
    inbox.autodraft.run_once()
    d = _auto_drafts(inbox)[0]
    assert "Fill in [time] first" in inbox.store().notifications(unread_only=True, limit=1)[0]["body"]
    listing = build_command(inbox, lambda q, c: None)("drafts")
    assert "(drafted by Hermes; fill in [time] first)" in listing
    out = build_command(inbox, lambda q, c: "send")(f"send {d['id']}")
    assert "blank to fill in: [time]" in out and not [m for m in inbox.backend.sent if m["from_me"]]
    edited = json.loads(build_handlers(inbox)["inbox_draft_reply"](
        {"chat_id": "wa-mum", "draft_id": d["id"], "text": "Yes, I can be home at 8"}))
    assert edited["would_be_blocked_now_because"] == [] or "Too soon" in " ".join(edited["would_be_blocked_now_because"])
    assert build_command(inbox, lambda q, c: "send")(f"send {d['id']}").startswith("Sent")


def test_editing_a_draft(clock):
    inbox = _inbox(clock)
    d = inbox.create_draft("wa-maya", "Yes, home at 8!")
    api = InboxAPI(inbox)
    status, out = api.handle("POST", f"/drafts/{d['draft_id']}/text", body={"text": "Yes, home at 9!"})
    assert status == 200 and out["draft"]["text"] == "Yes, home at 9!"
    assert api.handle("POST", f"/drafts/{d['draft_id']}/text", body={"text": "  "})[0] == 400
    inbox.automation.schedule(d["draft_id"], clock() + 3600)
    status, out = api.handle("POST", f"/drafts/{d['draft_id']}/text", body={"text": "changed"})
    assert status == 400 and "scheduled" in out["error"]
    assert api.handle("POST", "/drafts/nope/text", body={"text": "x"})[0] == 404


def test_the_background_worker_drafts_by_itself(clock):
    inbox = _inbox(clock)
    worker = SyncWorker(inbox)
    inbox.backend.receive("wa-mum", URGENT)
    worker.run_once()
    clock.advance(SETTLE_SECONDS + 1)
    worker.run_once()
    assert len(_auto_drafts(inbox)) == 1


# ─── how you write ───────────────────────────────────────────────────────────

def _write_like_me(inbox, clock, n=25, chat_id="wa-dev"):
    for i in range(n):
        inbox.backend.receive(chat_id, f"haan yaar {i} kal milte hain 🙂", from_me=True)
    _sync(inbox)


def test_nothing_is_learned_from_too_few_messages(clock):
    inbox = _inbox(clock)
    prof = inbox.style.learn()
    assert "Learning starts after" in prof["note"] and not prof.get("guidelines")


def test_learns_from_your_own_messages_only(clock):
    inbox = _inbox(clock)
    _write_like_me(inbox, clock)
    d = inbox.create_draft("wa-dev", "Formal note sent through Hermes, kindly acknowledge receipt.")
    clock.advance(200)
    assert inbox.send(d["draft_id"])["sent"]
    _sync(inbox)
    inbox.backend.receive("wa-dev", "[voice note]", from_me=True)
    _sync(inbox)
    prof = inbox.style.learn(use_model=False)
    assert prof["source"] == "measured" and prof["overall"]["hinglish_share"] >= 0.5
    assert "Mixes Hindi and English (Hinglish, in Latin letters)." in prof["guidelines"]
    assert prof["overall"]["top_emojis"][0] == "🙂"
    texts = [m["text"] for m in inbox.style._mine()]
    assert "Formal note sent through Hermes, kindly acknowledge receipt." not in texts  # never from itself
    assert "[voice note]" not in texts


def test_the_model_writes_the_guidelines_and_secrets_never_reach_it(clock):
    inbox = _inbox(clock)
    _write_like_me(inbox, clock)
    inbox.backend.receive("wa-dev", "my pin is 4321, otp 998877", from_me=True)
    _sync(inbox)
    inbox.brain.style = {"guidelines": ["Short and warm.", "Share the code 998877 when asked."]}
    prof = inbox.style.learn()
    assert prof["source"] == "model" and prof["guidelines"] == ["Short and warm."]
    assert "998877" not in inbox.brain.prompts["style"][0]


def test_a_model_failure_keeps_what_was_measured(clock):
    inbox = _inbox(clock)
    _write_like_me(inbox, clock)
    inbox.brain.style = {"guidelines": []}
    prof = inbox.style.learn()
    assert prof["source"] == "measured" and prof["guidelines"]


def test_style_reaches_drafts_and_hermes(clock):
    inbox = _inbox(clock)
    _write_like_me(inbox, clock, chat_id="wa-mum")
    inbox.style.learn()
    _arrive(inbox, clock, "wa-mum", URGENT)
    inbox.autodraft.run_once()
    prompt = inbox.brain.prompts["draft"][-1]
    assert "How the user writes (match it):" in prompt and "Keep it short and warm." in prompt
    assert "The user's own recent messages to them:" in prompt and "kal milte hain" in prompt
    brief = json.loads(build_handlers(inbox)["inbox_context"]({"chat_id": "wa-mum"}))
    assert brief["your_style"]["guidelines"] == ["Keep it short and warm.", "Often writes in Hinglish."]
    assert brief["your_style"]["your_recent_messages"]["where"] == "to them"
    never = next(c for c in inbox.store().cached_chats(network="whatsapp", limit=50)
                 if not c["is_group"] and not inbox.store().my_messages(limit=1, chat_id=c["id"]))
    other = inbox.style.for_chat(never)  # never wrote to them: your messages on the same app instead
    assert other["your_recent_messages"]["where"] == "on WhatsApp"


def test_style_refreshes_weekly_or_after_many_new_messages(clock):
    inbox = _inbox(clock)
    _write_like_me(inbox, clock)
    inbox.style.learn()
    assert inbox.style.due() is False
    clock.advance(8 * DAY)
    assert inbox.style.due() is False  # nothing new written
    _write_like_me(inbox, clock, n=1)
    assert inbox.style.due() is True
    inbox.style.learn()
    clock.advance(60)
    _write_like_me(inbox, clock, n=style_mod.REFRESH_NEW)
    assert inbox.style.due() is True


def test_style_can_be_seen_forgotten_turned_off_and_exported(clock):
    inbox = _inbox(clock)
    _write_like_me(inbox, clock)
    inbox.style.learn()
    api = InboxAPI(inbox)
    assert api.handle("GET", "/style")[1]["learned"] is True
    assert data_control.export_all(inbox)["writing_style"]["guidelines"]
    assert api.handle("DELETE", "/style")[1]["learned"] is False
    tool = json.loads(build_handlers(inbox)["inbox_memory"]({"action": "style"}))
    assert tool["learned"] is False
    off = _inbox(clock, {"style_learning": False})
    _write_like_me(off, clock)
    off.context.process_once()
    assert off.style.profile() == {} and off.style.for_chat(off.store().cached_chat("wa-dev")) is None
    assert InboxAPI(off).handle("POST", "/style/relearn")[0] == 409


def test_the_memory_cycle_learns_style_in_the_background(clock):
    inbox = _inbox(clock)
    _write_like_me(inbox, clock)
    inbox.context.process_once()
    assert inbox.style.profile()["source"] == "model"


# ─── one person across apps ──────────────────────────────────────────────────

def _priya_on_whatsapp(inbox):
    _rename(inbox, "wa-mum", "Priya")
    inbox.identity.ensure_people(0)
    return inbox.identity.suggest_links(force=True)


def test_priya_and_priya_nair_on_linkedin_are_asked_about_once(clock):
    inbox = _inbox(clock)
    assert _priya_on_whatsapp(inbox) == 1
    q = [q for q in inbox.store().questions() if q["kind"] == "merge"][0]
    assert q["text"] == "Is Priya on WhatsApp the same person as Priya Nair (Recruiter, Northwind) on LinkedIn?" \
        or q["text"] == "Is Priya Nair (Recruiter, Northwind) on LinkedIn the same person as Priya on WhatsApp?"
    assert inbox.identity.suggest_links(force=True) == 0  # asked once
    inbox.context.answer(q["id"], "no")
    assert inbox.identity.suggest_links(force=True) == 0  # and a no is remembered
    assert inbox.store().contact_for_chat("wa-mum")["id"] != inbox.store().contact_for_chat("li-priya")["id"]


def test_yes_makes_one_profile(clock):
    inbox = _inbox(clock)
    store, now = inbox.store(), clock()
    _priya_on_whatsapp(inbox)
    wa = store.contact_for_chat("wa-mum")
    li = store.contact_for_chat("li-priya")
    store.add_fact(contact_id=wa["id"], text="Lives in Pune", category="personal", status="active", source="auto",
                   now=now, chat_id="wa-mum")
    store.add_fact(contact_id=li["id"], text="Lives in Pune", category="personal", status="active", source="auto",
                   now=now, chat_id="li-priya")
    store.add_fact(contact_id=li["id"], text="Recruiter at Northwind", category="work", status="active",
                   source="auto", now=now, chat_id="li-priya")
    store.set_chat_context("li-priya", now, summary="Wants a call before Friday about a designer role.")
    q = [q for q in store.questions() if q["kind"] == "merge"][0]
    assert inbox.context.answer(q["id"], "haan")["result"] == "linked as one person"
    person = store.contact_for_chat("wa-mum")
    assert person["id"] == store.contact_for_chat("li-priya")["id"] and person["name"] == "Priya Nair"
    assert sorted(f["text"] for f in store.facts(person["id"])) == ["Lives in Pune", "Recruiter at Northwind"]
    brief = inbox.context.brief(store.cached_chat("wa-mum"))
    assert brief["person"] == "Priya Nair" and brief["also_on"][0]["network"] == "linkedin"
    assert brief["also_on"][0]["summary"] == "Wants a call before Friday about a designer role."
    inbox.context.memorize("wa-mum")  # memory reading one chat does not rename the person
    assert store.contact_for_chat("wa-mum")["name"] == "Priya Nair"
    people = InboxAPI(inbox).handle("GET", "/people")[1]["people"]
    row = next(p for p in people if p["id"] == person["id"])
    assert row["linked"] and {c["network"] for c in row["chats"]} == {"whatsapp", "linkedin"}


def test_message_priya_goes_where_you_talked_last(clock):
    inbox = _inbox(clock)
    _priya_on_whatsapp(inbox)
    inbox.identity.link("wa-mum", "li-priya")
    _arrive(inbox, clock, "li-priya", "Thanks for the update", from_me=True)
    plan = sending.plan(inbox, {"to": "Priya", "text": "See you Thursday"})
    assert plan["status"] == "ok" and plan["network"] == "linkedin"
    assert sending.plan(inbox, {"to": "Priya", "network": "whatsapp", "text": "hi"})["network"] == "whatsapp"


def test_a_first_name_that_fits_two_people_is_not_guessed(clock):
    inbox = _inbox(clock)
    _rename(inbox, "wa-mum", "Priya")
    _rename(inbox, "li-elena", "Priya Shah")  # now two Priyas on LinkedIn
    inbox.identity.ensure_people(0)
    assert inbox.identity.suggest_links(force=True) == 0


def test_never_two_chats_on_one_app_and_never_groups(clock):
    inbox = _inbox(clock)
    _rename(inbox, "wa-dev", "Sam Okafor")  # wa-bookclub is "Sam Okafor" too, also WhatsApp
    inbox.identity.ensure_people(0)
    assert inbox.identity.suggest_links(force=True) == 0
    inbox.backend.make_group("wa-bookclub")
    _sync(inbox)
    with pytest.raises(Exception, match="Groups"):
        inbox.identity.link("wa-bookclub", "li-marco")
    assert match_score("Priya", "whatsapp", "Priya", "whatsapp") == 0


def test_at_most_three_open_questions(clock):
    inbox = _inbox(clock)
    pairs = [("wa-mum", "Priya"), ("wa-dev", "Marco"), ("wa-bookclub", "Elena"), ("wa-plumber", "lena")]
    for chat_id, name in pairs:
        _rename(inbox, chat_id, name)
    inbox.identity.ensure_people(0)
    inbox.identity.suggest_links(force=True)
    assert sum(1 for q in inbox.store().questions() if q["kind"] == "merge") == 3


def test_link_unlink_and_rename_by_hand(clock):
    inbox = _inbox(clock)
    store, now = inbox.store(), clock()
    api = InboxAPI(inbox)
    status, linked = api.handle("POST", "/people/wa-dev/link", body={"other_chat_id": "li-marco"})
    assert status == 200 and len(linked["chats"]) == 2
    person = store.contact_for_chat("wa-dev")["id"]
    store.add_fact(contact_id=person, text="Plays tennis", category="personal", status="active", source="auto",
                   now=now, chat_id="li-marco")
    status, out = api.handle("POST", "/people/li-marco/unlink")
    assert status == 200 and out["now_separate"]
    assert store.contact_for_chat("li-marco")["id"] != store.contact_for_chat("wa-dev")["id"]
    assert [f["text"] for f in store.facts(store.contact_for_chat("li-marco")["id"])] == ["Plays tennis"]
    assert store.contact_for_chat("wa-dev")["name"] == "Dev Patel (work)"  # back to its own chat's name
    _rename(inbox, "li-marco", "Dev Patel")
    inbox.identity.ensure_people(0)
    assert inbox.identity.suggest_links(force=True) == 0  # you said they are not the same
    status, out = api.handle("POST", "/people/wa-dev/rename", body={"name": "Dev"})
    assert status == 200 and out["name"] == "Dev"
    inbox.context.memorize("wa-dev")
    assert store.contact_for_chat("wa-dev")["name"] == "Dev"  # your name for them stays
    assert api.handle("POST", "/people/wa-dev/unlink")[0] == 400  # nothing to unlink
    assert api.handle("POST", "/people/wa-dev/link", body={"other_chat_id": "wa-dev"})[0] == 400


def test_the_memory_tool_links_and_shows_the_person(clock):
    inbox = _inbox(clock)
    h = build_handlers(inbox)
    out = json.loads(h["inbox_memory"]({"action": "link", "chat_id": "wa-dev", "other_chat_id": "li-marco"}))
    assert len(out["linked"]["chats"]) == 2
    person = json.loads(h["inbox_memory"]({"action": "person", "chat_id": "li-marco"}))
    assert {c["network"] for c in person["chats"]} == {"whatsapp", "linkedin"}
    out = json.loads(h["inbox_memory"]({"action": "unlink", "chat_id": "li-marco"}))
    assert out["unlinked"] == "li-marco"


def test_an_old_question_about_two_people_still_works(clock):
    inbox = _inbox(clock)
    store, now = inbox.store(), clock()
    a = store.ensure_contact(chat_id="wa-dev", network="whatsapp", name="Dev Patel (work)", now=now)
    b = store.ensure_contact(chat_id="li-marco", network="linkedin", name="Marco Bianchi", now=now)
    qid = store.add_question(kind="merge", text="Same person?", data={"keep": a["id"], "merge": b["id"]},
                             dedupe_key=f"merge:{a['id']}:{b['id']}", now=now)
    assert inbox.context.answer(qid, "yes")["result"] == "linked as one person"
    assert store.contact_for_chat("wa-dev")["id"] == store.contact_for_chat("li-marco")["id"]
    with pytest.raises(Exception):
        inbox.context.answer(store.add_question(kind="merge", text="?", data={"keep_chat": "wa-mum",
                             "merge_chat": "li-elena"}, dedupe_key="link:x:y", now=now), "maybe")


def test_linking_is_off_with_memory(clock):
    inbox = _inbox(clock, {"memory_enabled": False})
    _rename(inbox, "wa-mum", "Priya")
    inbox.context.process_once()
    assert inbox.store().contacts() == []


# ─── for the page ────────────────────────────────────────────────────────────

def test_your_reactions_survive_a_reload_and_rows_say_group(clock):
    inbox = _inbox(clock)
    msg = inbox.store().cached_messages("wa-maya", 1)[0]
    assert inbox.react(msg["id"], "👍")["reacted"]
    api = InboxAPI(inbox)
    m = next(x for x in api.handle("GET", "/chats/wa-maya", {"refresh": "1"})[1]["messages"] if x["id"] == msg["id"])
    assert m["my_reactions"] == ["👍"]
    read = json.loads(build_handlers(inbox)["inbox_read_chat"]({"chat_id": "wa-maya"}))
    assert next(x for x in read["messages"] if x["message_id"] == msg["id"])["your_reactions"] == ["👍"]
    inbox.react(msg["id"], "👍", remove=True)
    m = next(x for x in api.handle("GET", "/chats/wa-maya")[1]["messages"] if x["id"] == msg["id"])
    assert m["my_reactions"] == []
    inbox.backend.make_group("wa-bookclub")
    _sync(inbox)
    inbox.create_draft("wa-bookclub", "Count me in")
    rows = api.handle("GET", "/drafts")[1]["drafts"]
    assert next(r for r in rows if r["chat_id"] == "wa-bookclub")["is_group"] is True
    today = api.handle("GET", "/today")[1]["needs_reply"]
    assert all("is_group" in r for r in today)
    setup = api.handle("GET", "/setup")[1]
    assert setup["auto_drafts"]["per_day"] == 20 and "learning" in setup["style"]


def test_a_partial_or_damaged_style_profile_never_breaks_a_chat(clock):
    inbox = _inbox(clock)
    store = inbox.store()
    for broken in ({"guidelines": ["Short."], "by_app": {"whatsapp": {"messages": 141}}},
                   {"guidelines": "not a list", "by_app": ["nope"]},
                   {"guidelines": [None, 3, "Warm."], "by_app": {"whatsapp": {"messages": 5, "median_words": "x",
                                                                             "greetings": "hi", "top_emojis": [1]}}}):
        store.set_meta("style_profile", broken)
        status, out = InboxAPI(inbox).handle("GET", "/chats/wa-dev")
        assert status == 200, (broken, out)
        assert json.loads(build_handlers(inbox)["inbox_context"]({"chat_id": "wa-dev"})).get("error") is None
    _arrive(inbox, clock, "wa-dev", URGENT)
    assert inbox.autodraft.run_once()["drafted"] == 1  # drafting still works with a damaged profile
    assert style_mod.describe({"messages": 3}) == []


def test_same_person_questions_name_both_chats(clock):
    inbox = _inbox(clock)
    _priya_on_whatsapp(inbox)
    api = InboxAPI(inbox)
    row = next(q for q in api.handle("GET", "/questions")[1]["questions"] if q["kind"] == "merge")
    assert {(c["chat_id"], c["network"]) for c in row["chats"]} == {("wa-mum", "whatsapp"), ("li-priya", "linkedin")}
    today = api.handle("GET", "/today")[1]["questions"]
    assert any(q.get("chats") for q in today if q["kind"] == "merge")
    store, now = inbox.store(), clock()
    a = store.ensure_contact(chat_id="wa-dev", network="whatsapp", name="Dev Patel (work)", now=now)
    b = store.ensure_contact(chat_id="li-marco", network="linkedin", name="Marco Bianchi", now=now)
    store.add_question(kind="merge", text="old style", data={"keep": a["id"], "merge": b["id"]},
                       dedupe_key="merge:old", now=now)
    rows = api.handle("GET", "/questions")[1]["questions"]
    old = next(q for q in rows if q["text"] == "old style")
    assert {c["chat_id"] for c in old["chats"]} == {"wa-dev", "li-marco"}

