"""How you write: learned from your own sent messages, so drafts sound like you.

Two parts, both kept small:
  * Numbers measured locally, without any model: how long your messages are, how often you use
    emojis (and which), whether you start in lower case, how you greet and sign off, whether you
    mix Hindi and English, separately for each app (LinkedIn is rarely written like WhatsApp).
  * A few plain guidelines written by Hermes's language model from a sample of your messages
    (codes, passwords, card and account numbers removed first). Refreshed at most once a week, or
    after 50 new messages. Off when memory is off or ``style_learning`` is off.

Only messages you typed count: anything this plugin sent (drafts Hermes wrote, scheduled
messages) is left out, so Hermes never learns from itself. ``for_chat`` hands a draft writer the
guidelines for that app plus a few of your recent messages to that same person.
"""

from __future__ import annotations

import re
from collections import Counter
from statistics import median
from typing import Optional

from .pacing import DAY, NETWORK_LABELS
from .privacy import is_sensitive, redact

MIN_MESSAGES = 20          # fewer than this: nothing is learned yet
MIN_PER_APP = 10           # an app gets its own numbers from this many messages
REFRESH_DAYS = 7
REFRESH_NEW = 50
SAMPLE_FOR_MODEL = 60
EXAMPLES_PER_CHAT = 5
EXAMPLE_CHARS = 200

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF❤☺]")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINGLISH = {"hai", "hain", "nahi", "nahin", "kya", "haan", "acha", "accha", "theek", "thik", "kal", "bhai", "yaar",
             "ji", "kaise", "kab", "kyun", "kyu", "bas", "abhi", "matlab", "chalo", "arre", "bhi", "mujhe", "tum",
             "aap", "hum", "karo", "kar", "raha", "rahi", "gaya", "gayi", "wala", "wali", "sab", "jaldi", "pakka"}
_GREETINGS = ("hi", "hii", "hey", "hello", "hola", "yo", "namaste", "good morning", "gm", "dear", "heyy", "hiya")
_SIGNOFFS = ("thanks", "thank you", "thx", "ty", "cheers", "regards", "best regards", "take care", "bye", "tc",
             "see you", "talk soon", "shukriya", "dhanyavaad")
_ABBREVIATIONS = {"u", "ur", "pls", "plz", "thx", "k", "kk", "btw", "np", "idk", "tbh", "lol", "omg", "rn", "msg"}
_PLACEHOLDER_TEXT = re.compile(r"^\[(voice note|audio|photo|sticker|video|gif|file|attachment|contact|shared)[^\]]*\]$",
                               re.I)

