"""Who is who across apps: the same person on WhatsApp, Instagram and LinkedIn as one profile.

Linking is never automatic. ``suggest_links`` finds chats on different apps whose names point to
the same person and asks the user once ("Is Priya on WhatsApp the same person as Priya Nair on
LinkedIn?"). A "no" is remembered for good. After a "yes" (or a link made by hand) the person is
one profile: one name, the facts from every chat (duplicates folded together), and every chat's
summary and open items in one brief. A wrong link can be undone (``unlink``), and that pair is
then never suggested again.

Names are compared after cleaning: titles in brackets ("Priya Nair (Recruiter, Northwind)"),
taglines after a dash or bar, honorifics and accents go; an Instagram handle ("lena.makes",
"priya_nair92") is split into words. A full name (two or more words) matching is a strong sign;
a first name alone ("Priya" vs "Priya Nair") is asked only when it points to exactly one person on
that app. Two chats on the same app, and groups, are never linked.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .client import InboxError
from .pacing import NETWORK_LABELS

ASK_SCORE = 0.6           # at least this sure before asking
STRONG_SCORE = 0.85       # a full name: asked even if another weaker match exists
MAX_OPEN_LINK_QUESTIONS = 3  # never flood the user with "same person?" questions
CHECK_EVERY = 600.0       # seconds between full passes (sooner when a new person appears)

_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_TAGLINE = re.compile(r"\s+[-|•·–—:,]\s+.*$")
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "er", "prof", "sir", "madam", "shri", "smt", "sri", "ji", "jr", "sr"}
_WORD = re.compile(r"[^\W\d_]+")


def clean_name(name: str) -> str:
    """A person's name for display: "Priya Nair (Recruiter, Northwind)" -> "Priya Nair"."""
    raw = " ".join(str(name or "").split())
    cleaned = _TAGLINE.sub("", _BRACKETS.sub(" ", raw))
    cleaned = " ".join(cleaned.split()).strip(" -|•·,:")
    return cleaned or raw


def name_tokens(name: str, *, handle: bool = False) -> list[str]:
    """Comparable words of a name: no accents, no honorifics, lower case. A handle
    ("lena.makes", "priya_nair92") is split at dots, underscores and digits."""
    text = clean_name(name)
    if handle or (" " not in text and re.search(r"[._]", text)):
        text = re.sub(r"[._\d]+", " ", text)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).casefold()
    return [w for w in _WORD.findall(text) if w not in _HONORIFICS and len(w) >= 2]


def match_score(a_name: str, a_network: str, b_name: str, b_network: str) -> float:
    """How strongly two chat names point to the same person (0 = not at all)."""
    if a_network == b_network:
        return 0.0
    ta = name_tokens(a_name, handle=a_network == "instagram")
    tb = name_tokens(b_name, handle=b_network == "instagram")
    if not ta or not tb:
        return 0.0
    if ta == tb:
        if len(ta) >= 2:
            return 0.95
        return 0.6 if len(ta[0]) >= 4 else 0.0  # "Rahul" and "Rahul" (but not "Mum" and "Mum")
    sa, sb = set(ta), set(tb)
    if len(sa) >= 2 and len(sb) >= 2 and (sa <= sb or sb <= sa):
        return 0.85  # "Priya Nair" and "Priya Nair Sharma"
    if ta[0] == tb[0]:
        if (len(ta) == 1 or len(tb) == 1) and len(ta[0]) >= 3:
            return 0.6  # "Priya" and "Priya Nair"
        return 0.0  # same first name, different surnames: two people
    glued_a, glued_b = "".join(ta), "".join(tb)
    if (len(tb) >= 2 and len(glued_b) >= 6 and glued_b in glued_a) or \
            (len(ta) >= 2 and len(glued_a) >= 6 and glued_a in glued_b):
        return 0.7  # the handle "priyanair" and the name "Priya Nair"
    return 0.0


