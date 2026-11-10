"""Read every message in your language, reply in theirs.

Languages are recognised on this machine (by script, and for Latin-script text by common
words, including Hinglish). A message in a language you do not read (``my_language`` plus
``also_read``) is translated by Hermes's language model: new messages automatically in the
background (``auto_translate``, batched per chat, within memory's daily model-call cap), any
chat on request. Codes, passwords and account numbers are removed before anything is sent to the
model. A translation is kept with its message (on this machine only).

Replies: write in your language and translate the draft into the chat's language. The draft
keeps what you wrote, so the approval (in Hermes, /inbox send and the Inbox page) always shows
what the message says in your words. Translations follow your writing style for that app, keep
names, emojis, numbers, links and [blanks] as they are, and never add anything.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .client import InboxError
from .privacy import redact

BATCH = 20               # messages per model call
MAX_CHARS = 1500         # of one message
AUTO_CALLS_PER_CYCLE = 3

INSTRUCTIONS = """You translate chat messages for the user. Translate each item's text into the
target language. Keep the meaning and tone exactly; keep names, emojis, numbers, dates, links,
@mentions and anything in square brackets (like [code] or [time]) unchanged. Do not explain,
summarise, add or soften anything. Hinglish means Hindi written in Latin letters (often mixed
with English). Return JSON: items, each with the same id and the translated text."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
          "properties": {"items": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["id", "text"],
              "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}}}

REPLY_INSTRUCTIONS = """You translate the user's own chat reply so they can send it in another
language. Translate it into the target language as the user would write it there: follow the
style notes (length, tone, emojis), sound natural for a chat (not formal unless the notes say
so), and keep names, emojis, numbers, links, @mentions and anything in square brackets (like
[time]) unchanged. Never add, drop or soften anything. For Hinglish, write Hindi in Latin
letters, mixed with English the way people chat. Return JSON: text."""

REPLY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"text": {"type": "string"}}}

# Unicode script (from the character name) -> language shown to the user.
_SCRIPTS = [("DEVANAGARI", "Hindi"), ("BENGALI", "Bengali"), ("GURMUKHI", "Punjabi"), ("GUJARATI", "Gujarati"),
            ("TAMIL", "Tamil"), ("TELUGU", "Telugu"), ("KANNADA", "Kannada"), ("MALAYALAM", "Malayalam"),
            ("ORIYA", "Odia"), ("ARABIC", "Arabic"), ("CYRILLIC", "Russian"), ("HIRAGANA", "Japanese"),
            ("KATAKANA", "Japanese"), ("HANGUL", "Korean"), ("CJK", "Chinese"), ("THAI", "Thai"),
            ("GREEK", "Greek"), ("HEBREW", "Hebrew"), ("SINHALA", "Sinhala")]
_URDU = set("ٹڈڑںےۓھ")
_WORDS = {
    "English": "the and you are is to of in it that for on with this have was be not what can will my your me "
               "do at we so but just if all how thanks please yes no ok okay i'm it's don't know about".split(),
    "Spanish": "el la de que y en los las un una es por con para no se lo como pero más mi tu estás gracias "
               "hola sí está muy bien qué cuando también donde".split(),
    "French": "le la les de des et est un une que qui je tu il elle nous vous pas pour avec sur mais ce "
              "merci bonjour oui c'est très bien où quand aussi".split(),
    "German": "der die das und ist nicht ich du er sie wir ihr ein eine mit auf für den dem zu auch "
              "danke hallo ja nein wie was wann gut sehr".split(),
    "Portuguese": "o a os as de que e em um uma é não para com por mas você eu obrigado obrigada olá sim "
                  "está muito bem quando também onde".split(),
    "Italian": "il lo la gli le di che e è un una non per con ma sono io tu lui lei grazie ciao sì "
               "molto bene quando anche dove come".split(),
    "Indonesian": "yang dan di ke dari ini itu dengan untuk tidak ada saya kamu aku apa terima kasih ya "
                  "juga bisa sudah belum".split(),
    "Hinglish": "hai hain nahi nahin kya haan acha accha theek thik bhai yaar kar karo karna kaise kaisa "
                "kyun kyu mein main mujhe tum tumhe aap aapko hum hamein bahut bohot abhi chalo raha rahi "
                "rahe tha thi kab kahan jaldi matlab bas sahi pata batao bata dekho dekh kuch koi wala "
                "wali ho hoga hogi ji milte kal aaj kitna kitne gaya gayi".split(),
}
_WORD_SETS = {k: set(v) for k, v in _WORDS.items()}
_ALIASES = {"hindi": "hindi", "hinglish": "hinglish", "english": "english", "en": "english", "hi": "hindi",
            "español": "spanish", "espanol": "spanish", "français": "french", "francais": "french",
            "deutsch": "german", "português": "portuguese", "portugues": "portuguese", "italiano": "italian",
            "bahasa": "indonesian", "bangla": "bengali", "urdu": "urdu"}
