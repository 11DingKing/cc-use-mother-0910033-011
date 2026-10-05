"""多机构执业冲突的领域常量、角色与时间区间工具。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import ValidationError

# ---------------------------------------------------------------------------
# 角色（对应 domain/contract.json 中的 actors）
# ---------------------------------------------------------------------------
ROLE_REGULATOR = "regulator"        # 监管人员
ROLE_REVIEWER = "reviewer"          # 复核专家
ROLE_COMPLIANCE = "compliance"      # 机构合规员
ROLE_PRACTITIONER = "practitioner"  # 执业人员
ROLES = (ROLE_REGULATOR, ROLE_REVIEWER, ROLE_COMPLIANCE, ROLE_PRACTITIONER)


@dataclass(frozen=True)
class Actor:
    """一次请求的调用者身份。"""

    actor_id: str
    role: str
    institution_id: str | None = None


# ---------------------------------------------------------------------------
# 案件状态机：待核验 -> 处置中 -> 已决定 -> 已归档；已决定/已归档可复开
# ---------------------------------------------------------------------------
CASE_PENDING = "待核验"
CASE_HANDLING = "处置中"
CASE_DECIDED = "已决定"
CASE_ARCHIVED = "已归档"
CASE_STATUSES = (CASE_PENDING, CASE_HANDLING, CASE_DECIDED, CASE_ARCHIVED)

# 案件处置事件类型（全部保留来源，追加写入，不删除）
EVENT_IDENTITY_CORRECTION = "identity_correction"        # 纠正身份
EVENT_INSTITUTION_WITHDRAWAL = "institution_withdrawal"  # 机构撤报
EVENT_OVERLAP_JUSTIFICATION = "overlap_justification"    # 合理重叠说明
EVENT_REOPEN = "reopen"                                  # 决定复开
EVENT_ARCHIVE = "archive"                                # 归档
DECISION_EVENTS = (
    EVENT_IDENTITY_CORRECTION,
    EVENT_INSTITUTION_WITHDRAWAL,
    EVENT_OVERLAP_JUSTIFICATION,
)
CASE_EVENT_KINDS = DECISION_EVENTS + (EVENT_REOPEN, EVENT_ARCHIVE)

# 冲突类型
CONFLICT_OVERLAP = "overlap"                       # 跨机构时段重叠
CONFLICT_BUFFER = "buffer_violation"               # 间隔小于移动缓冲
CONFLICT_OUTSIDE_REGISTRATION = "outside_registration"  # 超出执业关系生效区间

# 记录 / 执业关系 / 计划的生命周期状态（历史版本保留，不物理删除）
RECORD_ACTIVE = "active"
RECORD_SUPERSEDED = "superseded"
RECORD_WITHDRAWN = "withdrawn"

REGISTRATION_ACTIVE = "active"
REGISTRATION_REVOKED = "revoked"

PLAN_ACTIVE = "active"
PLAN_CANCELLED = "cancelled"

DEFAULT_BUFFER_MINUTES = 0


# ---------------------------------------------------------------------------
# 时间工具：统一按 UTC 归一化，naive 时间视为 UTC
# ---------------------------------------------------------------------------
def parse_dt(value: object, field: str = "时间") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}必须是非空的 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(f"{field}不是合法的 ISO 8601 时间：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_window(start: datetime, end: datetime, label: str) -> None:
    if end <= start:
        raise ValidationError(f"{label}的结束时间必须晚于开始时间")


def interval_gap_seconds(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> float:
    """两个区间的间隔秒数；重叠时返回负数，绝对值为重叠时长。"""
    if a_end <= b_start:
        return (b_start - a_end).total_seconds()
    if b_end <= a_start:
        return (a_start - b_end).total_seconds()
    return -(min(a_end, b_end) - max(a_start, b_start)).total_seconds()
