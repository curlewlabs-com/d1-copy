"""The D1 REST calls a copy makes, each against an explicit database UUID.

Every mutating call here is addressed by the UUID this module resolved from an
exact name and the command printed, never by a name, so the database a command
writes is the one it said it would.

WHAT IS RETRIED, and what is not. A read - a listing, a poll, a download, a
query that selects - is repeated on a transient failure. Starting an export and
ingesting an import are not: neither takes an idempotency key, so a retry after
Cloudflare already accepted the first attempt would run the operation twice, and
a transient failure there has to surface.

The token is sent only as a header and never printed. A response body is never
echoed whole, since a query result is the database's data; only Cloudflare's own
error messages are, and an SQL error among them can quote part of a statement.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any, override

API = "https://api.cloudflare.com/client/v4"

# Between attempts of a read. Short, because a caller may be holding writes
# paused while it waits.
_READ_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_UNAUTHENTICATED = 401

# An export or import of a database of tens of megabytes settles in under a
# minute. The ceiling only stops a job that will never finish from holding a
# caller open silently.
_POLL_INTERVAL_SECONDS = 1.0
_POLL_LIMIT_SECONDS = 600.0

_ERROR_TEXT_LIMIT = 160


class D1Error(RuntimeError):
    """The D1 API refused, failed, or answered in a shape this tool cannot trust."""


@dataclasses.dataclass(frozen=True)
class Database:
    name: str
    uuid: str

    @override
    def __str__(self) -> str:
        return f"{self.name} ({self.uuid})"


@dataclasses.dataclass(frozen=True)
class QueryResult:
    rows: list[dict[str, Any]]
    # Cloudflare's own account of the statement: `rows_read` is how the live
    # integer check reports what it cost.
    meta: dict[str, Any]


class D1Client:
    def __init__(self, account_id: str, token: str) -> None:
        if not account_id or not token:
            raise D1Error("an account id and an API token are required")
        self._account = account_id
        self._token = token

    # --- transport -------------------------------------------------------

    def _send(
        self, method: str, url: str, body: dict[str, Any] | None, *, retry: bool
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        delays = _READ_RETRY_DELAYS_SECONDS if retry else ()
        rejections = 0
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    parsed: Any = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                # An authentication rejection ran nothing, so it is repeated even
                # for a call that mutates. A token minted moments ago is refused
                # for its first seconds. Bounded, so a token that is simply wrong
                # still fails promptly.
                if exc.code == _UNAUTHENTICATED and rejections < len(
                    _READ_RETRY_DELAYS_SECONDS
                ):
                    time.sleep(_READ_RETRY_DELAYS_SECONDS[rejections])
                    rejections += 1
                    continue
                if exc.code in _TRANSIENT_STATUSES and attempt < len(delays):
                    time.sleep(delays[attempt])
                    attempt += 1
                    continue
                raise D1Error(
                    f"{method} {_redact(url)} answered HTTP {exc.code}: "
                    f"{_error_messages(exc.read())}"
                ) from exc
            except OSError as exc:
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    attempt += 1
                    continue
                raise D1Error(f"{method} {_redact(url)} failed: {exc!r}") from exc
            if not isinstance(parsed, dict) or parsed.get("success") is not True:
                raise D1Error(
                    f"{method} {_redact(url)} was refused: "
                    f"{_error_messages(json.dumps(parsed).encode())}"
                )
            return parsed

    def _database_url(self, uuid: str, leaf: str = "") -> str:
        return f"{API}/accounts/{self._account}/d1/database/{uuid}{leaf}"

    # --- identity --------------------------------------------------------

    def databases(self) -> list[Database]:
        found: list[Database] = []
        page = 1
        while True:
            answer = self._send(
                "GET",
                f"{API}/accounts/{self._account}/d1/database?per_page=100&page={page}",
                None,
                retry=True,
            )
            batch = answer.get("result") or []
            found.extend(Database(str(row["name"]), str(row["uuid"])) for row in batch)
            info = answer.get("result_info") or {}
            if len(batch) < 100 or page * 100 >= int(info.get("total_count", 0) or 0):
                return found
            page += 1

    def resolve(self, name: str) -> Database:
        """The one database with exactly this name, or a refusal."""
        matches = [database for database in self.databases() if database.name == name]
        if len(matches) != 1:
            raise D1Error(f"expected one database named {name!r}, found {len(matches)}")
        return matches[0]

    # --- small control statements ----------------------------------------

    def query(self, uuid: str, sql: str, *, mutates: bool) -> QueryResult:
        """One statement. `mutates` decides whether a transient failure repeats."""
        (only,) = self.query_many(uuid, [sql], mutates=mutates)
        return only

    def query_many(
        self, uuid: str, statements: list[str], *, mutates: bool
    ) -> list[QueryResult]:
        """Several statements in one round trip, answered in order.

        For the reads a copy makes before it exports, where a round trip per
        table is time a caller holding writes paused spends waiting. Refuses a
        statement containing a semicolon rather than risk the API splitting one
        statement into several and the answers no longer lining up.
        """
        if any(";" in statement for statement in statements):
            raise D1Error("a batched statement may not contain a semicolon")
        answer = self._send(
            "POST",
            self._database_url(uuid, "/query"),
            {"sql": ";\n".join(statements)},
            retry=not mutates,
        )
        results = answer.get("result") or []
        if len(results) != len(statements) or not all(
            isinstance(result, dict) and result.get("success") is True
            for result in results
        ):
            raise D1Error(
                f"query against {uuid} sent {len(statements)} statement(s) and did "
                f"not get that many successes back"
            )
        return [
            QueryResult(
                list(result.get("results") or []), dict(result.get("meta") or {})
            )
            for result in results
        ]

    # --- export ----------------------------------------------------------

    def export_to(self, uuid: str, destination: pathlib.Path) -> None:
        """Write a full SQL dump of `uuid` to `destination`.

        Blocks the database's other queries for as long as it runs.
        """
        url = self._database_url(uuid, "/export")
        state = self._send("POST", url, {"output_format": "polling"}, retry=False)
        deadline = time.monotonic() + _POLL_LIMIT_SECONDS
        while True:
            result = state.get("result") or {}
            signed = (result.get("result") or {}).get("signed_url")
            if isinstance(signed, str) and signed:
                _download(signed, destination)
                return
            if result.get("status") == "error":
                raise D1Error(
                    f"export of {uuid} failed: {_bounded(result.get('error'))}"
                )
            bookmark = result.get("at_bookmark")
            if not isinstance(bookmark, str) or not bookmark:
                raise D1Error(f"export of {uuid} gave no bookmark to poll with")
            if time.monotonic() > deadline:
                raise D1Error(
                    f"export of {uuid} did not finish in {_POLL_LIMIT_SECONDS}s"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
            state = self._send(
                "POST",
                url,
                {"output_format": "polling", "current_bookmark": bookmark},
                retry=True,
            )

    # --- import ----------------------------------------------------------

    def import_file(self, uuid: str, source: pathlib.Path) -> dict[str, Any]:
        """Run `source` against `uuid` through D1's import path, all or nothing.

        Returns Cloudflare's summary of the finished import. An import whose
        outcome cannot be read raises; it does not retry, because the caller's
        next step is to read the target back, which answers the question a retry
        could only guess at.
        """
        payload = source.read_bytes()
        etag = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        url = self._database_url(uuid, "/import")
        state = self._send("POST", url, {"action": "init", "etag": etag}, retry=False)
        opened = state.get("result") or {}
        upload_url = opened.get("upload_url")
        if isinstance(upload_url, str) and upload_url:
            filename = opened.get("filename")
            if not isinstance(filename, str) or not filename:
                raise D1Error(f"import into {uuid} offered an upload with no file name")
            _upload(upload_url, payload)
            state = self._send(
                "POST",
                url,
                {"action": "ingest", "etag": etag, "filename": filename},
                retry=False,
            )
        # No upload URL means D1 has already been sent a file with these exact
        # bytes, and naming it has STARTED it running: the answer to `init` is
        # then the first progress report, not a refusal. Observed, and it is how
        # wrangler reads it. It is the normal case for repeating a copy after a
        # failure, where the source has not changed - so treating it as an error
        # would report a failure while the statements ran.
        deadline = time.monotonic() + _POLL_LIMIT_SECONDS
        while True:
            result = state.get("result") or {}
            status = result.get("status")
            if status == "complete":
                return dict(result.get("result") or {})
            # An import answers with a success flag of its own inside the API
            # envelope's. A refused file arrives as an accepted request whose
            # inner flag is false, with no status and no bookmark, so it has to
            # be read here or it surfaces as a missing bookmark instead of as
            # the SQL error it is.
            if status == "error" or result.get("success") is False:
                raise D1Error(
                    f"import into {uuid} failed: "
                    f"{_bounded(result.get('error') or result.get('errors'))}"
                )
            bookmark = result.get("at_bookmark")
            if not isinstance(bookmark, str) or not bookmark:
                raise D1Error(f"import into {uuid} gave no bookmark to poll with")
            if time.monotonic() > deadline:
                raise D1Error(
                    f"import into {uuid} did not finish in {_POLL_LIMIT_SECONDS}s; "
                    "read the target back before doing anything else"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
            state = self._send(
                "POST",
                url,
                {"action": "poll", "current_bookmark": bookmark},
                retry=True,
            )


def _download(signed_url: str, destination: pathlib.Path) -> None:
    # No Authorization header: the URL is pre-signed, and sending the API token
    # to the object store would hand it to a host that has no use for it.
    request = urllib.request.Request(signed_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            destination.write_bytes(response.read())
    except OSError as exc:
        raise D1Error(f"downloading an export failed: {exc!r}") from exc


def _upload(upload_url: str, payload: bytes) -> None:
    request = urllib.request.Request(upload_url, method="PUT", data=payload)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            response.read()
    except OSError as exc:
        raise D1Error(f"uploading an import failed: {exc!r}") from exc


def _bounded(detail: object) -> str:
    """An error's text, cut short.

    SQLite names a constraint or a table in most errors, which is what an
    operator needs. A syntax error instead quotes the text near the fault, and in
    an import that text can be part of a row. The head of the message is enough
    to say which kind it was.
    """
    text = repr(detail)
    return text if len(text) <= _ERROR_TEXT_LIMIT else text[:_ERROR_TEXT_LIMIT] + "..."


def _redact(url: str) -> str:
    """The path of an API URL. Drops a query string, which is where a signature lives."""
    return url.split("?", 1)[0]


def _error_messages(body: bytes) -> str:
    """Cloudflare's own error messages from a response body, and nothing else in it."""
    try:
        parsed: Any = json.loads(body)
    except ValueError:
        return "(unparseable body)"
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if not isinstance(errors, list) or not errors:
        return "(no error detail)"
    return "; ".join(
        f"{error.get('code')}: {error.get('message')}"
        for error in errors
        if isinstance(error, dict)
    )
