"""时空冲突检测：时间重叠、移动缓冲不足、执业关系时态缺口。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .models import ConflictEvidence, EvidenceSide, MovementBuffer, PracticeRelation


@dataclass(frozen=True)
class Commitment:
    """参与冲突检测的时间承诺（计划或实际记录）。"""

    kind: str  # plan | record
    ref_id: str
    practitioner_id: str
    institution_id: str
    site: str
    start: datetime
    end: datetime

    def side(self) -> EvidenceSide:
        return EvidenceSide(self.kind, self.ref_id, self.institution_id, self.site, self.start, self.end)


class BufferTable:
    """站点间移动缓冲查询；未登记的跨站点对使用默认缓冲。"""

    def __init__(self, buffers: Iterable[MovementBuffer], default_minutes: int) -> None:
        self._minutes: dict[frozenset[str], int] = {}
        for buf in buffers:
            if buf.status == "active":
                self._minutes[frozenset((buf.site_a, buf.site_b))] = buf.minutes
        self._default = default_minutes

    def required(self, site_a: str, site_b: str) -> tuple[int, str]:
        """返回 (所需缓冲分钟数, 来源)。"""
        if site_a == site_b:
            return 0, "same_site"
        key = frozenset((site_a, site_b))
        if key in self._minutes:
            return self._minutes[key], "registered"
        return self._default, "default"


def _order(a: Commitment, b: Commitment) -> tuple[Commitment, Commitment]:
    if (a.start, a.end, a.ref_id) <= (b.start, b.end, b.ref_id):
        return a, b
    return b, a


def pair_evidence(a: Commitment, b: Commitment, buffers: BufferTable) -> ConflictEvidence | None:
    """判断两条承诺是否构成时空冲突，不构成时返回 None。"""
    first, second = _order(a, b)
    scope = "same_institution" if a.institution_id == b.institution_id else "cross_institution"
    overlap_start = max(a.start, b.start)
    overlap_end = min(a.end, b.end)
    if overlap_start < overlap_end:
        return ConflictEvidence(
            case_type="overlap",
            practitioner_id=a.practitioner_id,
            side_a=first.side(),
            side_b=second.side(),
            scope=scope,
            overlap_start=overlap_start,
            overlap_end=overlap_end,
        )
    required, source = buffers.required(a.site, b.site)
    if required <= 0:
        return None
    gap_minutes = (second.start - first.end).total_seconds() / 60
    if gap_minutes < required:
        return ConflictEvidence(
            case_type="insufficient_travel",
            practitioner_id=a.practitioner_id,
            side_a=first.side(),
            side_b=second.side(),
            scope=scope,
            gap_minutes=gap_minutes,
            required_buffer_minutes=required,
            buffer_source=source,
        )
    return None


def detect_conflicts(
    new: Commitment,
    existing: Iterable[Commitment],
    buffers: BufferTable,
) -> list[ConflictEvidence]:
    """将新承诺与同一执业人员的既有有效承诺逐一比对。"""
    found: list[ConflictEvidence] = []
    for other in existing:
        if other.ref_id == new.ref_id:
            continue
        evidence = pair_evidence(new, other, buffers)
        if evidence is not None:
            found.append(evidence)
    return found


def uncovered_intervals(
    start: datetime,
    end: datetime,
    relations: Iterable[PracticeRelation],
) -> list[tuple[datetime, datetime]]:
    """计算 [start, end) 中未被任一有效执业关系覆盖的部分。"""
    spans: list[tuple[datetime, datetime]] = []
    for rel in relations:
        lo = max(start, rel.valid_from)
        hi = min(end, rel.valid_to) if rel.valid_to else end
        if lo < hi:
            spans.append((lo, hi))
    spans.sort()
    missing: list[tuple[datetime, datetime]] = []
    cursor = start
    for lo, hi in spans:
        if lo > cursor:
            missing.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < end:
        missing.append((cursor, end))
    return missing
