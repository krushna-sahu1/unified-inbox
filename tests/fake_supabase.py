"""An in-memory stand-in for a Supabase project, speaking the HTTP the cloud client uses
(PostgREST upserts and PATCH, the inbox_* SQL functions, and the Management API query)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlparse

from unified_inbox.client import InboxError
from unified_inbox.cloud_store import SPEC

BASE = datetime(2027, 1, 1, tzinfo=timezone.utc)


class FakeSupabase:
    def __init__(self, key: str = "sb_secret_test", token: str = "sbp_test"):
        self.key, self.token = key, token
        self.installed = False
        self.version = 2  # schema version of the tables (1: made by an earlier plugin version)
        self.down = False
        self.tables: dict[str, dict] = {s: {} for s in SPEC}
        self.leases: dict[str, tuple[str, float]] = {}
        self.tick = 0
        self.lease_now = 0.0  # seconds; tests move it to expire leases
        self.calls: list[tuple[str, str]] = []
        self.sql: list[str] = []

    def _stamp(self) -> str:
        self.tick += 1
        return (BASE + timedelta(milliseconds=self.tick)).isoformat()

    def _err(self, status: int, code: str, message: str) -> tuple[int, bytes]:
        return status, json.dumps({"code": code, "message": message, "details": None, "hint": None}).encode()

    def __call__(self, method: str, url: str, headers: dict, body):
        if self.down:
            raise InboxError("Could not reach Supabase: connection refused", kind="cloud")
        u = urlparse(url)
        self.calls.append((method, u.path))
        data = json.loads(body) if body else None
        if u.netloc == "api.supabase.com":
            if headers.get("Authorization") != f"Bearer {self.token}":
                return self._err(401, "", "bad token")
            self.sql.append(data["query"])
            self.installed = True
            self.version = max(self.version, 2 if "add column if not exists" in data["query"] else 1)
            return 201, b"[]"
        if headers.get("apikey") != self.key:
            return self._err(401, "", "Invalid API key")
        if not self.installed:
            return self._err(404, "PGRST205", "Could not find the table in the schema cache")
        path = u.path[len("/rest/v1/"):]
        query = dict(parse_qsl(u.query))
        if path == "inbox_meta":
            return 200, json.dumps([{"value": str(self.version)}]).encode()
        if path.startswith("rpc/"):
            return self._rpc(path[4:], data)
        table = path[len("inbox_"):]
        spec = SPEC[table]
        if method == "POST":
            assert "resolution=merge-duplicates" in headers.get("Prefer", "")
            assert len({tuple(sorted(r)) for r in data}) <= 1, "bulk rows must share keys"
            if self.version < 2:  # PostgREST refuses a column the table does not have
                unknown = {c for r in data for c in r if c in spec.v2}
                if unknown:
                    return self._err(400, "PGRST204", f"Could not find the {sorted(unknown)[0]!r} column")
            for row in data:
                cur = self.tables[table].setdefault(row[spec.key], {})
                cur.update(row)
                cur["modified_at"] = self._stamp()
            return 201, b""
        if method == "PATCH":
            out = []
            for row in self.tables[table].values():
                if all(_match(row, k, v) for k, v in query.items()):
                    row.update(data)
                    row["modified_at"] = self._stamp()
                    out.append(dict(row))
            return 200, json.dumps(out).encode()
        raise AssertionError(f"unexpected {method} {url}")

    def _rpc(self, fn: str, args: dict):
        if fn == "inbox_pull":
            rows = []
            for table, items in self.tables.items():
                for row in items.values():
                    if row["modified_at"] > args["p_since"] and row.get("device") != args["p_device"]:
                        r = {k: v for k, v in row.items() if k != "device"}
                        rows.append({"t": table, "r": r, "m": row["modified_at"]})
            rows.sort(key=lambda x: x["m"])
            return 200, json.dumps(rows[:args["p_limit"]]).encode()
        if fn == "inbox_acquire_lease":
            name, owner = args["p_name"], args["p_owner"]
            holder = self.leases.get(name)
            if not holder or holder[0] == owner or holder[1] < self.lease_now:
                self.leases[name] = (owner, self.lease_now + args["p_seconds"])
            return 200, json.dumps(self.leases[name][0] == owner).encode()
        if fn == "inbox_release_lease":
            if self.leases.get(args["p_name"], ("",))[0] == args["p_owner"]:
                self.leases.pop(args["p_name"])
            return 204, b""
        if fn == "inbox_cleanup":
            for items in self.tables.values():
                for k in [k for k, r in items.items() if r.get("deleted")]:
                    items.pop(k)
            return 204, b""
        raise AssertionError(fn)

    def live(self, table: str) -> dict:
        return {k: r for k, r in self.tables[table].items() if not r.get("deleted")}


def _match(row: dict, col: str, cond: str) -> bool:
    op, _, value = cond.partition(".")
    if op == "eq":
        return str(row.get(col)) == value
    if op == "is":
        return {"false": False, "true": True, "null": None}[value] == (row.get(col) or False if value != "null"
                                                                         else row.get(col))
    raise AssertionError(cond)

