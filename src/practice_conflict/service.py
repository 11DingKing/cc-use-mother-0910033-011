"""多机构执业冲突应用服务。

覆盖契约不变量：
- 执业关系时态：计划/记录提交时校验执业关系生效区间覆盖；
- 时空冲突检测：提交时比对时间重叠与站点间移动缓冲；
- 待核案件流程：待核验 → 处置中 → 已决定 → 已归档，支持复开且保留来源；
- 机构明细隔离：查询与时间线按角色裁剪。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .conflicts import BufferTable, Commitment, detect_conflicts, uncovered_intervals
from .errors import NotFoundError, PermissionDeniedError, StateError, ValidationError
from .models import (
    Actor,
    Case,
    CaseState,
    ConflictEvidence,
    MovementBuffer,
    PracticeRelation,
    Resolution,
    Role,
    ServicePlan,
    ServiceRecord,
    format_ts,
    parse_ts,
)
from .security import (
    can_decide_case,
    can_read_all,
    case_visible_to,
    institution_visible_to,
    mask_case_dict,
)
from .store import Store


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PracticeConflictService:
    """登记执业数据、检测时空冲突并管理待核案件。"""

    def __init__(self, store: Store | None = None, default_buffer_minutes: int = 30) -> None:
        self.store = store or Store()
        self.default_buffer_minutes = default_buffer_minutes

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _buffer_table(self) -> BufferTable:
        return BufferTable(self.store.buffers.values(), self.default_buffer_minutes)

    @staticmethod
    def _interval(start: str, end: str) -> tuple[datetime, datetime]:
        s, e = parse_ts(start), parse_ts(end)
        if e <= s:
            raise ValidationError("结束时间必须晚于开始时间")
        return s, e

    def _require_institution(self, actor: Actor, institution_id: str) -> None:
        if actor.role is Role.REGULATOR:
            return
        if actor.role is Role.OFFICER and actor.institution_id == institution_id:
            return
        raise PermissionDeniedError("仅本机构合规员或监管人员可执行该操作")

    def _require_read(self, actor: Actor, institution_id: str, practitioner_id: str) -> None:
        if can_read_all(actor):
            return
        if actor.role is Role.OFFICER and actor.institution_id == institution_id:
            return
        if actor.role is Role.PRACTITIONER and actor.practitioner_id == practitioner_id:
            return
        raise PermissionDeniedError("无权查看该明细")

    def _relation(self, relation_id: str) -> PracticeRelation:
        relation = self.store.relations.get(relation_id)
        if relation is None:
            raise NotFoundError(f"执业关系不存在：{relation_id}")
        return relation

    def _plan(self, plan_id: str) -> ServicePlan:
        plan = self.store.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"服务计划不存在：{plan_id}")
        return plan

    def _record(self, record_id: str) -> ServiceRecord:
        record = self.store.records.get(record_id)
        if record is None:
            raise NotFoundError(f"服务记录不存在：{record_id}")
        return record

    def _case(self, case_id: str) -> Case:
        case = self.store.cases.get(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return case

    def _active_commitments(self, practitioner_id: str) -> list[Commitment]:
        items: list[Commitment] = []
        for plan in self.store.plans.values():
            if plan.practitioner_id == practitioner_id and plan.status == "active":
                items.append(
                    Commitment("plan", plan.plan_id, plan.practitioner_id, plan.institution_id, plan.site, plan.start, plan.end)
                )
        for rec in self.store.records.values():
            if rec.practitioner_id == practitioner_id and rec.status == "active":
                items.append(
                    Commitment("record", rec.record_id, rec.practitioner_id, rec.institution_id, rec.site, rec.start, rec.end)
                )
        return items

    def _transition(self, case: Case, actor_label: str, to: CaseState, reason: str) -> None:
        case.history.append(
            {"at": format_ts(_utcnow()), "by": actor_label, "from": case.state.value, "to": to.value, "reason": reason}
        )
        case.state = to

    def _new_case(self, practitioner_id: str, evidence: ConflictEvidence) -> Case:
        case = Case(
            case_id=self.store.next_id("CASE"),
            practitioner_id=practitioner_id,
            case_type=evidence.case_type,
            evidence=evidence,
            created_at=_utcnow(),
        )
        case.history.append(
            {"at": format_ts(case.created_at), "by": "system", "from": None, "to": CaseState.PENDING.value, "reason": "提交时自动检测"}
        )
        self.store.cases[case.case_id] = case
        self.store.log(
            "system",
            "case.create",
            "case",
            case.case_id,
            {"case_type": evidence.case_type, "practitioner_id": practitioner_id},
        )
        return case

    def _evaluate(self, commitment: Commitment, skip_plan_id: str | None = None) -> list[Case]:
        """提交时检测：执业关系时态缺口 + 时空冲突，逐条生成待核案件。"""
        cases: list[Case] = []
        relations = [
            rel
            for rel in self.store.relations.values()
            if rel.practitioner_id == commitment.practitioner_id
            and rel.institution_id == commitment.institution_id
            and rel.status == "active"
        ]
        missing = uncovered_intervals(commitment.start, commitment.end, relations)
        if missing:
            cases.append(
                self._new_case(
                    commitment.practitioner_id,
                    ConflictEvidence(
                        case_type="out_of_relation",
                        practitioner_id=commitment.practitioner_id,
                        side_a=commitment.side(),
                        side_b=None,
                        scope="same_institution",
                        missing_intervals=missing,
                    ),
                )
            )
        existing = self._active_commitments(commitment.practitioner_id)
        for evidence in detect_conflicts(commitment, existing, self._buffer_table()):
            if skip_plan_id and skip_plan_id in (
                evidence.side_a.ref_id if evidence.side_a else None,
                evidence.side_b.ref_id if evidence.side_b else None,
            ):
                continue
            cases.append(self._new_case(commitment.practitioner_id, evidence))
        return cases

    def _fulfill_plans(self, record: ServiceRecord) -> None:
        """同机构同人且时间相交的计划视为被该记录履约。"""
        for plan in self.store.plans.values():
            if (
                plan.practitioner_id == record.practitioner_id
                and plan.institution_id == record.institution_id
                and plan.status == "active"
                and plan.start < record.end
                and record.start < plan.end
            ):
                plan.status = "fulfilled"
                self.store.log("system", "plan.fulfill", "plan", plan.plan_id, {"record_id": record.record_id})

    def _register_record(
        self,
        actor_label: str,
        practitioner_id: str,
        institution_id: str,
        site: str,
        start: datetime,
        end: datetime,
        plan_id: str | None = None,
        corrected_from: str | None = None,
    ) -> tuple[ServiceRecord, list[Case]]:
        record = ServiceRecord(
            record_id=self.store.next_id("REC"),
            practitioner_id=practitioner_id,
            institution_id=institution_id,
            site=site,
            start=start,
            end=end,
            submitted_by=actor_label,
            submitted_at=_utcnow(),
            plan_id=plan_id,
            corrected_from=corrected_from,
        )
        self.store.records[record.record_id] = record
        self._fulfill_plans(record)
        commitment = Commitment("record", record.record_id, practitioner_id, institution_id, site, start, end)
        cases = self._evaluate(commitment, skip_plan_id=plan_id)
        return record, cases

    # ------------------------------------------------------------------
    # 执业关系
    # ------------------------------------------------------------------
    def register_relation(
        self,
        actor: Actor,
        practitioner_id: str,
        institution_id: str,
        valid_from: str,
        valid_to: str | None = None,
    ) -> PracticeRelation:
        self._require_institution(actor, institution_id)
        start = parse_ts(valid_from)
        end = parse_ts(valid_to) if valid_to else None
        if end is not None and end <= start:
            raise ValidationError("执业关系失效时间必须晚于生效时间")
        relation = PracticeRelation(
            relation_id=self.store.next_id("REL"),
            practitioner_id=practitioner_id,
            institution_id=institution_id,
            valid_from=start,
            valid_to=end,
            registered_by=actor.actor_id,
            registered_at=_utcnow(),
        )
        self.store.relations[relation.relation_id] = relation
        self.store.log(
            actor.actor_id,
            "relation.register",
            "relation",
            relation.relation_id,
            {"practitioner_id": practitioner_id, "institution_id": institution_id},
        )
        return relation

    def revoke_relation(self, actor: Actor, relation_id: str, reason: str = "") -> PracticeRelation:
        relation = self._relation(relation_id)
        self._require_institution(actor, relation.institution_id)
        if relation.status != "active":
            raise StateError("执业关系已撤销，不能重复操作")
        relation.status = "revoked"
        relation.revoked = {"by": actor.actor_id, "at": format_ts(_utcnow()), "reason": reason}
        self.store.log(actor.actor_id, "relation.revoke", "relation", relation.relation_id, {"reason": reason})
        return relation

    def list_relations(
        self,
        actor: Actor,
        practitioner_id: str | None = None,
        institution_id: str | None = None,
    ) -> list[PracticeRelation]:
        if actor.role is Role.PRACTITIONER:
            practitioner_id = actor.practitioner_id
        items = []
        for rel in self.store.relations.values():
            if practitioner_id and rel.practitioner_id != practitioner_id:
                continue
            if institution_id and rel.institution_id != institution_id:
                continue
            if not institution_visible_to(actor, rel.institution_id):
                continue
            items.append(rel)
        return sorted(items, key=lambda rel: rel.relation_id)

    # ------------------------------------------------------------------
    # 移动缓冲
    # ------------------------------------------------------------------
    def register_buffer(self, actor: Actor, site_a: str, site_b: str, minutes: int) -> MovementBuffer:
        if actor.role is not Role.REGULATOR:
            raise PermissionDeniedError("仅监管人员可登记移动缓冲")
        if not site_a or not site_b or site_a == site_b:
            raise ValidationError("移动缓冲需要两个不同的站点")
        minutes = int(minutes)
        if minutes < 0:
            raise ValidationError("移动缓冲分钟数不能为负")
        for buf in self.store.buffers.values():
            if buf.status == "active" and {buf.site_a, buf.site_b} == {site_a, site_b}:
                buf.status = "revoked"
                self.store.log(actor.actor_id, "buffer.replace", "buffer", buf.buffer_id, {"reason": "被新登记覆盖"})
        buffer = MovementBuffer(
            buffer_id=self.store.next_id("BUF"),
            site_a=site_a,
            site_b=site_b,
            minutes=minutes,
            registered_by=actor.actor_id,
            registered_at=_utcnow(),
        )
        self.store.buffers[buffer.buffer_id] = buffer
        self.store.log(
            actor.actor_id,
            "buffer.register",
            "buffer",
            buffer.buffer_id,
            {"site_a": site_a, "site_b": site_b, "minutes": minutes},
        )
        return buffer

    def list_buffers(self, actor: Actor) -> list[MovementBuffer]:
        return sorted(self.store.buffers.values(), key=lambda buf: buf.buffer_id)

    # ------------------------------------------------------------------
    # 服务计划
    # ------------------------------------------------------------------
    def create_plan(
        self,
        actor: Actor,
        practitioner_id: str,
        institution_id: str,
        site: str,
        start: str,
        end: str,
    ) -> tuple[ServicePlan, list[Case]]:
        self._require_institution(actor, institution_id)
        s, e = self._interval(start, end)
        plan = ServicePlan(
            plan_id=self.store.next_id("PLAN"),
            practitioner_id=practitioner_id,
            institution_id=institution_id,
            site=site,
            start=s,
            end=e,
            created_by=actor.actor_id,
            created_at=_utcnow(),
        )
        self.store.plans[plan.plan_id] = plan
        cases = self._evaluate(Commitment("plan", plan.plan_id, practitioner_id, institution_id, site, s, e))
        self.store.log(
            actor.actor_id,
            "plan.create",
            "plan",
            plan.plan_id,
            {"practitioner_id": practitioner_id, "institution_id": institution_id, "cases": [c.case_id for c in cases]},
        )
        return plan, cases

    def cancel_plan(self, actor: Actor, plan_id: str, reason: str = "") -> ServicePlan:
        plan = self._plan(plan_id)
        self._require_institution(actor, plan.institution_id)
        if plan.status != "active":
            raise StateError("仅进行中的计划可取消")
        plan.status = "cancelled"
        plan.cancelled = {"by": actor.actor_id, "at": format_ts(_utcnow()), "reason": reason}
        self.store.log(actor.actor_id, "plan.cancel", "plan", plan.plan_id, {"reason": reason})
        return plan

    def list_plans(
        self,
        actor: Actor,
        practitioner_id: str | None = None,
        institution_id: str | None = None,
    ) -> list[ServicePlan]:
        if actor.role is Role.PRACTITIONER:
            practitioner_id = actor.practitioner_id
        items = []
        for plan in self.store.plans.values():
            if practitioner_id and plan.practitioner_id != practitioner_id:
                continue
            if institution_id and plan.institution_id != institution_id:
                continue
            if not institution_visible_to(actor, plan.institution_id):
                continue
            items.append(plan)
        return sorted(items, key=lambda plan: plan.plan_id)

    # ------------------------------------------------------------------
    # 实际记录
    # ------------------------------------------------------------------
    def submit_record(
        self,
        actor: Actor,
        practitioner_id: str,
        institution_id: str,
        site: str,
        start: str,
        end: str,
        plan_id: str | None = None,
    ) -> tuple[ServiceRecord, list[Case]]:
        self._require_institution(actor, institution_id)
        s, e = self._interval(start, end)
        if plan_id is not None:
            plan = self._plan(plan_id)
            if plan.practitioner_id != practitioner_id or plan.institution_id != institution_id:
                raise ValidationError("关联计划与该记录的人员或机构不一致")
        record, cases = self._register_record(actor.actor_id, practitioner_id, institution_id, site, s, e, plan_id=plan_id)
        self.store.log(
            actor.actor_id,
            "record.submit",
            "record",
            record.record_id,
            {"practitioner_id": practitioner_id, "institution_id": institution_id, "cases": [c.case_id for c in cases]},
        )
        return record, cases

    def withdraw_record(self, actor: Actor, record_id: str, reason: str = "") -> ServiceRecord:
        """机构撤报：记录标记为 withdrawn，保留在库不参与后续检测。"""
        record = self._record(record_id)
        self._require_institution(actor, record.institution_id)
        if record.status != "active":
            raise StateError("记录已撤报或已被更正，不能重复撤报")
        record.status = "withdrawn"
        record.withdrawn = {"by": actor.actor_id, "at": format_ts(_utcnow()), "reason": reason}
        self.store.log(actor.actor_id, "record.withdraw", "record", record.record_id, {"reason": reason})
        return record

    def correct_identity(
        self,
        actor: Actor,
        record_id: str,
        new_practitioner_id: str,
        reason: str = "",
    ) -> tuple[ServiceRecord, list[Case]]:
        """纠正身份：原记录标记 superseded，生成归属新人员的记录并重新检测。"""
        record = self._record(record_id)
        self._require_institution(actor, record.institution_id)
        if record.status != "active":
            raise StateError("仅有效记录可纠正身份")
        if not new_practitioner_id or new_practitioner_id == record.practitioner_id:
            raise ValidationError("纠正后的人员标识必须不同且非空")
        new_record, cases = self._register_record(
            actor.actor_id,
            new_practitioner_id,
            record.institution_id,
            record.site,
            record.start,
            record.end,
            corrected_from=record.record_id,
        )
        record.status = "superseded"
        record.superseded_by = new_record.record_id
        self.store.log(
            actor.actor_id,
            "record.correct_identity",
            "record",
            record.record_id,
            {
                "reason": reason,
                "from_practitioner": record.practitioner_id,
                "to_practitioner": new_practitioner_id,
                "new_record_id": new_record.record_id,
                "cases": [c.case_id for c in cases],
            },
        )
        return new_record, cases

    def get_record(self, actor: Actor, record_id: str) -> ServiceRecord:
        record = self._record(record_id)
        self._require_read(actor, record.institution_id, record.practitioner_id)
        return record

    def list_records(
        self,
        actor: Actor,
        practitioner_id: str | None = None,
        institution_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[ServiceRecord]:
        if actor.role is Role.PRACTITIONER:
            practitioner_id = actor.practitioner_id
        items = []
        for rec in self.store.records.values():
            if practitioner_id and rec.practitioner_id != practitioner_id:
                continue
            if institution_id and rec.institution_id != institution_id:
                continue
            if not include_inactive and rec.status != "active":
                continue
            if not institution_visible_to(actor, rec.institution_id):
                continue
            items.append(rec)
        return sorted(items, key=lambda rec: rec.record_id)

    # ------------------------------------------------------------------
    # 待核案件
    # ------------------------------------------------------------------
    def submit_explanation(self, actor: Actor, case_id: str, text: str) -> Case:
        """合理重叠说明：涉案机构合规员、当事执业人员或监管人员可提交。"""
        case = self._case(case_id)
        if case.state not in (CaseState.PENDING, CaseState.HANDLING):
            raise StateError("案件已决定，如需补充说明请先复开")
        if not text or not text.strip():
            raise ValidationError("说明内容不能为空")
        allowed = actor.role is Role.REGULATOR
        if actor.role is Role.OFFICER:
            allowed = actor.institution_id in case.institutions
        if actor.role is Role.PRACTITIONER:
            allowed = actor.practitioner_id == case.practitioner_id
        if not allowed:
            raise PermissionDeniedError("仅涉案机构、当事执业人员或监管人员可提交说明")
        case.explanations.append(
            {
                "by": actor.actor_id,
                "role": actor.role.value,
                "institution_id": actor.institution_id,
                "text": text,
                "at": format_ts(_utcnow()),
            }
        )
        if case.state is CaseState.PENDING:
            self._transition(case, actor.actor_id, CaseState.HANDLING, "收到合理重叠说明")
        self.store.log(actor.actor_id, "case.explain", "case", case.case_id, {})
        return case

    def decide_case(self, actor: Actor, case_id: str, resolution: str, note: str = "") -> Case:
        if not can_decide_case(actor):
            raise PermissionDeniedError("仅监管人员或复核专家可作出决定")
        case = self._case(case_id)
        if case.state not in (CaseState.PENDING, CaseState.HANDLING):
            raise StateError("案件当前状态不可决定，如需重新决定请先复开")
        try:
            resolution_value = Resolution(resolution)
        except ValueError:
            raise ValidationError("无效的决定类型，可选：" + "、".join(item.value for item in Resolution)) from None
        case.decisions.append(
            {
                "seq": len(case.decisions) + 1,
                "resolution": resolution_value.value,
                "note": note,
                "by": actor.actor_id,
                "at": format_ts(_utcnow()),
            }
        )
        self._transition(case, actor.actor_id, CaseState.DECIDED, resolution_value.value)
        self.store.log(actor.actor_id, "case.decide", "case", case.case_id, {"resolution": resolution_value.value})
        return case

    def archive_case(self, actor: Actor, case_id: str) -> Case:
        if not can_decide_case(actor):
            raise PermissionDeniedError("仅监管人员或复核专家可归档")
        case = self._case(case_id)
        if case.state is not CaseState.DECIDED:
            raise StateError("仅已决定的案件可归档")
        self._transition(case, actor.actor_id, CaseState.ARCHIVED, "归档")
        self.store.log(actor.actor_id, "case.archive", "case", case.case_id, {})
        return case

    def reopen_case(self, actor: Actor, case_id: str, reason: str = "") -> Case:
        """复开：回到待核验，既往说明与决定全部保留。"""
        if not can_decide_case(actor):
            raise PermissionDeniedError("仅监管人员或复核专家可复开案件")
        case = self._case(case_id)
        if case.state not in (CaseState.DECIDED, CaseState.ARCHIVED):
            raise StateError("仅已决定或已归档的案件可复开")
        self._transition(case, actor.actor_id, CaseState.PENDING, reason or "复开")
        self.store.log(actor.actor_id, "case.reopen", "case", case.case_id, {"reason": reason})
        return case

    def case_view(self, actor: Actor, case: Case) -> dict:
        return mask_case_dict(case.to_dict(), actor)

    def get_case(self, actor: Actor, case_id: str) -> dict:
        case = self._case(case_id)
        if not case_visible_to(actor, case):
            raise PermissionDeniedError("无权查看该案件")
        return self.case_view(actor, case)

    def list_cases(
        self,
        actor: Actor,
        state: str | None = None,
        practitioner_id: str | None = None,
    ) -> list[dict]:
        state_value: CaseState | None = None
        if state:
            try:
                state_value = CaseState(state)
            except ValueError:
                raise ValidationError("无效的案件状态，可选：" + "、".join(item.value for item in CaseState)) from None
        if actor.role is Role.PRACTITIONER:
            practitioner_id = actor.practitioner_id
        items = []
        for case in self.store.cases.values():
            if state_value and case.state is not state_value:
                continue
            if practitioner_id and case.practitioner_id != practitioner_id:
                continue
            if not case_visible_to(actor, case):
                continue
            items.append(self.case_view(actor, case))
        return sorted(items, key=lambda item: item["case_id"])

    # ------------------------------------------------------------------
    # 人员时间线与审计
    # ------------------------------------------------------------------
    def timeline(
        self,
        actor: Actor,
        practitioner_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        """按人员汇总关系、计划、记录与冲突案件，按角色做机构隔离。"""
        if actor.role is Role.PRACTITIONER and actor.practitioner_id != practitioner_id:
            raise PermissionDeniedError("执业人员只能查看本人时间线")
        lo = parse_ts(start) if start else None
        hi = parse_ts(end) if end else None

        def intersects(s: datetime, e: datetime | None) -> bool:
            if lo is not None and e is not None and e <= lo:
                return False
            if hi is not None and s >= hi:
                return False
            return True

        items: list[dict] = []
        for rel in self.list_relations(actor, practitioner_id=practitioner_id):
            if intersects(rel.valid_from, rel.valid_to):
                items.append({"type": "relation", "when": format_ts(rel.valid_from), "relation": rel.to_dict()})
        for plan in self.list_plans(actor, practitioner_id=practitioner_id):
            if intersects(plan.start, plan.end):
                items.append({"type": "plan", "when": format_ts(plan.start), "plan": plan.to_dict()})
        for rec in self.list_records(actor, practitioner_id=practitioner_id, include_inactive=True):
            if intersects(rec.start, rec.end):
                items.append({"type": "record", "when": format_ts(rec.start), "record": rec.to_dict()})
        for case in self.store.cases.values():
            if case.practitioner_id != practitioner_id or not case_visible_to(actor, case):
                continue
            if lo is not None and case.created_at < lo:
                continue
            if hi is not None and case.created_at >= hi:
                continue
            items.append({"type": "case", "when": format_ts(case.created_at), "case": self.case_view(actor, case)})
        items.sort(key=lambda item: item["when"])
        return {"practitioner_id": practitioner_id, "start": start, "end": end, "items": items}

    def audit_log(self, actor: Actor) -> list[dict]:
        if actor.role is not Role.REGULATOR:
            raise PermissionDeniedError("仅监管人员可查看审计日志")
        return [event.to_dict() for event in self.store.audit]
