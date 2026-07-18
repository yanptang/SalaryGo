from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from salarygo.server import LOOPBACK_HOST, create_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LocalServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server(
            port=0,
            data_dir=self.temporary.name,
            web_root=PROJECT_ROOT / "web",
            fixture_root=PROJECT_ROOT / "examples",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.temporary.cleanup()

    def get_json(self, path: str) -> dict:
        with urlopen(self.base + path) as response:
            return json.loads(response.read())

    def test_server_binds_only_loopback_and_page_reads_json(self) -> None:
        self.assertEqual(self.server.server_address[0], LOOPBACK_HOST)
        state = self.get_json("/api/state")
        with urlopen(self.base + "/") as response:
            html = response.read().decode()

        self.assertIn("csrf_token", state)
        self.assertIn("SalaryGo", html)

    def test_write_requires_csrf_token(self) -> None:
        request = Request(
            self.base + "/api/allocation-context",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 403)

    def test_demo_backtest_fixture_is_readable_but_not_personal_data(self) -> None:
        fixture = self.get_json("/api/demo/backtest")
        self.assertIn("periods", fixture)
        self.assertNotIn("profile", fixture)


if __name__ == "__main__":
    unittest.main()