INSTRUCTIONS = """You describe how one person writes their own chat messages, so an assistant can
write replies that sound exactly like them. You get measured numbers and a sample of their
messages (oldest topics mixed, some apps more formal than others). Write 3 to 7 short guidelines,
each one plain sentence an assistant can follow: length, tone, greetings and sign-offs, emoji and
punctuation habits, language mix (English, Hindi, Hinglish) and how it differs by app (WhatsApp,
Instagram, LinkedIn) when it clearly does. Describe habits, never facts about people or content
of the messages. No quotes of whole messages. Return JSON: guidelines (list of strings)."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["guidelines"],
          "properties": {"guidelines": {"type": "array", "items": {"type": "string"}}}}


def measure(texts: list[str]) -> dict:
    """Numbers about a list of messages (no model)."""
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return {"messages": 0}
    words = [len(t.split()) for t in texts]
    emojis = Counter(e for t in texts for e in _EMOJI.findall(t))
    lowered = [t for t in texts if t[:1].isalpha()]
    first = Counter()
    last = Counter()
    abbreviations = Counter()
    hinglish = 0
    for t in texts:
        low = t.casefold()
        for g in _GREETINGS:
            if low == g or low.startswith(g + " ") or low.startswith(g + ",") or low.startswith(g + "!"):
                first[g] += 1
                break
        for s in _SIGNOFFS:
            if low.rstrip(" .!🙂😊🙏").endswith(s):
                last[s] += 1
                break
        tokens = set(re.findall(r"[a-z]+", low))
        abbreviations.update(tokens & _ABBREVIATIONS)
        if len(tokens & _HINGLISH) >= 2 or (tokens & _HINGLISH and len(tokens) <= 4):
            hinglish += 1
    n = len(texts)
    return {
        "messages": n,
        "median_words": int(median(words)),
        "long_share": round(sum(1 for w in words if w > 40) / n, 2),
        "emoji_share": round(sum(1 for t in texts if _EMOJI.search(t)) / n, 2),
        "top_emojis": [e for e, _ in emojis.most_common(3)],
        "lowercase_start_share": round(sum(1 for t in lowered if t[0].islower()) / max(1, len(lowered)), 2),
        "ends_with_period_share": round(sum(1 for t in texts if t.endswith(".")) / n, 2),
        "exclamation_share": round(sum(1 for t in texts if "!" in t) / n, 2),
        "hinglish_share": round(hinglish / n, 2),
        "hindi_script_share": round(sum(1 for t in texts if _DEVANAGARI.search(t)) / n, 2),
        "greetings": [g for g, c in first.most_common(2) if c >= max(2, n * 0.05)],
        "signoffs": [s for s, c in last.most_common(2) if c >= max(2, n * 0.05)],
        "abbreviations": [a for a, c in abbreviations.most_common(4) if c >= 2],
    }


def describe(numbers: dict) -> list[str]:
    """The numbers as plain guidelines (used as they are when no model is available). Any number
    that is missing or not a number is simply left out: a partial or older profile never fails."""
    if not isinstance(numbers, dict) or not numbers.get("messages"):
        return []

    def num(key: str) -> Optional[float]:
        v = numbers.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def words(key: str) -> list[str]:
        v = numbers.get(key)
        return [str(x) for x in v if isinstance(x, str) and x] if isinstance(v, list) else []

    out = []
    w = num("median_words")
    if w is not None:
        w = int(w)
        out.append(f"Keep it short: usually about {w} words." if w <= 12 else
                   f"Usually writes full sentences, about {w} words." if w <= 30 else
                   f"Writes longer messages, often {w} words or more.")
    e = num("emoji_share")
    if e is not None and e >= 0.3:
        top = words("top_emojis")
        out.append("Uses emojis often" + (f" ({' '.join(top)})" if top else "") + ".")
    elif e is not None and e <= 0.05:
        out.append("Almost never uses emojis.")
    if (num("lowercase_start_share") or 0) >= 0.6:
        out.append("Often starts messages in lower case.")
    period = num("ends_with_period_share")
    if period is not None and period <= 0.15:
        out.append("Rarely ends with a full stop.")
    if (num("hinglish_share") or 0) >= 0.25:
        out.append("Mixes Hindi and English (Hinglish, in Latin letters).")
    if (num("hindi_script_share") or 0) >= 0.25:
        out.append("Often writes Hindi in Devanagari.")
    for key, label in (("greetings", "Greets with"), ("signoffs", "Signs off with"),
                       ("abbreviations", "Uses short forms like")):
        found = words(key)
        if found:
            out.append(f"{label}: " + ", ".join(found) + ".")
    return out


class Style:
    def __init__(self, inbox):
        self.inbox = inbox

    def _mine(self, limit: int = 400, **kw) -> list[dict]:
        """Your own typed messages: not attachments' labels, not what this plugin sent."""
        store, now = self.inbox.store(), self.inbox.clock()
        by_plugin = store.sent_texts_by_plugin(now - 120 * DAY)
        out = []
        for m in store.my_messages(limit=limit, **kw):
            t = (m["text"] or "").strip()
            if not t or t in by_plugin or _PLACEHOLDER_TEXT.match(t):
                continue
            out.append(m)
        return out

    def profile(self) -> dict:
        return self.inbox.store().get_meta("style_profile", {}) or {}

    def due(self) -> bool:
        s, store, now = self.inbox.settings, self.inbox.store(), self.inbox.clock()
        if not s.style_learning:
            return False
        p = self.profile()
        mine = store.my_messages(limit=1)
        newest = mine[0]["ts"] if mine else 0.0
        if not p:
            return bool(mine)
        if now - float(p.get("updated_at", 0)) >= REFRESH_DAYS * DAY and newest > float(p.get("newest_ts", 0)):
            return True
        return len([m for m in store.my_messages(limit=REFRESH_NEW + 1) if m["ts"] > float(p.get("newest_ts", 0))]) \
            >= REFRESH_NEW

    def learn(self, *, use_model: bool = True) -> dict:
        """Measure (and, with the model, describe) how you write. Returns the stored profile."""
        inbox, store, now = self.inbox, self.inbox.store(), self.inbox.clock()
        mine = self._mine()
        prof: dict = {"updated_at": now, "messages": len(mine), "newest_ts": mine[0]["ts"] if mine else 0.0}
        if len(mine) < MIN_MESSAGES:
            prof["note"] = f"Learning starts after {MIN_MESSAGES} messages you wrote (so far {len(mine)})."
            store.set_meta("style_profile", prof)
            return prof
        prof["overall"] = measure([m["text"] for m in mine])
        prof["by_app"] = {}
        for net in ("whatsapp", "instagram", "linkedin"):
            texts = [m["text"] for m in mine if m["network"] == net]
            if len(texts) >= MIN_PER_APP:
                prof["by_app"][net] = measure(texts)
        prof["guidelines"] = describe(prof["overall"])
        prof["source"] = "measured"
        ctx = inbox.context
        s = inbox.settings
        if use_model and ctx.llm and s.memory_enabled and \
                store.usage_today(ctx._day(), "llm_calls") < s.memory_llm_calls_per_day:
            sample = _spread(mine, SAMPLE_FOR_MODEL)
            lines = [f"Numbers (all apps): {prof['overall']}"]
            lines += [f"Numbers ({NETWORK_LABELS.get(n, n)}): {v}" for n, v in prof["by_app"].items()]
            lines.append("Sample of their messages (app: text):")
            lines += [f"  {NETWORK_LABELS.get(m['network'], m['network'])}: {redact(m['text'])[:EXAMPLE_CHARS]}"
                      for m in sample]
            store.add_usage(ctx._day(), "llm_calls", 1)
            try:
                result = ctx.llm(INSTRUCTIONS, "\n".join(lines), SCHEMA) or {}
            except Exception:  # noqa: BLE001 - keep the measured guidelines
                result = {}
            guides = [" ".join(str(g).split())[:160] for g in (result.get("guidelines") or [])
                      if str(g).strip() and not is_sensitive(str(g))][:7]
            if guides:
                prof["guidelines"] = guides
                prof["source"] = "model"
        store.set_meta("style_profile", prof)
        return prof

    def forget(self) -> None:
        self.inbox.store().set_meta("style_profile", {})

    def for_chat(self, chat: dict) -> Optional[dict]:
        """What a draft writer needs to sound like you in this chat: guidelines, how you write
        on this app, and a few of your recent messages to this same person (or on this app)."""
        if not self.inbox.settings.style_learning:
            return None
        p = self.profile()
        examples = [m for m in self._mine(limit=40, chat_id=chat["id"])][:EXAMPLES_PER_CHAT]
        where = "to them"
        if not examples:
            examples = [m for m in self._mine(limit=60, network=chat["network"]) if not m["is_group"]][:3]
            where = f"on {NETWORK_LABELS.get(chat['network'], chat['network'])}"
        if not isinstance(p, dict):
            p = {}
        if not p.get("guidelines") and not examples:
            return None
        out: dict = {}
        guidelines = [g for g in (p.get("guidelines") or []) if isinstance(g, str) and g.strip()] \
            if isinstance(p.get("guidelines"), list) else []
        if guidelines:
            out["guidelines"] = guidelines
        by_app = p.get("by_app") if isinstance(p.get("by_app"), dict) else {}
        on_app = describe(by_app.get(chat["network"]) or {})
        if on_app:
            out["on_this_app"] = on_app
        if examples:
            out["your_recent_messages"] = {"where": where,
                                           "texts": [redact(m["text"])[:EXAMPLE_CHARS] for m in reversed(examples)]}
        return out

    def prompt_lines(self, chat: dict) -> list[str]:
        """The same, as lines for a model prompt."""
        st = self.for_chat(chat)
        if not st:
            return []
        lines = ["How the user writes (match it):"]
        lines += [f"  - {g}" for g in st.get("guidelines", [])]
        lines += [f"  - On this app: {g}" for g in st.get("on_this_app", [])]
        if st.get("your_recent_messages"):
            lines.append(f"The user's own recent messages {st['your_recent_messages']['where']}:")
            lines += [f"  \"{t}\"" for t in st["your_recent_messages"]["texts"]]
        return lines


def _spread(msgs: list[dict], n: int) -> list[dict]:
    """Up to ``n`` messages spread over apps and time (not just the latest chat)."""
    by_app: dict[str, list] = {}
    for m in msgs:
        by_app.setdefault(m["network"], []).append(m)
    out: list[dict] = []
    while len(out) < n and any(by_app.values()):
        for net in list(by_app):
            if by_app[net]:
                out.append(by_app[net].pop(0))
            if len(out) >= n:
                break
    return out
