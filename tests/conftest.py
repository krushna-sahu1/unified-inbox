"""Load the plugin folder as package ``unified_inbox`` (its directory name has a hyphen)."""

import importlib.util
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent

if "unified_inbox" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "unified_inbox", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["unified_inbox"] = module
    spec.loader.exec_module(module)

KEY = {"UNIPILE_API_KEY": "test-key"}


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def _close_what_a_test_opens(monkeypatch):
    """Every SQLite connection a test opens (directly or through the plugin) is closed when the
    test ends, and a plugin registered by a test has its background threads stopped, so no
    test leaves a thread or an open database behind (the suite also passes with -W error)."""
    opened = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield
    sys.modules["unified_inbox"]._unload()
    for conn in opened:
        conn.close()


def make_inbox(clock, config=None, secrets=None, sleeps=None, local_hour=12, backend=None, conn=None):
    """An Inbox wired to ``backend`` (any object with UnipileClient's methods) and an in-memory
    database. ``config``/``secrets`` default to nothing: pass them to make it connected."""
    from unified_inbox.inbox import Inbox

    config = dict(config or {})
    secrets = dict(secrets or {})
    db = conn or sqlite3.connect(":memory:", check_same_thread=False)
    keys = []

    def factory(api_key):
        keys.append(api_key)
        return backend

    inbox = Inbox(
        get_config=lambda k, d=None: config.get(k, d),
        get_secret=lambda n: secrets.get(n),
        open_db=lambda: db,
        clock=clock,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        rand=lambda: 0.5,
        local_now=lambda: datetime(2026, 11, 9, local_hour, 0),
        client_factory=factory,
    )
    inbox.client_keys = keys
    return inbox


def make_sample_inbox(clock, config=None, **kw):
    """A connected inbox over the fictional sample chats in tests/fixtures."""
    from fakes import SAMPLE_ACCOUNT_IDS, SampleBackend

    backend = SampleBackend(clock)
    inbox = make_inbox(clock, {"unipile_account_id": SAMPLE_ACCOUNT_IDS, **(config or {})}, secrets=KEY,
                       backend=backend, **kw)
    inbox.backend = backend
    return inbox


@pytest.fixture
def sample_inbox(clock):
    return make_sample_inbox(clock)
