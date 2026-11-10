"""Scam and fraud shield: warns about messages that look like a scam before you act on them.

Runs entirely on this machine, with fixed rules (no model call, nothing sent anywhere), in
English, Hindi and Hinglish. A message is checked for the signs scams share:
  * asking for a one-time code, PIN or password ("send me the OTP", "code bata do"), or the
    account-takeover trick "I sent you a code by mistake";
  * asking for money, gift cards, a fee to receive a prize or a job, or to scan a QR code to
    "receive" money;
  * prizes, lotteries, "guaranteed returns", part-time "task" jobs;
  * pretending to be a bank, the police, a courier or the electricity board ("KYC", "account
    blocked", "parcel seized", "digital arrest"), or asking you to install a screen-sharing app;
  * links that hide where they go: short links, raw IP addresses, look-alike web addresses
    (paypa1.com, sbi-kyc-update.com), app files (.apk) and throwaway domains;
  * pressure: urgency, secrecy, "this is my new number".
Signs add up; a stranger (someone you have never written to) adds one more. Ordinary
messages from people you know ("send me 500 for dinner") stay quiet, because one weak sign is
never enough. The result is a level (``high`` = likely a scam, ``caution`` = be careful), the
plain reasons, and one line of advice.

Where it shows: on the message and the chat (tools and the Inbox page), as an alert when such a
message arrives, in triage (a likely scam is never "urgent": urgency is the scammer's tool),
in the approval prompt of a reply, and as a hard stop when a reply to such a chat would
share a code, password, card or account number. Auto-drafts are never written for it. You
can mark a chat as safe; that is remembered.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .pacing import DAY, NETWORK_LABELS
from .privacy import is_sensitive

HIGH = 5          # score for "likely a scam"
CAUTION = 4       # score for "be careful" (one ordinary sign from someone you know is never enough)
TAIL_MESSAGES = 8  # how many of their latest messages (since your last reply) are read together
TAIL_SECONDS = 2 * DAY
ALERT_KEEP = 500   # remembered alert marks (one alert per chat and burst)

_F = re.IGNORECASE


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts), _F)


# Each sign: (key, weight, pattern, reason). A sign counts once, however often it appears.
_SIGNS: list[tuple[str, int, re.Pattern, str]] = [
    ("code", 5, _rx(
        r"\b(?:share|send|tell|give|forward|read out|type|provide|bata|batao|bhej|bhejo|bhejna|de ?do)\b[^.\n?!]{0,40}"
        r"\b(?:otp|o\.t\.p|verification code|login code|security code|sms code|6[- ]digit code|whatsapp code"
        r"|one[- ]time (?:password|code|pin)|upi pin|atm pin|card pin|mpin|cvv)\b",
        r"\b(?:otp|o\.t\.p|verification code|upi pin|atm pin|mpin|cvv)\b[^.\n?!]{0,30}"
        r"\b(?:share|send|bata|batao|bhej|bhejo|bhejna|de ?do|dedo|forward|chahiye|need|tell me)\b",
        r"\bthe (?:code|otp) (?:you|u) (?:got|received|just got)\b",
        r"(?:ओटीपी|ओ\.टी\.पी|कोड|पिन)\s*(?:बताओ|बताइए|बताएं|भेजो|भेजिए|शेयर)"),
     "asks for a one-time code or PIN"),
    ("takeover", 5, _rx(
        r"\b(?:sent|send|went)\b[^.\n]{0,30}\b(?:code|otp|sms)\b[^.\n]{0,40}"
        r"\b(?:by mistake|accidentally|wrong number|galti se|galati se)\b",
        r"\b(?:by mistake|accidentally|galti se)\b[^.\n]{0,40}\b(?:code|otp|sms)\b"),
     "the \"I sent you a code by mistake\" account-takeover trick"),
    ("remote", 5, _rx(r"\b(?:anydesk|any desk|teamviewer|team viewer|quick ?support|rustdesk|airdroid|"
                      r"screen ?share app)\b"),
     "asks you to install a screen-sharing app"),
    ("gift_card", 5, _rx(
        r"\b(?:buy|send|purchase|get|scratch)\b[^.\n]{0,40}\b(?:gift ?cards?|itunes cards?|apple cards?|"
        r"google play (?:cards?|gift|codes?)|steam (?:cards?|wallet)|amazon (?:gift|vouchers?))\b"),
     "asks for gift cards"),
    ("qr_receive", 4, _rx(r"\bscan\b[^.\n]{0,40}\b(?:receive|get|claim|credit|refund)\b",
                          r"\b(?:receive|get|claim)\b[^.\n]{0,30}\bscan\b[^.\n]{0,20}\bqr\b"),
     "asks you to scan a QR code to \"receive\" money (scanning only ever pays)"),
    ("prize", 4, _rx(
        r"\byou(?:'ve| have)? (?:won|been selected)\b", r"\blottery\b", r"\blucky (?:draw|winner|customer)\b",
        r"\bclaim (?:your )?(?:prize|reward|gift|cash)\b", r"\bkbc\b", r"\bjackpot\b", r"\bcash prize\b",
        r"\bcongratulations?\b[^.\n]{0,50}\b(?:won|winner|selected|prize)\b", r"(?:लॉटरी|इनाम जीत)"),
     "a prize or lottery you never entered"),
    ("investment", 4, _rx(
        r"\b(?:guaranteed|assured|fixed|daily|risk[- ]free)\s+(?:returns?|profits?|income)\b",
        r"\bdouble (?:your|the) (?:money|investment)\b", r"\bpaisa double\b",
        r"\b\d{1,3}\s?%\s?(?:daily|per day|a day|weekly|per week|monthly returns?)\b",
        r"\b(?:crypto|bitcoin|usdt|forex|binary option)\b[^.\n]{0,40}\b(?:invest|returns?|profit|earn)\b",
        r"\b(?:invest|returns?|profit|earn)\b[^.\n]{0,40}\b(?:crypto|bitcoin|usdt|forex|binary option)\b"),
     "promises guaranteed or very high returns"),
    ("authority", 3, _rx(
        r"\b(?:kyc|re-?kyc)\b",
        r"\b(?:account|card|sim|number|wallet|upi)\b[^.\n]{0,30}\b(?:will be |has been |is |get )?"
        r"(?:blocked|suspended|deactivated|frozen|closed|band ho)\b",
        r"\b(?:electricity|power|bijli)\b[^.\n]{0,40}\b(?:disconnect|disconnected|cut|kat)\b",
        r"\bpan(?: card)?\b[^.\n]{0,20}\b(?:update|link|expire|expired|blocked)\b",
        r"\b(?:parcel|package|courier|shipment)\b[^.\n]{0,40}\b(?:held|seized|stuck|illegal|drugs|customs)\b",
        r"\bdigital arrest\b",
        r"\b(?:cbi|narcotics|cyber ?crime|police|income tax|trai|customs)\b[^.\n]{0,40}"
        r"\b(?:case|arrest|warrant|notice|fir|complaint|penalty)\b"),
     "pretends to be a bank, the police or a company"),
    ("job", 3, _rx(
        r"\bpart[- ]time (?:job|work|income)\b",
        r"\bwork from home\b[^.\n]{0,40}\b(?:earn|income|salary|₹|rs\.?|per day|daily)\b",
        r"\bearn\b[^.\n]{0,15}(?:₹|rs\.?|inr|\$)?\s?\d[\d,]*\s*(?:k\b)?[^.\n]{0,10}"
        r"\b(?:daily|per day|a day|per hour|an hour|weekly|per task)\b",
        r"\b(?:like|subscribe to)\b[^.\n]{0,20}\b(?:youtube )?videos?\b[^.\n]{0,30}\b(?:earn|paid|pay|₹|rs)\b",
        r"\b(?:rate|review)\b[^.\n]{0,20}\b(?:hotels|products|apps)\b[^.\n]{0,30}\b(?:earn|paid|commission)\b",
        r"\b(?:prepaid|paid|merchant) tasks?\b", r"\btask[- ]based (?:job|work)\b"),
     "a too-easy job or \"task\" that pays"),
    ("fee", 3, _rx(r"\b(?:processing|registration|activation|clearance|release|delivery|verification|joining)"
                   r" (?:fee|charges?)\b",
                   r"\brefundable (?:deposit|fee|amount)\b", r"\btoken (?:amount|money)\b",
                   r"\badvance payment\b"),
     "asks for a fee before you get something"),
    ("money", 2, _rx(
        r"\b(?:send|transfer|pay|lend|need|deposit|bhej|bhejo|transfer kar|pay kar)\b[^.\n]{0,30}"
        r"(?:₹|\brs\.?|\binr\b|\brupees?\b|\$|\bmoney\b|\bpaise\b|\bpaisa\b|\bpayment\b|\bamount\b|\b\d{3,}\b)",
        r"\b(?:paise|paisa|money)\b[^.\n]{0,20}\b(?:bhej|bhejo|chahiye|de do|dedo|transfer|send)\b",
        r"\b(?:upi|gpay|google pay|phonepe|paytm)\b[^.\n]{0,30}\b(?:send|pay|transfer|request|kar do|bhej)\b",
        r"पैसे\s*(?:भेजो|भेजिए|चाहिए|दे दो)"),
     "asks for money"),
    ("new_number", 2, _rx(r"\b(?:this is|it'?s|here is) my new (?:number|no\.?|whatsapp)\b",
                          r"\bmera naya (?:number|no)\b", r"\bchanged my (?:number|phone)\b",
                          r"\bnew number\b[^.\n]{0,20}\b(?:save|delete)\b",
                          r"\b(?:old|my) (?:number|phone)\b[^.\n]{0,20}\b(?:lost|stolen|not working|broke)\b"),
     "says it is a new number"),
    ("secrecy", 2, _rx(r"\b(?:don'?t|do not|dont) tell (?:anyone|anybody|mom|mum|dad|papa|mummy|your)\b",
                       r"\bkeep (?:this|it) (?:a )?(?:secret|between us)\b", r"\bkisi ko (?:mat|na) (?:batana|bolna)\b",
                       r"किसी को (?:मत|न) बताना"),
     "asks you to keep it secret"),
    ("urgency", 1, _rx(r"\burgent(?:ly)?\b", r"\bimmediately\b", r"\bright now\b", r"\basap\b",
                       r"\bwithin \d+ ?(?:hours?|hrs?|minutes?|mins?)\b", r"\blast (?:chance|warning|reminder)\b",
                       r"\babhi ke abhi\b", r"\bturant\b", r"\bjaldi (?:karo|se|bhej)\b", r"\btoday itself\b",
                       r"\bexpires? (?:today|tonight|in)\b", r"तुरंत"),
     "pushes you to hurry"),
]

# Service messages that carry a code for you ("Your OTP is 123456. Do not share it") are not
# someone asking for one.
_SERVICE_CODE = _rx(r"\b(?:do not|don'?t|never|dont)\s+share\b", r"\bshare\s+na\s+kare?n?\b",
                    r"\bkisi (?:ke saath|se) share\b", r"साझा न करें", r"शेयर न करें",
                    r"\bis your (?:otp|code|verification code)\b", r"\byour (?:otp|code) is\b")

_URL = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>\"')\]]+|"
                  r"(?:[a-z0-9-]+\.)+(?:ly|gd|at|cc|io|me|co|in|com|xyz|top|click|link|info|net|org|site|online|"
                  r"live|icu|buzz|cyou|sbs|rest|monster|cfd|lol|quest|support|zip|mov|apk)/[^\s<>\"')\]]*)")
_SHORTENERS = {"bit.ly", "tinyurl.com", "cutt.ly", "is.gd", "rb.gy", "shorturl.at", "tiny.cc", "ow.ly", "t.ly",
               "s.id", "rebrand.ly", "bl.ink", "shorte.st", "adf.ly", "goo.su", "clck.ru", "v.gd", "tiny.one",
               "short.gy", "rb.gy", "u.to", "qr.ae", "surl.li", "shorturl.asia"}
_BAD_TLDS = {"xyz", "top", "click", "icu", "buzz", "cyou", "sbs", "rest", "monster", "cfd", "lol", "quest",
             "support", "zip", "mov", "loan", "work", "bar", "mom", "country", "kim", "gq", "tk", "ml", "cf", "ga"}
# Brand -> the registrable names it really uses. A web address naming the brand anywhere else
# (or spelling it with look-alike characters) is pretending.
_BRANDS = {
    "sbi": {"sbi", "onlinesbi", "sbicard", "sbimf", "sbilife", "sbigeneral", "yonosbi"},
    "hdfc": {"hdfcbank", "hdfc", "hdfclife", "hdfcfund", "hdfcergo", "hdfcsec"},
    "icici": {"icicibank", "icici", "icicidirect", "iciciprulife", "icicilombard"},
    "kotak": {"kotak", "kotakbank", "kotaksecurities"},
    "paytm": {"paytm", "paytmbank", "paytmmoney", "paytmpayments"},
    "phonepe": {"phonepe"},
    "amazon": {"amazon", "amazonaws", "amzn", "media-amazon", "amazonpay"},
    "flipkart": {"flipkart"},
    "paypal": {"paypal", "paypalobjects"},
    "netflix": {"netflix"},
    "whatsapp": {"whatsapp"},
    "instagram": {"instagram", "cdninstagram"},
    "facebook": {"facebook", "fb", "fbcdn"},
    "linkedin": {"linkedin", "lnkd", "licdn"},
    "apple": {"apple", "icloud", "apple-cloudkit"},
    "microsoft": {"microsoft", "live", "office", "microsoftonline", "outlook", "office365", "sharepoint"},
    "google": {"google", "googleusercontent", "googleapis", "gstatic", "youtube", "goo", "googledrive"},
    "incometax": {"incometax", "incometaxindia"},
    "indiapost": {"indiapost"},
    "fedex": {"fedex"},
    "dhl": {"dhl"},
    "bluedart": {"bluedart"},
    "npci": {"npci"},
}
_BRAND_NAMES = {"sbi": "SBI", "hdfc": "HDFC", "icici": "ICICI", "npci": "NPCI", "dhl": "DHL", "paypal": "PayPal",
                "phonepe": "PhonePe", "whatsapp": "WhatsApp", "linkedin": "LinkedIn", "incometax": "Income Tax",
                "indiapost": "India Post", "fedex": "FedEx", "bluedart": "Blue Dart"}
# Words scam sites glue to a brand: sbi-kyc, amazon-rewards, paypalsecure.
_SCAMMY = {"kyc", "update", "verify", "verification", "login", "signin", "secure", "security", "support", "help",
           "reward", "rewards", "bank", "online", "care", "service", "services", "official", "refund", "pay",
           "india", "app", "account", "customer", "alert", "gift", "offer", "offers", "prize", "win", "auth", "id",
           "web", "net", "claim", "bonus", "points", "recovery", "unlock", "team"}
_LOOKALIKE = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_SECOND_LEVEL = {"co", "com", "gov", "org", "net", "ac", "edu", "nic", "res", "gen", "firm", "ind"}
_IP_HOST = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_LOGIN_WORDS = re.compile(r"(?i)login|signin|verify|update|kyc|account|bank|secure|wallet|reward|refund")

_LINK_REASONS = {
    "apk": "a link to install an app file (.apk)",
    "lookalike": "a web address pretending to be {brand}",
    "ip": "a link to a bare number address instead of a name",
    "punycode": "a look-alike web address (unusual letters)",
    "short": "a short link that hides where it goes",
    "bad_tld": "a link on a throwaway kind of domain",
    "http_login": "an unsecured link asking you to log in or verify",
}
_LINK_WEIGHTS = {"apk": 4, "lookalike": 4, "ip": 3, "punycode": 3, "short": 2, "bad_tld": 2, "http_login": 1}

_ADVICE = [  # (signs, advice), first match wins
    ({"code", "takeover"}, "Never share a one-time code or PIN, not even with family: it lets someone take over"
                           " your account or empty your bank."),
    ({"remote"}, "Never install a screen-sharing app because someone asked in a chat."),
    ({"qr_receive"}, "Scanning a QR code or entering your UPI PIN only ever sends money; you never need it to"
                     " receive."),
    ({"authority"}, "Banks, the police and couriers never ask for this in a chat. Call the official number"
                    " yourself."),
    ({"apk", "lookalike", "ip", "punycode", "short", "bad_tld", "http_login"},
     "Don't open the link. Go to the official app or website yourself."),
    ({"prize", "investment", "job", "fee", "gift_card"}, "Real prizes, jobs and investments never ask you to pay"
                                                         " first."),
    ({"money", "new_number", "secrecy"}, "Before paying, call them on the number you already have to check it"
                                         " is really them."),
]


def _registrable(host: str) -> tuple[str, list[str]]:
    """(the name a site registered, e.g. "sbi-kyc" of sbi-kyc.co.in; all labels)."""
    labels = [x for x in host.lower().split(".") if x]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return labels[-3], labels
    return (labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")), labels


def _pretends(name: str, tokens: list[str]) -> str:
    """The brand a web address pretends to be ('' if none): it names the brand (or spells it with
    look-alike characters, or glues a word like "kyc" to it) but is not one of its real sites."""
    for brand, real in _BRANDS.items():
        if name in real:
            continue
        for t in tokens:
            n = t.translate(_LOOKALIKE)
            if n == brand or (len(n) > len(brand) and (n.startswith(brand) and n[len(brand):] in _SCAMMY
                                                      or n.endswith(brand) and n[:-len(brand)] in _SCAMMY)):
                return brand
    return ""


def link_signs(url: str) -> list[tuple[str, str]]:
    """The warning signs of one link: [(key, reason)]."""
    raw = url if re.match(r"(?i)https?://", url) else "http://" + url
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return []
    if not host:
        return []
    path = (parts.path or "").lower()
    out: list[tuple[str, str]] = []
    if path.endswith(".apk") or host.endswith(".apk"):
        out.append(("apk", _LINK_REASONS["apk"]))
    if _IP_HOST.match(host):
        out.append(("ip", _LINK_REASONS["ip"]))
        return out
    if "xn--" in host:
        out.append(("punycode", _LINK_REASONS["punycode"]))
    name, labels = _registrable(host)
    if host in _SHORTENERS or ".".join(labels[-2:]) in _SHORTENERS:
        out.append(("short", _LINK_REASONS["short"]))
    if labels and labels[-1] in _BAD_TLDS:
        out.append(("bad_tld", _LINK_REASONS["bad_tld"]))
    tokens = [t for lab in labels[:-1] for t in lab.split("-") if t]
    brand = _pretends(name, tokens)
    if brand:
        out.append(("lookalike", _LINK_REASONS["lookalike"].format(brand=_BRAND_NAMES.get(brand, brand.title()))))
    if parts.scheme == "http" and re.match(r"(?i)https?://", url) and _LOGIN_WORDS.search(host + path):
        out.append(("http_login", _LINK_REASONS["http_login"]))
    return out


def signs(text: str) -> dict[str, str]:
    """Every warning sign in one message: {key: reason}."""
    text = text or ""
    if not text.strip():
        return {}
    found: dict[str, str] = {}
    service = bool(_SERVICE_CODE.search(text))
    for key, _weight, pattern, reason in _SIGNS:
        if key in ("code", "takeover") and service:
            continue
        if pattern.search(text):
            found[key] = reason
    for m in _URL.finditer(text):
        for key, reason in link_signs(m.group(1).rstrip(".,;:!?")):
            found.setdefault(key, reason)
    return found


def _weight(key: str) -> int:
    for k, w, _p, _r in _SIGNS:
        if k == key:
            return w
    return _LINK_WEIGHTS.get(key, 0)


def verdict(texts: Iterable[str], *, stranger: bool = False) -> Optional[dict]:
    """Read together (a scam is often split over several messages). None when nothing is wrong."""
    found: dict[str, str] = {}
    for t in texts:
        for k, r in signs(t).items():
            found.setdefault(k, r)
    if not found:
        return None
    score = sum(_weight(k) for k in found)
    risky = any(k != "urgency" for k in found)
    reasons = [found[k] for k in sorted(found, key=lambda k: -_weight(k))]
    if stranger and risky:
        score += 1
        reasons.append("from someone you have never written to")
    if score < CAUTION or not risky:
        return None
    advice = next((a for keys, a in _ADVICE if keys & set(found)), "Check another way before you act.")
    return {"level": "high" if score >= HIGH else "caution", "score": score, "reasons": reasons[:5],
            "advice": advice, "signs": sorted(found)}


def label(v: dict) -> str:
    return "Likely a scam" if v["level"] == "high" else "Be careful"


class Shield:
    """The shield for one inbox: verdicts per chat and message, alerts, and chats marked safe."""

    def __init__(self, inbox):
        self.inbox = inbox

    def enabled(self) -> bool:
        return bool(self.inbox.settings.scam_shield)

    # ── what you trust ──
    def trusted(self) -> set:
        return set(self.inbox.store().get_meta("shield_trusted", []) or [])

    def trust(self, chat_id: str, safe: bool = True) -> dict:
        chat = self.inbox.find_chat(chat_id)
        store = self.inbox.store()
        ids = [c for c in (store.get_meta("shield_trusted", []) or []) if c != chat["id"]]
        if safe:
            ids.append(chat["id"])
        store.set_meta("shield_trusted", ids[-2000:])
        return {"chat_id": chat["id"], "name": chat["name"], "marked_safe": safe}

    def stranger(self, chat: dict) -> bool:
        """You never wrote in this chat, nor to this person on another app."""
        if chat.get("is_group"):
            return False
        store = self.inbox.store()
        ids = [chat["id"]]
        contact = store.contact_for_chat(chat["id"])
        if contact:
            ids += [c["chat_id"] for c in (store.contact(contact["id"]) or {}).get("chats", [])]
        marks = ",".join("?" * len(ids))
        return not store._q(f"SELECT 1 FROM messages WHERE chat_id IN ({marks}) AND from_me=1 LIMIT 1", ids)

    # ── verdicts ──
    def for_chat(self, chat: dict, msgs: Optional[list[dict]] = None) -> Optional[dict]:
        """The chat's verdict, from their latest messages since your last reply (None = fine)."""
        if not chat or not self.enabled() or chat["id"] in self.trusted():
            return None
        msgs = msgs if msgs is not None else self.inbox.store().cached_messages(chat["id"], TAIL_MESSAGES * 2)
        tail: list[dict] = []
        for m in reversed(msgs):
            if m["from_me"] or len(tail) >= TAIL_MESSAGES:
                break
            tail.append(m)
        if not tail or self.inbox.clock() - tail[0]["ts"] > TAIL_SECONDS:
            return None
        if chat.get("is_group"):  # in a group, each person's messages are their own
            by_sender: dict[str, list[str]] = {}
            for m in tail:
                by_sender.setdefault(m.get("sender_id") or m.get("sender") or "", []).append(m["text"] or "")
            found = [v for v in (verdict(t) for t in by_sender.values()) if v]
            out = max(found, key=lambda v: v["score"]) if found else None
        else:
            out = verdict([m["text"] or "" for m in tail], stranger=self.stranger(chat))
        if out:
            out = dict(out, message_id=tail[-1]["id"], chat_id=chat["id"])  # where their burst starts
        return out

    def for_messages(self, chat: dict, msgs: list[dict]) -> dict[str, dict]:
        """Per message: {message_id: verdict} for the messages that look wrong on their own."""
        if not chat or not self.enabled() or chat["id"] in self.trusted():
            return {}
        stranger = None
        out = {}
        for m in msgs:
            if m["from_me"] or not signs(m["text"] or ""):
                continue
            if stranger is None:
                stranger = self.stranger(chat)
            v = verdict([m["text"] or ""], stranger=stranger)
            if v:
                out[m["id"]] = v
        return out

    def reply_risk(self, chat_id: str, text: str) -> Optional[str]:
        """Why a reply must not go out: it would share a code, password, card or account number
        with a chat that looks like a scam. None = fine."""
        if not text or not is_sensitive(text):
            return None
        chat = self.inbox.store().cached_chat(chat_id)
        v = self.for_chat(chat) if chat else None
        if not v:
            return None
        return ("Stopped: this chat looks like a scam (" + v["reasons"][0] + ") and your message shares a code,"
                " password, card or account number. If you are sure it is safe, mark the chat as safe first.")

    # ── alerts ──
    def scan_new(self) -> int:
        """Alert once for each chat whose new messages look like a scam. Returns alerts made."""
        inbox = self.inbox
        store = inbox.store()
        cursor = store.get_meta("shield_cursor")
        if cursor is None or not self.enabled():
            store.set_meta("shield_cursor", store.last_event_id())
            return 0
        events = store.events_after(int(cursor), types=["message.new"], limit=1000)
        if not events:
            return 0
        store.set_meta("shield_cursor", events[-1]["id"])
        chats = {e["chat_id"] for e in events if e["chat_id"] and not e["data"].get("from_me")}
        done = dict(store.get_meta("shield_alerted", {}) or {})
        made = 0
        for chat_id in sorted(chats):
            chat = store.cached_chat(chat_id)
            if not chat or chat["is_muted"]:
                continue
            v = self.for_chat(chat)
            if not v:
                continue
            mark = f"{v['message_id']}:{v['level']}"  # once per burst (again if caution turns into high)
            if done.get(chat_id) == mark:
                continue
            done[chat_id] = mark
            name = chat["name"] or "someone"
            app = NETWORK_LABELS.get(chat["network"], chat["network"])
            title = f"{label(v)}: {name} ({app})"
            inbox.automation.notify("scam", title, "; ".join(v["reasons"][:3]).capitalize() + ". " + v["advice"],
                                    chat_id=chat_id, data={"level": v["level"]})
            made += 1
        store.set_meta("shield_alerted", dict(list(done.items())[-ALERT_KEEP:]))
        return made

    def flagged(self, *, days: int = 7, limit: int = 20) -> list[dict]:
        """Chats whose latest messages look like a scam, most recent first."""
        if not self.enabled():
            return []
        store, now = self.inbox.store(), self.inbox.clock()
        out = []
        for chat in store.cached_chats(limit=200):
            if chat["ts"] < now - days * DAY or chat["last_from_me"]:
                continue
            v = self.for_chat(chat)
            if v:
                out.append({"chat_id": chat["id"], "name": chat["name"], "network": chat["network"],
                            "level": v["level"], "reasons": v["reasons"], "advice": v["advice"],
                            "message_id": v["message_id"], "ts": chat["ts"]})
            if len(out) >= limit:
                break
        return out
