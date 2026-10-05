"""多机构执业冲突领域模型。

所有实体只增不删：撤报、更正、撤销、取消均以状态字段与审计事件表达，
历史版本始终可追溯。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def parse_ts(value: str) -> datetime:
    """解析 ISO 8601 时间并统一为 UTC。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def format_ts(value: datetime) -> str:
    """以 UTC ISO 8601 输出时间。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _opt_ts(value: str | None) -> datetime | None:
    return parse_ts(value) if value else None


class Role(str, Enum):
    """契约角色。"""

    REGULATOR = "regulator"  # 监管人员
    REVIEWER = "reviewer"  # 复核专家
    OFFICER = "officer"  # 机构合规员
    PRACTITIONER = "practitioner"  # 执业人员


@dataclass(frozen=True)
class Actor:
    """一次调用对应的操作者。"""

    actor_id: str
    role: Role
    institution_id: str | None = None
    practitioner_id: str | None = None


class CaseState(str, Enum):
    """待核案件状态，与领域契约 states 对齐。"""

    PENDING = "待核验"
    HANDLING = "处置中"
    DECIDED = "已决定"
    ARCHIVED = "已归档"


class Resolution(str, Enum):
    """案件决定类型。"""

    CONFIRMED_VIOLATION = "确认违规"
    JUSTIFIED_OVERLAP = "合理重叠"
    IDENTITY_CORRECTED = "身份纠正"
    REPORT_WITHDRAWN = "机构撤报"
    NOT_A_CONFLICT = "不构成冲突"


@dataclass
class PracticeRelation:
    """执业关系生效区间；撤销只标记不删除。"""

    relation_id: str
    practitioner_id: str
    institution_id: str
    valid_from: datetime
    valid_to: datetime | None  # None 表示长期有效
    registered_by: str
    registered_at: datetime
    status: str = "active"  # active | revoked
    revoked: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "practitioner_id": self.practitioner_id,
            "institution_id": self.institution_id,
            "valid_from": format_ts(self.valid_from),
            "valid_to": format_ts(self.valid_to) if self.valid_to else None,
            "registered_by": self.registered_by,
            "registered_at": format_ts(self.registered_at),
            "status": self.status,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PracticeRelation":
        return cls(
            relation_id=data["relation_id"],
            practitioner_id=data["practitioner_id"],
            institution_id=data["institution_id"],
            valid_from=parse_ts(data["valid_from"]),
            valid_to=_opt_ts(data.get("valid_to")),
            registered_by=data["registered_by"],
            registered_at=parse_ts(data["registered_at"]),
            status=data.get("status", "active"),
            revoked=data.get("revoked"),
        )


@dataclass
class MovementBuffer:
    """两个站点之间的移动缓冲分钟数。"""

    buffer_id: str
    site_a: str
    site_b: str
    minutes: int
    registered_by: str
    registered_at: datetime
    status: str = "active"  # active | revoked

    def to_dict(self) -> dict[str, Any]:
        return {
            "buffer_id": self.buffer_id,
            "site_a": self.site_a,
            "site_b": self.site_b,
            "minutes": self.minutes,
            "registered_by": self.registered_by,
            "registered_at": format_ts(self.registered_at),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MovementBuffer":
        return cls(
            buffer_id=data["buffer_id"],
            site_a=data["site_a"],
            site_b=data["site_b"],
            minutes=int(data["minutes"]),
            registered_by=data["registered_by"],
            registered_at=parse_ts(data["registered_at"]),
            status=data.get("status", "active"),
        )


@dataclass
class ServicePlan:
    """服务计划；被实际记录履约后标记 fulfilled，取消只标记不删除。"""

    plan_id: str
    practitioner_id: str
    institution_id: str
    site: str
    start: datetime
    end: datetime
    created_by: str
    created_at: datetime
    status: str = "active"  # active | fulfilled | cancelled
    cancelled: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "practitioner_id": self.practitioner_id,
            "institution_id": self.institution_id,
            "site": self.site,
            "start": format_ts(self.start),
            "end": format_ts(self.end),
            "created_by": self.created_by,
            "created_at": format_ts(self.created_at),
            "status": self.status,
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServicePlan":
        return cls(
            plan_id=data["plan_id"],
            practitioner_id=data["practitioner_id"],
            institution_id=data["institution_id"],
            site=data["site"],
            start=parse_ts(data["start"]),
            end=parse_ts(data["end"]),
            created_by=data["created_by"],
            created_at=parse_ts(data["created_at"]),
            status=data.get("status", "active"),
            cancelled=data.get("cancelled"),
        )


@dataclass
class ServiceRecord:
    """实际服务记录；撤报与身份更正均保留原记录。"""

    record_id: str
    practitioner_id: str
    institution_id: str
    site: str
    start: datetime
    end: datetime
    submitted_by: str
    submitted_at: datetime
    status: str = "active"  # active | withdrawn | superseded
    plan_id: str | None = None
    withdrawn: dict[str, Any] | None = None
    corrected_from: str | None = None
    superseded_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "practitioner_id": self.practitioner_id,
            "institution_id": self.institution_id,
            "site": self.site,
            "start": format_ts(self.start),
            "end": format_ts(self.end),
            "submitted_by": self.submitted_by,
            "submitted_at": format_ts(self.submitted_at),
            "status": self.status,
            "plan_id": self.plan_id,
            "withdrawn": self.withdrawn,
            "corrected_from": self.corrected_from,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServiceRecord":
        return cls(
            record_id=data["record_id"],
            practitioner_id=data["practitioner_id"],
            institution_id=data["institution_id"],
            site=data["site"],
            start=parse_ts(data["start"]),
            end=parse_ts(data["end"]),
            submitted_by=data["submitted_by"],
            submitted_at=parse_ts(data["submitted_at"]),
            status=data.get("status", "active"),
            plan_id=data.get("plan_id"),
            withdrawn=data.get("withdrawn"),
            corrected_from=data.get("corrected_from"),
            superseded_by=data.get("superseded_by"),
        )


@dataclass
class EvidenceSide:
    """冲突证据中一方承诺的快照，案件创建时固化，后续变更不影响来源。"""

    kind: str  # plan | record
    ref_id: str
    institution_id: str
    site: str
    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "institution_id": self.institution_id,
            "site": self.site,
            "start": format_ts(self.start),
            "end": format_ts(self.end),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceSide":
        return cls(
            kind=data["kind"],
            ref_id=data["ref_id"],
            institution_id=data["institution_id"],
            site=data["site"],
            start=parse_ts(data["start"]),
            end=parse_ts(data["end"]),
        )


@dataclass
class ConflictEvidence:
    """一次时空冲突的完整证据。"""

    case_type: str  # overlap | insufficient_travel | out_of_relation
    practitioner_id: str
    side_a: EvidenceSide | None
    side_b: EvidenceSide | None
    scope: str = "cross_institution"  # cross_institution | same_institution
    overlap_start: datetime | None = None
    overlap_end: datetime | None = None
    gap_minutes: float | None = None
    required_buffer_minutes: int | None = None
    buffer_source: str | None = None  # registered | default
    missing_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_type": self.case_type,
            "practitioner_id": self.practitioner_id,
            "scope": self.scope,
            "side_a": self.side_a.to_dict() if self.side_a else None,
            "side_b": self.side_b.to_dict() if self.side_b else None,
            "overlap_start": format_ts(self.overlap_start) if self.overlap_start else None,
            "overlap_end": format_ts(self.overlap_end) if self.overlap_end else None,
            "gap_minutes": self.gap_minutes,
            "required_buffer_minutes": self.required_buffer_minutes,
            "buffer_source": self.buffer_source,
            "missing_intervals": [[format_ts(s), format_ts(e)] for s, e in self.missing_intervals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConflictEvidence":
        return cls(
            case_type=data["case_type"],
            practitioner_id=data["practitioner_id"],
            side_a=EvidenceSide.from_dict(data["side_a"]) if data.get("side_a") else None,
            side_b=EvidenceSide.from_dict(data["side_b"]) if data.get("side_b") else None,
            scope=data.get("scope", "cross_institution"),
            overlap_start=_opt_ts(data.get("overlap_start")),
            overlap_end=_opt_ts(data.get("overlap_end")),
            gap_minutes=data.get("gap_minutes"),
            required_buffer_minutes=data.get("required_buffer_minutes"),
            buffer_source=data.get("buffer_source"),
            missing_intervals=[(parse_ts(s), parse_ts(e)) for s, e in data.get("missing_intervals", [])],
        )


@dataclass
class Case:
    """待核案件；说明、决定、状态流转全部留痕，复开不清空历史。"""

    case_id: str
    practitioner_id: str
    case_type: str
    evidence: ConflictEvidence
    created_at: datetime
    created_by: str = "system"
    state: CaseState = CaseState.PENDING
    explanations: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def institutions(self) -> list[str]:
        found = set()
        for side in (self.evidence.side_a, self.evidence.side_b):
            if side is not None:
                found.add(side.institution_id)
        return sorted(found)

    @property
    def current_decision(self) -> dict[str, Any] | None:
        return self.decisions[-1] if self.decisions else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "practitioner_id": self.practitioner_id,
            "case_type": self.case_type,
            "state": self.state.value,
            "institutions": self.institutions,
            "evidence": self.evidence.to_dict(),
            "explanations": self.explanations,
            "decisions": self.decisions,
            "current_decision": self.current_decision,
            "history": self.history,
            "created_at": format_ts(self.created_at),
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Case":
        return cls(
            case_id=data["case_id"],
            practitioner_id=data["practitioner_id"],
            case_type=data["case_type"],
            evidence=ConflictEvidence.from_dict(data["evidence"]),
            created_at=parse_ts(data["created_at"]),
            created_by=data.get("created_by", "system"),
            state=CaseState(data["state"]),
            explanations=list(data.get("explanations", [])),
            decisions=list(data.get("decisions", [])),
            history=list(data.get("history", [])),
        )


@dataclass
class AuditEvent:
    """审计事件，只追加不修改。"""

    seq: int
    at: datetime
    actor: str
    action: str
    entity: str
    entity_id: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": format_ts(self.at),
            "actor": self.actor,
            "action": self.action,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEvent":
        return cls(
            seq=int(data["seq"]),
            at=parse_ts(data["at"]),
            actor=data["actor"],
            action=data["action"],
            entity=data["entity"],
            entity_id=data["entity_id"],
            detail=dict(data.get("detail", {})),
        )
