"""Local preview of the Inbox page, for development only (never shipped to users).

    python scripts/dev_server.py [--port 8765]

Serves the built web bundle (dashboard/dist/index.js) inside a stand-in for the Hermes
dashboard, with the real API (api.py) over the fictional chats in tests/fixtures. Memory,
open items, a scheduled message, a watch and alerts are seeded so every page has content.
Nothing here talks to Unipile.

To watch the page react live, make a message arrive (dev only):

    curl -X POST http://127.0.0.1:8765/__dev/receive -d '{"chat_id": "wa-maya", "text": "Are you home?"}'
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import math
import sqlite3
import struct
import sys
import tempfile
import time
import wave
import zlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
if "unified_inbox" not in sys.modules:
    spec = importlib.util.spec_from_file_location("unified_inbox", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unified_inbox"] = mod
    spec.loader.exec_module(mod)

from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend  # noqa: E402
from unified_inbox.api import FileResult, InboxAPI  # noqa: E402
from unified_inbox.inbox import Inbox  # noqa: E402
from unified_inbox.sync import SyncEngine  # noqa: E402

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inbox preview</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: Canvas; color: CanvasText; }
  .bar { height: 48px; display: flex; align-items: center; padding: 0 16px; border-bottom: 1px solid #8884; font-weight: 600; }
  #root { padding: 16px; }
  @media (max-width: 860px) { #root { padding: 0; } }
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"></script>
</head><body><div class="bar">Hermes (preview)</div><div id="root"></div>
<script>
  window.__HERMES_PLUGIN_SDK__ = {
    React: window.React,
    fetchJSON: function (url, init) {
      return fetch(url, init).then(function (r) {
        return r.text().then(function (t) { if (!r.ok) throw new Error(t); return t ? JSON.parse(t) : {}; });
      });
    },
    authedFetch: function (url, init) { return fetch(url, init); }
  };
  window.__HERMES_PLUGINS__ = { register: function (name, C) {
    ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(C));
  } };
</script>
<script src="/dist/index.js"></script>
</body></html>"""


