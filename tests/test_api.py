"""HTTP API 层测试：路由、状态码映射、认证与机构隔离。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from practice_conflict.api import Api, make_server
from practice_conflict.models import Actor, Role
from practice_conflict.security import MASKED_INSTITUTION
from practice_conflict.service import PracticeConflictService

TOKENS = {
    "tok-reg": Actor("reg-1", Role.REGULATOR),
    "tok-rev": Actor("rev-1", Role.REVIEWER),
    "tok-a": Actor("off-a", Role.OFFICER, institution_id="INST-A"),
    "tok-b": Actor("off-b", Role.OFFICER, institution_id="INST-B"),
    "tok-p1": Actor("p1-user", Role.PRACTITIONER, practitioner_id="P1"),
}


def at(hhmm: str) -> str:
    return f"2026-10-05T{hhmm}:00Z"


class ApiDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PracticeConflictService(default_buffer_minutes=30)
        self.api = Api(self.service, TOKENS)
        self.api.dispatch(
            "POST", "/relations", {},
            {"practitioner_id": "P1", "institution_id": "INST-A", "valid_from": "2026-01-01T00:00:00Z"}, "tok-reg",
        )
        self.api.dispatch(
            "POST", "/relations", {},
            {"practitioner_id": "P1", "institution_id": "INST-B", "valid_from": "2026-01-01T00:00:00Z"}, "tok-reg",
        )
        self.api.dispatch("POST", "/buffers", {}, {"site_a": "门诊楼A", "site_b": "住院楼B", "minutes": 45}, "tok-reg")

    def _conflict_case(self) -> str:
        self.api.dispatch(
            "POST", "/records", {},
            {"practitioner_id": "P1", "institution_id": "INST-A", "site": "门诊楼A", "start": at("09:00"), "end": at("10:00")},
            "tok-a",
        )
        status, payload = self.api.dispatch(
            "POST", "/records", {},
            {"practitioner_id": "P1", "institution_id": "INST-B", "site": "住院楼B", "start": at("09:30"), "end": at("10:30")},
            "tok-b",
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(payload["cases"]), 1)
        return payload["cases"][0]["case_id"]

    def test_unauthenticated_rejected(self) -> None:
        status, payload = self.api.dispatch("GET", "/cases", {}, {}, "bad-token")
        self.assertEqual(status, 401)
        status, _ = self.api.dispatch("GET", "/cases", {}, {}, None)
        self.assertEqual(status, 401)

    def test_submit_record_returns_created_cases(self) -> None:
        case_id = self._conflict_case()
        status, payload = self.api.dispatch("GET", f"/cases/{case_id}", {}, {}, "tok-reg")
        self.assertEqual(status, 200)
        self.assertEqual(payload["case"]["case_type"], "overlap")
        self.assertEqual(payload["case"]["state"], "待核验")

    def test_validation_error_maps_to_400(self) -> None:
        status, payload = self.api.dispatch("POST", "/records", {}, {"practitioner_id": "P1"}, "tok-a")
        self.assertEqual(status, 400)
        self.assertIn("缺少参数", payload["error"])
        status, _ = self.api.dispatch(
            "POST", "/records", {},
            {"practitioner_id": "P1", "institution_id": "INST-A", "site": "门诊楼A", "start": at("10:00"), "end": at("09:00")},
            "tok-a",
        )
        self.assertEqual(status, 400)

    def test_permission_denied_maps_to_403(self) -> None:
        status, _ = self.api.dispatch(
            "POST", "/records", {},
            {"practitioner_id": "P1", "institution_id": "INST-B", "site": "住院楼B", "start": at("09:00"), "end": at("10:00")},
            "tok-a",
        )
        self.assertEqual(status, 403)
        status, _ = self.api.dispatch("GET", "/audit", {}, {}, "tok-a")
        self.assertEqual(status, 403)
        status, _ = self.api.dispatch("POST", "/buffers", {}, {"site_a": "X", "site_b": "Y", "minutes": 10}, "tok-a")
        self.assertEqual(status, 403)

    def test_not_found_maps_to_404(self) -> None:
        status, _ = self.api.dispatch("GET", "/records/REC-9999", {}, {}, "tok-reg")
        self.assertEqual(status, 404)
        status, _ = self.api.dispatch("GET", "/nope", {}, {}, "tok-reg")
        self.assertEqual(status, 404)

    def test_state_error_maps_to_409(self) -> None:
        case_id = self._conflict_case()
        status, _ = self.api.dispatch("POST", f"/cases/{case_id}/decide", {}, {"resolution": "确认违规"}, "tok-rev")
        self.assertEqual(status, 200)
        status, _ = self.api.dispatch("POST", f"/cases/{case_id}/decide", {}, {"resolution": "确认违规"}, "tok-rev")
        self.assertEqual(status, 409)

    def test_officer_cannot_read_other_institution_record(self) -> None:
        self.api.dispatch(
            "POST", "/records", {},
            {"practitioner_id": "P1", "institution_id": "INST-A", "site": "门诊楼A", "start": at("12:00"), "end": at("13:00")},
            "tok-a",
        )
        status, payload = self.api.dispatch(
            "POST", "/records", {},
            {"practitioner_id": "P1", "institution_id": "INST-B", "site": "住院楼B", "start": at("14:00"), "end": at("15:00")},
            "tok-b",
        )
        record_id = payload["record"]["record_id"]
        status, _ = self.api.dispatch("GET", f"/records/{record_id}", {}, {}, "tok-a")
        self.assertEqual(status, 403)
        status, payload = self.api.dispatch("GET", "/records", {"practitioner_id": "P1"}, {}, "tok-a")
        self.assertEqual(status, 200)
        self.assertEqual({item["institution_id"] for item in payload["records"]}, {"INST-A"})

    def test_timeline_masks_counterpart_details(self) -> None:
        self._conflict_case()
        status, payload = self.api.dispatch("GET", "/persons/P1/timeline", {}, {}, "tok-a")
        self.assertEqual(status, 200)
        case_items = [item for item in payload["items"] if item["type"] == "case"]
        self.assertEqual(len(case_items), 1)
        evidence = case_items[0]["case"]["evidence"]
        sides = [evidence["side_a"], evidence["side_b"]]
        own = next(side for side in sides if side.get("institution_id") == "INST-A")
        other = next(side for side in sides if side.get("institution_id") != "INST-A")
        self.assertIn("ref_id", own)
        self.assertEqual(other["institution_id"], MASKED_INSTITUTION)
        self.assertTrue(other["masked"])
        self.assertNotIn("ref_id", other)
        # 监管视角为完整证据
        status, payload = self.api.dispatch("GET", "/persons/P1/timeline", {}, {}, "tok-reg")
        case_item = next(item for item in payload["items"] if item["type"] == "case")
        institutions = {case_item["case"]["evidence"]["side_a"]["institution_id"], case_item["case"]["evidence"]["side_b"]["institution_id"]}
        self.assertEqual(institutions, {"INST-A", "INST-B"})

    def test_practitioner_only_sees_own_timeline(self) -> None:
        status, _ = self.api.dispatch("GET", "/persons/P2/timeline", {}, {}, "tok-p1")
        self.assertEqual(status, 403)
        status, payload = self.api.dispatch("GET", "/persons/P1/timeline", {}, {}, "tok-p1")
        self.assertEqual(status, 200)

    def test_full_case_workflow_over_api(self) -> None:
        case_id = self._conflict_case()
        status, payload = self.api.dispatch("POST", f"/cases/{case_id}/explanations", {}, {"text": "远程会诊，合理重叠"}, "tok-a")
        self.assertEqual(status, 200)
        self.assertEqual(payload["case"]["state"], "处置中")
        status, payload = self.api.dispatch("POST", f"/cases/{case_id}/decide", {}, {"resolution": "合理重叠", "note": "采纳"}, "tok-rev")
        self.assertEqual(payload["case"]["state"], "已决定")
        status, payload = self.api.dispatch("POST", f"/cases/{case_id}/reopen", {}, {"reason": "新证据"}, "tok-reg")
        self.assertEqual(payload["case"]["state"], "待核验")
        self.assertEqual(len(payload["case"]["decisions"]), 1)  # 复开保留来源
        status, payload = self.api.dispatch("POST", f"/cases/{case_id}/decide", {}, {"resolution": "确认违规"}, "tok-reg")
        status, payload = self.api.dispatch("POST", f"/cases/{case_id}/archive", {}, {}, "tok-reg")
        self.assertEqual(payload["case"]["state"], "已归档")
        status, payload = self.api.dispatch("GET", "/cases", {"state": "已归档"}, {}, "tok-reg")
        self.assertEqual([item["case_id"] for item in payload["cases"]], [case_id])

    def test_withdraw_and_correct_identity_over_api(self) -> None:
        status, payload = self.api.dispatch(
            "POST", "/records", {},
            {"practitioner_id": "P1", "institution_id": "INST-A", "site": "门诊楼A", "start": at("12:00"), "end": at("13:00")},
            "tok-a",
        )
        record_id = payload["record"]["record_id"]
        status, payload = self.api.dispatch("POST", f"/records/{record_id}/withdraw", {}, {"reason": "重复报送"}, "tok-a")
        self.assertEqual(status, 200)
        self.assertEqual(payload["record"]["status"], "withdrawn")
        status, payload = self.api.dispatch("GET", "/records", {"include_inactive": "true"}, {}, "tok-reg")
        self.assertEqual(payload["records"][-1]["status"], "withdrawn")  # 历史保留
        status, payload = self.api.dispatch(
            "POST", f"/records/{record_id}/correct-identity", {}, {"new_practitioner_id": "P2"}, "tok-a"
        )
        self.assertEqual(status, 409)  # 已撤报记录不可再纠正


class ApiHttpTest(unittest.TestCase):
    """通过真实 HTTP 往返验证服务器装配。"""

    def test_http_roundtrip(self) -> None:
        service = PracticeConflictService()
        server = make_server(service, TOKENS, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/cases", headers={"X-Actor-Token": "tok-reg"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(payload["cases"], [])
            conn.request("GET", "/cases")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 401)
            resp.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
