"""请求上下文与通用数据辅助。"""
from __future__ import annotations

import sqlite3
import uuid

from .database import dumps
from .errors import bad_request, forbidden, not_found
from .timeutil import Clock


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Context:
    def __init__(self, db: sqlite3.Connection, clock: Clock, principal: str | None,
                 party: sqlite3.Row | None, fund):
        self.db = db
        self.clock = clock
        self.principal = principal
        self.party = party
        self.fund = fund

    @property
    def now(self):
        return self.clock.now()

    @property
    def now_iso(self) -> str:
        return self.now.isoformat()

    @property
    def party_id(self) -> str:
        return self.party["id"]

    @property
    def role(self) -> str:
        return self.party["role"]

    def require_role(self, *roles: str):
        if self.role not in roles:
            raise forbidden(f"当前身份 {self.party_id}（{self.role}）无权办理此操作，需要: {','.join(roles)}")

    def event(self, kind: str, payload: dict):
        self.db.execute(
            "INSERT INTO events(at,actor,kind,payload_json) VALUES(?,?,?,?)",
            (self.now_iso, self.party_id, kind, dumps(payload)),
        )

    def notify(self, party_id: str, topic: str, payload: dict):
        self.db.execute(
            "INSERT INTO notifications(id,party_id,topic,payload_json,created_at) VALUES(?,?,?,?,?)",
            (new_id("ntf"), party_id, topic, dumps(payload), self.now_iso),
        )


def require_fields(payload: dict, fields: list[str]):
    if not isinstance(payload, dict):
        raise bad_request("invalid_body", "请求体必须为 JSON 对象")
    missing = [f for f in fields if f not in payload or payload[f] in (None, "")]
    if missing:
        raise bad_request("missing_fields", f"缺少必填字段: {', '.join(missing)}")


def get_unit(db, unit_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
    if not row:
        raise not_found(f"作业单元不存在: {unit_id}")
    return row


def get_contract(db, contract_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
    if not row:
        raise not_found(f"合同不存在: {contract_id}")
    return row


def get_declaration(db, declaration_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM declarations WHERE id=?", (declaration_id,)).fetchone()
    if not row:
        raise not_found(f"申报不存在: {declaration_id}")
    return row


def get_defect(db, defect_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM defects WHERE id=?", (defect_id,)).fetchone()
    if not row:
        raise not_found(f"缺陷不存在: {defect_id}")
    return row
