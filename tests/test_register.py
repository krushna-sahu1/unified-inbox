import json
import re
from pathlib import Path

import unified_inbox

MANIFEST = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")


class RecordingCtx:
    def __init__(self):
        self.tools, self.commands, self.blueprints = [], [], []

    def get_config(self, key, default=None):
        return default

    def ask_choice(self, question, choices):
        return None

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        assert schema["name"] == name and callable(handler)
        self.tools.append(name)

    def register_command(self, name, handler, **kwargs):
        self.commands.append(name)

    def register_automation_blueprint(self, key, **kwargs):
        slots = {sl["name"] for sl in kwargs["slots"]}
        for name in re.findall(r"{(\w+)}", kwargs["prompt_template"] + kwargs["schedule_template"]):
            assert name in slots | {"minute", "hour"}, (key, name)  # every placeholder has a slot
        self.blueprints.append(key)


def test_registers_exactly_the_declared_tools():
    ctx = RecordingCtx()
    unified_inbox.register(ctx)
    declared = re.findall(r"^  - (inbox_\w+)$", MANIFEST, re.MULTILINE)
    assert sorted(ctx.tools) == sorted(declared)
    assert ctx.commands == ["inbox"]
    assert ctx.blueprints == ["daily-digest", "evening-wrap", "weekly-review", "inbox-alerts"]


def test_the_send_tool_always_comes_with_its_approval_hook():
    ctx = RecordingCtx()
    hooks = []
    ctx.register_hook = lambda name, fn: hooks.append(name)
    unified_inbox.register(ctx)
    assert "inbox_send" in ctx.tools and hooks == ["pre_tool_call"]


def test_database_is_shared_safely_across_threads(tmp_path, monkeypatch, clock):
    """Hermes's plugin_db may hand out a same-thread-only connection; the plugin reopens the
    file so the sync thread, webhook receiver and tool calls can all use it at once."""
    import sqlite3
    import sys
    import threading
    import types

    from conftest import KEY
    from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend
    from unified_inbox.inbox import Inbox
    from unified_inbox.sync import SyncEngine
    from unified_inbox.tools import build_handlers

    db_file = tmp_path / "data.db"
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_db = lambda name, filename: sqlite3.connect(db_file)  # check_same_thread=True
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)

    conn = unified_inbox._open_db()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    backend = SampleBackend(clock)
    config = {"unipile_account_id": SAMPLE_ACCOUNT_IDS}
    inbox = Inbox(get_config=lambda k, d=None: config.get(k, d), get_secret=KEY.get, open_db=lambda: conn,
                  clock=clock, sleep=lambda s: None, client_factory=lambda key: backend)
    errors = []

    def background():
        try:
            for _ in range(5):
                SyncEngine(inbox).sync_once()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=background)
    t.start()
    h = build_handlers(inbox)
    for _ in range(5):
        assert "error" not in json.loads(h["inbox_list_chats"]({}))
        assert "error" not in json.loads(h["inbox_search"]({"query": "plumber"}))
    t.join(10)
    assert errors == []


def _file_storage(tmp_path, monkeypatch):
    """Hermes's plugin_db, pointed at a real file."""
    import sqlite3
    import sys
    import types

    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_db = lambda name, filename: sqlite3.connect(tmp_path / filename, check_same_thread=False)
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)


def test_unload_stops_the_threads_and_closes_the_database(tmp_path, monkeypatch):
    """Hermes calls the on_unload callback when it unloads or reloads the plugin (and its plugin
    check does it on a temporary folder it then deletes, which fails on Windows while the
    database is open)."""
    import sqlite3

    _file_storage(tmp_path, monkeypatch)
    ctx = RecordingCtx()
    callbacks = []
    ctx.on_unload = callbacks.append
    unified_inbox.register(ctx)
    runtime = unified_inbox._RUNTIME
    inbox, worker = runtime["inbox"], runtime["worker"]
    conn = inbox.store()._conn
    assert len(callbacks) == 1 and worker._thread.is_alive()

    callbacks[0]()
    assert not worker._thread.is_alive() and unified_inbox._RUNTIME == {}
    try:
        conn.execute("SELECT 1")
        raise AssertionError("the database is still open")
    except sqlite3.ProgrammingError:
        pass
    (tmp_path / "data.db").unlink()  # nothing holds the file any more (Windows refuses otherwise)


def test_registering_again_replaces_the_previous_registration(tmp_path, monkeypatch):
    _file_storage(tmp_path, monkeypatch)
    unified_inbox.register(RecordingCtx())
    first, first_worker = unified_inbox._RUNTIME["inbox"], unified_inbox._RUNTIME["worker"]
    first.store()
    unified_inbox.register(RecordingCtx())
    assert unified_inbox._RUNTIME["inbox"] is not first and first._store is None
    assert not first_worker._thread.is_alive()