_TOKEN = re.compile(r"[a-zà-öø-ÿ']+")
_NOISE = re.compile(r"https?://\S+|www\.\S+|@\w+|\[[^\]]{1,40}\]")


def norm(lang: str) -> str:
    key = (lang or "").strip().lower()
    return _ALIASES.get(key, key)


def detect(text: str) -> Optional[str]:
    """The language a message is written in, or None when it is too short or unclear to say."""
    text = _NOISE.sub(" ", text or "")
    counts: dict[str, int] = {}
    latin = 0
    urdu = False
    for ch in text:
        if not ch.isalpha():
            continue
        if ch.isascii() or "LATIN" in unicodedata.name(ch, ""):
            latin += 1
            continue
        name = unicodedata.name(ch, "")
        for key, lang in _SCRIPTS:
            if key in name:
                counts[lang] = counts.get(lang, 0) + 1
                break
        urdu = urdu or ch in _URDU
    if counts:
        lang, n = max(counts.items(), key=lambda kv: kv[1])
        if n >= 2 and n >= latin * 0.3:
            return "Urdu" if lang == "Arabic" and urdu else lang
    tokens = _TOKEN.findall(text.lower())
    if len(tokens) < 3:
        return None
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in _WORD_SETS.items()}
    best = max(scores, key=lambda k: scores[k])
    ranked = sorted(scores.values(), reverse=True)
    if scores["Hinglish"] >= 2 and scores["Hinglish"] >= scores["English"] * 0.5:
        return "Hinglish"
    if ranked[0] < 2 or ranked[0] == ranked[1]:
        return None
    return best


