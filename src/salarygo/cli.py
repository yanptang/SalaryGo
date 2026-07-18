"""Command line interface exposed to Codex and the local user."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .profile import ProfileValidationError, validate_profile
from .agent import AgentWorkflow, StrategyRepository, extract_funds
from .allocation import generate_allocation
from .backtest import run_backtest
from .server import serve
from .storage import (
    BackupIntegrityError,
    ProfileNotFoundError,
    ProfileRepository,
    RestoreConflictError,
    RevisionConflictError,
)


def _load_input(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("档案根节点必须是对象")
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="salarygo", description="SalaryGo 本地确定性工具")
    parser.add_argument("--data-dir", help="覆盖私有数据目录（也可使用 SALARYGO_DATA_DIR）")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="校验档案 JSON，不保存")
    validate.add_argument("file")

    save = commands.add_parser("save", help="校验并保存档案")
    save.add_argument("file")
    save.add_argument("--expected-revision", type=int)

    commands.add_parser("show", help="读取当前档案")
    commands.add_parser("backup", help="备份当前档案")

    restore = commands.add_parser("restore", help="恢复档案备份")
    restore.add_argument("file")
    restore.add_argument("--replace", action="store_true", help="允许覆盖同一用户的当前档案")
    commands.add_parser("health", help="Codex/M0 结构化工具连通性检查")

    allocate = commands.add_parser("allocate", help="运行确定性资金分配")
    allocate.add_argument("file")

    agent = commands.add_parser("agent-request", help="解析月度投资请求并生成策略")
    agent.add_argument("text")
    agent.add_argument("--context", required=True)

    backtest = commands.add_parser("backtest", help="运行独立回测")
    backtest.add_argument("file")

    local_server = commands.add_parser("serve", help="启动仅本机可访问的页面")
    local_server.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = ProfileRepository(args.data_dir)
    try:
        if args.command == "validate":
            profile = _load_input(args.file)
            issues = validate_profile(profile)
            _print_json({"valid": not issues, "issues": [issue.to_dict() for issue in issues]})
            return 0 if not issues else 2
        if args.command == "save":
            saved = repository.save(_load_input(args.file), expected_revision=args.expected_revision)
            _print_json({"saved": True, "path": str(repository.profile_path), "profile": saved})
            return 0
        if args.command == "show":
            _print_json(repository.load())
            return 0
        if args.command == "backup":
            path = repository.backup()
            _print_json({"backed_up": True, "path": str(path)})
            return 0
        if args.command == "restore":
            restored = repository.restore(args.file, replace=args.replace)
            _print_json({"restored": True, "profile": restored})
            return 0
        if args.command == "health":
            _print_json({"ok": True, "tool": "salarygo", "schema_version": 1})
            return 0
        if args.command == "allocate":
            result = generate_allocation(_load_input(args.file))
            _print_json(result)
            return 0 if result.get("status") == "formal" else 2
        if args.command == "agent-request":
            context = _load_input(args.context)
            result = AgentWorkflow(StrategyRepository(args.data_dir)).monthly_request(args.text, context)
            _print_json(result)
            return 0 if result.get("status") == "formal" else 2
        if args.command == "backtest":
            _print_json(run_backtest(_load_input(args.file)))
            return 0
        if args.command == "serve":
            serve(args.port, args.data_dir)
            return 0
    except (BackupIntegrityError, ProfileNotFoundError, RevisionConflictError, RestoreConflictError) as exc:
        _print_json({"error": type(exc).__name__, "message": str(exc)})
        return 3
    except (json.JSONDecodeError, OSError, ValueError, ProfileValidationError) as exc:
        issues = getattr(exc, "issues", None)
        payload: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
        if issues:
            payload["issues"] = [issue.to_dict() for issue in issues]
        _print_json(payload)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
