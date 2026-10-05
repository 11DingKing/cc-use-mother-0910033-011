"""多机构执业冲突的核心业务逻辑。

覆盖契约中的四条不变式：
- 执业关系时态：执业关系带生效区间，计划/记录必须落在有效区间内；
- 时空冲突检测：提交实际记录时按重叠与移动缓冲检测跨机构冲突；
- 待核案件流程：待核验 -> 处置中 -> 已决定 -> 已归档，支持决定复开；
- 机构明细隔离：机构合规员只能查看本机构明细，他机构证据一律脱敏。
"""
from __future__ import annotations

from typing import Any

from .errors import NotFoundError, PermissionDeniedError, StateConflictError, ValidationError
from .models import (
    CASE_ARCHIVED,
    CASE_DECIDED,
    CASE_EVENT_KINDS,
    CASE_HANDLING,
    CASE_PENDING,
    CONFLICT_BUFFER,
    CONFLICT_OUTSIDE_REGISTRATION,
    CONFLICT_OVERLAP,
    DECISION_EVENTS,
    DEFAULT_BUFFER_MINUTES,
    EVENT_ARCHIVE,
    EVENT_IDENTITY_CORRECTION,
    EVENT_INSTITUTION_WITHDRAWAL,
    EVENT_OVERLAP_JUSTIFICATION,
    EVENT_REOPEN,
    PLAN_ACTIVE,
    PLAN_CANCELLED,
    RECORD_ACTIVE,
    RECORD_SUPERSEDED,
    RECORD_WITHDRAWN,
    REGISTRATION_ACTIVE,
    REGISTRATION_REVOKED,
    ROLE_COMPLIANCE,
    ROLE_PRACTITIONER,
    ROLE_REGULATOR,
    ROLE_REVIEWER,
    Actor,
    interval_gap_seconds,
    iso,
    parse_dt,
    require_window,
    utcnow,
)
from .store import Store

# 冲突严重度优先级：重叠 > 缓冲不足 > 超出执业关系区间
_CONFLICT_PRIORITY = {
    CONFLICT_OVERLAP: 0,
    CONFLICT_BUFFER: 1,
    CONFLICT_OUTSIDE_REGISTRATION: 2,
}

# 案件事件的可见字段（机构/执业人员视角的脱敏事件）
_EVENT_SAFE_FIELDS = ("id", "kind", "actor_role", "reason", "source", "created_at")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _require_text(value: Any, label: str) -> str:
    _require(isinstance(value, str) and value.strip(), f"{label}不能为空")
    return value.strip()