class Translator:
    def __init__(self, inbox):
        self.inbox = inbox

    # ── settings ──
    def my_language(self) -> str:
        return self.inbox.settings.my_language or "English"

    def readable(self) -> set:
        """Languages you read: yours, the ones you listed, and the ones you write in yourself (a
        language you chat in is never translated for you)."""
        s = self.inbox.settings
        out = {norm(self.my_language())}
        out |= {norm(x) for x in re.split(r"[,;/]", s.also_read or "") if x.strip()}
        return out | self.written()

    def written(self) -> set:
        """Languages of your own recent messages (at least 10% of them), worked out once a day."""
        store, now = self.inbox.store(), self.inbox.clock()
        saved = store.get_meta("languages_written") or {}
        if now - float(saved.get("at") or 0) < 86400 and isinstance(saved.get("langs"), list):
            return set(saved["langs"])
        votes: dict[str, int] = {}
        mine = store.my_messages(limit=300)
        for m in mine:
            lang = detect(m["text"])
            if lang:
                votes[norm(lang)] = votes.get(norm(lang), 0) + 1
        total = sum(votes.values())
        langs = sorted(k for k, v in votes.items() if total and v >= max(3, total * 0.1))
        store.set_meta("languages_written", {"at": now, "langs": langs})
        return set(langs)

    def needs(self, lang: Optional[str]) -> bool:
        return bool(lang) and norm(lang) not in self.readable()

    def available(self) -> bool:
        return bool(self.inbox.context.llm)

    # ── messages ──
    def _call(self, instructions: str, prompt: str, schema: dict) -> dict:
        ctx = self.inbox.context
        if not ctx.llm:
            raise InboxError("Translation needs Hermes's language model, which this Hermes does not offer to"
                             " plugins.", kind="memory")
        self.inbox.store().add_usage(ctx._day(), "llm_calls", 1)
        try:
            result = ctx.llm(instructions, prompt, schema)
        except Exception as exc:  # noqa: BLE001 - provider errors vary
            raise InboxError(f"The language model could not translate: {exc}", kind="memory") from None
        if not isinstance(result, dict):
            raise InboxError("The language model gave no usable translation.", kind="memory")
        return result

    def pending(self, msgs: list[dict]) -> list[tuple[dict, str]]:
        """Messages from others in a language you do not read and not translated yet: (msg, language)."""
        done = self.inbox.store().translations_for(m["id"] for m in msgs)
        target = norm(self.my_language())
        out = []
        for m in msgs:
            if m["from_me"] or not (m.get("text") or "").strip():
                continue
            have = done.get(m["id"])
            if have and norm(have["target"]) == target:
                continue
            lang = detect(m["text"])
            if self.needs(lang):
                out.append((m, lang))
        return out

    def translate_messages(self, chat: dict, msgs: list[dict], *, limit: int = 40) -> list[dict]:
        """Translate the given messages that need it (at most ``limit``). Returns what was saved."""
        todo = self.pending(msgs)[-limit:]
        saved = []
        target = self.my_language()
        for i in range(0, len(todo), BATCH):
            part = todo[i:i + BATCH]
            items = [{"id": str(n), "text": redact(m["text"])[:MAX_CHARS]} for n, (m, _l) in enumerate(part)]
            prompt = (f"Target language: {target}\nChat: {chat.get('name') or 'a chat'}\n"
                      + "\n".join(f"[{x['id']}] {x['text']}" for x in items)
                      + "\nItems as JSON (translate each text):\n" + _json(items))
            result = self._call(INSTRUCTIONS, prompt, SCHEMA)
            back = {str(x.get("id")): str(x.get("text") or "").strip() for x in result.get("items") or []
                    if isinstance(x, dict)}
            rows = []
            for n, (m, lang) in enumerate(part):
                text = back.get(str(n))
                if text:
                    rows.append((m["id"], lang or "", target, text[:4000]))
            if rows:
                self.inbox.store().save_translations(chat["id"], rows, self.inbox.clock())
                saved += [{"message_id": r[0], "from": r[1], "to": r[2], "text": r[3]} for r in rows]
        return saved

    def chat(self, chat_id: str, *, limit: int = 20) -> dict:
        """Translate a chat's latest messages (on request, even with auto_translate off)."""
        inbox = self.inbox
        chat = inbox.find_chat(chat_id)
        msgs = inbox.store().cached_messages(chat["id"], max(1, min(int(limit), 50)))
        saved = self.translate_messages(chat, msgs)
        have = inbox.store().translations_for(m["id"] for m in msgs)
        return {"chat_id": chat["id"], "name": chat["name"], "into": self.my_language(),
                "translated_now": len(saved),
                "messages": [{"message_id": m["id"], "from": "me" if m["from_me"] else (m.get("sender") or chat["name"]),
                              "text": m["text"], **({"translation": have[m["id"]]["text"],
                                                     "language": have[m["id"]]["source"]} if m["id"] in have else {})}
                             for m in msgs if not m["from_me"]][-limit:]}

    def run_auto(self) -> int:
        """Background: translate new messages in languages you do not read. Returns messages done."""
        inbox = self.inbox
        store, s = inbox.store(), inbox.settings
        cursor = store.get_meta("translate_cursor")
        if cursor is None or not (s.auto_translate and s.memory_enabled and self.available()):
            store.set_meta("translate_cursor", store.last_event_id())
            return 0
        events = store.events_after(int(cursor), types=["message.new"], limit=1000)
        if not events:
            return 0
        by_chat: dict[str, list[str]] = {}
        for e in events:
            if not e["data"].get("from_me") and e["chat_id"] and e["message_id"]:
                if self.needs(detect(e["data"].get("text") or "")):
                    by_chat.setdefault(e["chat_id"], []).append(e["message_id"])
        done = calls = 0
        last_id = events[-1]["id"]
        for chat_id, ids in by_chat.items():
            chat = store.cached_chat(chat_id)
            msgs = [m for m in (store.get_message(i) for i in ids) if m]
            if not chat or chat["is_muted"] or not msgs or not self.pending(msgs):
                continue
            if calls >= AUTO_CALLS_PER_CYCLE or store.usage_today(inbox.context._day(), "llm_calls") >= \
                    s.memory_llm_calls_per_day:
                last_id = None  # the rest waits for the next cycle (done ones are skipped then)
                break
            try:
                done += len(self.translate_messages(chat, msgs, limit=BATCH))
            except InboxError as exc:
                store.set_meta("translate_state", {"at": inbox.clock(), "error": str(exc)})
            calls += 1
        if last_id is not None:
            store.set_meta("translate_cursor", last_id)
        return done

    # ── replies ──
    def chat_language(self, chat: dict) -> Optional[str]:
        """The language they wrote in last (a short "ok" says nothing), else what memory noted."""
        store = self.inbox.store()
        for m in reversed(store.cached_messages(chat["id"], 30)):
            if not m["from_me"]:
                lang = detect(m["text"] or "")
                if lang:
                    return lang
        noted = store.chat_context(chat["id"]).get("language") or ""
        return noted.split(",")[0].strip() or None

    def reply(self, chat: dict, text: str, to: str = "") -> dict:
        """Your reply in their language (with your style). {"text", "to", "from"}."""
        text = (text or "").strip()
        if not text:
            raise InboxError("Nothing to translate.", kind="input")
        target = (to or "").strip() or self.chat_language(chat)
        if not target:
            raise InboxError("I can't tell which language this chat uses yet. Say which one (e.g. to=Hindi).",
                             kind="input")
        mine = detect(text) or self.my_language()
        if norm(mine) == norm(target):
            return {"text": text, "to": target, "from": mine, "unchanged": True}
        if redact(text) != text:
            raise InboxError("Your reply has a code, password, card or account number. It is never sent to a"
                             " model, so it can't be translated: remove it, or translate it yourself.", kind="input")
        lines = [f"Target language: {target}", f"Chat: {chat.get('network', '')} with {chat.get('name') or 'someone'}"]
        lines += self.inbox.style.prompt_lines(chat)
        lines += ["The user's reply (translate this):", text[:4000]]
        result = self._call(REPLY_INSTRUCTIONS, "\n".join(lines), REPLY_SCHEMA)
        out = str(result.get("text") or "").strip()
        if not out:
            raise InboxError("The language model returned an empty translation.", kind="memory")
        return {"text": out[:4000], "to": target, "from": mine}

    def translate_draft(self, draft_id: str, to: str = "") -> dict:
        """Translate a waiting draft into the chat's language, keeping what you wrote."""
        inbox = self.inbox
        store = inbox.store()
        draft = store.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            raise InboxError(f"No pending draft {draft_id!r}.", kind="not_found")
        if store.schedule_for_draft(draft["id"]):
            raise InboxError("This draft is scheduled: cancel the schedule first, then translate it.", kind="input")
        original = draft["original_text"] or draft["text"]
        chat = store.cached_chat(draft["chat_id"]) or {"id": draft["chat_id"], "name": draft["chat_name"],
                                                       "network": draft["network"]}
        result = self.reply(chat, original, to)
        if result.get("unchanged"):
            store.set_draft_text(draft["id"], original, inbox.clock())
        else:
            store.set_draft_text(draft["id"], result["text"], inbox.clock(), original=original)
        draft = store.get_draft(draft["id"])
        return {"draft_id": draft["id"], "to": draft["chat_name"], "network": draft["network"],
                "text": draft["text"], "language": result["to"],
                **({"your_words": original} if draft["original_text"] else {"note": "Already in that language."}),
                "sent": False}


def _json(items: list) -> str:
    import json

    return json.dumps(items, ensure_ascii=False)
