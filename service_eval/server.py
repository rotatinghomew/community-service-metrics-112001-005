"""HTTP server and JSON API for the staged-rollout analysis app.

The user-facing surface is a single page plus a JSON API:

* ``GET  /api/state``            source counts, active protocol, saved reports
* ``GET  /api/sources``          raw tables and validation findings
* ``POST /api/protocol/draft``   propose a draft protocol from current sources
* ``POST /api/protocol/freeze``  freeze the first protocol or append a revision
* ``POST /api/analyze``          recompute with the frozen protocol
* ``GET  /api/report/<id>``      a previously saved report
* ``GET  /api/diff``             what changed between the last two protocol versions
* ``POST /api/outcomes``         append an outcome observation
* ``POST /api/shocks``           append a concurrent shock
* ``GET  /``                     the single-page interface
"""

from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import engine
from .domain import SourceValidationError, check_sources
from .protocol import ProtocolError, StateStore, diff_protocol, draft_protocol, validate_protocol
from .sources import SourceError, append_outcome, append_shock, load_directory

MAX_BODY = 2_000_000
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class AppContext:
    def __init__(self, source_dir: str | Path, state_path: str | Path):
        self.source_dir = Path(source_dir)
        self.store = StateStore(state_path)
        self.lock = threading.Lock()

    def bundle(self):
        return load_directory(self.source_dir)

    def state(self) -> dict[str, Any]:
        bundle = self.bundle()
        data = self.store.load()
        active = self.store.active_protocol(data)
        return {
            "sources": {
                "origin": bundle.origin,
                "counts": bundle.fingerprints(),
                "issues": bundle.check(),
            },
            "active_protocol": active,
            "protocols": [
                {
                    "version": p["version"],
                    "active": p.get("active", False),
                    "created_at": p.get("created_at"),
                    "reason": p.get("reason"),
                    "changes_from_previous": p.get("changes_from_previous", []),
                    "source_fingerprint": p.get("source_fingerprint"),
                }
                for p in data["protocols"]
            ],
            "reports": [
                {
                    "id": r.get("id"),
                    "created_at": r.get("created_at"),
                    "protocol_version": r.get("protocol_version"),
                    "status": r.get("estimate", {}).get("status"),
                    "status_label": r.get("estimate", {}).get("status_label"),
                    "can_conclude": r.get("conclusion", {}).get("can_conclude"),
                }
                for r in data["reports"]
            ],
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "StagedRolloutEval/1.0"
    context: AppContext

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        if os.environ.get("SERVICE_EVAL_QUIET") != "1":
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(_json_safe(payload), ensure_ascii=False, indent=1).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, detail: Any = None) -> None:
        self._send(status, {"error": message, "detail": detail})

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise SourceError("请求过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"请求不是有效 JSON：{exc}") from exc

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html", "/app.js", "/style.css"):
                name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
                return self._serve_static(name)
            if path == "/api/state":
                return self._send(HTTPStatus.OK, self.context.state())
            if path == "/api/sources":
                bundle = self.context.bundle()
                return self._send(
                    HTTPStatus.OK,
                    {
                        "tables": bundle.tables,
                        "issues": bundle.check(),
                        "counts": bundle.fingerprints(),
                        "empty_value_rule": bundle.empty_months_note(),
                    },
                )
            if path == "/api/draft":
                bundle = self.context.bundle()
                return self._send(
                    HTTPStatus.OK,
                    {
                        "draft": draft_protocol(bundle.tables),
                        "issues": bundle.check(),
                    },
                )
            if path == "/api/diff":
                data = self.context.store.load()
                protocols = data["protocols"]
                if len(protocols) < 2:
                    return self._send(HTTPStatus.OK, {"diff": [], "latest": protocols[-1] if protocols else None})
                return self._send(
                    HTTPStatus.OK,
                    {
                        "diff": diff_protocol(protocols[-2]["spec"], protocols[-1]["spec"]),
                        "from_version": protocols[-2]["version"],
                        "to_version": protocols[-1]["version"],
                        "reason": protocols[-1].get("reason"),
                    },
                )
            if path.startswith("/api/report/"):
                report_id = path.rsplit("/", 1)[-1]
                report = self.context.store.report(report_id)
                return self._send(HTTPStatus.OK, report)
            if path == "/api/reports":
                return self._send(HTTPStatus.OK, {"reports": self.context.store.reports()})
            if path == "/api/report-impact":
                query = parse_qs(urlparse(self.path).query)
                report_id = (query.get("report") or [""])[0]
                old = self.context.store.report(report_id)
                bundle = self.context.bundle()
                new = engine.analyze(bundle.tables, old["protocol"], old.get("protocol_version"))
                return self._send(HTTPStatus.OK, {"impact": engine.compare_reports(old, new)})
            return self._error(HTTPStatus.NOT_FOUND, "没有这个地址")
        except SourceValidationError as exc:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "资料未通过校验", exc.issues)
        except (SourceError, ProtocolError) as exc:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except FileNotFoundError:
            return self._error(HTTPStatus.NOT_FOUND, "找不到资料文件")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/protocol/freeze":
                payload = self._read_json()
                if not isinstance(payload, dict):
                    raise ProtocolError("请求应为对象")
                spec = validate_protocol(payload.get("protocol", payload.get("spec", {})))
                reason = payload.get("reason", "")
                bundle = self.context.bundle()
                with self.context.lock:
                    record = self.context.store.freeze(spec, reason, bundle.tables)
                return self._send(HTTPStatus.CREATED, {"protocol": record})
            if path == "/api/analyze":
                payload = self._read_json() if self.headers.get("Content-Length") else {}
                data = self.context.store.load()
                version = None
                if isinstance(payload, dict) and payload.get("protocol_version"):
                    version = int(payload["protocol_version"])
                if version is not None:
                    record = self.context.store.protocol_version(version)
                else:
                    record = self.context.store.active_protocol(data)
                if record is None:
                    return self._error(
                        HTTPStatus.CONFLICT,
                        "还没有冻结方案：分析前必须先确定结果口径和比较时段",
                    )
                bundle = self.context.bundle()
                report = engine.analyze(bundle.tables, record["spec"], record["version"])
                report["quality"]["issues"] = _merge_quality(report["quality"]["issues"], bundle.check())
                previous = data["reports"][-1] if data["reports"] else None
                if previous:
                    if previous.get("sources_fingerprint") == report.get(
                        "sources_fingerprint"
                    ) and previous.get("protocol_version") == report.get(
                        "protocol_version"
                    ):
                        previous = None
                    else:
                        report["previous"] = engine.compare_reports(previous, report)
                if isinstance(payload, dict) and payload.get("save", True):
                    with self.context.lock:
                        report = self.context.store.save_report(report)
                return self._send(HTTPStatus.OK, {"report": report})
            if path == "/api/outcomes":
                payload = self._read_json()
                bundle = self.context.bundle()
                with self.context.lock:
                    updated = append_outcome(
                        self.context.source_dir, bundle.tables, payload if isinstance(payload, dict) else {}
                    )
                return self._send(HTTPStatus.CREATED, _source_response(updated))
            if path == "/api/shocks":
                payload = self._read_json()
                bundle = self.context.bundle()
                with self.context.lock:
                    updated = append_shock(
                        self.context.source_dir, bundle.tables, payload if isinstance(payload, dict) else {}
                    )
                return self._send(HTTPStatus.CREATED, _source_response(updated))
            if path == "/api/protocol/draft":
                payload = self._read_json()
                bundle = self.context.bundle()
                merged = draft_protocol(bundle.tables)
                if isinstance(payload, dict):
                    merged.update({k: v for k, v in payload.items() if k in merged})
                return self._send(HTTPStatus.OK, {"draft": validate_protocol(merged)})
            return self._error(HTTPStatus.NOT_FOUND, "没有这个地址")
        except (SourceValidationError, SourceError, ProtocolError, ValueError) as exc:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except FileNotFoundError:
            return self._error(HTTPStatus.NOT_FOUND, "找不到资料文件")

    def _serve_static(self, name: str) -> None:
        if not name.replace("/", "").replace(".", "").isalnum():
            return self._error(HTTPStatus.BAD_REQUEST, "非法文件名")
        target = (WEB_ROOT / name).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            return self._error(HTTPStatus.FORBIDDEN, "禁止访问")
        if not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "找不到页面")
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)


def _merge_quality(primary: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {(item["severity"], item["message"]) for item in primary}
    merged = list(primary)
    for item in extra:
        key = (item["severity"], item["message"])
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _source_response(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {"counts": {name: len(rows) for name, rows in tables.items()}, "issues": check_sources(tables)}


def make_server(host: str, port: int, source_dir: str | Path, state_path: str | Path) -> ThreadingHTTPServer:
    context = AppContext(source_dir, state_path)
    handler = type("BoundHandler", (Handler,), {"context": context})
    return ThreadingHTTPServer((host, port), handler)
