"""Private local profile persistence with revisions, backups and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .profile import ProfileValidationError, ValidationIssue, assert_valid_profile


class ProfileNotFoundError(FileNotFoundError):
    pass


class RevisionConflictError(RuntimeError):
    pass


class RestoreConflictError(RuntimeError):
    pass


class BackupIntegrityError(RuntimeError):
    pass


def default_data_dir() -> Path:
    override = os.environ.get("SALARYGO_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data" / "private"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileValidationError(
            [ValidationIssue("$", "invalid_json", f"不是有效 JSON：{exc.msg}")]
        ) from exc
    if not isinstance(value, dict):
        raise ProfileValidationError(
            [ValidationIssue("$", "type", "档案根节点必须是对象")]
        )
    return value


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, path)


class ProfileRepository:
    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.profile_path = self.data_dir / "profile.json"
        self.backup_dir = self.data_dir.parent / "backups"

    def load(self) -> dict[str, Any]:
        if not self.profile_path.exists():
            raise ProfileNotFoundError(f"用户档案不存在：{self.profile_path}")
        profile = _read_json(self.profile_path)
        assert_valid_profile(profile)
        return profile

    def save(self, profile: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        candidate = deepcopy(profile)
        current: dict[str, Any] | None = None
        if self.profile_path.exists():
            current = self.load()
            current_revision = current["revision"]
            if expected_revision is not None and expected_revision != current_revision:
                raise RevisionConflictError(
                    f"档案已更新：期望版本 {expected_revision}，当前版本 {current_revision}"
                )
        elif expected_revision not in (None, 0):
            raise RevisionConflictError(f"新档案的期望版本只能是 0，收到 {expected_revision}")

        now = datetime.now(timezone.utc).isoformat()
        candidate["revision"] = 1 if current is None else current["revision"] + 1
        candidate["created_at"] = current["created_at"] if current is not None else now
        candidate["updated_at"] = now
        assert_valid_profile(candidate)
        _atomic_json_write(self.profile_path, candidate)
        return candidate

    def backup(self) -> Path:
        profile = self.load()
        raw = self.profile_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        destination = self.backup_dir / f"profile-r{profile['revision']}-{digest[:12]}.json"
        if not destination.exists():
            shutil.copy2(self.profile_path, destination)
            destination.chmod(0o600)
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile["profile_id"],
            "revision": profile["revision"],
            "sha256": digest,
            "file": destination.name,
        }
        _atomic_json_write(destination.with_suffix(".manifest.json"), manifest)
        return destination

    def restore(self, backup_path: Path | str, *, replace: bool = False) -> dict[str, Any]:
        source = Path(backup_path)
        if not source.is_file():
            raise ProfileNotFoundError(f"备份不存在：{source}")
        manifest_path = source.with_suffix(".manifest.json")
        if manifest_path.exists():
            manifest = _read_json(manifest_path)
            expected_digest = manifest.get("sha256")
            actual_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if not isinstance(expected_digest, str) or expected_digest != actual_digest:
                raise BackupIntegrityError("备份摘要校验失败，文件可能已损坏或被修改")
        restored = _read_json(source)
        assert_valid_profile(restored)
        if self.profile_path.exists() and not replace:
            raise RestoreConflictError("当前档案已存在；确认覆盖后请使用 replace=True")
        if self.profile_path.exists():
            current = self.load()
            if current["profile_id"] != restored["profile_id"]:
                raise RestoreConflictError("备份与当前档案的 profile_id 不一致，拒绝覆盖")
            return self.save(restored, expected_revision=current["revision"])
        restored["revision"] = 0
        restored["created_at"] = None
        restored["updated_at"] = None
        return self.save(restored, expected_revision=0)
