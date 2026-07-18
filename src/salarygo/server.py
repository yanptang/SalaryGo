"""M0/M8 local-only HTTP service and JSON API."""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .application import SalaryGoApplication

LOOPBACK_HOST = "127.0.0.1"


class SalaryGoHandler(BaseHTTPRequestHandler):
    app: SalaryGoApplication
    web_root: Path
    csrf_token: str
    fixture_root: Path

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def _write_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                return False
        return self.headers.get("X-SalaryGo-CSRF") == self.csrf_token

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求内容过大")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是对象")
        return value

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid_host"})
            return
        path = urlparse(self.path).path
        if path == "/api/state":
            state = self.app.state()
            state["csrf_token"] = self.csrf_token
            self._send_json(HTTPStatus.OK, state)
            return
        if path == "/api/demo/backtest":
            fixture = json.loads((self.fixture_root / "backtest.fixture.json").read_text(encoding="utf-8"))
            self._send_json(HTTPStatus.OK, fixture)
            return
        static_path = "index.html" if path == "/" else path.lstrip("/")
        target = (self.web_root / static_path).resolve()
        if self.web_root.resolve() not in target.parents and target != self.web_root.resolve():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not target.is_file():
            target = self.web_root / "index.html"
        content = target.read_bytes()
        content_type = "text/html" if target.suffix == ".html" else "text/css" if target.suffix == ".css" else "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        if not self._host_allowed() or not self._write_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "write_forbidden"})
            return
        try:
            body = self._body()
            path = urlparse(self.path).path
            if path == "/api/profile":
                result = self.app.save_profile(body["profile"], body.get("expected_revision"))
            elif path == "/api/ledger":
                result = self.app.save_ledger(body["ledger"], body.get("expected_revision"))
            elif path == "/api/ledger/mutate":
                result = self.app.mutate_ledger(body["entity"], body["action"], body.get("id"), body.get("record"))
            elif path == "/api/market/refresh-demo":
                result = self.app.refresh(body["requests"], self.fixture_root / "market.fixture.json")
            elif path == "/api/allocation-context":
                result = self.app.save_allocation_context(body["context"])
            elif path == "/api/strategies/generate":
                result = self.app.generate_strategy(body["text"])
            elif path == "/api/strategies/execution":
                result = self.app.record_execution(body["strategy_id"], body["status"], body.get("trades", []))
            elif path == "/api/backtest":
                result = self.app.run_backtest(body["config"])
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, result)
        except Exception as exc:
            issues = getattr(exc, "issues", None)
            payload: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
            if issues:
                payload["issues"] = [item.to_dict() for item in issues]
            self._send_json(HTTPStatus.BAD_REQUEST, payload)


def create_server(
    *,
    port: int = 8765,
    data_dir: Path | str | None = None,
    web_root: Path | None = None,
    fixture_root: Path | None = None,
) -> ThreadingHTTPServer:
    project_root = Path(__file__).resolve().parents[2]
    handler = type("BoundSalaryGoHandler", (SalaryGoHandler,), {})
    handler.app = SalaryGoApplication(data_dir)
    handler.web_root = web_root or project_root / "web"
    handler.fixture_root = fixture_root or project_root / "examples"
    handler.csrf_token = secrets.token_urlsafe(32)
    return ThreadingHTTPServer((LOOPBACK_HOST, port), handler)


def serve(port: int = 8765, data_dir: Path | str | None = None) -> None:
    server = create_server(port=port, data_dir=data_dir)
    print(f"SalaryGo 已启动：http://{LOOPBACK_HOST}:{server.server_port}")
    print("按 Control-C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
