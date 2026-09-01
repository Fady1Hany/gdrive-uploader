#!/usr/bin/env python3
"""Integration tests for the resumable-upload retry loop.

Requires google-api-python-client (see requirements.txt).
Run: python3 test_upload_loop.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

from gdrive_uploader import upload_file


def http_error(status: int, body: bytes = b'{"error":{"message":"not found"}}') -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


class FakeStatus:
    def __init__(self, progress: float):
        self._progress = progress

    def progress(self) -> float:
        return self._progress


class ScriptedRequest:
    def __init__(self, events: list):
        self.events = list(events)

    def next_chunk(self):
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


class FakeFiles:
    def __init__(self, owner: "FakeService"):
        self.owner = owner

    def create(self, **_kwargs):
        self.owner.sessions += 1
        return ScriptedRequest(self.owner.scripts.pop(0))


class FakeService:
    def __init__(self, scripts: list[list]):
        self.scripts = scripts
        self.sessions = 0

    def files(self):
        return FakeFiles(self)


class UploadLoopTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        handle.write(b"backup-payload")
        handle.close()
        self.path = handle.name
        self.sleeps: list[int] = []

    def tearDown(self):
        os.unlink(self.path)

    def _upload(self, service: FakeService) -> str:
        return upload_file(
            self.path,
            parent_folder_id="folder-xyz",
            service=service,
            sleeper=self.sleeps.append,
        )

    def test_404_on_first_chunk_is_bad_folder_and_does_not_retry(self):
        service = FakeService([[http_error(404)]])
        with self.assertRaises(RuntimeError) as ctx:
            self._upload(service)
        self.assertIn("--folder-id", str(ctx.exception))
        self.assertEqual(service.sessions, 1)
        self.assertEqual(self.sleeps, [])

    def test_404_after_accepted_chunk_restarts_session_instead_of_blaming_folder(self):
        # Session 1: chunk 1 accepted (response still None), chunk 2 → 404
        # Session 2: upload completes
        service = FakeService(
            [
                [(FakeStatus(0.4), None), http_error(404)],
                [(None, {"id": "file-ok"})],
            ]
        )
        file_id = self._upload(service)
        self.assertEqual(file_id, "file-ok")
        self.assertEqual(service.sessions, 2)
        self.assertEqual(self.sleeps, [5])  # exponential attempt 1, not linear

    def test_rate_limit_uses_triple_exponential_backoff(self):
        # Rate-limit path retries the SAME request object (does not recreate
        # the session). First next_chunk raises 429, second returns success.
        service = FakeService(
            [
                [http_error(429), (None, {"id": "after-429"})],
            ]
        )
        file_id = self._upload(service)
        self.assertEqual(file_id, "after-429")
        self.assertEqual(service.sessions, 1)
        self.assertEqual(self.sleeps, [15])  # 5 * 2^0 * 3

    def test_410_mid_upload_recreates_media_session(self):
        service = FakeService(
            [
                [(FakeStatus(0.5), None), http_error(410)],
                [(None, {"id": "resumed"})],
            ]
        )
        file_id = self._upload(service)
        self.assertEqual(file_id, "resumed")
        self.assertEqual(service.sessions, 2)


if __name__ == "__main__":
    unittest.main()