def _png(width: int = 160, height: int = 120) -> bytes:
    """A small gradient picture (stdlib only), standing in for a photo someone sent."""
    rows = b"".join(b"\x00" + bytes(v for x in range(width) for v in (40 + x, 90 + y // 2, 200 - x // 2))
                    for y in range(height))
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _wav(seconds: float = 2.0) -> bytes:
    """A short tone (stdlib only), standing in for a voice note."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * i / 8000)))
                               for i in range(int(8000 * seconds))))
    return buf.getvalue()


def _preview_transcriber(path: str) -> dict:
    """Preview only: real installs use Hermes's own speech-to-text."""
    return {"success": True, "transcript": "Hi, it's Maya. The plumber just called, he can come at 8 tomorrow."
                                           " Can you be home? Call me back when you can."}


def _preview_llm(instructions: str, text: str, schema: dict):
    """Preview only: translations marked as such; real installs use Hermes's model."""
    from unified_inbox import translate

    if instructions == translate.INSTRUCTIONS:
        items = json.loads(text.rsplit("\n", 1)[1])
        samples = {"Hola": "Hi! Are we still on for the climbing session tomorrow at 7?"}
        return {"items": [{"id": x["id"], "text": next((v for k, v in samples.items() if x["text"].startswith(k)),
                                                       "(preview translation) " + x["text"])} for x in items]}
    if instructions == translate.REPLY_INSTRUCTIONS:
        return {"text": "¡Sí! Nos vemos mañana a las 7 😊"}
    return None


def _preview_speaker(text: str, path: str) -> dict:
    """Preview only: a short tone instead of Hermes's text-to-speech."""
    target = Path(path).with_suffix(".wav")
    target.write_bytes(_wav(1.5))
    return {"success": True, "file_path": str(target), "media_tag": f"MEDIA:{target}"}


def seed() -> tuple[InboxAPI, SampleBackend, SyncEngine]:
    config = {"unipile_account_id": SAMPLE_ACCOUNT_IDS}
    backend = SampleBackend(time.time)
    folder = tempfile.mkdtemp(prefix="inbox-preview-")  # files people send need a folder to live in
    conn = sqlite3.connect(f"{folder}/data.db", check_same_thread=False)
    inbox = Inbox(get_config=lambda k, d=None: config.get(k, d), get_secret={"UNIPILE_API_KEY": "dev"}.get,
                  open_db=lambda: conn, sleep=lambda s: None, client_factory=lambda key: backend,
                  llm=_preview_llm, transcriber=_preview_transcriber, speaker=_preview_speaker)
    _seed_messaging(backend)
    engine = SyncEngine(inbox)
    while engine.sync_once()["pending_chats"]:
        pass
    store, now = inbox.store(), time.time()
    for chat_id, facts in {"li-priya": ["Recruiter at Northwind, hiring for a senior designer role",
                                        "Prefers calls in the afternoon"],
                           "wa-maya": ["Flatmate; handles things with the landlord"],
                           "wa-mum": ["Birthday is 4 June"]}.items():
        chat = store.cached_chat(chat_id)
        contact = store.ensure_contact(chat_id=chat_id, network=chat["network"], name=chat["name"], now=now)
        for text in facts:
            store.add_fact(contact_id=contact["id"], text=text, category="other", status="active", source="auto",
                           now=now, chat_id=chat_id)
    store.set_chat_context("li-priya", now, summary="Priya wants a 30-minute call before Friday about the senior"
                           " designer role and asked for availability.", language="English")
    store.set_chat_context("wa-maya", now, summary="The plumber can only come tomorrow at 8am; Maya needs you to"
                           " confirm you will be home, by tonight.", language="English")
    store.add_loop(chat_id="wa-maya", kind="they_wait", text="Confirm being home at 8am for the plumber",
                   due_ts=now + 6 * 3600, message_id="", now=now - 3 * 3600)
    store.add_loop(chat_id="li-priya", kind="they_wait", text="Send availability for a call before Friday",
                   due_ts=now + 2 * 86400, message_id="", now=now - 4 * 3600)
    store.add_loop(chat_id="wa-dev", kind="you_promised", text="Send Dev the updated deck", due_ts=now - 1800,
                   message_id="", now=now - 5 * 3600)
    store.add_loop(chat_id="li-marco", kind="you_wait", text="Marco to share the contract draft", due_ts=None,
                   message_id="", now=now - 26 * 3600)
    c = store.contact_for_chat("wa-mum")
    fid = store.add_fact(contact_id=c["id"], text="Mum is visiting on the 20th", category="plans", status="pending",
                         source="auto", now=now)
    store.add_question(kind="fact", text='About Mum: "Mum is visiting on the 20th". Is that right?',
                       data={"fact_id": fid}, dedupe_key="fact:dev", now=now)
    d = inbox.create_draft("li-priya", "Hi Priya, Thursday 3-5pm works for me. Shall I send an invite?")
    inbox.create_draft("wa-maya", "Yes, I'll be home at 8, tell him to ring twice!")
    draft = store.get_draft(d["draft_id"])
    store.add_schedule(draft=draft, send_at=now + 3 * 3600, now=now)
    inbox.automation.add_watch("keyword", text="invoice")
    inbox.automation.add_watch("no_reply", chat=store.cached_chat("li-marco"), until=now + 2 * 86400, nudge=True)
    inbox.automation.notify("watch", "noor.events wrote about \"tickets\" (Instagram)",
                            "A spot opened for Friday, confirm within 2 hours", chat_id="ig-tickets")
    inbox.automation.notify("schedule", "Sent your scheduled message to Elena Park on LinkedIn",
                            "\"Thanks Elena, talk soon!\"", chat_id="li-elena")
    _seed_smart(inbox, backend, store, now)
    _seed_insights(inbox, store, now)
    return InboxAPI(inbox, sync_once=engine.sync_once), backend, engine


def _seed_smart(inbox: Inbox, backend: SampleBackend, store, now: float) -> None:
    """An auto-draft with a blank, a learned writing style and a "same person?" question."""
    store.add_draft(chat_id="wa-dev", account_id="mock-wa", network="whatsapp", chat_name="Dev Patel (work)",
                    text="Hey Dev, sending the updated deck by [time] today, sorry for the delay!", now=now - 60,
                    origin="auto")
    inbox.automation.notify("draft", "Draft ready for Dev Patel (work) (WhatsApp)",
                            "They wrote: \"Can you send me the updated deck before the deadline today? ASAP please\"."
                            " Fill in [time] first.", chat_id="wa-dev")
    from unified_inbox.style import measure  # noqa: E402 - a profile shaped exactly like a learned one

    whatsapp = measure(["haan done 👍", "kal milte hain", "ok sending now", "yaar running 10 min late",
                        "sure, call me after 6", "thanks bhai"] * 20)
    linkedin = measure(["Hi Priya, thanks for reaching out. Thursday afternoon works for me.",
                        "Thanks, Marco. I'll review the draft and get back to you by Friday."] * 12)
    store.set_meta("style_profile", {
        "updated_at": now - 3600, "messages": 184, "newest_ts": now - 3600, "source": "model",
        "overall": whatsapp, "by_app": {"whatsapp": whatsapp, "linkedin": linkedin},
        "guidelines": ["Keep it short: usually one or two lines.", "Friendly and direct; rarely uses emojis.",
                       "Mixes Hindi and English with family and friends (Hinglish).",
                       "On LinkedIn: full sentences, a greeting with their first name, signs off with Thanks."]})
    for c in backend._data["chats"]:
        if c["id"] == "wa-old":
            c["name"] = "Arjun"
    store._x("UPDATE chats SET name='Arjun' WHERE id='wa-old'")
    inbox.identity.ensure_people(0)
    inbox.identity.suggest_links(force=True)


def _seed_insights(inbox: Inbox, store, now: float) -> None:
    """A likely scam, plans (one clashing with an added one), a translated chat and draft, a drifting
    friend and a birthday tomorrow."""
    for chat_id in ("ig-tickets", "wa-mum", "ig-arjun"):
        chat = store.cached_chat(chat_id)
        for m in store.cached_messages(chat_id, 5):
            if chat_id == "ig-tickets" and not m["from_me"] and "won" in m["text"]:
                v = inbox.shield.for_chat(chat)
                if v:
                    inbox.automation.notify("scam", f"Likely a scam: {chat['name']} (Instagram)",
                                            "; ".join(v["reasons"][:3]).capitalize() + ". " + v["advice"],
                                            chat_id=chat_id, data={"level": v["level"]})
            elif not m["from_me"]:
                pid = inbox.planner.detect(m, chat)
                if pid:
                    view = inbox.planner.view(store.plan(pid))
                    inbox.automation.notify("plan", f"Plan: {view['title']}, {view['when']}",
                                            f"From your chat: \"{view['said']}\".", chat_id=chat_id,
                                            data={"plan_id": pid})
    first = store.plans(since=now)
    if first:
        inbox.planner.add(first[0]["id"])  # one is in your calendar already
    inbox.translator.translate_messages(store.cached_chat("ig-arjun"), store.cached_messages("ig-arjun", 10))
    d = inbox.create_draft("ig-arjun", "Yes! See you tomorrow at 7")
    inbox.translator.translate_draft(d["draft_id"], "Spanish")
    msgs = [{"id": f"ravi-{i}", "chat_id": "wa-ravi", "from_me": i % 2 == 0, "sender": "" if i % 2 == 0 else "Ravi",
             "text": "Cricket this weekend?" if i % 2 else "Haan, pakka!", "ts": now - (40 + (24 - i) * 3) * 86400}
            for i in range(24)]
    store.upsert_chats([{"id": "wa-ravi", "account_id": "mock-wa", "network": "whatsapp", "name": "Ravi Kumar",
                         "ts": msgs[-1]["ts"], "last_text": msgs[-1]["text"], "last_from_me": True}], now)
    store.upsert_messages(msgs, now)
    when = datetime.fromtimestamp(now) + timedelta(days=1)
    store.upsert_messages([{"id": "wish", "chat_id": "li-elena", "from_me": True, "text": "Happy birthday Elena! 🎂",
                            "ts": when.replace(year=when.year - 1).timestamp()}], now)
    inbox.radar.report()  # the birthday is remembered before the year-old wish is cleaned up
    store.set_meta("radar_weekly_at", now)


def _seed_messaging(backend: SampleBackend) -> None:
    """Files, a voice note, a quoted reply, reactions and a group with a mention, so every part of
    the chat view has something to show."""
    backend.files.update({"dev-photo": _png(), "dev-voice": _wav(), "dev-pdf": b"%PDF-1.4\n% preview invoice\n"})
    backend.receive("wa-maya", "", attachments=[{"id": "dev-voice", "type": "audio", "voice_note": True,
                                                 "mimetype": "audio/wav", "duration": 2}])
    backend.receive("wa-dev", "Here's the venue", attachments=[{"id": "dev-photo", "type": "img",
                                                               "mimetype": "image/png"}])
    backend.receive("li-priya", "", attachments=[{"id": "dev-pdf", "type": "file", "mimetype": "application/pdf",
                                                  "filename": "Job description - Senior Designer.pdf"}])
    backend.receive("ig-tickets", "Congratulations! You have won Rs 25,000 in our lucky draw. Pay the Rs 499"
                    " processing fee within 2 hours to claim: https://bit.ly/claim-prize-now")
    backend.receive("wa-mum", "Beta, call me tomorrow at 10? Papa wants to talk about the trip")
    backend.receive("ig-arjun", "Hola! ¿Seguimos con la escalada mañana a las 7?")
    backend.make_group("wa-bookclub")
    backend.receive("wa-bookclub", "Next book: The Overstory?", sender="Sam Okafor", sender_id="111@s.whatsapp.net")
    msg = backend.receive("wa-bookclub", "I'm in! Who's hosting?", sender="Ana", sender_id="222@s.whatsapp.net")
    backend.receive("wa-bookclub", "@you could you host this time?", sender="Sam Okafor",
                    sender_id="111@s.whatsapp.net", mentioned=True,
                    quoted={"id": msg["id"], "text": "I'm in! Who's hosting?", "sender": "Ana"})
    seeded = [m for m in backend.sent]
    for i, m in enumerate(seeded):  # a minute apart, in the order above
        m["ts"] -= (len(seeded) - i) * 60
    for m in backend._data["chats"]:
        if m["id"] == "wa-bookclub":
            m["name"] = "Book club"
    for m in backend.sent:
        if m["chat_id"] == "wa-dev":
            m["extra"]["reactions"] = [{"reaction": "👍", "count": 1}]


def serve(port: int) -> None:
    api, backend, engine = seed()
    bundle = ROOT / "dashboard" / "dist" / "index.js"

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _api(self, method: str) -> None:
            url = urlparse(self.path)
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
            status, payload = api.handle(method, url.path[len("/api/plugins/unified-inbox"):], dict(parse_qsl(url.query)),
                                         body)
            if isinstance(payload, FileResult):  # a file someone sent: shown or downloaded
                with open(payload.path, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                for key, value in {"Content-Type": payload.mimetype, "Content-Length": str(len(data)),
                                   **payload.headers()}.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/plugins/unified-inbox/"):
                return self._api("GET")
            if self.path.startswith("/dist/index.js"):
                return self._send(200, bundle.read_bytes(), "text/javascript; charset=utf-8")
            if urlparse(self.path).path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/__dev/receive":  # a message arrives on the phone, then one background sync
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                chat_id = body.get("chat_id", "wa-maya")
                msg = backend.receive(chat_id, body.get("text", "New message"), from_me=bool(body.get("from_me")))
                engine.sync_once()
                api.handle("GET", "/chats/" + chat_id, {"refresh": "1"}, {})  # sync may skip a chat it just read
                return self._send(200, json.dumps(msg).encode(), "application/json")
            self._api("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._api("DELETE")

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Inbox preview on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    serve(parser.parse_args().port)
