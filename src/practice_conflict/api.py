"""FastAPI 接口层。

身份模型（演示用）：通过请求头声明调用者
- X-Actor-Id：调用者标识（执业人员角色时为人员编号）
- X-Actor-Role：regulator / reviewer / compliance / practitioner
- X-Institution-Id：机构合规员所属机构（compliance 角色必填）
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import DomainError, ValidationError
from .models import ROLES, ROLE_COMPLIANCE, Actor
from .service import ConflictService
from .store import Store


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class InstitutionIn(BaseModel):
    name: str = Field(min_length=1)
    location: str = ""


class PractitionerIn(BaseModel):
    name: str = Field(min_length=1)


class MovementBufferIn(BaseModel):
    from_institution_id: str = Field(min_length=1)
    to_institution_id: str = Field(min_length=1)
    minutes: int = Field(ge=0)


class RegistrationIn(BaseModel):
    practitioner_id: str = Field(min_length=1)
    institution_id: str = Field(min_length=1)
    effective_from: str
    effective_to: str | None = None
    source: str = Field(min_length=1)
    reason: str = ""


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1)


class PlanIn(BaseModel):
    practitioner_id: str = Field(min_length=1)
    institution_id: str = Field(min_length=1)
    planned_start: str
    planned_end: str
    location: str = ""
    source: str = Field(min_length=1)


class RecordIn(BaseModel):
    practitioner_id: str = Field(min_length=1)
    institution_id: str = Field(min_length=1)
    actual_start: str
    actual_end: str
    location: str = ""
    note: str = ""
    source: str = Field(min_length=1)


class CaseEventIn(BaseModel):
    kind: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 身份解析
# ---------------------------------------------------------------------------
def get_actor(
    x_actor_id: str = Header(...),
    x_actor_role: str = Header(...),
    x_institution_id: str | None = Header(None),
) -> Actor:
    role = x_actor_role.strip().lower()
    if role not in ROLES:
        raise ValidationError(f"未知角色：{x_actor_role}，可选：{'/'.join(ROLES)}")
    if role == ROLE_COMPLIANCE and not (x_institution_id or "").strip():
        raise ValidationError("机构合规员必须提供 X-Institution-Id 请求头")
    return Actor(
        actor_id=x_actor_id.strip(),
        role=role,
        institution_id=(x_institution_id or "").strip() or None,
    )


def create_app(db_path: str = ":memory:") -> FastAPI:
    store = Store(db_path)
    service = ConflictService(store)
    app = FastAPI(title="多机构执业冲突后端", version="0.1.0")
    app.state.store = store
    app.state.service = service

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    # -- 健康检查 ------------------------------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # -- 基础登记 ------------------------------------------------------------
    @app.post("/institutions", status_code=201)
    def create_institution(body: InstitutionIn, actor: Actor = Depends(get_actor)) -> dict:
        return service.create_institution(actor, body.name, body.location)

    @app.get("/institutions")
    def list_institutions(actor: Actor = Depends(get_actor)) -> list[dict]:
        return store.list_institutions()

    @app.post("/practitioners", status_code=201)
    def create_practitioner(body: PractitionerIn, actor: Actor = Depends(get_actor)) -> dict:
        return service.create_practitioner(actor, body.name)

    @app.get("/practitioners")
    def list_practitioners(actor: Actor = Depends(get_actor)) -> list[dict]:
        return store.list_practitioners()

    @app.post("/movement-buffers", status_code=201)
    def set_movement_buffer(body: MovementBufferIn, actor: Actor = Depends(get_actor)) -> dict:
        return service.set_movement_buffer(
            actor, body.from_institution_id, body.to_institution_id, body.minutes
        )

    @app.post("/registrations", status_code=201)
    def register_practice(body: RegistrationIn, actor: Actor = Depends(get_actor)) -> dict:
        return service.register_practice(
            actor,
            body.practitioner_id,
            body.institution_id,
            body.effective_from,
            body.effective_to,
            body.source,
            body.reason,
        )

    @app.post("/registrations/{registration_id}/revoke")
    def revoke_registration(
        registration_id: str, body: ReasonIn, actor: Actor = Depends(get_actor)
    ) -> dict:
        return service.revoke_registration(actor, registration_id, body.reason)

    @app.post("/plans", status_code=201)
    def create_plan(body: PlanIn, actor: Actor = Depends(get_actor)) -> dict:
        return service.create_plan(
            actor,
            body.practitioner_id,
            body.institution_id,
            body.planned_start,
            body.planned_end,
            body.location,
            body.source,
        )

    @app.post("/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: str, body: ReasonIn, actor: Actor = Depends(get_actor)) -> dict:
        return service.cancel_plan(actor, plan_id, body.reason)

    # -- 实际记录与冲突检测 ----------------------------------------------------
    @app.post("/records", status_code=201)
    def submit_record(body: RecordIn, actor: Actor = Depends(get_actor)) -> dict:
        return service.submit_record(
            actor,
            body.practitioner_id,
            body.institution_id,
            body.actual_start,
            body.actual_end,
            body.location,
            body.note,
            body.source,
        )

    @app.get("/records/{record_id}")
    def get_record(record_id: str, actor: Actor = Depends(get_actor)) -> dict:
        return service.get_record(actor, record_id)

    @app.get("/records/{record_id}/versions")
    def get_record_versions(record_id: str, actor: Actor = Depends(get_actor)) -> list[dict]:
        return service.get_record_versions(actor, record_id)

    # -- 待核案件 ------------------------------------------------------------
    @app.get("/cases")
    def list_cases(
        status: str | None = Query(None),
        practitioner_id: str | None = Query(None),
        actor: Actor = Depends(get_actor),
    ) -> list[dict]:
        return service.list_cases(actor, status, practitioner_id)

    @app.get("/cases/{case_id}")
    def get_case(case_id: str, actor: Actor = Depends(get_actor)) -> dict:
        return service.get_case(actor, case_id)

    @app.post("/cases/{case_id}/events", status_code=201)
    def file_case_event(
        case_id: str, body: CaseEventIn, actor: Actor = Depends(get_actor)
    ) -> dict:
        return service.file_case_event(
            actor, case_id, body.kind, body.reason, body.source, body.payload
        )

    # -- 人员时间线 ------------------------------------------------------------
    @app.get("/practitioners/{practitioner_id}/timeline")
    def practitioner_timeline(
        practitioner_id: str,
        start: str | None = Query(None),
        end: str | None = Query(None),
        actor: Actor = Depends(get_actor),
    ) -> dict:
        return service.timeline(actor, practitioner_id, start, end)

    return app
