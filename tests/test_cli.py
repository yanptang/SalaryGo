from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from salarygo.cli import main
from test_profile import example_profile


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "input.json"
        self.input_path.write_text(json.dumps(example_profile()), encoding="utf-8")

    def invoke(self, *arguments: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--data-dir", str(self.root / "private"), *arguments])
        return status, json.loads(output.getvalue())

    def test_validate_command_returns_machine_readable_result(self) -> None:
        status, payload = self.invoke("validate", str(self.input_path))

        self.assertEqual(status, 0)
        self.assertEqual(payload, {"issues": [], "valid": True})

    def test_save_show_backup_restore_round_trip(self) -> None:
        save_status, save_payload = self.invoke("save", str(self.input_path), "--expected-revision", "0")
        show_status, shown = self.invoke("show")
        backup_status, backup_payload = self.invoke("backup")

        self.assertEqual((save_status, show_status, backup_status), (0, 0, 0))
        self.assertTrue(save_payload["saved"])
        self.assertEqual(shown["revision"], 1)
        self.assertTrue(Path(backup_payload["path"]).exists())


if __name__ == "__main__":
    unittest.main()

