"""基于标准库的 HTTP API。

认证：请求头 `X-Actor-Token: <令牌>` 或 `Authorization: Bearer <令牌>`，
令牌到操作者的映射由部署方在启动时注入。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import NotFoundError, PermissionDeniedError, StateError, ValidationError
from .models import Actor
from .service import PracticeConflictService


def _q1(query: dict[str, Any], name: str) -> str | None:
    value = query.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _flag(query: dict[str, Any], name: str) -> bool:
    return str(_q1(query, name) or "").lower() in ("1", "true", "yes")


def _need(body: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if body.get(name) in (None, "")]
    if missing:
        raise ValidationError("缺少参数：" + "、".join(missing))


class Api:
    """无状态路由层，可直接单测，也可挂到 HTTP 服务器。"""

    def __init__(
        self,
        service: PracticeConflictService,
        tokens: dict[str, Actor],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.service = service
        self.tokens = tokens
        self.on_change = on_change

    def dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
        token: str | None,
    ) -> tuple[int, dict[str, Any]]:
        actor = self.tokens.get(token or "")
        if actor is None:
            return 401, {"error": "未认证或令牌无效"}
        try:
            status, payload = self._route(method, path, query or {}, body or {}, actor)
        except ValidationError as exc:
            return 400, {"error": str(exc)}
        except PermissionDeniedError as exc:
            return 403, {"error": str(exc)}
        except NotFoundError as exc:
            return 404, {"error": str(exc)}
        except StateError as exc:
            return 409, {"error": str(exc)}
        if method == "POST" and status < 300 and self.on_change is not None:
            self.on_change()
        return status, payload

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, Any],
        body: dict[str, Any],
        actor: Actor,
    ) -> tuple[int, dict[str, Any]]:
        parts = [seg for seg in path.strip("/").split("/") if seg]
        if not parts:
            if method == "GET":
                return 200, {"service": "多机构执业冲突后端", "status": "ok"}
            raise NotFoundError("未知路径")
        head, rest = parts[0], parts[1:]

        if head == "relations":
            if method == "POST" and not rest:
                _need(body, "practitioner_id", "institution_id", "valid_from")
                relation = self.service.register_relation(
                    actor, body["practitioner_id"], body["institution_id"], body["valid_from"], body.get("valid_to")
                )
                return 201, {"relation": relation.to_dict()}
            if method == "GET" and not rest:
                items = self.service.list_relations(actor, _q1(query, "practitioner_id"), _q1(query, "institution_id"))
                return 200, {"relations": [item.to_dict() for item in items]}
            if method == "POST" and len(rest) == 2 and rest[1] == "revoke":
                relation = self.service.revoke_relation(actor, rest[0], body.get("reason", ""))
                return 200, {"relation": relation.to_dict()}

        elif head == "buffers":
            if method == "POST" and not rest:
                _need(body, "site_a", "site_b", "minutes")
                buffer = self.service.register_buffer(actor, body["site_a"], body["site_b"], body["minutes"])
                return 201, {"buffer": buffer.to_dict()}
            if method == "GET" and not rest:
                items = self.service.list_buffers(actor)
                return 200, {"buffers": [item.to_dict() for item in items]}

        elif head == "plans":
            if method == "POST" and not rest:
                _need(body, "practitioner_id", "institution_id", "site", "start", "end")
                plan, cases = self.service.create_plan(
                    actor, body["practitioner_id"], body["institution_id"], body["site"], body["start"], body["end"]
                )
                return 201, {"plan": plan.to_dict(), "cases": [self.service.case_view(actor, c) for c in cases]}
            if method == "GET" and not rest:
                items = self.service.list_plans(actor, _q1(query, "practitioner_id"), _q1(query, "institution_id"))
                return 200, {"plans": [item.to_dict() for item in items]}
            if method == "POST" and len(rest) == 2 and rest[1] == "cancel":
                plan = self.service.cancel_plan(actor, rest[0], body.get("reason", ""))
                return 200, {"plan": plan.to_dict()}

        elif head == "records":
            if method == "POST" and not rest:
                _need(body, "practitioner_id", "institution_id", "site", "start", "end")
                record, cases = self.service.submit_record(
                    actor,
                    body["practitioner_id"],
                    body["institution_id"],
                    body["site"],
                    body["start"],
                    body["end"],
                    body.get("plan_id"),
                )
                return 201, {"record": record.to_dict(), "cases": [self.service.case_view(actor, c) for c in cases]}
            if method == "GET" and not rest:
                items = self.service.list_records(
                    actor,
                    _q1(query, "practitioner_id"),
                    _q1(query, "institution_id"),
                    include_inactive=_flag(query, "include_inactive"),
                )
                return 200, {"records": [item.to_dict() for item in items]}
            if method == "GET" and len(rest) == 1:
                return 200, {"record": self.service.get_record(actor, rest[0]).to_dict()}
            if method == "POST" and len(rest) == 2 and rest[1] == "withdraw":
                record = self.service.withdraw_record(actor, rest[0], body.get("reason", ""))
                return 200, {"record": record.to_dict()}
            if method == "POST" and len(rest) == 2 and rest[1] == "correct-identity":
                _need(body, "new_practitioner_id")
                record, cases = self.service.correct_identity(
                    actor, rest[0], body["new_practitioner_id"], body.get("reason", "")
                )
                return 200, {"record": record.to_dict(), "cases": [self.service.case_view(actor, c) for c in cases]}

        elif head == "cases":
            if method == "GET" and not rest:
                items = self.service.list_cases(actor, _q1(query, "state"), _q1(query, "practitioner_id"))
                return 200, {"cases": items}
            if method == "GET" and len(rest) == 1:
                return 200, {"case": self.service.get_case(actor, rest[0])}
            if method == "POST" and len(rest) == 2 and rest[1] == "explanations":
                _need(body, "text")
                case = self.service.submit_explanation(actor, rest[0], body["text"])
                return 200, {"case": self.service.case_view(actor, case)}
            if method == "POST" and len(rest) == 2 and rest[1] == "decide":
                _need(body, "resolution")
                case = self.service.decide_case(actor, rest[0], body["resolution"], body.get("note", ""))
                return 200, {"case": self.service.case_view(actor, case)}
            if method == "POST" and len(rest) == 2 and rest[1] == "archive":
                case = self.service.archive_case(actor, rest[0])
                return 200, {"case": self.service.case_view(actor, case)}
            if method == "POST" and len(rest) == 2 and rest[1] == "reopen":
                case = self.service.reopen_case(actor, rest[0], body.get("reason", ""))
                return 200, {"case": self.service.case_view(actor, case)}

        elif head == "persons" and method == "GET" and len(rest) == 2 and rest[1] == "timeline":
            result = self.service.timeline(actor, rest[0], _q1(query, "start"), _q1(query, "end"))
            return 200, result

        elif head == "audit" and method == "GET" and not rest:
            return 200, {"audit": self.service.audit_log(actor)}

        raise NotFoundError("未知路径")


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PracticeConflict/0.1"

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def _handle(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body: dict[str, Any] = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(400, {"error": "请求体不是合法 JSON"})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": "请求体必须是 JSON 对象"})
                    return
            token = self.headers.get("X-Actor-Token", "")
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            try:
                status, payload = api.dispatch(self.command, parsed.path, parse_qs(parsed.query), body, token)
            except Exception:  # pragma: no cover - 兜底，避免连接悬挂
                self._send(500, {"error": "服务内部错误"})
                return
            self._send(status, payload)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def make_server(
    service: PracticeConflictService,
    tokens: dict[str, Actor],
    host: str = "127.0.0.1",
    port: int = 8000,
    on_change: Callable[[], None] | None = None,
) -> ThreadingHTTPServer:
    api = Api(service, tokens, on_change=on_change)
    return ThreadingHTTPServer((host, port), make_handler(api))
