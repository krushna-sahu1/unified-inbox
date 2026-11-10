"""Talk to your inbox: a spoken "what did I miss?", and replies you dictate.

The brief is written for listening: short sentences, the most important first (who is waiting
and why, likely scams, drafts ready, plans and birthdays today or tomorrow). It is built from
the local copy only, so it is instant and costs nothing. It is read aloud with Hermes's own
text-to-speech (the voice you set up in Hermes: Edge, OpenAI, ElevenLabs, a local engine...);
in a messaging app Hermes can send it to you as a voice note.

Dictation turns a short recording (from the Inbox page's microphone button) into text with
Hermes's own speech-to-text, for you to check before it becomes a draft. The recording is
deleted as soon as it is transcribed; spoken briefs are kept for a day at most.
"""

from __future__ import annotations

import base64
import binascii
import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import triage as triage_mod
from .client import InboxError
from .pacing import DAY, NETWORK_LABELS

MAX_AUDIO_BYTES = 10 * 1024 * 1024
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
               "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav", "audio/x-wav": ".wav",
               "audio/wave": ".wav", "audio/flac": ".flac", "video/webm": ".webm"}
KEEP_SECONDS = DAY
BRIEF_CHATS = 40


def _app(network: str) -> str:
    return NETWORK_LABELS.get(network, network)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _clip(text: str, words: int = 14) -> str:
    parts = " ".join((text or "").split()).split(" ")
    out = " ".join(parts[:words])
    return out + ("..." if len(parts) > words else "")


