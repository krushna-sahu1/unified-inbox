"""Optional sync with the user's own Supabase project (local-first).

SQLite stays the fast working copy; Supabase holds a full synced copy: chats, messages, drafts,
scheduled messages, the send log, memory (facts, open items, questions, summaries), watches,
alerts, and conversation chunks with their embeddings (pgvector). With several devices on the
same project:
  * only one device at a time runs the background work (reading Unipile, memory, scheduled
    sends): a lease held in Supabase; the others mirror its results and add their own changes;
  * the send limits count every device's sends, and a send waits for a fresh pull first. If
    Supabase cannot be reached, sends are refused (fail closed) rather than risk going over;
  * a scheduled message is claimed in Supabase before it goes out, so it is sent once.

Requests use only the standard library. The secret key travels in the ``apikey`` header (never
in a URL or a log). Tables live in ``public`` with the ``inbox_`` prefix, have Row Level
Security on with no policies (the publishable key sees nothing) and are granted to
``service_role`` only. Sync also keeps a free project from pausing for inactivity.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib import error as urlerror
from urllib import parse, request

from .client import InboxError
from .cloud_store import SPECS, remote_type
from .memory_store import unpack

SCHEMA_VERSION = 2       # what schema_sql() creates
MIN_SCHEMA_VERSION = 1   # the oldest tables this version still syncs with (without the newer columns)
PUSH_BATCH = 500
PULL_LIMIT = 2000
PULL_OVERLAP_SECONDS = 5.0     # re-read a few seconds back; applying is idempotent
WORKER_LEASE_SECONDS = 180
SEND_LEASE_SECONDS = 120
TOMBSTONE_DAYS = 30
USER_AGENT = "unified-inbox (Hermes plugin)"

Transport = Callable[[str, str, dict, Optional[bytes]], tuple[int, bytes]]


def _http(method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple[int, bytes]:
    req = request.Request(url, data=body, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urlerror.HTTPError as exc:
        try:
            return exc.code, exc.read() or b""
        finally:
            exc.close()  # the error response holds the connection open until closed
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise InboxError(f"Could not reach Supabase: {exc}", kind="cloud") from None


def project_ref(url: str) -> str:
    m = re.match(r"^https://([a-z0-9]+)\.supabase\.(co|in)/?$", (url or "").strip().lower())
    return m.group(1) if m else ""


def _vector_text(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def _parse_vector(value: Any) -> list[float]:
    if isinstance(value, list):
        return [float(x) for x in value]
    if isinstance(value, str) and value.startswith("["):
        return [float(x) for x in value.strip("[]").split(",") if x.strip()]
    return []


# ─── the Supabase schema ─────────────────────────────────────────────────────

def schema_sql() -> str:
    """Everything the sync needs in the user's project. Safe to run again (idempotent)."""
    out = ["-- unified-inbox: tables for syncing your inbox. Safe to run more than once.",
           "create extension if not exists vector with schema extensions;",
           "create table if not exists public.inbox_meta (key text primary key, value text not null);",
           "create table if not exists public.inbox_leases (name text primary key, owner text not null,"
           " until timestamptz not null);"]
    names = ["inbox_meta", "inbox_leases"]
    for spec in SPECS:
        t = f"inbox_{spec.table}"
        names.append(t)
        cols = [f"{spec.key} text primary key"] + [f"{c} {remote_type(spec, c)}" for c in spec.cols if c != spec.key]
        if spec.table == "chunks":
            cols += ["model text", "dims integer", "embedding extensions.vector"]
        cols += ["device text not null default ''", "deleted boolean not null default false",
                 "modified_at timestamptz not null default clock_timestamp()"]
        out.append(f"create table if not exists public.{t} ({', '.join(cols)});")
        for c in spec.v2:  # tables made by an earlier version gain the newer columns
            out.append(f"alter table public.{t} add column if not exists {c} {remote_type(spec, c)};")
        out.append(f"create index if not exists {t}_modified on public.{t} (modified_at);")
        out.append(f"drop trigger if exists {t}_touch on public.{t};")
        out.append(f"create trigger {t}_touch before insert or update on public.{t}"
                   " for each row execute function public.inbox_touch();")
    out.insert(4, "create or replace function public.inbox_touch() returns trigger language plpgsql as $$"
                  " begin new.modified_at := clock_timestamp(); return new; end $$;")
    for t in names:
        out.append(f"alter table public.{t} enable row level security;")
        out.append(f"revoke all on table public.{t} from anon, authenticated;")
        out.append(f"grant select, insert, update, delete on table public.{t} to service_role;")
    unions = " union all ".join(
        f"select '{s.table}'::text as t, to_jsonb(x) - 'device' as r, x.modified_at as m"
        f" from public.inbox_{s.table} x where x.modified_at > p_since and x.device <> p_device" for s in SPECS)
    out += [
        "create or replace function public.inbox_pull(p_since timestamptz, p_device text, p_limit integer)"
        " returns json language sql stable security definer set search_path = public, extensions as $$"
        f" select coalesce(json_agg(json_build_object('t', y.t, 'r', y.r, 'm', y.m) order by y.m), '[]'::json)"
        f" from (select * from ({unions}) u order by u.m limit p_limit) y $$;",
        "create or replace function public.inbox_acquire_lease(p_name text, p_owner text, p_seconds integer)"
        " returns boolean language plpgsql security definer set search_path = public as $$"
        " declare holder text; begin"
        " insert into public.inbox_leases(name, owner, until) values (p_name, p_owner,"
        " clock_timestamp() + make_interval(secs => p_seconds))"
        " on conflict (name) do update set owner = excluded.owner, until = excluded.until"
        " where public.inbox_leases.owner = excluded.owner or public.inbox_leases.until < clock_timestamp();"
        " select owner into holder from public.inbox_leases where name = p_name;"
        " return holder = p_owner; end $$;",
        "create or replace function public.inbox_release_lease(p_name text, p_owner text)"
        " returns void language sql security definer set search_path = public as $$"
        " delete from public.inbox_leases where name = p_name and owner = p_owner $$;",
        "create or replace function public.inbox_cleanup(p_days integer) returns void language plpgsql"
        " security definer set search_path = public as $$ begin "
        + " ".join(f"delete from public.inbox_{s.table} where deleted and modified_at < clock_timestamp()"
                   " - make_interval(days => p_days);" for s in SPECS) + " end $$;",
    ]
    for fn in ("inbox_pull(timestamptz, text, integer)", "inbox_acquire_lease(text, text, integer)",
               "inbox_release_lease(text, text)", "inbox_cleanup(integer)", "inbox_touch()"):
        out.append(f"revoke all on function public.{fn} from public, anon, authenticated;")
        out.append(f"grant execute on function public.{fn} to service_role;")
    out.append(f"insert into public.inbox_meta(key, value) values ('schema_version', '{SCHEMA_VERSION}')"
               " on conflict (key) do update set value = excluded.value;")
    out.append("notify pgrst, 'reload schema';")
    return "\n".join(out) + "\n"