class ConflictService:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # 权限工具
    # ------------------------------------------------------------------
    @staticmethod
    def _require_role(actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDeniedError(f"角色 {actor.role} 无权执行该操作")

    def _require_compliance_scope(self, actor: Actor, institution_id: str) -> None:
        """机构合规员只能操作本机构数据；监管/复核角色不受限。"""
        if actor.role == ROLE_COMPLIANCE:
            if actor.institution_id != institution_id:
                raise PermissionDeniedError("机构合规员只能操作本机构数据")
            if self.store.get_institution(institution_id) is None:
                raise NotFoundError(f"机构不存在：{institution_id}")

    def _can_view_record(self, actor: Actor, record: dict) -> bool:
        if actor.role in (ROLE_REGULATOR, ROLE_REVIEWER):
            return True
        if actor.role == ROLE_PRACTITIONER:
            return actor.actor_id == record["practitioner_id"]
        if actor.role == ROLE_COMPLIANCE:
            return actor.institution_id == record["institution_id"]
        return False

    # ------------------------------------------------------------------
    # 基础登记
    # ------------------------------------------------------------------
    def create_institution(self, actor: Actor, name: str, location: str = "") -> dict:
        self._require_role(actor, ROLE_REGULATOR)
        return self.store.add_institution(
            _require_text(name, "机构名称"), location.strip(), iso(utcnow())
        )

    def create_practitioner(self, actor: Actor, name: str) -> dict:
        self._require_role(actor, ROLE_REGULATOR)
        return self.store.add_practitioner(_require_text(name, "执业人员姓名"), iso(utcnow()))

    def set_movement_buffer(
        self, actor: Actor, from_id: str, to_id: str, minutes: int
    ) -> dict:
        self._require_role(actor, ROLE_REGULATOR)
        _require(isinstance(minutes, int) and minutes >= 0, "移动缓冲分钟数必须是非负整数")
        _require(from_id != to_id, "移动缓冲的起止机构不能相同")
        for institution_id in (from_id, to_id):
            if self.store.get_institution(institution_id) is None:
                raise NotFoundError(f"机构不存在：{institution_id}")
        self.store.set_movement_buffer(from_id, to_id, minutes, actor.actor_id, iso(utcnow()))
        return {
            "from_institution_id": from_id,
            "to_institution_id": to_id,
            "minutes": minutes,
        }

    def register_practice(
        self,
        actor: Actor,
        practitioner_id: str,
        institution_id: str,
        effective_from: str,
        effective_to: str | None,
        source: str,
        reason: str = "",
    ) -> dict:
        """登记执业关系生效区间。"""
        self._require_role(actor, ROLE_REGULATOR, ROLE_COMPLIANCE)
        self._require_compliance_scope(actor, institution_id)
        if self.store.get_practitioner(practitioner_id) is None:
            raise NotFoundError(f"执业人员不存在：{practitioner_id}")
        if self.store.get_institution(institution_id) is None:
            raise NotFoundError(f"机构不存在：{institution_id}")
        start = parse_dt(effective_from, "生效开始时间")
        end = parse_dt(effective_to, "生效结束时间") if effective_to else None
        if end is not None:
            require_window(start, end, "执业关系生效区间")
        return self.store.add_registration(
            {
                "practitioner_id": practitioner_id,
                "institution_id": institution_id,
                "effective_from": iso(start),
                "effective_to": iso(end) if end else None,
                "status": REGISTRATION_ACTIVE,
                "reason": reason.strip(),
                "source": _require_text(source, "来源"),
                "created_by": actor.actor_id,
                "created_at": iso(utcnow()),
            }
        )

    def revoke_registration(self, actor: Actor, registration_id: str, reason: str) -> dict:
        """撤销执业关系登记：状态翻转，历史保留。"""
        self._require_role(actor, ROLE_REGULATOR)
        registration = self.store.get_registration(registration_id)
        if registration is None:
            raise NotFoundError(f"执业关系不存在：{registration_id}")
        if registration["status"] != REGISTRATION_ACTIVE:
            raise StateConflictError("仅有效状态的执业关系可以撤销")
        self.store.revoke_registration(registration_id, iso(utcnow()), _require_text(reason, "撤销原因"))
        return self.store.get_registration(registration_id)

    def create_plan(
        self,
        actor: Actor,
        practitioner_id: str,
        institution_id: str,
        planned_start: str,
        planned_end: str,
        location: str = "",
        source: str = "",
    ) -> dict:
        """登记服务计划；计划必须落在该机构有效执业关系区间内。"""
        self._require_role(actor, ROLE_REGULATOR, ROLE_COMPLIANCE)
        self._require_compliance_scope(actor, institution_id)
        if self.store.get_practitioner(practitioner_id) is None:
            raise NotFoundError(f"执业人员不存在：{practitioner_id}")
        start = parse_dt(planned_start, "计划开始时间")
        end = parse_dt(planned_end, "计划结束时间")
        require_window(start, end, "服务计划")
        if not self._covered_by_registration(practitioner_id, institution_id, start, end):
            raise ValidationError("服务计划不在该机构任何有效执业关系区间内")
        return self.store.add_plan(
            {
                "practitioner_id": practitioner_id,
                "institution_id": institution_id,
                "planned_start": iso(start),
                "planned_end": iso(end),
                "location": location.strip(),
                "status": PLAN_ACTIVE,
                "source": _require_text(source, "来源"),
                "created_by": actor.actor_id,
                "created_at": iso(utcnow()),
            }
        )

    def cancel_plan(self, actor: Actor, plan_id: str, reason: str) -> dict:
        self._require_role(actor, ROLE_REGULATOR, ROLE_COMPLIANCE)
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"服务计划不存在：{plan_id}")
        self._require_compliance_scope(actor, plan["institution_id"])
        if plan["status"] != PLAN_ACTIVE:
            raise StateConflictError("仅有效状态的服务计划可以取消")
        self.store.cancel_plan(plan_id, iso(utcnow()), _require_text(reason, "取消原因"))
        return self.store.get_plan(plan_id)

    # ------------------------------------------------------------------
    # 实际记录提交与时空冲突检测
    # ------------------------------------------------------------------
    def submit_record(
        self,
        actor: Actor,
        practitioner_id: str,
        institution_id: str,
        actual_start: str,
        actual_end: str,
        location: str = "",
        note: str = "",
        source: str = "",
    ) -> dict:
        """提交实际服务记录，同步检测时空冲突并创建待核案件。"""
        self._require_role(actor, ROLE_REGULATOR, ROLE_COMPLIANCE)
        self._require_compliance_scope(actor, institution_id)
        if self.store.get_practitioner(practitioner_id) is None:
            raise NotFoundError(f"执业人员不存在：{practitioner_id}")
        if self.store.get_institution(institution_id) is None:
            raise NotFoundError(f"机构不存在：{institution_id}")
        start = parse_dt(actual_start, "实际开始时间")
        end = parse_dt(actual_end, "实际结束时间")
        require_window(start, end, "实际服务记录")
        record = self.store.add_record(
            {
                "root_id": None,
                "practitioner_id": practitioner_id,
                "institution_id": institution_id,
                "actual_start": iso(start),
                "actual_end": iso(end),
                "location": location.strip(),
                "note": note.strip(),
                "status": RECORD_ACTIVE,
                "version": 1,
                "supersedes": None,
                "source": _require_text(source, "来源"),
                "submitted_by": actor.actor_id,
                "submitted_at": iso(utcnow()),
            }
        )
        conflicts = self._detect_conflicts(record)
        case = self._create_case(record, conflicts) if conflicts else None
        return {
            "record": self._serialize_record(actor, record),
            "conflicts": conflicts,
            "case": self.serialize_case(actor, case) if case else None,
        }

    def _covered_by_registration(
        self, practitioner_id: str, institution_id: str, start, end
    ) -> bool:
        for registration in self.store.registrations_for(practitioner_id, institution_id):
            if registration["status"] != REGISTRATION_ACTIVE:
                continue
            reg_from = parse_dt(registration["effective_from"])
            reg_to = parse_dt(registration["effective_to"]) if registration["effective_to"] else None
            if reg_from <= start and (reg_to is None or end <= reg_to):
                return True
        return False

    def _detect_conflicts(self, record: dict) -> list[dict]:
        """对新提交的记录做时空冲突检测。"""
        conflicts: list[dict] = []
        start = parse_dt(record["actual_start"])
        end = parse_dt(record["actual_end"])

        if not self._covered_by_registration(
            record["practitioner_id"], record["institution_id"], start, end
        ):
            conflicts.append(
                {
                    "kind": CONFLICT_OUTSIDE_REGISTRATION,
                    "detail": "服务时段不在该机构任何有效执业关系区间内",
                }
            )

        for other in self.store.records_for_practitioner(record["practitioner_id"]):
            if other["id"] == record["id"] or other["status"] != RECORD_ACTIVE:
                continue
            if other["institution_id"] == record["institution_id"]:
                continue  # 同机构内部重叠不属于跨院冲突
            other_start = parse_dt(other["actual_start"])
            other_end = parse_dt(other["actual_end"])
            gap = interval_gap_seconds(start, end, other_start, other_end)
            buffer_seconds = (
                self.store.buffer_minutes(
                    record["institution_id"],
                    other["institution_id"],
                    DEFAULT_BUFFER_MINUTES,
                )
                * 60
            )
            if gap < 0:
                conflicts.append(
                    {
                        "kind": CONFLICT_OVERLAP,
                        "other_record_id": other["id"],
                        "other_institution_id": other["institution_id"],
                        "overlap_seconds": int(-gap),
                    }
                )
            elif gap < buffer_seconds:
                conflicts.append(
                    {
                        "kind": CONFLICT_BUFFER,
                        "other_record_id": other["id"],
                        "other_institution_id": other["institution_id"],
                        "required_buffer_seconds": buffer_seconds,
                        "actual_gap_seconds": int(gap),
                    }
                )
        conflicts.sort(key=lambda item: _CONFLICT_PRIORITY[item["kind"]])
        return conflicts

    def _create_case(self, record: dict, conflicts: list[dict]) -> dict:
        case = self.store.add_case(
            {
                "practitioner_id": record["practitioner_id"],
                "kind": conflicts[0]["kind"],
                "status": CASE_PENDING,
                "outcome": None,
                "evidence": {
                    "subject_record_id": record["id"],
                    "conflicts": conflicts,
                },
                "created_at": iso(utcnow()),
                "decided_at": None,
            }
        )
        self.store.link_case_record(case["id"], record["id"], "subject")
        for conflict in conflicts:
            other_id = conflict.get("other_record_id")
            if other_id:
                self.store.link_case_record(case["id"], other_id, "conflicting")
        return self.store.get_case(case["id"])

    # ------------------------------------------------------------------
    # 案件处置：纠正身份 / 机构撤报 / 合理重叠说明 / 决定复开 / 归档
    # ------------------------------------------------------------------
    def file_case_event(
        self,
        actor: Actor,
        case_id: str,
        kind: str,
        reason: str,
        source: str,
        payload: dict | None = None,
    ) -> dict:
        payload = dict(payload or {})
        _require(kind in CASE_EVENT_KINDS, f"不支持的案件事件类型：{kind}")
        reason = _require_text(reason, "处置原因")
        source = _require_text(source, "来源")
        case = self.store.get_case(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")

        side_effects: dict[str, Any] = {}
        if kind == EVENT_REOPEN:
            self._require_role(actor, ROLE_REGULATOR, ROLE_REVIEWER)
            if case["status"] not in (CASE_DECIDED, CASE_ARCHIVED):
                raise StateConflictError("仅已决定或已归档的案件可以复开")
            new_status, outcome, decided_at = CASE_HANDLING, None, None
        elif kind == EVENT_ARCHIVE:
            self._require_role(actor, ROLE_REGULATOR)
            if case["status"] != CASE_DECIDED:
                raise StateConflictError("仅已决定的案件可以归档")
            new_status, outcome, decided_at = CASE_ARCHIVED, case["outcome"], case["decided_at"]
        else:
            # 决定类事件：纠正身份 / 机构撤报 / 合理重叠说明
            if case["status"] not in (CASE_PENDING, CASE_HANDLING):
                raise StateConflictError("案件已决定，需先复开才能再次处置")
            if kind == EVENT_IDENTITY_CORRECTION:
                self._require_role(actor, ROLE_REGULATOR, ROLE_REVIEWER)
                side_effects = self._apply_identity_correction(actor, case, payload, source)
            elif kind == EVENT_INSTITUTION_WITHDRAWAL:
                self._require_role(actor, ROLE_REGULATOR, ROLE_COMPLIANCE)
                side_effects = self._apply_withdrawal(actor, case, payload)
            elif kind == EVENT_OVERLAP_JUSTIFICATION:
                self._check_justification_permission(actor, case)
                _require_text(payload.get("explanation"), "合理重叠说明")
            new_status, outcome, decided_at = CASE_DECIDED, kind, iso(utcnow())

        event = self.store.add_case_event(
            {
                "case_id": case_id,
                "kind": kind,
                "actor_id": actor.actor_id,
                "actor_role": actor.role,
                "institution_id": actor.institution_id,
                "reason": reason,
                "source": source,
                "payload": payload,
                "created_at": iso(utcnow()),
            }
        )
        self.store.update_case(case_id, new_status, outcome, decided_at)
        return {
            "event": event,
            "case": self.serialize_case(actor, self.store.get_case(case_id)),
            "side_effects": side_effects,
        }

    def _case_record_ids(self, case_id: str) -> list[str]:
        return [link["record_id"] for link in self.store.case_record_links(case_id)]

    def _linked_record(self, case_id: str, record_id: Any, label: str = "记录") -> dict:
        record_id = _require_text(record_id, f"{label}编号")
        record = self.store.get_record(record_id)
        if record is None:
            raise NotFoundError(f"{label}不存在：{record_id}")
        if record_id not in self._case_record_ids(case_id):
            raise ValidationError(f"{label} {record_id} 不在案件证据范围内")
        return record

    def _apply_identity_correction(
        self, actor: Actor, case: dict, payload: dict, source: str
    ) -> dict:
        """纠正身份：生成指向新执业人员的新版本记录，旧版本标记 superseded。"""
        record = self._linked_record(case["id"], payload.get("record_id"))
        if record["status"] != RECORD_ACTIVE:
            raise StateConflictError("仅有效状态的记录可以纠正身份")
        corrected_id = _require_text(payload.get("corrected_practitioner_id"), "纠正后执业人员")
        if corrected_id == record["practitioner_id"]:
            raise ValidationError("纠正后的执业人员与原记录相同")
        if self.store.get_practitioner(corrected_id) is None:
            raise NotFoundError(f"执业人员不存在：{corrected_id}")

        new_record = self.store.add_record(
            {
                "root_id": record["root_id"],
                "practitioner_id": corrected_id,
                "institution_id": record["institution_id"],
                "actual_start": record["actual_start"],
                "actual_end": record["actual_end"],
                "location": record["location"],
                "note": record["note"],
                "status": RECORD_ACTIVE,
                "version": record["version"] + 1,
                "supersedes": record["id"],
                "source": source,
                "submitted_by": actor.actor_id,
                "submitted_at": iso(utcnow()),
            }
        )
        self.store.mark_record(record["id"], RECORD_SUPERSEDED)
        # 纠正后的新记录重新做时空冲突检测
        conflicts = self._detect_conflicts(new_record)
        follow_up = self._create_case(new_record, conflicts) if conflicts else None
        return {
            "superseded_record_id": record["id"],
            "new_record": new_record,
            "follow_up_case": follow_up,
        }

    def _apply_withdrawal(self, actor: Actor, case: dict, payload: dict) -> dict:
        """机构撤报：记录状态翻转为 withdrawn，历史版本保留。"""
        record = self._linked_record(case["id"], payload.get("record_id"))
        self._require_compliance_scope(actor, record["institution_id"])
        if record["status"] != RECORD_ACTIVE:
            raise StateConflictError("仅有效状态的记录可以撤报")
        self.store.mark_record(
            record["id"],
            RECORD_WITHDRAWN,
            withdrawn_at=iso(utcnow()),
            withdraw_reason=_require_text(payload.get("withdraw_reason"), "撤报说明"),
        )
        return {"withdrawn_record_id": record["id"]}

    def _check_justification_permission(self, actor: Actor, case: dict) -> None:
        if actor.role in (ROLE_REGULATOR, ROLE_REVIEWER):
            return
        if actor.role == ROLE_PRACTITIONER and actor.actor_id == case["practitioner_id"]:
            return
        if actor.role == ROLE_COMPLIANCE and actor.institution_id:
            own = {
                self.store.get_record(record_id)["institution_id"]
                for record_id in self._case_record_ids(case["id"])
            }
            if actor.institution_id in own:
                return
        raise PermissionDeniedError("无权为该案件提交合理重叠说明")

    # ------------------------------------------------------------------
    # 查询：记录 / 案件 / 人员时间线（含机构明细隔离）
    # ------------------------------------------------------------------
    def get_record(self, actor: Actor, record_id: str) -> dict:
        record = self.store.get_record(record_id)
        if record is None:
            raise NotFoundError(f"记录不存在：{record_id}")
        return self._serialize_record(actor, record)

    def get_record_versions(self, actor: Actor, record_id: str) -> list[dict]:
        record = self.store.get_record(record_id)
        if record is None:
            raise NotFoundError(f"记录不存在：{record_id}")
        return [
            self._serialize_record(actor, version)
            for version in self.store.record_versions(record["root_id"])
        ]

    def list_cases(
        self,
        actor: Actor,
        status: str | None = None,
        practitioner_id: str | None = None,
    ) -> list[dict]:
        if status is not None:
            _require(status in (CASE_PENDING, CASE_HANDLING, CASE_DECIDED, CASE_ARCHIVED),
                     f"未知的案件状态：{status}")
        if actor.role in (ROLE_REGULATOR, ROLE_REVIEWER):
            cases = self.store.list_cases(status, practitioner_id)
        elif actor.role == ROLE_PRACTITIONER:
            if practitioner_id is not None and practitioner_id != actor.actor_id:
                raise PermissionDeniedError("执业人员只能查看本人案件")
            cases = self.store.list_cases(status, actor.actor_id)
        elif actor.role == ROLE_COMPLIANCE:
            self._require_compliance_scope(actor, actor.institution_id or "")
            involved = set(self.store.cases_involving_institution(actor.institution_id))
            cases = [
                case
                for case in self.store.list_cases(status, practitioner_id)
                if case["id"] in involved
            ]
        else:
            raise PermissionDeniedError(f"角色 {actor.role} 无权查看案件")
        return [self.serialize_case(actor, case, with_events=False) for case in cases]

    def get_case(self, actor: Actor, case_id: str) -> dict:
        case = self.store.get_case(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        if actor.role == ROLE_COMPLIANCE:
            involved = set(self.store.cases_involving_institution(actor.institution_id or ""))
            if case_id not in involved:
                raise PermissionDeniedError("该案件不涉及本机构，无权查看")
        elif actor.role == ROLE_PRACTITIONER and actor.actor_id != case["practitioner_id"]:
            raise PermissionDeniedError("执业人员只能查看本人案件")
        return self.serialize_case(actor, case)

    def timeline(
        self,
        actor: Actor,
        practitioner_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        """按人员时间线展示执业关系、计划、实际记录与冲突证据。"""
        practitioner = self.store.get_practitioner(practitioner_id)
        if practitioner is None:
            raise NotFoundError(f"执业人员不存在：{practitioner_id}")
        if actor.role == ROLE_PRACTITIONER and actor.actor_id != practitioner_id:
            raise PermissionDeniedError("执业人员只能查看本人时间线")
        if actor.role == ROLE_COMPLIANCE:
            self._require_compliance_scope(actor, actor.institution_id or "")

        start_dt = parse_dt(start, "时间线开始") if start else None
        end_dt = parse_dt(end, "时间线结束") if end else None

        def in_range(moment: str) -> bool:
            parsed = parse_dt(moment)
            return (start_dt is None or parsed >= start_dt) and (
                end_dt is None or parsed <= end_dt
            )

        items: list[dict] = []
        for registration in self.store.registrations_for(practitioner_id):
            if not in_range(registration["effective_from"]):
                continue
            items.append(
                {
                    "type": "registration",
                    "start": registration["effective_from"],
                    "end": registration["effective_to"],
                    "data": self._serialize_registration(actor, registration),
                }
            )
        for plan in self.store.plans_for(practitioner_id):
            if not in_range(plan["planned_start"]):
                continue
            items.append(
                {
                    "type": "plan",
                    "start": plan["planned_start"],
                    "end": plan["planned_end"],
                    "data": self._serialize_plan(actor, plan),
                }
            )
        for record in self.store.records_for_practitioner(practitioner_id):
            if not in_range(record["actual_start"]):
                continue
            items.append(
                {
                    "type": "record",
                    "start": record["actual_start"],
                    "end": record["actual_end"],
                    "data": self._serialize_record(actor, record),
                }
            )
        for case in self.store.list_cases(practitioner_id=practitioner_id):
            if actor.role == ROLE_COMPLIANCE:
                involved = set(
                    self.store.cases_involving_institution(actor.institution_id or "")
                )
                if case["id"] not in involved:
                    continue
            if not in_range(case["created_at"]):
                continue
            items.append(
                {
                    "type": "case",
                    "start": case["created_at"],
                    "end": case["decided_at"],
                    "data": self.serialize_case(actor, case, with_events=False),
                }
            )
        items.sort(key=lambda item: (item["start"], item["type"]))
        return {
            "practitioner": practitioner,
            "viewer": {"role": actor.role, "institution_id": actor.institution_id},
            "items": items,
        }

    # ------------------------------------------------------------------
    # 序列化与脱敏
    # ------------------------------------------------------------------
    def _serialize_registration(self, actor: Actor, registration: dict) -> dict:
        if self._can_view_institution_data(actor, registration["institution_id"]):
            return dict(registration)
        return {
            "id": registration["id"],
            "masked": True,
            "effective_from": registration["effective_from"],
            "effective_to": registration["effective_to"],
            "status": registration["status"],
        }

    def _serialize_plan(self, actor: Actor, plan: dict) -> dict:
        if self._can_view_institution_data(actor, plan["institution_id"]):
            return dict(plan)
        return {
            "id": plan["id"],
            "masked": True,
            "planned_start": plan["planned_start"],
            "planned_end": plan["planned_end"],
            "status": plan["status"],
        }

    def _serialize_record(self, actor: Actor, record: dict) -> dict:
        if self._can_view_record(actor, record):
            return dict(record)
        # 他机构明细脱敏：仅保留判断冲突所需的时间窗与状态
        return {
            "id": record["id"],
            "masked": True,
            "practitioner_id": record["practitioner_id"],
            "actual_start": record["actual_start"],
            "actual_end": record["actual_end"],
            "status": record["status"],
            "version": record["version"],
        }

    def _can_view_institution_data(self, actor: Actor, institution_id: str) -> bool:
        if actor.role in (ROLE_REGULATOR, ROLE_REVIEWER, ROLE_PRACTITIONER):
            return True
        return actor.role == ROLE_COMPLIANCE and actor.institution_id == institution_id

    def serialize_case(
        self, actor: Actor, case: dict | None, with_events: bool = True
    ) -> dict | None:
        if case is None:
            return None
        links = self.store.case_record_links(case["id"])
        records = []
        for link in links:
            record = self.store.get_record(link["record_id"])
            if record is not None:
                records.append(
                    {"role": link["role"], "record": self._serialize_record(actor, record)}
                )
        data = {
            "id": case["id"],
            "practitioner_id": case["practitioner_id"],
            "kind": case["kind"],
            "status": case["status"],
            "outcome": case["outcome"],
            "created_at": case["created_at"],
            "decided_at": case["decided_at"],
            "evidence": case["evidence"],
            "records": records,
        }
        if with_events:
            data["events"] = [
                self._serialize_event(actor, event)
                for event in self.store.events_for_case(case["id"])
            ]
        return data

    def _serialize_event(self, actor: Actor, event: dict) -> dict:
        if actor.role in (ROLE_REGULATOR, ROLE_REVIEWER):
            return dict(event)
        # 机构/执业人员视角：保留来源与理由，隐藏处置细节负载
        return {key: event[key] for key in _EVENT_SAFE_FIELDS}
