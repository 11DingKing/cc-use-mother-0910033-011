"""应用服务的功能回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from practice_conflict.errors import PermissionDeniedError, StateError, ValidationError
from practice_conflict.models import Actor, CaseState, Role
from practice_conflict.security import MASKED_INSTITUTION
from practice_conflict.service import PracticeConflictService
from practice_conflict.store import Store

REG = Actor("reg-1", Role.REGULATOR)
REV = Actor("rev-1", Role.REVIEWER)
OFF_A = Actor("off-a", Role.OFFICER, institution_id="INST-A")
OFF_B = Actor("off-b", Role.OFFICER, institution_id="INST-B")
OFF_C = Actor("off-c", Role.OFFICER, institution_id="INST-C")
PRAC_1 = Actor("p1-user", Role.PRACTITIONER, practitioner_id="P1")

DAY = "2026-10-05"


def at(hhmm: str, day: str = DAY) -> str:
    return f"{day}T{hhmm}:00Z"


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = PracticeConflictService(default_buffer_minutes=30)
        self.svc.register_relation(REG, "P1", "INST-A", "2026-01-01T00:00:00Z")
        self.svc.register_relation(REG, "P1", "INST-B", "2026-01-01T00:00:00Z")
        self.svc.register_buffer(REG, "门诊楼A", "住院楼B", 45)


class RelationTest(ServiceTestBase):
    def test_officer_registers_only_own_institution(self) -> None:
        relation = self.svc.register_relation(OFF_A, "P2", "INST-A", "2026-02-01T00:00:00Z", "2026-12-31T00:00:00Z")
        self.assertEqual(relation.status, "active")
        with self.assertRaises(PermissionDeniedError):
            self.svc.register_relation(OFF_A, "P2", "INST-B", "2026-02-01T00:00:00Z")

    def test_invalid_interval_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.register_relation(REG, "P2", "INST-A", "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")

    def test_revoke_is_soft_and_blocks_coverage(self) -> None:
        relation = self.svc.register_relation(REG, "P3", "INST-A", "2026-01-01T00:00:00Z")
        self.svc.revoke_relation(REG, relation.relation_id, "备案注销")
        self.assertEqual(relation.status, "revoked")
        _, cases = self.svc.submit_record(REG, "P3", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        self.assertEqual([c.case_type for c in cases], ["out_of_relation"])
        with self.assertRaises(StateError):
            self.svc.revoke_relation(REG, relation.relation_id)


class ConflictDetectionTest(ServiceTestBase):
    def test_cross_institution_overlap_creates_case(self) -> None:
        _, cases1 = self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        self.assertEqual(cases1, [])
        _, cases2 = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("09:30"), at("10:30"))
        self.assertEqual(len(cases2), 1)
        case = cases2[0]
        self.assertEqual(case.case_type, "overlap")
        self.assertEqual(case.state, CaseState.PENDING)
        self.assertEqual(case.institutions, ["INST-A", "INST-B"])
        self.assertEqual(case.evidence.overlap_start.isoformat(), "2026-10-05T09:30:00+00:00")
        self.assertEqual(case.evidence.overlap_end.isoformat(), "2026-10-05T10:00:00+00:00")

    def test_insufficient_travel_buffer_creates_case(self) -> None:
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        _, cases = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("10:20"), at("11:00"))
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case.case_type, "insufficient_travel")
        self.assertEqual(case.evidence.gap_minutes, 20.0)
        self.assertEqual(case.evidence.required_buffer_minutes, 45)
        self.assertEqual(case.evidence.buffer_source, "registered")

    def test_sufficient_travel_buffer_no_case(self) -> None:
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        _, cases = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("10:45"), at("11:00"))
        self.assertEqual(cases, [])

    def test_default_buffer_used_when_unregistered(self) -> None:
        self.svc.register_relation(REG, "P1", "INST-C", "2026-01-01T00:00:00Z")
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        _, cases = self.svc.submit_record(REG, "P1", "INST-C", "未知站点", at("10:10"), at("11:00"))
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].evidence.buffer_source, "default")
        self.assertEqual(cases[0].evidence.required_buffer_minutes, 30)

    def test_same_site_overlap_flagged_as_same_institution(self) -> None:
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        _, cases = self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:30"), at("10:30"))
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].case_type, "overlap")
        self.assertEqual(cases[0].evidence.scope, "same_institution")

    def test_plan_fulfilled_by_record_is_not_conflict(self) -> None:
        plan, _ = self.svc.create_plan(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        _, cases = self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        self.assertEqual(cases, [])
        self.assertEqual(plan.status, "fulfilled")

    def test_plan_conflicts_with_other_institution_record(self) -> None:
        self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("09:30"), at("10:30"))
        _, cases = self.svc.create_plan(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        self.assertEqual(len(cases), 1)
        kinds = {cases[0].evidence.side_a.kind, cases[0].evidence.side_b.kind}
        self.assertEqual(kinds, {"plan", "record"})

    def test_out_of_relation_case_reports_missing_interval(self) -> None:
        _, cases = self.svc.submit_record(REG, "P9", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case.case_type, "out_of_relation")
        self.assertEqual(len(case.evidence.missing_intervals), 1)
        self.assertIsNone(case.evidence.side_b)


class WithdrawAndCorrectTest(ServiceTestBase):
    def _conflicting_pair(self) -> tuple[str, str, str]:
        rec1, _ = self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        rec2, cases = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("09:30"), at("10:30"))
        return rec1.record_id, rec2.record_id, cases[0].case_id

    def test_withdraw_keeps_record_and_excludes_from_detection(self) -> None:
        rec1_id, rec2_id, _ = self._conflicting_pair()
        record = self.svc.withdraw_record(OFF_B, rec2_id, "重复报送，撤回")
        self.assertEqual(record.status, "withdrawn")
        self.assertEqual(record.withdrawn["reason"], "重复报送，撤回")
        # 历史记录仍可查询，不被删除
        self.assertEqual(self.svc.get_record(REG, rec2_id).record_id, rec2_id)
        # 撤报后不再参与冲突检测
        _, cases = self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("10:30"), at("11:00"))
        self.assertEqual(cases, [])
        with self.assertRaises(StateError):
            self.svc.withdraw_record(OFF_B, rec2_id, "重复撤报")
        with self.assertRaises(PermissionDeniedError):
            self.svc.withdraw_record(OFF_B, rec1_id, "越权撤报")

    def test_correct_identity_supersedes_and_redetects(self) -> None:
        rec1_id, rec2_id, _ = self._conflicting_pair()
        self.svc.register_relation(REG, "P2", "INST-A", "2026-01-01T00:00:00Z")
        new_record, new_cases = self.svc.correct_identity(OFF_A, rec1_id, "P2", "身份录入错误")
        old = self.svc.get_record(REG, rec1_id)
        self.assertEqual(old.status, "superseded")
        self.assertEqual(old.superseded_by, new_record.record_id)
        self.assertEqual(new_record.practitioner_id, "P2")
        self.assertEqual(new_record.corrected_from, rec1_id)
        self.assertEqual(new_cases, [])  # P2 暂无其他承诺
        # 原记录已被取代，不再参与 P1 的冲突检测
        _, cases = self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:15"), at("09:45"))
        self.assertEqual(len(cases), 1)  # 仅与 INST-B 09:30-10:30 的记录冲突
        side_ids = {cases[0].evidence.side_a.ref_id, cases[0].evidence.side_b.ref_id}
        self.assertNotIn(rec1_id, side_ids)
        # 新身份 P2 的记录参与自身检测
        _, cases_p2 = self.svc.submit_record(REG, "P2", "INST-A", "门诊楼A", at("09:15"), at("09:45"))
        self.assertEqual(len(cases_p2), 1)
        self.assertEqual(cases_p2[0].evidence.scope, "same_institution")

    def test_correct_identity_requires_active_record(self) -> None:
        rec1_id, _, _ = self._conflicting_pair()
        self.svc.withdraw_record(OFF_A, rec1_id, "撤报")
        with self.assertRaises(StateError):
            self.svc.correct_identity(OFF_A, rec1_id, "P2")
        with self.assertRaises(ValidationError):
            rec2, _ = self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("12:00"), at("13:00"))
            self.svc.correct_identity(OFF_A, rec2.record_id, "P1")


class CaseWorkflowTest(ServiceTestBase):
    def _make_case(self) -> str:
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        _, cases = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("09:30"), at("10:30"))
        return cases[0].case_id

    def test_explanation_decide_reopen_preserves_history(self) -> None:
        case_id = self._make_case()
        case = self.svc.submit_explanation(OFF_A, case_id, "远程会诊，合理重叠")
        self.assertEqual(case.state, CaseState.HANDLING)
        self.svc.submit_explanation(PRAC_1, case_id, "本人确认跨院出诊")
        case = self.svc.decide_case(REV, case_id, "合理重叠", "采纳机构说明")
        self.assertEqual(case.state, CaseState.DECIDED)
        self.assertEqual(len(case.decisions), 1)
        with self.assertRaises(StateError):
            self.svc.submit_explanation(OFF_A, case_id, "已决定后补充")
        with self.assertRaises(StateError):
            self.svc.decide_case(REV, case_id, "确认违规")
        # 复开保留来源
        case = self.svc.reopen_case(REG, case_id, "出现新证据")
        self.assertEqual(case.state, CaseState.PENDING)
        self.assertEqual(len(case.decisions), 1)
        self.assertEqual(len(case.explanations), 2)
        case = self.svc.decide_case(REG, case_id, "确认违规", "复核后认定违规")
        self.assertEqual(len(case.decisions), 2)
        self.assertEqual(case.current_decision["resolution"], "确认违规")
        # 归档与再复开
        case = self.svc.archive_case(REV, case_id)
        self.assertEqual(case.state, CaseState.ARCHIVED)
        case = self.svc.reopen_case(REG, case_id, "监管抽查")
        self.assertEqual(case.state, CaseState.PENDING)
        transitions = [(h["from"], h["to"]) for h in case.history]
        self.assertIn(("已归档", "待核验"), transitions)

    def test_case_permissions(self) -> None:
        case_id = self._make_case()
        with self.assertRaises(PermissionDeniedError):
            self.svc.decide_case(OFF_A, case_id, "确认违规")
        with self.assertRaises(PermissionDeniedError):
            self.svc.reopen_case(PRAC_1, case_id)
        with self.assertRaises(PermissionDeniedError):
            self.svc.submit_explanation(OFF_C, case_id, "无关机构")
        with self.assertRaises(ValidationError):
            self.svc.decide_case(REG, case_id, "不存在的决定类型")

    def test_list_cases_filters_by_state(self) -> None:
        case_id = self._make_case()
        pending = self.svc.list_cases(REG, state="待核验")
        self.assertEqual([c["case_id"] for c in pending], [case_id])
        self.svc.decide_case(REG, case_id, "确认违规")
        self.assertEqual(self.svc.list_cases(REG, state="待核验"), [])
        with self.assertRaises(ValidationError):
            self.svc.list_cases(REG, state="不存在的状态")


class VisibilityTest(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        _, cases = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("09:30"), at("10:30"))
        self.case_id = cases[0].case_id
        self.svc.submit_explanation(OFF_B, self.case_id, "他机构说明")

    def test_officer_timeline_only_shows_own_details(self) -> None:
        timeline = self.svc.timeline(OFF_A, "P1")
        institutions = {
            item.get("record", item.get("relation", item.get("plan", {}))).get("institution_id")
            for item in timeline["items"]
            if item["type"] != "case"
        }
        self.assertEqual(institutions, {"INST-A"})
        case_item = next(item for item in timeline["items"] if item["type"] == "case")
        evidence = case_item["case"]["evidence"]
        side_a, side_b = evidence["side_a"], evidence["side_b"]
        own = side_a if side_a["institution_id"] == "INST-A" else side_b
        other = side_b if own is side_a else side_a
        self.assertIn("ref_id", own)
        self.assertEqual(other["institution_id"], MASKED_INSTITUTION)
        self.assertNotIn("ref_id", other)
        self.assertNotIn("site", other)
        # 他机构说明被屏蔽
        self.assertTrue(case_item["case"]["explanations"][0]["masked"])

    def test_regulator_sees_full_evidence(self) -> None:
        case = self.svc.get_case(REG, self.case_id)
        institutions = {case["evidence"]["side_a"]["institution_id"], case["evidence"]["side_b"]["institution_id"]}
        self.assertEqual(institutions, {"INST-A", "INST-B"})
        self.assertEqual(case["explanations"][0]["text"], "他机构说明")

    def test_officer_cannot_read_other_institution_record(self) -> None:
        record_b = self.svc.list_records(REG, institution_id="INST-B")[0]
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_record(OFF_A, record_b.record_id)
        own_records = self.svc.list_records(OFF_A)
        self.assertEqual({rec.institution_id for rec in own_records}, {"INST-A"})

    def test_practitioner_sees_own_full_timeline(self) -> None:
        timeline = self.svc.timeline(PRAC_1, "P1")
        types = {item["type"] for item in timeline["items"]}
        self.assertIn("case", types)
        self.assertIn("record", types)
        with self.assertRaises(PermissionDeniedError):
            self.svc.timeline(PRAC_1, "P2")

    def test_officer_case_list_scoped_to_involved_cases(self) -> None:
        self.svc.register_relation(REG, "P1", "INST-C", "2026-01-01T00:00:00Z")
        self.svc.submit_record(REG, "P1", "INST-C", "站点C", at("12:00"), at("13:00"))
        self.svc.submit_record(REG, "P1", "INST-C", "站点C", at("12:30"), at("13:30"))
        cases_a = self.svc.list_cases(OFF_A)
        self.assertEqual(len(cases_a), 1)
        self.assertEqual(cases_a[0]["case_id"], self.case_id)
        cases_c = self.svc.list_cases(OFF_C)
        self.assertEqual(len(cases_c), 1)
        self.assertNotEqual(cases_c[0]["case_id"], self.case_id)


class AuditAndSnapshotTest(ServiceTestBase):
    def test_audit_trail_records_mutations(self) -> None:
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        rec, cases = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("09:30"), at("10:30"))
        self.svc.decide_case(REG, cases[0].case_id, "确认违规")
        audit = self.svc.audit_log(REG)
        actions = [event["action"] for event in audit]
        self.assertIn("record.submit", actions)
        self.assertIn("case.create", actions)
        self.assertIn("case.decide", actions)
        self.assertEqual([event["seq"] for event in audit], list(range(1, len(audit) + 1)))
        with self.assertRaises(PermissionDeniedError):
            self.svc.audit_log(OFF_A)

    def test_snapshot_roundtrip_preserves_state(self) -> None:
        self.svc.submit_record(OFF_A, "P1", "INST-A", "门诊楼A", at("09:00"), at("10:00"))
        rec, cases = self.svc.submit_record(OFF_B, "P1", "INST-B", "住院楼B", at("09:30"), at("10:30"))
        self.svc.withdraw_record(OFF_B, rec.record_id, "撤报")
        self.svc.decide_case(REG, cases[0].case_id, "机构撤报")
        restored = Store.restore(self.svc.store.snapshot())
        svc2 = PracticeConflictService(restored)
        self.assertEqual(len(svc2.store.records), 2)
        self.assertEqual(svc2.get_record(REG, rec.record_id).status, "withdrawn")
        case = svc2.get_case(REG, cases[0].case_id)
        self.assertEqual(case["state"], "已决定")
        self.assertEqual(case["current_decision"]["resolution"], "机构撤报")
        self.assertEqual(len(svc2.store.audit), len(self.svc.store.audit))
        # 恢复后编号不重复
        rec2, _ = svc2.submit_record(REG, "P1", "INST-A", "门诊楼A", at("12:00"), at("13:00"))
        self.assertNotIn(rec2.record_id, self.svc.store.records)


if __name__ == "__main__":
    unittest.main()