# ─── REST client ─────────────────────────────────────────────────────────────

class CloudClient:
    def __init__(self, url: str, key: str, transport: Optional[Transport] = None, access_token: str = ""):
        self.url = url.rstrip("/")
        self.ref = project_ref(url)
        self._key = key
        self._token = access_token
        self._send = transport or _http

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"apikey": self._key, "Accept": "application/json", "Content-Type": "application/json",
             "User-Agent": USER_AGENT}
        if self._key.startswith("eyJ"):  # legacy service_role JWT keys also go as a bearer token
            h["Authorization"] = f"Bearer {self._key}"
        h.update(extra or {})
        return h

    def _rest(self, method: str, path: str, *, query: Optional[dict] = None, body: Any = None,
              prefer: str = "") -> Any:
        url = f"{self.url}/rest/v1/{path}" + ("?" + parse.urlencode(query, safe=",.()*") if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        status, raw = self._send(method, url, self._headers({"Prefer": prefer} if prefer else None), data)
        if status >= 400:
            try:
                err = json.loads(raw or b"{}")
            except ValueError:
                err = {}
            code = str(err.get("code") or "")
            msg = err.get("message") or raw[:200].decode("utf-8", "replace") or f"HTTP {status}"
            if status == 404 or code in ("PGRST205", "PGRST202", "42P01", "42883"):
                raise InboxError(f"Supabase is not set up for unified-inbox yet ({msg}).", status=status,
                                 kind="cloud_schema")
            if status in (401, 403):
                raise InboxError("Supabase rejected the key. Use the project's secret key (Settings -> API Keys).",
                                 status=status, kind="cloud_auth")
            raise InboxError(f"Supabase answered {status}: {msg}", status=status, kind="cloud")
        return json.loads(raw) if raw else None

    # schema
    def schema_version(self) -> int:
        rows = self._rest("GET", "inbox_meta", query={"key": "eq.schema_version", "select": "value"})
        return int(rows[0]["value"]) if rows else 0

    def run_sql(self, sql: str) -> None:
        """Create the tables through the Management API (needs a personal access token)."""
        if not self._token or not self.ref:
            raise InboxError("An access token and a https://<ref>.supabase.co URL are needed to set up tables"
                             " automatically.", kind="input")
        url = f"https://api.supabase.com/v1/projects/{self.ref}/database/query"
        status, raw = self._send("POST", url, {"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT,
                                               "Content-Type": "application/json"},
                                 json.dumps({"query": sql}).encode("utf-8"))
        if status >= 400:
            raise InboxError(f"Supabase could not create the tables ({status}): "
                             f"{raw[:300].decode('utf-8', 'replace')}", status=status, kind="cloud")

    # data
    def upsert(self, table: str, rows: list[dict]) -> None:
        if rows:
            self._rest("POST", f"inbox_{table}", body=rows, prefer="resolution=merge-duplicates,return=minimal")

    def pull(self, since: str, device: str, limit: int) -> list[dict]:
        return self._rest("POST", "rpc/inbox_pull", body={"p_since": since, "p_device": device, "p_limit": limit}) or []

    def lease(self, name: str, owner: str, seconds: int) -> bool:
        return bool(self._rest("POST", "rpc/inbox_acquire_lease",
                               body={"p_name": name, "p_owner": owner, "p_seconds": seconds}))

    def release(self, name: str, owner: str) -> None:
        self._rest("POST", "rpc/inbox_release_lease", body={"p_name": name, "p_owner": owner})

    def claim_schedule(self, schedule_id: str, device: str) -> bool:
        """Atomically move a scheduled message to "sending" in Supabase. False if another device
        already took it, or it was cancelled or changed."""
        rows = self._rest("PATCH", "inbox_schedules",
                          query={"id": f"eq.{schedule_id}", "status": "eq.scheduled", "deleted": "is.false"},
                          body={"status": "sending", "device": device}, prefer="return=representation")
        return bool(rows)

    def cleanup(self, days: int) -> None:
        self._rest("POST", "rpc/inbox_cleanup", body={"p_days": days})


# ─── the sync engine ─────────────────────────────────────────────────────────

class CloudSync:
    def __init__(self, inbox, transport: Optional[Transport] = None):
        self.inbox = inbox
        self._transport = transport
        self._client: Optional[CloudClient] = None
        self._client_key: tuple = ()

    # configuration
    def _settings(self) -> tuple[str, str, str]:
        url = str(self.inbox._get_config("supabase_url", "") or "").strip().rstrip("/")
        key = (self.inbox._get_secret("SUPABASE_SECRET_KEY") or "").strip()
        token = (self.inbox._get_secret("SUPABASE_ACCESS_TOKEN") or "").strip()
        return url, key, token

    @property
    def configured(self) -> bool:
        url, key, _ = self._settings()
        return bool(url and key)

    def client(self) -> CloudClient:
        url, key, token = self._settings()
        if not (url and key):
            raise InboxError("Cloud sync is off. Add your Supabase URL and secret key in plugin settings.",
                             kind="input")
        if not url.startswith("https://"):
            raise InboxError("The Supabase URL must start with https://", kind="input")
        ident = (url, key, token)
        if ident != self._client_key:
            self._client = CloudClient(url, key, transport=self._transport, access_token=token)
            self._client_key = ident
        return self._client

    @property
    def active(self) -> bool:
        """Configured, and the tables were found: changes are being recorded and synced."""
        store = self.inbox.store()
        return self.configured and store.get_meta("cloud_active") == self._settings()[0] and store.outbox_enabled()

    def activate(self) -> dict:
        """Check the project and start syncing (the first time: copy everything up)."""
        store = self.inbox.store()
        url = self._settings()[0]
        version = self.client().schema_version()
        if version < MIN_SCHEMA_VERSION:
            raise InboxError("Supabase is not set up for unified-inbox yet.", kind="cloud_schema")
        first = store.get_meta("cloud_active") != url
        store.enable_outbox(seed=first)
        if first:
            store.set_meta("cloud_pull_since", "1970-01-01T00:00:00+00:00")
            store.set_meta("cloud_schema_version", version)
        else:
            self._note_version(version)
        store.set_meta("cloud_active", url)
        self._state(error="")
        return self.status()

    def remote_version(self) -> int:
        """The schema version of the Supabase tables, as last seen."""
        try:
            return int(self.inbox.store().get_meta("cloud_schema_version") or MIN_SCHEMA_VERSION)
        except (TypeError, ValueError):
            return MIN_SCHEMA_VERSION

    def _note_version(self, version: int) -> None:
        """Remember the tables' version; when they were just updated, send again the rows whose
        newer columns Supabase has not seen yet."""
        store = self.inbox.store()
        before = self.remote_version()
        if version == before:
            return
        store.set_meta("cloud_schema_version", version)
        if before < SCHEMA_VERSION <= version:
            store.requeue_outbox([s.table for s in SPECS if s.v2])

    def deactivate(self) -> None:
        store = self.inbox.store()
        store.disable_outbox()
        store.set_meta("cloud_active", "")

    def setup(self) -> dict:
        """Create the tables (with an access token) and start syncing."""
        client = self.client()
        try:
            if client.schema_version() >= SCHEMA_VERSION:
                return self.activate()
        except InboxError as exc:
            if exc.kind != "cloud_schema":
                raise
        client.run_sql(schema_sql())
        return self.activate()

    def ensure(self) -> bool:
        """Called by the worker: follow the settings (turn on, off, or switch projects)."""
        store = self.inbox.store()
        if not self.configured:
            if store.get_meta("cloud_active") or store.outbox_enabled():
                self.deactivate()
            return False
        if self.active:
            return True
        try:
            self.activate()
            return True
        except InboxError as exc:
            self._state(error=str(exc))
            return False

    # push and pull
    def push(self, *, limit_rounds: int = 20) -> int:
        """Send recorded local changes. Returns how many rows went up."""
        store, client = self.inbox.store(), self.client()
        device = store.device_id()
        version = self.remote_version()
        sent = 0
        for spec in SPECS:
            for _ in range(limit_rounds):
                batch = store.outbox_batch(spec.table, PUSH_BATCH)
                if not batch:
                    break
                rows = store.export_rows(spec.table, [k for k, op, _, _ in batch if op == "u"])
                payload = [self._remote_row(spec, r, device, version) for r in rows]
                if spec.table == "chunks" and payload:
                    vecs = store.chunk_vectors_for([p["gid"] for p in payload])
                    for p in payload:
                        model, dims, blob = vecs.get(p["gid"], (None, None, None))
                        p["model"], p["dims"] = model, dims
                        p["embedding"] = _vector_text(unpack(blob)) if blob else None
                # a deleted row becomes a tombstone, so other devices delete it too
                tombstones = [{spec.key: (gid if spec.gid else k), "device": device, "deleted": True}
                              for k, op, gid, _ in batch if op == "d" and (gid if spec.gid else k)]
                client.upsert(spec.table, payload)
                client.upsert(spec.table, tombstones)
                store.outbox_done(spec.table, [(k, ts) for k, _, _, ts in batch])
                sent += len(payload) + len(tombstones)
                if len(batch) < PUSH_BATCH:
                    break
        self._state(last_push_at=self.inbox.clock())
        return sent

    @staticmethod
    def _remote_row(spec, row: dict, device: str, version: int = SCHEMA_VERSION) -> dict:
        out = {c: row[c] for c in spec.cols if version >= 2 or c not in spec.v2}
        out.update(device=device, deleted=False)
        return out

    def pull(self) -> int:
        """Apply other devices' changes. Returns how many rows came down."""
        store, client, inbox = self.inbox.store(), self.client(), self.inbox
        device = store.device_id()
        since = store.get_meta("cloud_pull_since") or "1970-01-01T00:00:00+00:00"
        total = 0
        for page in range(50):
            # Only the first request looks a few seconds back (for rows that were still being
            # written last time). Later pages continue exactly where the page before ended:
            # stepping back again would return the same full page for ever when more than a page
            # of rows was written within those seconds (a first full copy from another device).
            items = client.pull(since if page else self._back(since), device, PULL_LIMIT)
            if not items:
                break
            grouped: dict[str, list[dict]] = {}
            for it in items:
                grouped.setdefault(it["t"], []).append(it["r"])
            now = inbox.clock()
            for spec in SPECS:  # dependency order
                rows = grouped.get(spec.table)
                if not rows:
                    continue
                if spec.table == "messages":
                    store.apply_remote_messages(rows, now, self._baseline)
                else:
                    store.apply_remote(spec.table, rows, now)
                if spec.table == "chunks":
                    store.apply_remote_vectors([(r["gid"], r.get("model") or "", int(r.get("dims") or 0),
                                                 _parse_vector(r.get("embedding")))
                                                for r in rows if not r.get("deleted") and r.get("embedding")])
            total += len(items)
            newest = items[-1]["m"]
            if len(items) < PULL_LIMIT or (page and newest == since):
                since = newest
                break
            since = newest
        store.set_meta("cloud_pull_since", since)
        self._state(last_pull_at=inbox.clock())
        return total

    def _baseline(self, account_id: str) -> float:
        """Messages that arrive by pull after this device joined count as news here too."""
        store = self.inbox.store()
        value = store.get_meta(f"baseline:{account_id}")
        if value is None:
            value = self.inbox.clock()
            store.set_meta(f"baseline:{account_id}", value)
        return float(value)

    @staticmethod
    def _back(iso: str) -> str:
        try:
            ts = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() - PULL_OVERLAP_SECONDS
        except ValueError:
            ts = 0.0
        return datetime.fromtimestamp(max(ts, 0.0), tz=timezone.utc).isoformat()

    def push_quietly(self) -> None:
        """After a change made by a person (page or command): share it right away if possible;
        otherwise the worker pushes it on its next step."""
        if not self.active:
            return
        try:
            self.push()
        except InboxError as exc:
            self._state(error=str(exc))

    def sync(self) -> dict:
        pushed = self.push()
        pulled = self.pull()
        return {"pushed": pushed, "pulled": pulled}

    # coordination
    def worker_role(self) -> str:
        """"leader" runs the background work for this project; "follower" only mirrors."""
        store = self.inbox.store()
        role = "leader" if self.client().lease("worker", store.device_id(), WORKER_LEASE_SECONDS) else "follower"
        store.set_meta("cloud_role", role)
        return role

    def last_role(self) -> str:
        return self.inbox.store().get_meta("cloud_role") or "leader"

    def before_send(self) -> None:
        """Fail closed: a send waits for every device's latest sends and the send lease."""
        if not self.active:
            return
        store = self.inbox.store()
        try:
            self.push()
            self.pull()
            if not self.client().lease("send", store.device_id(), SEND_LEASE_SECONDS):
                raise InboxError("Another device is sending a message right now. Try again in a moment.",
                                 kind="paused")
        except InboxError as exc:
            if exc.kind == "paused":
                raise
            raise InboxError(f"Not sent: your Supabase can't be reached, so sends from your other devices can't be"
                             f" counted against the limits. Try again shortly. ({exc})", kind="cloud") from None

    def after_send(self) -> None:
        if not self.active:
            return
        store = self.inbox.store()
        try:
            self.push()
            self.client().release("send", store.device_id())
        except InboxError as exc:
            self._state(error=str(exc))  # the lease expires on its own; the push retries next cycle

    def claim_schedule(self, schedule_id: str) -> bool:
        if not self.active:
            return True
        self.push()
        return self.client().claim_schedule(schedule_id, self.inbox.store().device_id())

    def daily_cleanup(self) -> None:
        store, now = self.inbox.store(), self.inbox.clock()
        if now - float(store.get_meta("cloud_cleanup_at", 0.0)) < 86400:
            return
        self.client().cleanup(TOMBSTONE_DAYS)
        store.set_meta("cloud_cleanup_at", now)
        self._note_version(self.client().schema_version())  # the tables may have been updated meanwhile

    # status
    def _state(self, **fields: Any) -> None:
        store = self.inbox.store()
        state = store.get_meta("cloud_state", {}) or {}
        state.update(fields)
        store.set_meta("cloud_state", state)

    def status(self) -> dict:
        store = self.inbox.store()
        url, _, token = self._settings()
        st = store.get_meta("cloud_state", {}) or {}
        ref = project_ref(url)
        out = {"configured": self.configured, "active": self.active, "url": url, "project_ref": ref,
               "can_create_tables": bool(token and ref), "pending_changes": store.outbox_count(),
               "role": store.get_meta("cloud_role") or "", "last_push_at": st.get("last_push_at"),
               "last_pull_at": st.get("last_pull_at"), "error": st.get("error", ""),
               "device_id": store.device_id()}
        if out["active"]:
            out["schema_version"] = self.remote_version()
            # still syncing, but reply quotes, "drafted by Hermes" marks, a draft's original words and
            # the new-conversation count stay on each device until the tables are updated
            out["update_available"] = out["schema_version"] < SCHEMA_VERSION
        if ref:
            out["sql_editor_url"] = f"https://supabase.com/dashboard/project/{ref}/sql/new"
        return out
