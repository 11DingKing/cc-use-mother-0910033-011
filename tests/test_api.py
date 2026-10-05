"""多机构执业冲突后端的端到端测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from practice_conflict.api import create_app  # noqa: E402

REGULATOR = {"X-Actor-Id": "reg-1", "X-Actor-Role": "regulator"}
REVIEWER = {"X-Actor-Id": "rev-1", "X-Actor-Role": "reviewer"}


def compliance(institution_id: str) -> dict:
    return {
        "X-Actor-Id": f"comp-{institution_id}",
        "X-Actor-Role": "compliance",
        "X-Institution-Id": institution_id,
    }


def practitioner(practitioner_id: str) -> dict:
    return {"X-Actor-Id": practitioner_id, "X-Actor-Role": "practitioner"}


@pytest.fixture()
def client():
    app = create_app(":memory:")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded(client: TestClient) -> dict:
    """两机构、一名执业人员、双向执业关系、30 分钟移动缓冲。"""
    ins_a = client.post(
        "/institutions", json={"name": "甲医院", "location": "城东"}, headers=REGULATOR
    ).json()["id"]
    ins_b = client.post(
        "/institutions", json={"name": "乙诊所", "location": "城西"}, headers=REGULATOR
    ).json()["id"]
    pra = client.post("/practitioners", json={"name": "王医生"}, headers=REGULATOR).json()["id"]
    for ins in (ins_a, ins_b):
        resp = client.post(
            "/registrations",
            json={
                "practitioner_id": pra,
                "institution_id": ins,
                "effective_from": "2026-01-01T00:00:00+08:00",
                "effective_to": "2026-12-31T23:59:59+08:00",
                "source": "卫健委备案2026-001",
            },
            headers=REGULATOR,
        )
        assert resp.status_code == 201, resp.text
    client.post(
        "/movement-buffers",
        json={"from_institution_id": ins_a, "to_institution_id": ins_b, "minutes": 30},
        headers=REGULATOR,
    )
    return {"ins_a": ins_a, "ins_b": ins_b, "pra": pra}


def submit(client: TestClient, ins: str, pra: str, start: str, end: str, **extra):
    payload = {
        "practitioner_id": pra,
        "institution_id": ins,
        "actual_start": start,
        "actual_end": end,
        "source": extra.pop("source", f"{ins}考勤系统"),
        **extra,
    }
    return client.post("/records", json=payload, headers=compliance(ins))


# ---------------------------------------------------------------------------
# 基础登记与时态校验
# ---------------------------------------------------------------------------
def test_health(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}


def test_registration_requires_valid_interval(client: TestClient, seeded: dict):
    resp = client.post(
        "/registrations",
        json={
            "practitioner_id": seeded["pra"],
            "institution_id": seeded["ins_a"],
            "effective_from": "2026-06-01T00:00:00+08:00",
            "effective_to": "2026-01-01T00:00:00+08:00",
            "source": "测试",
        },
        headers=REGULATOR,
    )
    assert resp.status_code == 400


def test_registration_revoke_keeps_history(client: TestClient, seeded: dict):
    regs = client.get(f"/practitioners", headers=REGULATOR)  # 列表可用
    assert regs.status_code == 200
    resp = client.post(
        "/registrations",
        json={
            "practitioner_id": seeded["pra"],
            "institution_id": seeded["ins_a"],
            "effective_from": "2027-01-01T00:00:00+08:00",
            "source": "续约备案",
        },
        headers=REGULATOR,
    )
    reg_id = resp.json()["id"]
    revoked = client.post(
        f"/registrations/{reg_id}/revoke", json={"reason": "录入错误"}, headers=REGULATOR
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    # 再次撤销被拒绝，历史仍保留
    again = client.post(
        f"/registrations/{reg_id}/revoke", json={"reason": "重复"}, headers=REGULATOR
    )
    assert again.status_code == 409


def test_plan_must_be_within_registration(client: TestClient, seeded: dict):
    resp = client.post(
        "/plans",
        json={
            "practitioner_id": seeded["pra"],
            "institution_id": seeded["ins_a"],
            "planned_start": "2027-03-01T09:00:00+08:00",
            "planned_end": "2027-03-01T10:00:00+08:00",
            "source": "排班系统",
        },
        headers=compliance(seeded["ins_a"]),
    )
    assert resp.status_code == 400
    ok = client.post(
        "/plans",
        json={
            "practitioner_id": seeded["pra"],
            "institution_id": seeded["ins_a"],
            "planned_start": "2026-03-01T09:00:00+08:00",
            "planned_end": "2026-03-01T10:00:00+08:00",
            "source": "排班系统",
        },
        headers=compliance(seeded["ins_a"]),
    )
    assert ok.status_code == 201
    plan_id = ok.json()["id"]
    cancelled = client.post(
        f"/plans/{plan_id}/cancel", json={"reason": "调班"}, headers=compliance(seeded["ins_a"])
    )
    assert cancelled.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 时空冲突检测
# ---------------------------------------------------------------------------
def test_clean_record_creates_no_case(client: TestClient, seeded: dict):
    resp = submit(client, seeded["ins_a"], seeded["pra"],
                  "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00")
    assert resp.status_code == 201
    assert resp.json()["conflicts"] == []
    assert resp.json()["case"] is None


def test_overlap_creates_pending_case(client: TestClient, seeded: dict):
    submit(client, seeded["ins_a"], seeded["pra"],
           "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00")
    resp = submit(client, seeded["ins_b"], seeded["pra"],
                  "2026-03-01T09:30:00+08:00", "2026-03-01T10:30:00+08:00")
    body = resp.json()
    assert body["conflicts"][0]["kind"] == "overlap"
    case = body["case"]
    assert case["status"] == "待核验"
    assert case["kind"] == "overlap"
    assert {item["record"]["id"] for item in case["records"]} == {
        body["record"]["id"],
        case["evidence"]["conflicts"][0]["other_record_id"],
    }


def test_buffer_violation_creates_case(client: TestClient, seeded: dict):
    submit(client, seeded["ins_a"], seeded["pra"],
           "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00")
    resp = submit(client, seeded["ins_b"], seeded["pra"],
                  "2026-03-01T10:20:00+08:00", "2026-03-01T11:00:00+08:00")
    conflict = resp.json()["conflicts"][0]
    assert conflict["kind"] == "buffer_violation"
    assert conflict["required_buffer_seconds"] == 30 * 60
    assert conflict["actual_gap_seconds"] == 20 * 60


def test_sufficient_buffer_creates_no_case(client: TestClient, seeded: dict):
    submit(client, seeded["ins_a"], seeded["pra"],
           "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00")
    resp = submit(client, seeded["ins_b"], seeded["pra"],
                  "2026-03-01T10:45:00+08:00", "2026-03-01T11:00:00+08:00")
    assert resp.json()["case"] is None


def test_same_institution_overlap_is_not_cross_site(client: TestClient, seeded: dict):
    submit(client, seeded["ins_a"], seeded["pra"],
           "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00")
    resp = submit(client, seeded["ins_a"], seeded["pra"],
                  "2026-03-01T09:30:00+08:00", "2026-03-01T10:30:00+08:00")
    assert resp.json()["case"] is None


def test_outside_registration_creates_case(client: TestClient, seeded: dict):
    resp = submit(client, seeded["ins_a"], seeded["pra"],
                  "2027-01-10T09:00:00+08:00", "2027-01-10T10:00:00+08:00")
    body = resp.json()
    assert body["conflicts"][0]["kind"] == "outside_registration"
    assert body["case"]["kind"] == "outside_registration"


def test_record_window_must_be_valid(client: TestClient, seeded: dict):
    resp = submit(client, seeded["ins_a"], seeded["pra"],
                  "2026-03-01T10:00:00+08:00", "2026-03-01T09:00:00+08:00")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 案件处置：四类事件与状态机
# ---------------------------------------------------------------------------
def make_overlap_case(client: TestClient, seeded: dict) -> dict:
    first = submit(client, seeded["ins_a"], seeded["pra"],
                   "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00").json()
    second = submit(client, seeded["ins_b"], seeded["pra"],
                    "2026-03-01T09:30:00+08:00", "2026-03-01T10:30:00+08:00").json()
    return {"first": first["record"], "second": second["record"], "case": second["case"]}


def test_identity_correction_versions_records(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    other = client.post("/practitioners", json={"name": "李医生"}, headers=REGULATOR).json()["id"]
    resp = client.post(
        f"/cases/{ctx['case']['id']}/events",
        json={
            "kind": "identity_correction",
            "reason": "考勤机误绑工号",
            "source": "乙诊所人事说明2026-03",
            "payload": {
                "record_id": ctx["second"]["id"],
                "corrected_practitioner_id": other,
            },
        },
        headers=REVIEWER,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["case"]["status"] == "已决定"
    assert body["case"]["outcome"] == "identity_correction"
    new_record = body["side_effects"]["new_record"]
    assert new_record["practitioner_id"] == other
    assert new_record["version"] == 2
    assert new_record["supersedes"] == ctx["second"]["id"]
    # 旧版本保留为 superseded，历史链完整
    versions = client.get(
        f"/records/{ctx['second']['id']}/versions", headers=REGULATOR
    ).json()
    assert [item["status"] for item in versions] == ["superseded", "active"]
    # 原执业人员时间线上该记录不再是有效冲突来源
    timeline = client.get(
        f"/practitioners/{seeded['pra']}/timeline", headers=REGULATOR
    ).json()
    active = [
        item for item in timeline["items"]
        if item["type"] == "record" and item["data"]["status"] == "active"
    ]
    assert len(active) == 1


def test_identity_correction_rechecks_conflicts(client: TestClient, seeded: dict):
    """纠正身份后，新执业人员名下的记录会重新检测冲突。"""
    ctx = make_overlap_case(client, seeded)
    other = client.post("/practitioners", json={"name": "李医生"}, headers=REGULATOR).json()["id"]
    # 李医生在同一时段本机构已有记录 -> 纠正后仍与他机构记录重叠
    submit(client, seeded["ins_a"], other,
           "2026-03-01T09:15:00+08:00", "2026-03-01T10:00:00+08:00")
    resp = client.post(
        f"/cases/{ctx['case']['id']}/events",
        json={
            "kind": "identity_correction",
            "reason": "身份归并",
            "source": "监管函件",
            "payload": {"record_id": ctx["second"]["id"], "corrected_practitioner_id": other},
        },
        headers=REVIEWER,
    )
    follow_up = resp.json()["side_effects"]["follow_up_case"]
    assert follow_up is not None
    assert follow_up["practitioner_id"] == other
    assert follow_up["status"] == "待核验"


def test_institution_withdrawal_keeps_record(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    resp = client.post(
        f"/cases/{ctx['case']['id']}/events",
        json={
            "kind": "institution_withdrawal",
            "reason": "重复报送，撤回本机构记录",
            "source": "乙诊所合规函2026-007",
            "payload": {"record_id": ctx["second"]["id"], "withdraw_reason": "重复报送"},
        },
        headers=compliance(seeded["ins_b"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["case"]["status"] == "已决定"
    # 记录仍在库，状态为 withdrawn，不物理删除
    record = client.get(f"/records/{ctx['second']['id']}", headers=REGULATOR).json()
    assert record["status"] == "withdrawn"
    assert record["withdraw_reason"] == "重复报送"
    # 撤报后的记录不再参与后续冲突检测
    third = submit(client, seeded["ins_a"], seeded["pra"],
                   "2026-03-01T09:45:00+08:00", "2026-03-01T10:15:00+08:00")
    assert third.json()["case"] is None


def test_overlap_justification_decides_case(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    resp = client.post(
        f"/cases/{ctx['case']['id']}/events",
        json={
            "kind": "overlap_justification",
            "reason": "远程会诊，人在甲医院线上支持乙诊所",
            "source": "王医生书面说明",
            "payload": {"explanation": "远程会诊合理重叠"},
        },
        headers=practitioner(seeded["pra"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["case"]["outcome"] == "overlap_justification"


def test_reopen_and_archive_flow(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    case_id = ctx["case"]["id"]
    client.post(
        f"/cases/{case_id}/events",
        json={
            "kind": "overlap_justification",
            "reason": "初次说明",
            "source": "说明材料v1",
            "payload": {"explanation": "远程指导"},
        },
        headers=REVIEWER,
    )
    # 决定复开
    reopened = client.post(
        f"/cases/{case_id}/events",
        json={"kind": "reopen", "reason": "补充新证据", "source": "监管复核通知"},
        headers=REGULATOR,
    )
    assert reopened.json()["case"]["status"] == "处置中"
    # 复开后重新决定并归档
    client.post(
        f"/cases/{case_id}/events",
        json={
            "kind": "overlap_justification",
            "reason": "补充说明成立",
            "source": "说明材料v2",
            "payload": {"explanation": "远程指导，附排班佐证"},
        },
        headers=REVIEWER,
    )
    archived = client.post(
        f"/cases/{case_id}/events",
        json={"kind": "archive", "reason": "办结归档", "source": "监管系统"},
        headers=REGULATOR,
    )
    assert archived.json()["case"]["status"] == "已归档"
    # 全部事件留痕，来源可查
    events = client.get(f"/cases/{case_id}", headers=REGULATOR).json()["events"]
    assert [event["kind"] for event in events] == [
        "overlap_justification",
        "reopen",
        "overlap_justification",
        "archive",
    ]
    assert {event["source"] for event in events} == {
        "说明材料v1", "监管复核通知", "说明材料v2", "监管系统"
    }


def test_invalid_transitions_rejected(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    case_id = ctx["case"]["id"]
    # 待核验案件不能复开、不能归档
    for kind in ("reopen", "archive"):
        resp = client.post(
            f"/cases/{case_id}/events",
            json={"kind": kind, "reason": "测试", "source": "测试"},
            headers=REGULATOR,
        )
        assert resp.status_code == 409
    # 已决定后不能重复决定
    client.post(
        f"/cases/{case_id}/events",
        json={
            "kind": "overlap_justification",
            "reason": "说明",
            "source": "材料",
            "payload": {"explanation": "合理"},
        },
        headers=REVIEWER,
    )
    again = client.post(
        f"/cases/{case_id}/events",
        json={
            "kind": "overlap_justification",
            "reason": "再次",
            "source": "材料",
            "payload": {"explanation": "合理"},
        },
        headers=REVIEWER,
    )
    assert again.status_code == 409


def test_event_requires_reason_and_source(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    resp = client.post(
        f"/cases/{ctx['case']['id']}/events",
        json={"kind": "reopen", "reason": "", "source": ""},
        headers=REGULATOR,
    )
    assert resp.status_code == 422  # pydantic 校验非空


# ---------------------------------------------------------------------------
# 机构明细隔离
# ---------------------------------------------------------------------------
def test_institution_timeline_masking(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    timeline = client.get(
        f"/practitioners/{seeded['pra']}/timeline", headers=compliance(seeded["ins_b"])
    ).json()
    records = {item["data"]["id"]: item["data"] for item in timeline["items"]
               if item["type"] == "record"}
    own = records[ctx["second"]["id"]]
    other = records[ctx["first"]["id"]]
    assert own["institution_id"] == seeded["ins_b"]
    assert own["location"] is not None
    assert other["masked"] is True
    assert "institution_id" not in other
    assert "location" not in other
    # 监管视角无脱敏
    full = client.get(
        f"/practitioners/{seeded['pra']}/timeline", headers=REGULATOR
    ).json()
    full_records = [item["data"] for item in full["items"] if item["type"] == "record"]
    assert all("institution_id" in item for item in full_records)


def test_institution_case_list_isolation(client: TestClient, seeded: dict):
    ctx = make_overlap_case(client, seeded)
    # 第三机构与案件无关
    ins_c = client.post("/institutions", json={"name": "丙体检"}, headers=REGULATOR).json()["id"]
    cases_b = client.get("/cases", headers=compliance(seeded["ins_b"])).json()
    assert [case["id"] for case in cases_b] == [ctx["case"]["id"]]
    cases_c = client.get("/cases", headers=compliance(ins_c)).json()
    assert cases_c == []
    denied = client.get(f"/cases/{ctx['case']['id']}", headers=compliance(ins_c))
    assert denied.status_code == 403
    # 涉及机构的合规员能看到案件，但他机构证据脱敏、事件负载不可见
    detail = client.get(f"/cases/{ctx['case']['id']}", headers=compliance(seeded["ins_b"])).json()
    masked = [item["record"] for item in detail["records"] if item["record"].get("masked")]
    assert len(masked) == 1
    client.post(
        f"/cases/{ctx['case']['id']}/events",
        json={
            "kind": "overlap_justification",
            "reason": "说明",
            "source": "材料",
            "payload": {"explanation": "内部排班细节"},
        },
        headers=REVIEWER,
    )
    detail = client.get(f"/cases/{ctx['case']['id']}", headers=compliance(seeded["ins_b"])).json()
    assert "payload" not in detail["events"][0]
    assert detail["events"][0]["source"] == "材料"


def test_compliance_cannot_touch_other_institution(client: TestClient, seeded: dict):
    resp = submit(client, seeded["ins_a"], seeded["pra"],
                  "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00")
    record_id = resp.json()["record"]["id"]
    # 乙机构合规员撤报甲机构记录 -> 403
    ctx_case = submit(client, seeded["ins_b"], seeded["pra"],
                      "2026-03-01T09:30:00+08:00", "2026-03-01T10:30:00+08:00").json()["case"]
    denied = client.post(
        f"/cases/{ctx_case['id']}/events",
        json={
            "kind": "institution_withdrawal",
            "reason": "越权测试",
            "source": "测试",
            "payload": {"record_id": record_id, "withdraw_reason": "越权"},
        },
        headers=compliance(seeded["ins_b"]),
    )
    assert denied.status_code == 403
    # 合规员不能冒充他机构提交记录
    forged = client.post(
        "/records",
        json={
            "practitioner_id": seeded["pra"],
            "institution_id": seeded["ins_a"],
            "actual_start": "2026-03-02T09:00:00+08:00",
            "actual_end": "2026-03-02T10:00:00+08:00",
            "source": "伪造",
        },
        headers=compliance(seeded["ins_b"]),
    )
    assert forged.status_code == 403


def test_role_permission_matrix(client: TestClient, seeded: dict):
    # 非监管角色不能建机构/设缓冲
    resp = client.post("/institutions", json={"name": "X"}, headers=compliance(seeded["ins_a"]))
    assert resp.status_code == 403
    resp = client.post(
        "/movement-buffers",
        json={"from_institution_id": seeded["ins_a"],
              "to_institution_id": seeded["ins_b"], "minutes": 10},
        headers=REVIEWER,
    )
    assert resp.status_code == 403
    # 未知角色被拒绝
    resp = client.get("/cases", headers={"X-Actor-Id": "x", "X-Actor-Role": "admin"})
    assert resp.status_code == 400


def test_practitioner_self_timeline(client: TestClient, seeded: dict):
    make_overlap_case(client, seeded)
    timeline = client.get(
        f"/practitioners/{seeded['pra']}/timeline", headers=practitioner(seeded["pra"])
    ).json()
    types = {item["type"] for item in timeline["items"]}
    assert {"registration", "record", "case"} <= types
    # 本人可见全部明细
    records = [item["data"] for item in timeline["items"] if item["type"] == "record"]
    assert all("institution_id" in item for item in records)
    # 查看他人时间线被拒
    other = client.post("/practitioners", json={"name": "赵医生"}, headers=REGULATOR).json()["id"]
    denied = client.get(f"/practitioners/{other}/timeline", headers=practitioner(seeded["pra"]))
    assert denied.status_code == 403


def test_timeline_range_filter(client: TestClient, seeded: dict):
    submit(client, seeded["ins_a"], seeded["pra"],
           "2026-03-01T09:00:00+08:00", "2026-03-01T10:00:00+08:00")
    submit(client, seeded["ins_a"], seeded["pra"],
           "2026-05-01T09:00:00+08:00", "2026-05-01T10:00:00+08:00")
    timeline = client.get(
        f"/practitioners/{seeded['pra']}/timeline",
        params={"start": "2026-04-01T00:00:00+08:00", "end": "2026-06-01T00:00:00+08:00"},
        headers=REGULATOR,
    ).json()
    record_items = [item for item in timeline["items"] if item["type"] == "record"]
    assert len(record_items) == 1
    assert record_items[0]["data"]["actual_start"].startswith("2026-05-01")
