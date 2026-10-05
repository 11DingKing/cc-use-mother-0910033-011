"""SQLite 持久化层。

设计约束：所有表只追加或做状态翻转（active/superseded/withdrawn 等），
不做物理删除，保证历史记录与来源可追溯。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS practitioners (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registrations (
    id TEXT PRIMARY KEY,
    practitioner_id TEXT NOT NULL,
    institution_id TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS movement_buffers (
    from_institution_id TEXT NOT NULL,
    to_institution_id TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (from_institution_id, to_institution_id)
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    practitioner_id TEXT NOT NULL,
    institution_id TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancel_reason TEXT
);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    practitioner_id TEXT NOT NULL,
    institution_id TEXT NOT NULL,
    actual_start TEXT NOT NULL,
    actual_end TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    supersedes TEXT,
    source TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT
);
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    practitioner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS case_records (
    case_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (case_id, record_id)
);
CREATE TABLE IF NOT EXISTS case_events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    institution_id TEXT,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 仓储。"""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- 基础工具 -----------------------------------------------------------
    def _next_id(self, key: str, prefix: str) -> str:
        cur = self._conn.execute(
            "INSERT INTO counters(name, value) VALUES(?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1 RETURNING value",
            (key,),
        )
        return f"{prefix}-{cur.fetchone()[0]:06d}"

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # -- 机构 / 执业人员 -----------------------------------------------------
    def add_institution(self, name: str, location: str, created_at: str) -> dict:
        with self._lock:
            new_id = self._next_id("institutions", "INS")
            self._execute(
                "INSERT INTO institutions(id, name, location, created_at) VALUES(?,?,?,?)",
                (new_id, name, location, created_at),
            )
        return self.get_institution(new_id)

    def get_institution(self, institution_id: str) -> dict | None:
        return self._one("SELECT * FROM institutions WHERE id = ?", (institution_id,))

    def list_institutions(self) -> list[dict]:
        return self._all("SELECT * FROM institutions ORDER BY id")

    def add_practitioner(self, name: str, created_at: str) -> dict:
        with self._lock:
            new_id = self._next_id("practitioners", "PRA")
            self._execute(
                "INSERT INTO practitioners(id, name, created_at) VALUES(?,?,?)",
                (new_id, name, created_at),
            )
        return self.get_practitioner(new_id)

    def get_practitioner(self, practitioner_id: str) -> dict | None:
        return self._one("SELECT * FROM practitioners WHERE id = ?", (practitioner_id,))

    def list_practitioners(self) -> list[dict]:
        return self._all("SELECT * FROM practitioners ORDER BY id")

    # -- 执业关系 -------------------------------------------------------------
    def add_registration(self, fields: dict) -> dict:
        with self._lock:
            new_id = self._next_id("registrations", "REG")
            self._execute(
                "INSERT INTO registrations(id, practitioner_id, institution_id, effective_from,"
                " effective_to, status, reason, source, created_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    fields["practitioner_id"],
                    fields["institution_id"],
                    fields["effective_from"],
                    fields["effective_to"],
                    fields["status"],
                    fields["reason"],
                    fields["source"],
                    fields["created_by"],
                    fields["created_at"],
                ),
            )
        return self.get_registration(new_id)

    def get_registration(self, registration_id: str) -> dict | None:
        return self._one("SELECT * FROM registrations WHERE id = ?", (registration_id,))

    def registrations_for(
        self, practitioner_id: str, institution_id: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM registrations WHERE practitioner_id = ?"
        params: list[Any] = [practitioner_id]
        if institution_id is not None:
            sql += " AND institution_id = ?"
            params.append(institution_id)
        return self._all(sql + " ORDER BY effective_from, id", tuple(params))

    def revoke_registration(self, registration_id: str, revoked_at: str, reason: str) -> None:
        self._execute(
            "UPDATE registrations SET status = 'revoked', revoked_at = ?, revoke_reason = ?"
            " WHERE id = ?",
            (revoked_at, reason, registration_id),
        )

    # -- 移动缓冲 -------------------------------------------------------------
    def set_movement_buffer(
        self, from_id: str, to_id: str, minutes: int, updated_by: str, updated_at: str
    ) -> None:
        self._execute(
            "INSERT INTO movement_buffers(from_institution_id, to_institution_id, minutes,"
            " updated_by, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(from_institution_id, to_institution_id) "
            "DO UPDATE SET minutes = excluded.minutes, updated_by = excluded.updated_by,"
            " updated_at = excluded.updated_at",
            (from_id, to_id, minutes, updated_by, updated_at),
        )

    def buffer_minutes(self, from_id: str, to_id: str, default: int = 0) -> int:
        """查询两机构间移动缓冲；先查正向，再查反向，最后回落默认值。"""
        row = self._one(
            "SELECT minutes FROM movement_buffers WHERE from_institution_id = ?"
            " AND to_institution_id = ?",
            (from_id, to_id),
        )
        if row is None:
            row = self._one(
                "SELECT minutes FROM movement_buffers WHERE from_institution_id = ?"
                " AND to_institution_id = ?",
                (to_id, from_id),
            )
        return int(row["minutes"]) if row is not None else default

    # -- 服务计划 -------------------------------------------------------------
    def add_plan(self, fields: dict) -> dict:
        with self._lock:
            new_id = self._next_id("plans", "PLN")
            self._execute(
                "INSERT INTO plans(id, practitioner_id, institution_id, planned_start,"
                " planned_end, location, status, source, created_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    fields["practitioner_id"],
                    fields["institution_id"],
                    fields["planned_start"],
                    fields["planned_end"],
                    fields["location"],
                    fields["status"],
                    fields["source"],
                    fields["created_by"],
                    fields["created_at"],
                ),
            )
        return self.get_plan(new_id)

    def get_plan(self, plan_id: str) -> dict | None:
        return self._one("SELECT * FROM plans WHERE id = ?", (plan_id,))

    def plans_for(self, practitioner_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM plans WHERE practitioner_id = ? ORDER BY planned_start, id",
            (practitioner_id,),
        )

    def cancel_plan(self, plan_id: str, cancelled_at: str, reason: str) -> None:
        self._execute(
            "UPDATE plans SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?"
            " WHERE id = ?",
            (cancelled_at, reason, plan_id),
        )

    # -- 实际服务记录 -----------------------------------------------------------
    def add_record(self, fields: dict) -> dict:
        with self._lock:
            new_id = self._next_id("records", "REC")
            self._execute(
                "INSERT INTO records(id, root_id, practitioner_id, institution_id, actual_start,"
                " actual_end, location, note, status, version, supersedes, source,"
                " submitted_by, submitted_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    fields["root_id"] or new_id,
                    fields["practitioner_id"],
                    fields["institution_id"],
                    fields["actual_start"],
                    fields["actual_end"],
                    fields["location"],
                    fields["note"],
                    fields["status"],
                    fields["version"],
                    fields["supersedes"],
                    fields["source"],
                    fields["submitted_by"],
                    fields["submitted_at"],
                ),
            )
        return self.get_record(new_id)

    def get_record(self, record_id: str) -> dict | None:
        return self._one("SELECT * FROM records WHERE id = ?", (record_id,))

    def records_for_practitioner(self, practitioner_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM records WHERE practitioner_id = ? ORDER BY actual_start, id",
            (practitioner_id,),
        )

    def record_versions(self, root_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM records WHERE root_id = ? OR id = ? ORDER BY version, id",
            (root_id, root_id),
        )

    def mark_record(self, record_id: str, status: str, **extra: Any) -> None:
        columns = {"status": status, **extra}
        assignments = ", ".join(f"{key} = ?" for key in columns)
        self._execute(
            f"UPDATE records SET {assignments} WHERE id = ?",
            (*columns.values(), record_id),
        )

    # -- 待核案件 -------------------------------------------------------------
    def add_case(self, fields: dict) -> dict:
        with self._lock:
            new_id = self._next_id("cases", "CASE")
            self._execute(
                "INSERT INTO cases(id, practitioner_id, kind, status, outcome, evidence,"
                " created_at, decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    fields["practitioner_id"],
                    fields["kind"],
                    fields["status"],
                    fields["outcome"],
                    json.dumps(fields["evidence"], ensure_ascii=False, sort_keys=True),
                    fields["created_at"],
                    fields["decided_at"],
                ),
            )
        return self.get_case(new_id)

    def get_case(self, case_id: str) -> dict | None:
        row = self._one("SELECT * FROM cases WHERE id = ?", (case_id,))
        if row is not None:
            row["evidence"] = json.loads(row["evidence"])
        return row

    def list_cases(
        self, status: str | None = None, practitioner_id: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM cases WHERE 1 = 1"
        params: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if practitioner_id is not None:
            sql += " AND practitioner_id = ?"
            params.append(practitioner_id)
        rows = self._all(sql + " ORDER BY created_at, id", tuple(params))
        for row in rows:
            row["evidence"] = json.loads(row["evidence"])
        return rows

    def update_case(
        self,
        case_id: str,
        status: str,
        outcome: str | None,
        decided_at: str | None,
    ) -> None:
        self._execute(
            "UPDATE cases SET status = ?, outcome = ?, decided_at = ? WHERE id = ?",
            (status, outcome, decided_at, case_id),
        )

    def link_case_record(self, case_id: str, record_id: str, role: str) -> None:
        self._execute(
            "INSERT OR IGNORE INTO case_records(case_id, record_id, role) VALUES(?,?,?)",
            (case_id, record_id, role),
        )

    def case_record_links(self, case_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM case_records WHERE case_id = ? ORDER BY record_id", (case_id,)
        )

    def cases_involving_institution(self, institution_id: str) -> list[str]:
        rows = self._all(
            "SELECT DISTINCT cr.case_id FROM case_records cr"
            " JOIN records r ON r.id = cr.record_id WHERE r.institution_id = ?",
            (institution_id,),
        )
        return [row["case_id"] for row in rows]

    # -- 案件事件（只追加） ------------------------------------------------------
    def add_case_event(self, fields: dict) -> dict:
        with self._lock:
            new_id = self._next_id("case_events", "EVT")
            self._execute(
                "INSERT INTO case_events(id, case_id, kind, actor_id, actor_role,"
                " institution_id, reason, source, payload, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    fields["case_id"],
                    fields["kind"],
                    fields["actor_id"],
                    fields["actor_role"],
                    fields["institution_id"],
                    fields["reason"],
                    fields["source"],
                    json.dumps(fields["payload"], ensure_ascii=False, sort_keys=True),
                    fields["created_at"],
                ),
            )
        return self.get_case_event(new_id)

    def get_case_event(self, event_id: str) -> dict | None:
        row = self._one("SELECT * FROM case_events WHERE id = ?", (event_id,))
        if row is not None:
            row["payload"] = json.loads(row["payload"])
        return row

    def events_for_case(self, case_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM case_events WHERE case_id = ? ORDER BY created_at, id", (case_id,)
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows
