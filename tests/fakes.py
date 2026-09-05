"""Test doubles for the two things the app cannot reach in a test run: the
database and the public internet.

Both fakes are *behavioural*. Neither is a ``MagicMock``, and that is the whole
point of this module.

The suite this replaces installed ``sys.modules["supabase"] = MagicMock()``, so
``client.table("favorites").select("*").eq("device_id", d).execute()`` returned
a mock whose ``.data`` was another mock. Every repository call "succeeded" and
no assertion about *which rows come back* meant anything. That matters here more
than usual: the entire reason ``app/db/repositories.py`` exists is to guarantee
that no user-owned row is read or written without a device filter, and the bug
it was written to fix — ``GET /api/favorites`` returning every row in the table
to every caller — is invisible to a mock that ignores filters. So
:class:`FakeSupabaseClient` actually applies them.

:class:`UpstreamRouter` does the same job for outbound HTTP. It is installed as
an ``httpx.MockTransport`` on the pooled client in ``app.core.http``, which
means every service goes through it without any per-module patching, and the
real retry, throttle and error-mapping code in ``request_json`` still runs. Any
outbound request that no test stubbed is recorded in
:attr:`UpstreamRouter.unmatched` and fails the test at teardown, so the suite
cannot quietly start depending on a live third-party API.
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class FakeDBError(Exception):
    """Shaped like a postgrest error.

    ``app.db.repositories._is_unique_violation`` inspects ``.code`` first and
    falls back to substring-matching the message, so both are populated.
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def unique_violation(constraint: str) -> FakeDBError:
    return FakeDBError(
        f'duplicate key value violates unique constraint "{constraint}"',
        code="23505",
    )


class FakeResponse:
    """The two attributes the repositories read off a postgrest result."""

    __slots__ = ("count", "data")

    def __init__(self, data: List[Dict[str, Any]], count: Optional[int] = None) -> None:
        self.data = data
        self.count = count


# Unique indexes and primary keys that exist in `migrations/0001_initial_schema.sql`
# and `0002_alert_delivery.sql`. Declared here so the duplicate-handling branches
# in the repositories (idempotent favourite creation, one-vote-per-device,
# one-delivery-per-event) are exercised against something that actually rejects
# the second write.
#
# NULLs are treated as distinct, like Postgres: two favourites with no
# station_uid do not collide. Without that, saving a second custom location
# would report "already saved".
UNIQUE_CONSTRAINTS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "favorites": (
        ("device_id", "station_uid"),
        ("device_id", "lat", "lon"),
    ),
    "report_votes": (("report_id", "device_id"),),
    "push_tokens": (("device_id",),),
    # The composite primary key that makes push delivery idempotent. Present
    # here so a test can prove the dispatcher's claim is what stops a second
    # send, rather than some ordering accident in the dispatcher's own logic.
    "sent_alerts": (("device_id", "event_id"),),
}


class ExecutedQuery:
    """A record of one query, so a test can assert on *how* it was scoped.

    ``test_security.py`` uses this to prove that a delete was filtered by
    ``device_id`` and not only by row id — a query that returns the right rows
    for the wrong reason still passes a response-shape assertion.

    ``on_conflict`` and ``ignore_duplicates`` are recorded for the same reason.
    The push dispatcher's idempotency rests on its claim being an
    ``ON CONFLICT DO NOTHING``; an upsert that merged instead would hand back
    every row as though this pass had won it, and the resulting double-send is
    indistinguishable from correct behaviour in the response shape alone.
    """

    __slots__ = ("columns", "filters", "ignore_duplicates", "on_conflict", "op", "table")

    def __init__(
        self,
        table: str,
        op: str,
        filters: Sequence[Tuple[str, str, Any]],
        columns: str,
        on_conflict: Optional[str] = None,
        ignore_duplicates: bool = False,
    ) -> None:
        self.table = table
        self.op = op
        self.filters = list(filters)
        self.columns = columns
        self.on_conflict = on_conflict
        self.ignore_duplicates = ignore_duplicates

    def filtered_on(self, column: str) -> bool:
        return any(f[1] == column for f in self.filters)

    def value_for(self, column: str) -> Any:
        for _op, col, value in self.filters:
            if col == column:
                return value
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pretty = ", ".join(f"{op}({col}={value!r})" for op, col, value in self.filters)
        return f"<{self.table}.{self.op} [{pretty}]>"


def _matches(row: Dict[str, Any], op: str, column: str, value: Any) -> bool:
    current = row.get(column)
    if op == "eq":
        return current == value
    if op == "neq":
        return current != value
    if op == "in":
        return current in value
    if current is None or value is None:
        # SQL comparisons against NULL are never true.
        return False
    try:
        if op == "gte":
            return current >= value
        if op == "lte":
            return current <= value
        if op == "gt":
            return current > value
        if op == "lt":
            return current < value
    except TypeError as exc:  # pragma: no cover - seed-data mistake
        raise AssertionError(
            f"cannot compare {column}={current!r} with {value!r}; the seeded row "
            f"has a different type than the query. Fix the test's seed data."
        ) from exc
    raise AssertionError(f"unsupported filter {op!r}")


class _QueryBuilder:
    """Mimics the fluent postgrest builder, for the operations actually used.

    Any method the repositories do not use raises. A fake that accepted an
    unknown filter and ignored it would let an unscoped query pass its test.
    """

    def __init__(self, client: FakeSupabaseClient, table: str) -> None:
        self._client = client
        self._table = table
        self._op = ""
        self._columns = "*"
        self._count: Optional[str] = None
        self._payload: Any = None
        self._on_conflict: Optional[str] = None
        self._ignore_duplicates = False
        self._filters: List[Tuple[str, str, Any]] = []
        self._order: Optional[Tuple[str, bool]] = None
        self._limit: Optional[int] = None
        self._range: Optional[Tuple[int, int]] = None

    # -- operation ----------------------------------------------------------
    def select(self, columns: str = "*", count: Optional[str] = None) -> _QueryBuilder:
        self._op = "select"
        self._columns = columns
        self._count = count
        return self

    def insert(self, payload: Any) -> _QueryBuilder:
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: Dict[str, Any]) -> _QueryBuilder:
        self._op = "update"
        self._payload = payload
        return self

    def upsert(
        self,
        payload: Any,
        on_conflict: Optional[str] = None,
        ignore_duplicates: bool = False,
    ) -> _QueryBuilder:
        """``ignore_duplicates`` switches the conflict action, as in postgrest.

        False sends ``Prefer: resolution=merge-duplicates`` — ``ON CONFLICT DO
        UPDATE``, which is what token registration wants. True sends
        ``resolution=ignore-duplicates`` — ``ON CONFLICT DO NOTHING``, whose
        representation contains *only* the rows this statement actually
        inserted. That difference is the whole of the push dispatcher's
        idempotency, so the fake has to reproduce it rather than approximate it.
        """
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    def delete(self) -> _QueryBuilder:
        self._op = "delete"
        return self

    # -- filters ------------------------------------------------------------
    def eq(self, column: str, value: Any) -> _QueryBuilder:
        self._filters.append(("eq", column, value))
        return self

    def neq(self, column: str, value: Any) -> _QueryBuilder:
        self._filters.append(("neq", column, value))
        return self

    def gt(self, column: str, value: Any) -> _QueryBuilder:
        self._filters.append(("gt", column, value))
        return self

    def gte(self, column: str, value: Any) -> _QueryBuilder:
        self._filters.append(("gte", column, value))
        return self

    def lt(self, column: str, value: Any) -> _QueryBuilder:
        self._filters.append(("lt", column, value))
        return self

    def lte(self, column: str, value: Any) -> _QueryBuilder:
        self._filters.append(("lte", column, value))
        return self

    def in_(self, column: str, values: Sequence[Any]) -> _QueryBuilder:
        self._filters.append(("in", column, list(values)))
        return self

    # -- shaping ------------------------------------------------------------
    def order(self, column: str, desc: bool = False) -> _QueryBuilder:
        self._order = (column, desc)
        return self

    def limit(self, count: int) -> _QueryBuilder:
        self._limit = count
        return self

    def range(self, start: int, end: int) -> _QueryBuilder:
        # postgrest's range is inclusive at both ends.
        self._range = (start, end)
        return self

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise NotImplementedError(
            f"FakeSupabaseClient has no .{name}(). Implement it in tests/fakes.py "
            "rather than working around it — a fake that silently drops a filter "
            "makes an unscoped query look correct."
        )

    # -- execution ----------------------------------------------------------
    def execute(self) -> FakeResponse:
        if not self._op:
            raise AssertionError("execute() called without select/insert/update/delete")
        self._client._record(
            ExecutedQuery(
                self._table,
                self._op,
                self._filters,
                self._columns,
                on_conflict=self._on_conflict,
                ignore_duplicates=self._ignore_duplicates,
            )
        )
        if self._client.failure is not None:
            raise self._client.failure
        handler = getattr(self, f"_run_{self._op}")
        with self._client._lock:
            return handler()

    # Each _run_* assumes the client lock is held.
    def _rows(self) -> List[Dict[str, Any]]:
        return self._client._tables.setdefault(self._table, [])

    def _select_matching(self) -> List[Dict[str, Any]]:
        return [row for row in self._rows() if all(_matches(row, *f) for f in self._filters)]

    def _project(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self._columns.strip() == "*":
            return [dict(row) for row in rows]
        wanted = [c.strip() for c in self._columns.split(",") if c.strip()]
        # `id` is dropped from the projection when it wasn't asked for, so a
        # handler that reads an unselected column fails here rather than in
        # production.
        return [{k: row.get(k) for k in wanted} for row in rows]

    def _run_select(self) -> FakeResponse:
        matching = self._select_matching()
        # The count is of everything matching the filters, before pagination —
        # that is what makes it usable as a page total.
        total = len(matching) if self._count == "exact" else None

        if self._order is not None:
            column, desc = self._order
            matching.sort(
                key=lambda row: (
                    row.get(column) is None,
                    row.get(column) if row.get(column) is not None else 0,
                    row.get("__seq", 0),
                ),
                reverse=desc,
            )

        if self._range is not None:
            start, end = self._range
            matching = matching[start : end + 1]
        elif self._limit is not None:
            matching = matching[: self._limit]

        return FakeResponse(self._project(_strip(matching)), total)

    def _check_unique(self, candidate: Dict[str, Any]) -> None:
        for constraint in UNIQUE_CONSTRAINTS.get(self._table, ()):
            if any(candidate.get(col) is None for col in constraint):
                continue  # NULLs are distinct, as in Postgres.
            for row in self._rows():
                if all(row.get(col) == candidate.get(col) for col in constraint):
                    raise unique_violation(f"{self._table}_{'_'.join(constraint)}_key")

    def _run_insert(self) -> FakeResponse:
        payloads = self._payload if isinstance(self._payload, list) else [self._payload]
        inserted: List[Dict[str, Any]] = []
        for payload in payloads:
            row = dict(payload)
            self._check_unique(row)
            row.setdefault("id", str(uuid.uuid4()))
            row["__seq"] = self._client._next_seq()
            self._rows().append(row)
            inserted.append(row)
        return FakeResponse(self._project(_strip(inserted)))

    def _run_update(self) -> FakeResponse:
        updated: List[Dict[str, Any]] = []
        for row in self._select_matching():
            row.update(self._payload)
            updated.append(row)
        # postgrest returns the updated representation, which is why the
        # repositories can treat an empty list as "no such row".
        return FakeResponse(self._project(_strip(updated)))

    def _run_upsert(self) -> FakeResponse:
        keys = [c.strip() for c in (self._on_conflict or "id").split(",") if c.strip()]
        payloads = self._payload if isinstance(self._payload, list) else [self._payload]
        result: List[Dict[str, Any]] = []
        for payload in payloads:
            existing = next(
                (row for row in self._rows() if all(row.get(k) == payload.get(k) for k in keys)),
                None,
            )
            if existing is not None:
                if self._ignore_duplicates:
                    # ON CONFLICT DO NOTHING: the row stays exactly as it is and
                    # is absent from the representation. A caller treating the
                    # returned rows as "the ones I just claimed" depends on this.
                    continue
                # ON CONFLICT DO UPDATE keeps columns the payload omits.
                existing.update(payload)
                result.append(existing)
            else:
                row = dict(payload)
                self._check_unique(row)
                row.setdefault("id", str(uuid.uuid4()))
                row["__seq"] = self._client._next_seq()
                self._rows().append(row)
                result.append(row)
        return FakeResponse(self._project(_strip(result)))

    def _run_delete(self) -> FakeResponse:
        doomed = self._select_matching()
        remaining = [row for row in self._rows() if row not in doomed]
        self._client._tables[self._table] = remaining
        return FakeResponse(self._project(_strip(doomed)))


def _strip(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Hide the bookkeeping column from anything the app sees."""
    return [{k: v for k, v in row.items() if k != "__seq"} for row in rows]


class FakeSupabaseClient:
    """In-memory stand-in for a Supabase client.

    Injected with ``app.db.supabase.set_client()``, which exists for exactly
    this. Note that ``app.db.supabase.run_db`` dispatches every query to a
    worker thread, so the store is guarded by a lock rather than relying on the
    event loop for serialisation.
    """

    def __init__(self) -> None:
        self._tables: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self.executed: List[ExecutedQuery] = []
        #: Set to an exception to make every query raise — the way to test that
        #: a database outage becomes a 503 rather than a 500.
        self.failure: Optional[BaseException] = None

    # -- client surface -----------------------------------------------------
    def table(self, name: str) -> _QueryBuilder:
        return _QueryBuilder(self, name)

    # -- internals ----------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _record(self, query: ExecutedQuery) -> None:
        with self._lock:
            self.executed.append(query)

    # -- test helpers -------------------------------------------------------
    def seed(self, table: str, *rows: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Insert rows directly, bypassing constraint checks.

        Returns copies with their assigned ids, so a test can reference them.
        """
        created: List[Dict[str, Any]] = []
        with self._lock:
            for payload in rows:
                row = dict(payload)
                row.setdefault("id", str(uuid.uuid4()))
                row["__seq"] = self._next_seq()
                self._tables.setdefault(table, []).append(row)
                created.append(row)
        return _strip(created)

    def rows(self, table: str) -> List[Dict[str, Any]]:
        with self._lock:
            return _strip(self._tables.get(table, []))

    def count(self, table: str) -> int:
        with self._lock:
            return len(self._tables.get(table, []))

    def find(self, table: str, **match: Any) -> Optional[Dict[str, Any]]:
        for row in self.rows(table):
            if all(row.get(k) == v for k, v in match.items()):
                return row
        return None

    def queries_for(self, table: str, op: Optional[str] = None) -> List[ExecutedQuery]:
        return [q for q in self.executed if q.table == table and (op is None or q.op == op)]

    def reset(self) -> None:
        with self._lock:
            self._tables.clear()
            self.executed.clear()
            self._seq = 0
            self.failure = None


# ---------------------------------------------------------------------------
# Outbound HTTP
# ---------------------------------------------------------------------------

Responder = Callable[[httpx.Request], httpx.Response]


class UpstreamRouter:
    """Canned responses for outbound HTTP, matched on the request URL.

    Routes are tried in registration order and the first regex to *search* the
    full URL wins, so a test can register a narrow route before a broad one.
    """

    def __init__(self) -> None:
        self._routes: List[Tuple[re.Pattern[str], Responder]] = []
        self.requests: List[httpx.Request] = []
        #: URLs no route matched. Asserted empty when the `upstream` fixture in
        #: conftest tears down, which is what makes an unstubbed call fail the
        #: test that made it rather than silently reaching the internet.
        self.unmatched: List[str] = []

    # -- registration -------------------------------------------------------
    def add(self, pattern: str, responder: Responder) -> UpstreamRouter:
        self._routes.append((re.compile(pattern), responder))
        return self

    def json(
        self,
        pattern: str,
        payload: Any,
        *,
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
    ) -> UpstreamRouter:
        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload, headers=headers)

        return self.add(pattern, respond)

    def text(
        self,
        pattern: str,
        body: str,
        *,
        status: int = 200,
        content_type: str = "text/plain",
    ) -> UpstreamRouter:
        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status, content=body.encode(), headers={"Content-Type": content_type}
            )

        return self.add(pattern, respond)

    def binary(
        self,
        pattern: str,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "image/png",
    ) -> UpstreamRouter:
        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=body, headers={"Content-Type": content_type})

        return self.add(pattern, respond)

    def status(
        self, pattern: str, status: int, *, body: str = "upstream failure"
    ) -> UpstreamRouter:
        """Answer with an error status, the way a rate-limited or broken provider does."""

        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, text=body)

        return self.add(pattern, respond)

    def network_error(self, pattern: str) -> UpstreamRouter:
        def respond(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        return self.add(pattern, respond)

    def timeout(self, pattern: str) -> UpstreamRouter:
        def respond(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        return self.add(pattern, respond)

    # -- transport ----------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for pattern, responder in self._routes:
            if pattern.search(url):
                return responder(request)

        self.unmatched.append(f"{request.method} {url}")
        # A 599 rather than an exception: `request_json` maps unknown statuses to
        # UpstreamError, so the endpoint under test returns its real degraded
        # response instead of a traceback. The teardown assertion is what makes
        # the omission visible.
        return httpx.Response(599, json={"error": "no route registered in this test"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # -- assertions ---------------------------------------------------------
    def called(self, pattern: str) -> bool:
        compiled = re.compile(pattern)
        return any(compiled.search(str(r.url)) for r in self.requests)

    def call_count(self, pattern: str) -> int:
        compiled = re.compile(pattern)
        return sum(1 for r in self.requests if compiled.search(str(r.url)))

    def last_request(self, pattern: str) -> Optional[httpx.Request]:
        compiled = re.compile(pattern)
        for request in reversed(self.requests):
            if compiled.search(str(request.url)):
                return request
        return None


__all__ = [
    "ExecutedQuery",
    "FakeDBError",
    "FakeResponse",
    "FakeSupabaseClient",
    "UpstreamRouter",
    "unique_violation",
]
