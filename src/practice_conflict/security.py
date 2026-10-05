"""访问控制与机构明细隔离。

监管人员、复核专家可见全部；机构合规员仅可见本机构明细，跨机构案件
证据中的他方机构标识与明细被屏蔽；执业人员仅可见本人数据。
"""
from __future__ import annotations

import copy
from typing import Any

from .models import Actor, Case, Role

MASKED_INSTITUTION = "（他机构）"


def can_read_all(actor: Actor) -> bool:
    return actor.role in (Role.REGULATOR, Role.REVIEWER)


def can_decide_case(actor: Actor) -> bool:
    return actor.role in (Role.REGULATOR, Role.REVIEWER)


def case_visible_to(actor: Actor, case: Case) -> bool:
    if can_read_all(actor):
        return True
    if actor.role is Role.PRACTITIONER:
        return actor.practitioner_id == case.practitioner_id
    if actor.role is Role.OFFICER:
        return actor.institution_id in case.institutions
    return False


def institution_visible_to(actor: Actor, institution_id: str) -> bool:
    if can_read_all(actor):
        return True
    if actor.role is Role.OFFICER:
        return actor.institution_id == institution_id
    if actor.role is Role.PRACTITIONER:
        return True  # 执业人员查看的是本人数据，不按机构过滤
    return False


def mask_case_dict(data: dict[str, Any], actor: Actor) -> dict[str, Any]:
    """按机构明细隔离要求裁剪案件视图。"""
    if can_read_all(actor):
        return data
    if actor.role is Role.PRACTITIONER and actor.practitioner_id == data.get("practitioner_id"):
        return data
    own = actor.institution_id
    masked = copy.deepcopy(data)
    evidence = masked.get("evidence") or {}
    for key in ("side_a", "side_b"):
        side = evidence.get(key)
        if side and side.get("institution_id") != own:
            evidence[key] = {
                "kind": side.get("kind"),
                "institution_id": MASKED_INSTITUTION,
                "masked": True,
            }
    masked["institutions"] = [inst if inst == own else MASKED_INSTITUTION for inst in masked.get("institutions", [])]
    masked["explanations"] = [
        entry
        if entry.get("institution_id") in (None, own)
        else {"institution_id": MASKED_INSTITUTION, "masked": True, "at": entry.get("at")}
        for entry in masked.get("explanations", [])
    ]
    return masked
