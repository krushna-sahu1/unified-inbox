"""FastAPI routes for the Inbox page, mounted by Hermes at /api/plugins/unified-inbox/.

Shared by the web dashboard tab and the Hermes Desktop plugin. Hermes puts these routes behind
the dashboard login. All logic lives in ``api.py``; this file only adapts it.

The plugin normally runs in the same process (its ``register`` stores the live inbox). When the
dashboard runs in a process where the plugin was not registered, a second inbox is opened on the
same database: the background worker and every send are guarded by database leases, so the two
never step on each other.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

router = APIRouter()

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_api: Any = None


def _plugin_module():
    for mod in list(sys.modules.values()):
        if getattr(mod, "PLUGIN_NAME", None) == "unified-inbox" and hasattr(mod, "_RUNTIME"):
            return mod
    name = "unified_inbox_dashboard"
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(_PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _config_reader():
    """Plugin settings when the plugin's own ``ctx.get_config`` is not available here."""
    try:
        from hermes_cli.config import load_config  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return lambda key, default=None: default

    def get(key: str, default: Any = None) -> Any:
        cfg = load_config() or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("unified-inbox") or {}
        for section in (entry.get("settings") or {}, entry.get("config") or {}, entry):
            if isinstance(section, dict) and key in section:
                return section[key]
        return default

    return get


def get_api():
    global _api
    with _lock:
        mod = _plugin_module()
        runtime = getattr(mod, "_RUNTIME", {}) or {}
        if _api is not None and runtime.get("inbox") not in (None, _api.inbox):
            _api.inbox.close()  # already closed if the plugin was reloaded; harmless then
            _api = None  # use the plugin's current inbox, never a closed one
        if _api is None:
            api_mod = sys.modules[mod.__name__ + ".api"] if mod.__name__ + ".api" in sys.modules else \
                importlib.import_module(mod.__name__ + ".api")
            inbox = runtime.get("inbox")
            if inbox is None:
                inbox_mod = importlib.import_module(mod.__name__ + ".inbox")
                inbox = inbox_mod.Inbox(get_config=_config_reader(), get_secret=mod._get_secret,
                                        open_db=mod._open_db, transcriber=mod._make_transcriber())
            worker = runtime.get("worker")
            sync_once = worker.engine.sync_once if worker else None
            if sync_once is None:
                sync_mod = importlib.import_module(mod.__name__ + ".sync")
                sync_once = sync_mod.SyncEngine(inbox).sync_once
            _api = api_mod.InboxAPI(inbox, sync_once=sync_once)
        return _api


async def _dispatch(method: str, path: str, request: Request) -> Response:
    body: Any = {}
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - empty or not JSON
            body = {}
    status, payload = await asyncio.to_thread(get_api().handle, method, path, dict(request.query_params), body)
    if hasattr(payload, "headers") and hasattr(payload, "path"):  # a received file (api.FileResult)
        return FileResponse(payload.path, media_type=payload.mimetype, headers=payload.headers())
    return JSONResponse(payload, status_code=status)


@router.get("/{path:path}")
async def get_route(path: str, request: Request) -> JSONResponse:
    return await _dispatch("GET", path, request)


@router.post("/{path:path}")
async def post_route(path: str, request: Request) -> JSONResponse:
    return await _dispatch("POST", path, request)


@router.delete("/{path:path}")
async def delete_route(path: str, request: Request) -> JSONResponse:
    return await _dispatch("DELETE", path, request)
