"""SQLite 结构与连接。

设计原则：
- 关键业务表仅追加或受控流转；原始申报/抽查/签认记录不提供删除与改写接口。
- 金额以整数“分”存储，面积以平方米数值存储。
- JSON 文本列存放仅用于回放的结构化字段，资格判断以关系表为准。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 演练参与方（合成）
CREATE TABLE IF NOT EXISTS parties(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('builder','maintainer','surveyor','acceptor','proposer','reviewer'))
);
CREATE TABLE IF NOT EXISTS principals(
  principal TEXT PRIMARY KEY,
  party_id TEXT NOT NULL REFERENCES parties(id)
);

-- 合同与作业范围
CREATE TABLE IF NOT EXISTS contracts(
  id TEXT PRIMARY KEY,
  builder_id TEXT NOT NULL REFERENCES parties(id),
  maintainer_id TEXT NOT NULL REFERENCES parties(id),
  retention_rate TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facilities(
  id TEXT PRIMARY KEY,
  status_fact TEXT NOT NULL DEFAULT 'inspection_pending',
  accepted_at TEXT
);
CREATE TABLE IF NOT EXISTS units(
  id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL REFERENCES contracts(id),
  parcel TEXT NOT NULL,
  area_m2 REAL NOT NULL CHECK(area_m2>0),
  amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),
  current_m2 REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unit_dependencies(
  unit_id TEXT NOT NULL REFERENCES units(id),
  facility_id TEXT NOT NULL REFERENCES facilities(id),
  PRIMARY KEY(unit_id, facility_id)
);
CREATE TABLE IF NOT EXISTS plantings(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES units(id),
  planted_at TEXT NOT NULL
);

-- 原始合成资料留痕（catalog 记录，不参与资格判断，只保留来源）
CREATE TABLE IF NOT EXISTS catalog_records(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  refs_json TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

-- 施工/管护分批申报
CREATE TABLE IF NOT EXISTS declarations(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES units(id),
  contract_id TEXT NOT NULL REFERENCES contracts(id),
  stage TEXT NOT NULL CHECK(stage IN ('planting','care')),
  declared_m2 REAL NOT NULL CHECK(declared_m2>0),
  submitted_by TEXT NOT NULL REFERENCES parties(id),
  submitted_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'submitted'
     CHECK(status IN ('submitted','accepted','replant_required','returned','superseded'))
);
-- 同一阶段不能被两个合同/两笔有效申报重复认领（被退回或替代后允许重新申报）
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_declaration
  ON declarations(unit_id, stage)
  WHERE status IN ('submitted','accepted','replant_required');

-- 测量员现场抽查记录
CREATE TABLE IF NOT EXISTS surveys(
  id TEXT PRIMARY KEY,
  declaration_id TEXT NOT NULL REFERENCES declarations(id),
  unit_id TEXT NOT NULL REFERENCES units(id),
  surveyor_id TEXT NOT NULL REFERENCES parties(id),
  surveyed_at TEXT NOT NULL,
  group_no INTEGER NOT NULL,
  sample_size INTEGER NOT NULL CHECK(sample_size>0),
  alive_count INTEGER NOT NULL CHECK(alive_count>=0),
  dead_count INTEGER NOT NULL CHECK(dead_count>=0),
  measured_m2 REAL NOT NULL CHECK(measured_m2>=0),
  spot_check_ratio REAL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS survey_dead_plants(
  survey_id TEXT NOT NULL REFERENCES surveys(id),
  plant_no INTEGER NOT NULL,
  PRIMARY KEY(survey_id, plant_no)
);

-- 验收人签认（独立第三方，施工方不得自验）
CREATE TABLE IF NOT EXISTS acceptances(
  id TEXT PRIMARY KEY,
  declaration_id TEXT NOT NULL REFERENCES declarations(id),
  unit_id TEXT NOT NULL REFERENCES units(id),
  acceptor_id TEXT NOT NULL REFERENCES parties(id),
  decision TEXT NOT NULL CHECK(decision IN ('accept','replant_required','return')),
  accepted_m2 REAL NOT NULL DEFAULT 0,
  replant_deadline TEXT,
  basis_json TEXT NOT NULL,
  signed_at TEXT NOT NULL
);

-- 缺陷（同一植株同阶段只保留一个未闭缺陷，重复报告合并）
CREATE TABLE IF NOT EXISTS defects(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES units(id),
  stage TEXT NOT NULL CHECK(stage IN ('planting','care')),
  plant_no INTEGER,
  description TEXT,
  first_reported_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','cause_set','replanting','closed','rejected')),
  cause TEXT CHECK(cause IS NULL OR cause IN ('construction','maintenance')),
  cause_set_by TEXT REFERENCES parties(id),
  cause_set_at TEXT,
  closed_at TEXT,
  resolution_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_defect_plant
  ON defects(unit_id, stage, plant_no)
  WHERE status IN ('open','cause_set','replanting') AND plant_no IS NOT NULL;
CREATE TABLE IF NOT EXISTS defect_reports(
  id TEXT PRIMARY KEY,
  defect_id TEXT NOT NULL REFERENCES defects(id),
  batch_id TEXT REFERENCES patrol_batches(id),
  reporter_id TEXT NOT NULL REFERENCES parties(id),
  reported_at TEXT NOT NULL,
  plant_no INTEGER,
  note TEXT
);

-- 管护巡护批次与补植作业
CREATE TABLE IF NOT EXISTS patrol_batches(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES units(id),
  maintainer_id TEXT NOT NULL REFERENCES parties(id),
  patrolled_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replants(
  id TEXT PRIMARY KEY,
  defect_id TEXT NOT NULL REFERENCES defects(id),
  unit_id TEXT NOT NULL REFERENCES units(id),
  fee_kind TEXT NOT NULL CHECK(fee_kind IN ('construction_rectification','maintenance_care')),
  responsible_party TEXT NOT NULL REFERENCES parties(id),
  fee_cents INTEGER NOT NULL CHECK(fee_cents>=0),
  ordered_by TEXT NOT NULL REFERENCES parties(id),
  ordered_at TEXT NOT NULL,
  performed_at TEXT,
  next_observation_at TEXT,
  status TEXT NOT NULL DEFAULT 'ordered' CHECK(status IN ('ordered','performed','verified')),
  verified_at TEXT
);

-- 管护接管（含更正后的重新交接）
CREATE TABLE IF NOT EXISTS takeovers(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES units(id),
  contract_id TEXT NOT NULL REFERENCES contracts(id),
  maintainer_id TEXT NOT NULL REFERENCES parties(id),
  area_m2 REAL NOT NULL CHECK(area_m2>0),
  starts_at TEXT NOT NULL,
  accepted_m2_snapshot REAL NOT NULL,
  open_construction_defects_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
  superseded_by TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facility_inspections(
  id TEXT PRIMARY KEY,
  facility_id TEXT NOT NULL REFERENCES facilities(id),
  acceptor_id TEXT NOT NULL REFERENCES parties(id),
  result TEXT NOT NULL CHECK(result IN ('accepted','returned')),
  inspected_at TEXT NOT NULL,
  note TEXT
);

-- 暂停（错误地块引用等）与通知
CREATE TABLE IF NOT EXISTS holds(
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK(scope IN ('unit','contract')),
  unit_id TEXT REFERENCES units(id),
  contract_id TEXT REFERENCES contracts(id),
  reason_code TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','resolved')),
  raised_by TEXT NOT NULL REFERENCES parties(id),
  raised_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution_note TEXT
);
CREATE TABLE IF NOT EXISTS notifications(
  id TEXT PRIMARY KEY,
  party_id TEXT NOT NULL REFERENCES parties(id),
  topic TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  acknowledged_at TEXT
);

-- 付款：受理逐作业项建账，派发对接合成资金流水，到账以回执为准
CREATE TABLE IF NOT EXISTS payments(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES units(id),
  contract_id TEXT NOT NULL REFERENCES contracts(id),
  kind TEXT NOT NULL CHECK(kind IN ('progress','retention','care_fee','replant_fee')),
  payee_id TEXT NOT NULL REFERENCES parties(id),
  amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
  status TEXT NOT NULL DEFAULT 'created'
    CHECK(status IN ('created','dispatched','paid','failed','recovered','partially_recovered')),
  basis_json TEXT NOT NULL,
  replant_id TEXT REFERENCES replants(id),
  care_period_index INTEGER,
  idempotency_key TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES parties(id),
  created_at TEXT NOT NULL,
  UNIQUE(idempotency_key)
);
CREATE TABLE IF NOT EXISTS payment_commands(
  id TEXT PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES payments(id),
  command_id TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  http_status INTEGER,
  remote_state TEXT,
  voucher TEXT,
  detail_json TEXT,
  dispatched_at TEXT,
  synced_at TEXT,
  UNIQUE(command_id, attempt)
);

-- 追偿/抵扣：旧付款事实不改零
CREATE TABLE IF NOT EXISTS recoveries(
  id TEXT PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES payments(id),
  amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
  method TEXT NOT NULL CHECK(method IN ('recover','deduct')),
  note TEXT,
  proposed_by TEXT NOT NULL REFERENCES parties(id),
  confirmed_by TEXT REFERENCES parties(id),
  created_at TEXT NOT NULL,
  confirmed_at TEXT,
  status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','confirmed','cancelled'))
);

-- 同步结清批次：观察期到期、管护转交、付款回执按同一实际责任批量结清
CREATE TABLE IF NOT EXISTS settlements(
  id TEXT PRIMARY KEY,
  run_at TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_items(
  settlement_id TEXT NOT NULL REFERENCES settlements(id),
  unit_id TEXT NOT NULL REFERENCES units(id),
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY(settlement_id, unit_id, kind, ref_id)
);

-- 领域事件流（仅追加）
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent and str(path.parent) not in (".", ""):
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=15000")
    return db


def init_db(db: sqlite3.Connection) -> None:
    # executescript 会先隐式提交；CREATE TABLE IF NOT EXISTS 保证可重复执行
    db.executescript(SCHEMA)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
