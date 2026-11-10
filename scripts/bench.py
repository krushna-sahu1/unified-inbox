"""Speed check at a realistic size (development only).

    python scripts/bench.py [--chats 400] [--messages 100000]

Fills a temporary database with fictional chats and messages (English, Hindi and Hinglish),
then times the hot paths: the chat list, one chat's messages, word search, the person brief,
the Today page, the context engine's "what changed" queries and pruning. Prints each against
its target. Nothing touches the network.
"""

from __future__ import annotations

import argparse
import importlib.util
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if "unified_inbox" not in sys.modules:
    spec = importlib.util.spec_from_file_location("unified_inbox", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unified_inbox"] = mod
    spec.loader.exec_module(mod)

from unified_inbox.api import InboxAPI  # noqa: E402
from unified_inbox.inbox import Inbox  # noqa: E402
from unified_inbox.store import Store  # noqa: E402

WORDS = ("kal milte hain", "plumber aayega subah", "send me the deck", "kiraya bhej dena", "नमस्ते कैसे हो",
         "interview on thursday", "can you confirm", "thanks, see you", "the invoice is attached", "ok done")

TARGETS_MS = {"chat list (200)": 50, "one chat (40 messages)": 20, "word search": 100, "person brief": 50,
              "today page": 300, "chats needing chunks": 60, "chats needing memory": 60, "prune (daily)": 2000}


def fill(store: Store, chats: int, messages: int, now: float) -> None:
    rnd = random.Random(7)
    nets = ("whatsapp", "instagram", "linkedin")
    store.upsert_chats([{"id": f"c{i}", "account_id": f"acc_{nets[i % 3]}", "network": nets[i % 3],
                         "name": f"Person {i}", "unread": rnd.randint(0, 3), "ts": now - i * 600,
                         "last_text": rnd.choice(WORDS), "last_from_me": bool(i % 2)} for i in range(chats)], now)
    batch = []
    for n in range(messages):
        chat = n % chats
        batch.append({"id": f"m{n}", "chat_id": f"c{chat}", "from_me": rnd.random() < 0.4, "sender": f"Person {chat}",
                      "text": f"{rnd.choice(WORDS)} #{n}", "ts": now - chat * 600 - (n // chats) * 60})
        if len(batch) == 5000:
            store.upsert_messages(batch, now)
            batch = []
    store.upsert_messages(batch, now)


def timed(fn, repeat: int = 5) -> float:
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best * 1000


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chats", type=int, default=400)
    parser.add_argument("--messages", type=int, default=100_000)
    args = parser.parse_args(argv)
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "bench.db"), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        inbox = Inbox(get_config=lambda k, d=None: d, get_secret={"UNIPILE_API_KEY": "bench"}.get,
                      open_db=lambda: conn, client_factory=lambda key: None)
        store = inbox.store()
        store.set_meta("active_accounts", ["acc_whatsapp", "acc_instagram", "acc_linkedin"])
        for net in ("whatsapp", "instagram", "linkedin"):
            store.save_account({"id": f"acc_{net}", "network": net})
        store.set_meta("accounts_at", now + 10 ** 6)
        t = time.perf_counter()
        fill(store, args.chats, args.messages, now)
        print(f"filled {args.chats} chats, {args.messages} messages in {time.perf_counter() - t:.1f}s"
              f" (fts={store.fts}, vec={store.vec_index})")
        api = InboxAPI(inbox)
        results = {
            "chat list (200)": timed(lambda: api.handle("GET", "/chats", {"limit": 200})),
            "one chat (40 messages)": timed(lambda: store.cached_messages("c3", 40)),
            "word search": timed(lambda: store.search_messages("kiraya", limit=30)),
            "person brief": timed(lambda: inbox.context.brief(store.cached_chat("c3"))),
            "today page": timed(lambda: api.handle("GET", "/today"), repeat=2),
            "chats needing chunks": timed(lambda: store.chats_behind("chunk_ts", since_ts=0, limit=51)),
            "chats needing memory": timed(lambda: store.chats_behind("memory_ts", since_ts=0, limit=20)),
            "prune (daily)": timed(lambda: store.prune(now, 180), repeat=1),
        }
        worst = 0
        for name, ms in results.items():
            target = TARGETS_MS[name]
            ok = ms <= target
            worst += not ok
            print(f"  {'ok  ' if ok else 'SLOW'} {name:<26} {ms:8.1f} ms   (target {target} ms)")
        size = sum(f.stat().st_size for f in Path(tmp).glob("bench.db*")) / 1e6
        print(f"database size: {size:.1f} MB")
        conn.close()
    return 1 if worst else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
