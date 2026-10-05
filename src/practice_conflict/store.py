"""内存仓储与审计日志；支持快照导出/恢复，历史永不物理删除。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import (
    AuditEvent,
    Case,
    MovementBuffer,
    PracticeRelation,
    ServicePlan,
    ServiceRecord,
)


class Store:
    """集中保存领域对象；所有变更通过状态字段与审计事件留痕。"""

    def __init__(self) -> None:
        self.relations: dict[str, PracticeRelation] = {}
        self.buffers: dict[str, MovementBuffer] = {}
        self.plans: dict[str, ServicePlan] = {}
        self.records: dict[str, ServiceRecord] = {}
        self.cases: dict[str, Case] = {}
        self.audit: list[AuditEvent] = []
        self._counters: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def log(self, actor: str, action: str, entity: str, entity_id: str, detail: dict[str, Any]) -> AuditEvent:
        event = AuditEvent(
            seq=len(self.audit) + 1,
            at=datetime.now(timezone.utc),
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail,
        )
        self.audit.append(event)
        return event

    def snapshot(self) -> dict[str, Any]:
        """导出完整状态（含审计日志），用于持久化。"""
        return {
            "counters": dict(self._counters),
            "relations": [item.to_dict() for item in self.relations.values()],
            "buffers": [item.to_dict() for item in self.buffers.values()],
            "plans": [item.to_dict() for item in self.plans.values()],
            "records": [item.to_dict() for item in self.records.values()],
            "cases": [item.to_dict() for item in self.cases.values()],
            "audit": [item.to_dict() for item in self.audit],
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> "Store":
        store = cls()
        store._counters = dict(data.get("counters", {}))
        for item in data.get("relations", []):
            rel = PracticeRelation.from_dict(item)
            store.relations[rel.relation_id] = rel
        for item in data.get("buffers", []):
            buf = MovementBuffer.from_dict(item)
            store.buffers[buf.buffer_id] = buf
        for item in data.get("plans", []):
            plan = ServicePlan.from_dict(item)
            store.plans[plan.plan_id] = plan
        for item in data.get("records", []):
            rec = ServiceRecord.from_dict(item)
            store.records[rec.record_id] = rec
        for item in data.get("cases", []):
            case = Case.from_dict(item)
            store.cases[case.case_id] = case
        store.audit = [AuditEvent.from_dict(item) for item in data.get("audit", [])]
        return store
