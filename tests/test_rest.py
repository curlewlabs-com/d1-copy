"""What the client repeats, and what it never repeats.

Starting an export and ingesting an import take no idempotency key, so a second
attempt after Cloudflare accepted the first would run the job twice. A read is
safe to repeat, and an authentication rejection ran nothing at all.
"""

from __future__ import annotations

import email.message
import io
import json
import unittest
import urllib.error
import urllib.request
from typing import Any
from unittest import mock

from d1_copy import rest

_LISTING: dict[str, Any] = {
    "success": True,
    "result": [],
    "result_info": {"total_count": 0},
}


class _Answers:
    """urlopen answering each call with the next of `answers`: an HTTP status
    to fail with, or a body to return."""

    def __init__(self, *answers: int | dict[str, Any]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        self.calls += 1
        answer = self._answers.pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(
                request.full_url,
                answer,
                "error",
                email.message.Message(),
                io.BytesIO(b"{}"),
            )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(answer).encode()
        return response


class RetryTest(unittest.TestCase):
    def _client(self, answers: _Answers) -> rest.D1Client:
        for patch in (
            mock.patch("urllib.request.urlopen", answers),
            mock.patch("d1_copy.rest.time.sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return rest.D1Client("test-account", "test-token")

    def test_a_read_is_repeated_through_a_transient_failure(self) -> None:
        answers = _Answers(503, 502, _LISTING)
        self.assertEqual(self._client(answers).databases(), [])
        self.assertEqual(answers.calls, 3)

    def test_starting_an_export_is_not_repeated_after_a_transient_failure(self) -> None:
        """Cloudflare may already have started the job the 503 answered for."""
        answers = _Answers(503, {"success": True, "result": {}})
        client = self._client(answers)
        with self.assertRaisesRegex(rest.D1Error, "HTTP 503"):
            client.export_to("00000000-0000-4000-8000-000000000001", mock.MagicMock())
        self.assertEqual(answers.calls, 1)

    def test_an_authentication_rejection_is_repeated_even_for_a_mutation(self) -> None:
        """A token minted moments ago is refused for its first seconds, and a
        refused call ran nothing."""
        answers = _Answers(
            401, {"success": True, "result": [{"success": True, "results": []}]}
        )
        client = self._client(answers)
        client.query("00000000-0000-4000-8000-000000000001", "SELECT 1", mutates=True)
        self.assertEqual(answers.calls, 2)

    def test_a_token_that_is_simply_wrong_still_fails_promptly(self) -> None:
        answers = _Answers(401, 401, 401, 401, _LISTING)
        with self.assertRaisesRegex(rest.D1Error, "HTTP 401"):
            self._client(answers).databases()
        self.assertEqual(answers.calls, 4)

    def test_a_result_without_its_rows_is_a_failure_not_an_empty_answer(
        self,
    ) -> None:
        """The empty-target check and the check of what an export would change
        both read "no rows" as "nothing there"; a malformed answer must not."""
        answers = _Answers({"success": True, "result": [{"success": True}]})
        with self.assertRaisesRegex(rest.D1Error, "successes back"):
            self._client(answers).query(
                "00000000-0000-4000-8000-000000000001", "SELECT 1", mutates=False
            )

    def test_a_statement_with_a_semicolon_is_never_batched(self) -> None:
        """The API splits on it, and the answers would no longer line up."""
        answers = _Answers()
        with self.assertRaisesRegex(rest.D1Error, "semicolon"):
            self._client(answers).query_many(
                "00000000-0000-4000-8000-000000000001",
                ["SELECT 1; SELECT 2"],
                mutates=False,
            )
        self.assertEqual(answers.calls, 0)

    def test_an_import_refused_inside_an_accepted_response_is_a_failure(self) -> None:
        """The envelope says success and the import's own flag says otherwise;
        reading only the envelope would turn a refused file into a missing
        bookmark."""
        answers = _Answers(
            {
                "success": True,
                "result": {
                    "upload_url": "https://upload.example.invalid/x",
                    "filename": "f",
                },
            },
            {"success": True, "result": {}},
            {
                "success": True,
                "result": {"success": False, "error": "no such table: authors"},
            },
        )
        client = self._client(answers)
        source = mock.MagicMock()
        source.read_bytes.return_value = b"SELECT 1;"
        with self.assertRaisesRegex(rest.D1Error, "no such table"):
            client.import_file("00000000-0000-4000-8000-000000000001", source)

    def test_an_import_d1_already_holds_is_followed_rather_than_refused(self) -> None:
        """No upload URL means D1 recognised the bytes and started running them;
        the answer to `init` is the first progress report."""
        answers = _Answers(
            {"success": True, "result": {"at_bookmark": "b1"}},
            {
                "success": True,
                "result": {"status": "complete", "result": {"num_queries": 3}},
            },
        )
        client = self._client(answers)
        source = mock.MagicMock()
        source.read_bytes.return_value = b"SELECT 1;"
        summary = client.import_file("00000000-0000-4000-8000-000000000001", source)
        self.assertEqual(summary, {"num_queries": 3})


class ErrorDetailTest(unittest.TestCase):
    """What an HTTP failure says about a body, which can hold anything: only
    Cloudflare's own error messages are repeated, and a body of another shape
    says so rather than being echoed."""

    def test_only_the_error_messages_of_an_object_body_are_repeated(self) -> None:
        body = json.dumps(
            {
                "errors": [{"code": 7003, "message": "no route"}, "stray", 5],
                "result": "rows that must not be echoed",
            }
        ).encode()
        self.assertEqual(rest._error_messages(body), "7003: no route")

    def test_a_body_of_another_shape_is_not_echoed(self) -> None:
        for body, said in (
            (b"not json", "(unparseable body)"),
            (b"[1, 2]", "(no error detail)"),
            (b'{"errors": []}', "(no error detail)"),
            (b'{"errors": "a string"}', "(no error detail)"),
        ):
            with self.subTest(body=body):
                self.assertEqual(rest._error_messages(body), said)

    def test_a_batch_with_a_failed_statement_is_refused(self) -> None:
        answers: list[dict[str, Any]] = [
            {"success": True, "result": [{"success": False, "results": []}]},
            {"success": True, "result": [{"success": True, "results": None}]},
            {"success": True, "result": ["not an object"]},
        ]
        for answer in answers:
            with (
                self.subTest(answer=answer),
                mock.patch("urllib.request.urlopen", _Answers(answer)),
            ):
                client = rest.D1Client("test-account", "test-token")
                with self.assertRaisesRegex(rest.D1Error, "did not get that many"):
                    client.query_many(
                        "00000000-0000-4000-8000-000000000001",
                        ["SELECT 1"],
                        mutates=True,
                    )


if __name__ == "__main__":
    unittest.main()