class Voice:
    def __init__(self, inbox, speaker=None):
        self.inbox = inbox
        # Text-to-speech: (text, output path) -> {"success", "file_path", "media_tag", "error"}.
        self.speaker = speaker

    # ── the brief ──
    def brief(self) -> dict:
        """What you missed, written to be heard. {"say": text, "items": [...]} (local data only)."""
        inbox = self.inbox
        store, s, now = inbox.store(), inbox.settings, inbox.clock()
        hour = datetime.fromtimestamp(now).hour
        greet = "Good morning." if hour < 12 else "Good afternoon." if hour < 17 else "Good evening."
        snoozed = store.snoozed_chat_ids(now)
        shield = getattr(inbox, "shield", None)
        waiting, scams = [], []
        for chat in store.cached_chats(limit=BRIEF_CHATS):
            if chat["last_from_me"] or chat["is_muted"] or chat["is_archived"] or chat["id"] in snoozed:
                continue
            if now - chat["ts"] > 2 * DAY:
                continue
            msgs = store.cached_messages(chat["id"], 10)
            if not msgs:
                continue
            verdict = shield.for_chat(chat, msgs) if shield is not None else None
            if verdict:
                scams.append((chat, verdict))
                continue
            a = triage_mod.assess(chat, msgs, now=now, unanswered_hours=s.unanswered_hours)
            if a["waiting_for_you"] and (not chat["is_group"] or "mentioned you" in a["reasons"]):
                a["last"] = next((m["text"] for m in reversed(msgs) if not m["from_me"] and m["text"]), "")
                waiting.append(a)
        waiting.sort(key=lambda a: (not a["urgent"], -a["score"], -a["waiting_hours"]))
        lines, items = [greet], []
        if not waiting:
            lines.append("Nobody is waiting for a reply.")
        else:
            urgent = [a for a in waiting if a["urgent"]]
            lines.append(_plural(len(waiting), "person is", "people are") + " waiting for you"
                         + (f", {len(urgent)} urgent." if urgent else "."))
            for a in waiting[:3]:
                said = _clip(a["last"])
                line = f"{a['name'] or 'Someone'} on {_app(a['network'])}" + (f" wrote: {said}" if said else
                                                                               " sent a file")
                lines.append(line if line.endswith((".", "?", "!")) else line + ".")
                items.append({"type": "waiting", "chat_id": a["chat_id"], "name": a["name"], "urgent": a["urgent"]})
            if len(waiting) > 3:
                lines.append(f"And {_plural(len(waiting) - 3, 'more chat', 'more chats')}.")
        for chat, v in scams[:2]:
            lines.append(f"Careful: the message from {chat['name'] or 'an unknown sender'} on {_app(chat['network'])}"
                         f" looks like a scam. It {v['reasons'][0]}." if v["reasons"][0].startswith("asks")
                         else f"Careful: the message from {chat['name'] or 'an unknown sender'} on"
                              f" {_app(chat['network'])} looks like a scam: {v['reasons'][0]}.")
            items.append({"type": "scam", "chat_id": chat["id"], "name": chat["name"], "level": v["level"]})
        drafts = store.list_drafts("pending", limit=50)
        if drafts:
            auto = sum(1 for d in drafts if d["origin"] == "auto")
            lines.append(_plural(len(drafts), "draft is", "drafts are") + " ready for you to send"
                         + (f", {auto} written by Hermes." if auto else "."))
            items.append({"type": "drafts", "count": len(drafts)})
        planner = getattr(inbox, "planner", None)
        if planner is not None:
            today = datetime.fromtimestamp(now).date()
            for v in planner.upcoming(days=2)[:3]:
                d = datetime.fromtimestamp(v["start_ts"])
                day = "Today" if d.date() == today else "Tomorrow" if (d.date() - today).days == 1 else None
                if not day:
                    continue
                at = "" if v["all_day"] else f" at {d.strftime('%I:%M %p').lstrip('0').replace(':00', '')}"
                clash = " That clashes with something else." if v.get("free") is False else ""
                lines.append(f"{day}: {v['title']}{at}.{clash}")
                items.append({"type": "plan", "plan_id": v["plan_id"], "title": v["title"]})
        radar = getattr(inbox, "radar", None)
        if radar is not None and s.people_radar:
            for b in radar.birthdays(radar._people(), now, days_ahead=1):
                lines.append(f"It's {b['name']}'s birthday {'today' if b['in_days'] == 0 else 'tomorrow'}.")
                items.append({"type": "birthday", "chat_id": b["chat_id"], "name": b["name"]})
        questions = store.count_questions()
        if questions:
            lines.append(f"Memory has {_plural(questions, 'question', 'questions')} for you.")
        return {"say": " ".join(lines), "items": items, "at": now}

    # ── speaking ──
    def _folder(self) -> Path:
        base = self.inbox.store().data_dir()
        if base is None:
            raise InboxError("The plugin has no folder for audio files in this Hermes.", kind="setup")
        folder = base / "voice"
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - KEEP_SECONDS
        for old in folder.iterdir():
            try:
                if old.is_file() and old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        return folder

    def speak(self, text: str) -> dict:
        """Read ``text`` aloud with Hermes's text-to-speech. {"path", "file", "media_tag"}."""
        text = (text or "").strip()
        if not text:
            raise InboxError("Nothing to say.", kind="input")
        if self.speaker is None:
            raise InboxError("Text-to-speech is not available in this Hermes. Set it up in Hermes (tts in"
                             " config.yaml), then try again.", kind="voice")
        folder = self._folder()
        name = f"brief-{uuid.uuid4().hex[:10]}.mp3"
        try:
            result = self.speaker(text[:3000], str(folder / name)) or {}
        except Exception as exc:  # noqa: BLE001 - the TTS backend failed; report it plainly
            result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        if not result.get("success") or not result.get("file_path"):
            raise InboxError(f"Could not make the audio: {result.get('error') or 'text-to-speech failed'}",
                             kind="voice")
        made = Path(str(result["file_path"]))
        if not made.is_file():
            raise InboxError("Text-to-speech reported success but wrote no file.", kind="voice")
        if made.parent.resolve() != folder.resolve():  # the engine chose its own place: keep a copy here
            target = folder / (f"brief-{uuid.uuid4().hex[:10]}" + (made.suffix or ".mp3"))
            shutil.copyfile(made, target)
            made = target
        return {"file": made.name, "path": str(made), "media_tag": result.get("media_tag") or f"MEDIA:{made}"}

    def audio_file(self, name: str) -> Path:
        """A spoken brief made earlier, by its file name (only from the plugin's voice folder)."""
        folder = self._folder()
        path = (folder / Path(str(name or "")).name).resolve()
        if path.parent != folder.resolve() or not path.is_file() or not path.name.startswith("brief-"):
            raise InboxError("That audio is gone (spoken briefs are kept for a day).", kind="not_found")
        return path

    # ── dictation ──
    def dictate(self, audio_b64: str, mimetype: str) -> dict:
        """A short recording -> its text (nothing is saved or sent)."""
        inbox = self.inbox
        if inbox.transcriber is None:
            raise InboxError("Speech-to-text is not available in this Hermes. Set it up in Hermes (stt in"
                             " config.yaml: local Whisper, Groq, OpenAI...), then try again.", kind="transcription")
        kind = (mimetype or "").split(";")[0].strip().lower()
        ext = AUDIO_TYPES.get(kind)
        if not ext:
            raise InboxError(f"Unsupported recording type {kind or '(none)'}.", kind="input")
        raw = str(audio_b64 or "")
        if "," in raw[:100] and raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        if len(raw) > MAX_AUDIO_BYTES * 4 // 3 + 16:
            raise InboxError("The recording is too long (10 MB at most).", kind="input")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise InboxError("The recording could not be read.", kind="input") from None
        if not data:
            raise InboxError("The recording is empty.", kind="input")
        path = self._folder() / f"in-{uuid.uuid4().hex}{ext}"
        try:
            path.write_bytes(data)
            try:
                result = inbox.transcriber(str(path)) or {}
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if not result.get("success"):
            raise InboxError(f"Could not turn the recording into text: {result.get('error') or 'failed'}",
                             kind="transcription")
        text = " ".join(str(result.get("transcript") or "").split())
        return {"text": text, "heard_nothing": not text}


def make_speaker(dispatch):
    """Text-to-speech through Hermes's own ``text_to_speech`` tool (``ctx.dispatch_tool``)."""

    def speak(text: str, output_path: str) -> dict:
        raw = dispatch("text_to_speech", {"text": text, "output_path": output_path})
        try:
            out = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError):
            return {"success": False, "error": str(raw)[:200]}
        if "error" in out and not out.get("success"):
            return {"success": False, "error": str(out.get("error"))[:300]}
        return out

    return speak