def pair_key(chat_a: str, chat_b: str) -> str:
    a, b = sorted([chat_a, chat_b])
    return f"link:{a}:{b}"


class Identity:
    def __init__(self, inbox):
        self.inbox = inbox

    # ── finding the same person ──
    def ensure_people(self, since: float) -> int:
        """Every one-to-one chat active since ``since`` gets its person (contact). Returns how many
        were new."""
        store, now = self.inbox.store(), self.inbox.clock()
        known = {c["chat_id"] for p in store.contacts() for c in p["chats"]}
        created = 0
        for chat in store.cached_chats(limit=5000):
            if chat["is_group"] or chat["id"] in known or chat["ts"] < since or not chat["name"]:
                continue
            store.ensure_contact(chat_id=chat["id"], network=chat["network"], name=chat["name"], now=now)
            created += 1
        return created

    def suggest_links(self, *, force: bool = False) -> int:
        """Ask about chats on different apps that look like the same person. Returns the number
        of new questions."""
        store, now = self.inbox.store(), self.inbox.clock()
        if not force and now - float(store.get_meta("identity_checked_at", 0.0) or 0.0) < CHECK_EVERY:
            return 0
        store.set_meta("identity_checked_at", now)
        open_now = sum(1 for q in store.questions(limit=1000) if q["kind"] == "merge")
        if open_now >= MAX_OPEN_LINK_QUESTIONS:
            return 0
        people = store.contacts()
        chats = {c["id"]: c for c in store.cached_chats(limit=5000)}
        entries = []  # (person, chat) for every one-to-one chat with a name
        for p in people:
            for c in p["chats"]:
                chat = chats.get(c["chat_id"])
                if chat and not chat["is_group"] and chat["name"]:
                    entries.append((p, chat))
        by_first: dict[str, list] = {}
        for p, chat in entries:
            tokens = name_tokens(chat["name"], handle=chat["network"] == "instagram")
            for t in tokens[:2]:  # a handle's or a name's first two words
                by_first.setdefault(t, []).append((p, chat))
        asked = 0
        seen: set = set()
        for p, chat in sorted(entries, key=lambda e: -e[1]["ts"]):  # people you talk to most recently first
            tokens = name_tokens(chat["name"], handle=chat["network"] == "instagram")
            candidates: dict[str, list] = {}  # other app -> [(score, person, chat)]
            for t in tokens[:2]:
                for q, other in by_first.get(t, []):
                    if q["id"] == p["id"] or other["network"] == chat["network"]:
                        continue
                    if other["network"] in {c["network"] for c in p["chats"]}:
                        continue  # this person already has a chat on that app
                    score = match_score(chat["name"], chat["network"], other["name"], other["network"])
                    if score >= ASK_SCORE:
                        candidates.setdefault(other["network"], []).append((score, q, other))
            for network, found in candidates.items():
                found = list({o["id"]: (s, q, o) for s, q, o in found}.values())
                found.sort(key=lambda x: -x[0])
                best = found[0]
                if best[0] < STRONG_SCORE and len([f for f in found if f[0] >= best[0]]) > 1:
                    continue  # "Priya" could be either of two Priyas there: do not guess
                score, q, other = best
                if score < STRONG_SCORE and self._matches_on(by_first, other, chat["network"], score) > 1:
                    continue  # ...and the other way round: two Priyas here could each be that one
                key = pair_key(chat["id"], other["id"])
                if key in seen or self._asked(store, key, p["id"], q["id"]):
                    continue
                seen.add(key)
                if store.add_question(
                        kind="merge",
                        text=f"Is {chat['name']} on {NETWORK_LABELS.get(chat['network'], chat['network'])} the same"
                             f" person as {other['name']} on {NETWORK_LABELS.get(network, network)}?",
                        data={"keep": p["id"], "merge": q["id"], "keep_chat": chat["id"], "merge_chat": other["id"]},
                        dedupe_key=key, now=now):
                    asked += 1
                    if open_now + asked >= MAX_OPEN_LINK_QUESTIONS:
                        return asked
        return asked

    @staticmethod
    def _matches_on(by_first: dict, chat: dict, network: str, score: float) -> int:
        """How many chats on ``network`` match ``chat`` at least this well."""
        tokens = name_tokens(chat["name"], handle=chat["network"] == "instagram")
        ids = {other["id"] for t in tokens[:2] for _, other in by_first.get(t, [])
               if other["network"] == network
               and match_score(chat["name"], chat["network"], other["name"], other["network"]) >= score}
        return len(ids)

    @staticmethod
    def _asked(store, key: str, contact_a: str, contact_b: str) -> bool:
        """Asked before about this pair (also under the older, person-based question key)."""
        a, b = sorted([contact_a, contact_b])
        return store.question_exists(key) or store.question_exists(f"merge:{a}:{b}")

    # ── one profile ──
    def link(self, chat_id: str, other_chat_id: str) -> dict:
        """Make two chats one person (by hand, or after you said yes)."""
        store, now = self.inbox.store(), self.inbox.clock()
        if chat_id == other_chat_id:
            raise InboxError("Those are the same chat.", kind="input")
        chats = [store.cached_chat(chat_id), store.cached_chat(other_chat_id)]
        if not all(chats):
            raise InboxError("One of those chats is not in the local copy.", kind="not_found")
        if any(c["is_group"] for c in chats):
            raise InboxError("Groups can't be linked to a person.", kind="input")
        a = store.ensure_contact(chat_id=chats[0]["id"], network=chats[0]["network"], name=chats[0]["name"], now=now)
        b = store.ensure_contact(chat_id=chats[1]["id"], network=chats[1]["network"], name=chats[1]["name"], now=now)
        if a["id"] == b["id"]:
            return self.profile(a["id"])
        keep, gone = (a, b) if a["id"] <= b["id"] else (b, a)
        user_named = [c for c in (keep, gone) if store.contact_name_source(c["id"]) == "user"]
        store.merge_contacts(keep["id"], gone["id"], now)
        name = user_named[0]["name"] if user_named else best_name([c["name"] for c in chats])
        store.set_contact_name(keep["id"], name, now, source="user" if user_named else "merged")
        self._fold_duplicate_facts(keep["id"], now)
        for q in store.questions(limit=1000):  # an open "same person?" question about this pair is settled
            d = q["data"]
            if q["kind"] == "merge" and {d.get("keep_chat"), d.get("merge_chat")} == {chat_id, other_chat_id}:
                store.close_question(q["id"], now, status="answered", answer="linked")
        return self.profile(keep["id"])

    def unlink(self, chat_id: str) -> dict:
        """Take a chat out of the person it was linked to. That pair is never suggested again."""
        store, now = self.inbox.store(), self.inbox.clock()
        chat = store.cached_chat(chat_id)
        contact = store.contact_for_chat(chat_id)
        if not chat or not contact:
            raise InboxError("That chat is not linked to anyone.", kind="not_found")
        before = store.contact(contact["id"]) or {"chats": []}
        others = [c["chat_id"] for c in before["chats"] if c["chat_id"] != chat_id]
        new_id = store.split_chat(chat_id, chat["name"], now)
        if new_id is None:
            raise InboxError("That chat is not linked to anyone else.", kind="input")
        for other in others:  # remember: not the same person
            key = pair_key(chat_id, other)
            qid = store.add_question(kind="merge", text="(unlinked by you)", data={"keep_chat": chat_id,
                                     "merge_chat": other}, dedupe_key=key, now=now)
            if qid:
                store.close_question(qid, now, status="answered", answer="no (unlinked)")
        remaining = store.contact(contact["id"])
        if remaining and store.contact_name_source(contact["id"]) == "merged":
            names = [c2["name"] for c2 in (store.cached_chat(c["chat_id"]) for c in remaining["chats"]) if c2]
            if len(remaining["chats"]) == 1:
                store.set_contact_name(contact["id"], names[0] if names else remaining["name"], now, source="chat")
            elif names:
                store.set_contact_name(contact["id"], best_name(names), now, source="merged")
        return {"unlinked": chat_id, "now_separate": True, "was_with": others}

    def rename(self, chat_id: str, name: str) -> dict:
        store, now = self.inbox.store(), self.inbox.clock()
        name = " ".join(str(name or "").split())[:80]
        if not name:
            raise InboxError("Give a name.", kind="input")
        chat = store.cached_chat(chat_id)
        if not chat or chat["is_group"]:
            raise InboxError("Pick a one-to-one chat.", kind="not_found" if not chat else "input")
        contact = store.ensure_contact(chat_id=chat_id, network=chat["network"], name=chat["name"], now=now)
        store.set_contact_name(contact["id"], name, now, source="user")
        return self.profile(contact["id"])

    def answer_link(self, question: dict, yes: bool) -> str:
        """The user's answer to "same person?"."""
        if not yes:
            return "kept separate"
        d = question["data"]
        store = self.inbox.store()
        a, b = d.get("keep_chat"), d.get("merge_chat")
        if not (a and b):  # a question from before chats were recorded: find a chat of each person
            ca, cb = store.contact(d.get("keep", "")), store.contact(d.get("merge", ""))
            if not ca or not cb or not ca["chats"] or not cb["chats"]:
                return "those chats are no longer here"
            a, b = ca["chats"][0]["chat_id"], cb["chats"][0]["chat_id"]
        if not store.cached_chat(a) or not store.cached_chat(b):
            return "those chats are no longer here"
        self.link(a, b)
        return "linked as one person"

    def _fold_duplicate_facts(self, contact_id: str, now: float) -> int:
        """After linking, the same fact learned on two apps is kept once (yours win over learned ones)."""
        from .context import _similar  # same notion of "the same fact" as memory uses

        store = self.inbox.store()
        facts = store.facts(contact_id, limit=500)
        facts.sort(key=lambda f: (f["source"] != "user", -f["updated_at"]))  # yours first, then newest
        kept: list[dict] = []
        folded = 0
        for f in facts:
            if any(_similar(f["text"], k["text"]) for k in kept):
                store.set_fact(f["id"], now, status="superseded")
                folded += 1
            else:
                kept.append(f)
        return folded

    def profile(self, contact_id: str) -> dict:
        """One person across apps: name, chats (newest activity first) and facts."""
        store = self.inbox.store()
        contact = store.contact(contact_id)
        if not contact:
            raise InboxError("No such person.", kind="not_found")
        chats = [c for c in (store.cached_chat(x["chat_id"]) for x in contact["chats"]) if c]
        chats.sort(key=lambda c: -c["ts"])
        return {"person_id": contact["id"], "name": contact["name"],
                "chats": [{"chat_id": c["id"], "network": c["network"], "name": c["name"], "last_activity": c["ts"],
                           "last_from_me": c["last_from_me"]} for c in chats],
                "facts": [{"id": f["id"], "fact": f["text"]} for f in store.facts(contact["id"], limit=50)]}


def best_name(names: list[str]) -> str:
    """The fullest real name among a person's chat names: "Priya Nair" over "Priya" or "priya_n"."""
    cleaned = [clean_name(n) for n in names if n]
    if not cleaned:
        return ""
    def rank(n: str) -> tuple:
        words = name_tokens(n)
        looks_like_handle = " " not in n and bool(re.search(r"[._\d]", n))
        return (not looks_like_handle, len(words), sum(map(len, words)))
    return max(cleaned, key=rank)


def link_view(question: dict) -> Optional[dict]:
    d = question.get("data") or {}
    if not d.get("keep_chat"):
        return None
    return {"chat_id": d["keep_chat"], "other_chat_id": d.get("merge_chat", "")}
