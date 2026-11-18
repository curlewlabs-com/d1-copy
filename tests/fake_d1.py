"""A stand-in for the D1 REST API at the HTTP edge, backed by local SQLite files.

It replaces `urllib.request.urlopen`, so every line of the client and the
command line runs as it does against Cloudflare. It reproduces the two
behaviours of D1 this tool exists for: an export writes each table's rows right
after its definition in catalog order, and passes integers through a double; an
import runs with foreign keys enforced and applies nothing when it fails.
"""

from __future__ import annotations

import contextlib
import email.message
import io
import json
import pathlib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any, Self
from unittest import mock

ACCOUNT = "test-account"
_API_PREFIX = f"/client/v4/accounts/{ACCOUNT}/d1/database"
_EXPORT_HOST = "export.example.invalid"
_UPLOAD_HOST = "upload.example.invalid"


class FakeD1:
    def __init__(self, root: pathlib.Path) -> None:
        self._root = root
        self._databases: dict[str, tuple[str, pathlib.Path]] = {}
        self._uploads: dict[str, bytes] = {}
        # Every request, as (method, host and path, whether it carried a token).
        self.requests: list[tuple[str, str, bool]] = []

    def create(self, name: str, sql: str = "") -> pathlib.Path:
        """A database named `name`, holding `sql` loaded without enforcement, as
        a live database built up over time can hold a child table defined
        before its parent."""
        uuid = f"00000000-0000-4000-8000-{len(self._databases) + 1:012d}"
        path = self._root / f"{name}.db"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(sql)
        finally:
            connection.close()
        self._databases[name] = (uuid, path)
        return path

    @contextlib.contextmanager
    def serving(self) -> Iterator[None]:
        with mock.patch("urllib.request.urlopen", self._urlopen):
            yield

    def paths(self, method: str) -> list[str]:
        return [path for verb, path, _ in self.requests if verb == method]

    # --- the HTTP edge -----------------------------------------------------

    def _urlopen(self, request: urllib.request.Request, timeout: float) -> Any:
        url = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        authorized = request.has_header("Authorization")
        self.requests.append((method, url.netloc + url.path, authorized))
        if url.netloc == _EXPORT_HOST:
            return _Response(self._export(url.path.strip("/")))
        if url.netloc == _UPLOAD_HOST:
            assert isinstance(request.data, bytes)
            self._uploads[url.path.strip("/")] = request.data
            return _Response(b"")
        body = json.loads(request.data) if isinstance(request.data, bytes) else None
        answer = self._api(method, url.path, body)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(
                request.full_url,
                answer,
                "error",
                email.message.Message(),
                io.BytesIO(b'{"success":false,"errors":[{"code":7500,"message":"x"}]}'),
            )
        return _Response(json.dumps(answer).encode())

    def _by_uuid(self, uuid: str) -> pathlib.Path:
        for known, path in self._databases.values():
            if known == uuid:
                return path
        raise AssertionError(f"no database {uuid}")

    def _api(self, method: str, path: str, body: Any) -> dict[str, Any] | int:
        assert path.startswith(_API_PREFIX), path
        rest = path[len(_API_PREFIX) :].strip("/")
        if not rest and method == "GET":
            listed = [
                {"name": name, "uuid": uuid}
                for name, (uuid, _) in self._databases.items()
            ]
            return {
                "success": True,
                "result": listed,
                "result_info": {"total_count": len(listed)},
            }
        uuid, leaf = rest.split("/", 1)
        database = self._by_uuid(uuid)
        if leaf == "query":
            return self._query(database, body["sql"])
        if leaf == "export":
            # The first answer is a job still running, so the poll is exercised.
            if "current_bookmark" not in body:
                return {"success": True, "result": {"at_bookmark": "b1"}}
            signed = f"https://{_EXPORT_HOST}/{uuid}?X-Amz-Signature=s"
            return {
                "success": True,
                "result": {
                    "at_bookmark": "b1",
                    "status": "complete",
                    "result": {"signed_url": signed},
                },
            }
        if leaf == "import":
            return self._import(uuid, database, body)
        raise AssertionError(f"unexpected call {method} {path}")

    # --- what D1 does ------------------------------------------------------

    def _query(self, database: pathlib.Path, sql: str) -> dict[str, Any] | int:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            results = []
            for statement in sql.split(";\n"):
                try:
                    rows = [dict(row) for row in connection.execute(statement)]
                except sqlite3.Error:
                    return 400
                results.append(
                    {"success": True, "results": rows, "meta": {"rows_read": len(rows)}}
                )
            connection.commit()
            return {"success": True, "result": results}
        finally:
            connection.close()

    def _export(self, uuid: str) -> bytes:
        """The database as D1 writes it: rows after their own table, integers
        through a double."""
        connection = sqlite3.connect(self._by_uuid(uuid))
        try:
            out = ["PRAGMA defer_foreign_keys=TRUE;"]
            catalog = connection.execute(
                "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            ).fetchall()
            for kind, name, sql in catalog:
                if kind == "table":
                    out.append(f"{sql};")
                    out.extend(_inserts(connection, name))
            for kind, _, sql in catalog:
                if kind != "table":
                    out.append(f"{sql};")
            tables = {
                row[0] for row in connection.execute("SELECT name FROM sqlite_schema")
            }
            if "sqlite_sequence" in tables:
                out.append("DELETE FROM sqlite_sequence;")
                out.extend(_inserts(connection, "sqlite_sequence"))
            return ("\n".join(out) + "\n").encode()
        finally:
            connection.close()

    def _import(
        self, uuid: str, database: pathlib.Path, body: dict[str, Any]
    ) -> dict[str, Any]:
        if body["action"] == "init":
            return {
                "success": True,
                "result": {
                    "upload_url": f"https://{_UPLOAD_HOST}/{uuid}",
                    "filename": "import.sql",
                },
            }
        assert body["action"] == "ingest", body
        before = database.read_bytes()
        connection = sqlite3.connect(database, autocommit=True)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(self._uploads.pop(uuid).decode())
        except sqlite3.Error as exc:
            connection.close()
            database.write_bytes(before)
            return {"success": True, "result": {"success": False, "error": str(exc)}}
        connection.close()
        return {
            "success": True,
            "result": {"status": "complete", "result": {"meta": {"rows_written": 1}}},
        }


def _inserts(connection: sqlite3.Connection, table: str) -> list[str]:
    columns = [
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]
    names = ",".join(f'"{column}"' for column in columns)
    return [
        f'INSERT INTO "{table}" ({names}) VALUES({",".join(_exported(v) for v in row)});'
        for row in connection.execute(f'SELECT {names} FROM "{table}"')
    ]


def _exported(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        # Through a double, as D1's export does.
        return str(int(float(value)))
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    if isinstance(value, float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body
