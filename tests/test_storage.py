from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from salarygo.profile import ProfileValidationError
from salarygo.storage import (
    BackupIntegrityError,
    ProfileRepository,
    RestoreConflictError,
    RevisionConflictError,
    default_data_dir,
)
from test_profile import example_profile


class ProfileRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = ProfileRepository(self.root / "private")

    def test_save_and_reload_complete_profile(self) -> None:
        saved = self.repository.save(example_profile(), expected_revision=0)
        loaded = self.repository.load()

        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["revision"], 1)
        self.assertIsNotNone(loaded["created_at"])
        self.assertIsNotNone(loaded["updated_at"])
        self.assertEqual(self.repository.profile_path.stat().st_mode & 0o777, 0o600)

    def test_second_save_increments_revision_and_preserves_creation_time(self) -> None:
        first = self.repository.save(example_profile())
        first["user"]["display_name"] = "更新后的名字"

        second = self.repository.save(first, expected_revision=1)

        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["user"]["display_name"], "更新后的名字")

    def test_stale_revision_is_rejected(self) -> None:
        self.repository.save(example_profile())

        with self.assertRaises(RevisionConflictError):
            self.repository.save(example_profile(), expected_revision=0)

    def test_invalid_update_does_not_change_existing_file(self) -> None:
        self.repository.save(example_profile())
        before = self.repository.profile_path.read_bytes()
        invalid = example_profile()
        del invalid["user"]["display_name"]

        with self.assertRaises(ProfileValidationError):
            self.repository.save(invalid, expected_revision=1)

        self.assertEqual(self.repository.profile_path.read_bytes(), before)

    def test_backup_has_matching_digest_and_manifest(self) -> None:
        saved = self.repository.save(example_profile())

        backup = self.repository.backup()
        manifest = json.loads(backup.with_suffix(".manifest.json").read_text(encoding="utf-8"))

        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        self.assertEqual(manifest["sha256"], digest)
        self.assertEqual(manifest["revision"], saved["revision"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_restore_requires_explicit_replace(self) -> None:
        self.repository.save(example_profile())
        backup = self.repository.backup()

        with self.assertRaises(RestoreConflictError):
            self.repository.restore(backup)

    def test_restore_rejects_a_tampered_backup(self) -> None:
        self.repository.save(example_profile())
        backup = self.repository.backup()
        tampered = json.loads(backup.read_text(encoding="utf-8"))
        tampered["user"]["display_name"] = "被修改的备份"
        backup.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaises(BackupIntegrityError):
            self.repository.restore(backup, replace=True)

    def test_restore_same_profile_creates_new_revision(self) -> None:
        first = self.repository.save(example_profile())
        backup = self.repository.backup()
        changed = dict(first)
        changed["user"] = dict(first["user"])
        changed["user"]["display_name"] = "临时名字"
        self.repository.save(changed, expected_revision=1)

        restored = self.repository.restore(backup, replace=True)

        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["user"]["display_name"], "示例用户")

    def test_restore_refuses_a_different_profile_id(self) -> None:
        self.repository.save(example_profile())
        other = example_profile()
        other["profile_id"] = str(uuid4())
        other_repository = ProfileRepository(self.root / "other-private")
        other_repository.save(other)
        backup = other_repository.backup()

        with self.assertRaises(RestoreConflictError):
            self.repository.restore(backup, replace=True)

    def test_data_directory_can_be_isolated_by_environment(self) -> None:
        isolated = self.root / "test-only"

        with patch.dict(os.environ, {"SALARYGO_DATA_DIR": str(isolated)}):
            self.assertEqual(default_data_dir(), isolated.resolve())


if __name__ == "__main__":
    unittest.main()
