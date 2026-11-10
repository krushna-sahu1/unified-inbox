"""Flag urgent and unanswered chats. Pure scoring over normalized chats and messages."""

from __future__ import annotations

import re
from typing import Iterable

URGENT_WORDS = (
    "urgent", "asap", "emergency", "immediately", "right now", "deadline", "today", "tonight",
    "help","important", "confirm", "within", "expires", "last chance",
)
_URGENT_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in URGENT_WORDS) + r")\b", re.IGNORECASE)


def assess(chat: dict, messages: list[dict], *, now: float, unanswered_hours: int) -> dict:
    """Score one chat. ``messages`` are oldest first."""
    inbound_tail = []
    for m in reversed(messages):
        if m["from_me"]:
            break
        inbound_tail.append(m)
    inbound_tail.reverse()
    last = messages[-1] if messages else None
    waiting = bool(inbound_tail)
    waiting_hours = (now - inbound_tail[0]["ts"]) / 3600 if waiting else 0.0

    reasons: list[str] = []
    score = 0
    if waiting:
        words = sorted({w.lower() for m in inbound_tail for w in _URGENT_RE.findall(m["text"] or "")})
        if words:
            score += 3
            reasons.append("mentions " + ", ".join(f'"{w}"' for w in words[:4]))
        if any("?" in (m["text"] or "") for m in inbound_tail):
            score += 1
            reasons.append("asked a question")
        if len(inbound_tail) >= 2:
            score += 1
            reasons.append(f"{len(inbound_tail)} messages in a row with no reply")
        if waiting_hours >= unanswered_hours:
            score += 1
            reasons.append(f"waiting {waiting_hours:.0f}h")
        if chat.get("is_group") and any((m.get("extra") or {}).get("mentioned") for m in inbound_tail):
            score += 3
            reasons.append("mentioned you")
    return {
        "chat_id": chat["id"],
        "name": chat.get("name", ""),
        "network": chat.get("network", ""),
        "is_group": bool(chat.get("is_group")),
        "waiting_for_you": waiting,
        "unanswered": waiting and waiting_hours >= unanswered_hours,
        "urgent": waiting and score >= 4,
        "score": score,
        "waiting_hours": round(waiting_hours, 1),
        "last_message": (last["text"] or "")[:200] if last else "",
        "last_from_them": waiting,
        "reasons": reasons,
    }


def rank(assessments: Iterable[dict]) -> dict:
    items = list(assessments)
    scams = [a for a in items if a.get("possible_scam")]
    rest = [a for a in items if not a.get("possible_scam")]
    urgent = sorted((a for a in rest if a["urgent"]), key=lambda a: (-a["score"], -a["waiting_hours"]))
    unanswered = sorted((a for a in rest if a["unanswered"] and not a["urgent"]),
                        key=lambda a: -a["waiting_hours"])
    waiting = [a for a in rest if a["waiting_for_you"] and not a["urgent"] and not a["unanswered"]]
    out = {"urgent": urgent, "unanswered": unanswered, "recent_waiting": waiting, "chats_checked": len(items)}
    if scams:
        out["possible_scams"] = scams
    return out
