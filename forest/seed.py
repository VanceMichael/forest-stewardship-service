"""把 data/catalog.json 的合成资料装入业务库（幂等）。

原始资料写入 catalog_records 留痕；主数据（参与方、合同、单元、设施、栽植）
进入关系表。catalog 中 unit-A 的两组抽查预置为一笔“待验收”栽植申报，
由独立验收人在服务中决定接收、限期补植或退回；其余记录只留痕，不产生结论。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .database import dumps
from .timeutil import iso, parse_dt

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "data" / "catalog.json"

PARTY_DEFS = [
    ("builder-A", "施工方甲", "builder"),
    ("care-A", "管护队甲", "maintainer"),
    ("care-B", "管护队乙", "maintainer"),
    ("survey", "独立测量员", "surveyor"),
    ("acceptance", "独立验收人", "acceptor"),
    ("payment-proposer", "项目经办人", "proposer"),
    ("payment-reviewer", "项目复核人", "reviewer"),
]


def seed(db: sqlite3.Connection, catalog_path: str | Path = DEFAULT_CATALOG) -> dict:
    catalog = json.loads(Path(catalog_path).read_text())
    done = db.execute("SELECT value FROM kv WHERE key='seed.version'").fetchone()
    if done:
        return {"seeded": False, "version": done[0]}
    db.execute("BEGIN IMMEDIATE")
    try:
        for pid, name, role in PARTY_DEFS:
            db.execute("INSERT INTO parties(id,name,role) VALUES(?,?,?)", (pid, name, role))
        for principal, party_id in catalog["principals"].items():
            db.execute("INSERT INTO principals(principal,party_id) VALUES(?,?)", (principal, party_id))

        records = {r["id"]: r for r in catalog["records"]}
        for rec in catalog["records"]:
            db.execute(
                "INSERT INTO catalog_records(id,kind,refs_json,payload_json) VALUES(?,?,?,?)",
                (rec["id"], rec["kind"], dumps(rec.get("refs", [])),
                 dumps({k: v for k, v in rec.items() if k not in ("id", "kind", "refs")})),
            )

        contract = records["contract-A"]
        db.execute(
            "INSERT INTO contracts(id,builder_id,maintainer_id,retention_rate,created_at) "
            "VALUES(?,?,?,?,?)",
            (contract["id"], contract["builder"], contract["maintainer"],
             contract["retention_rate"], iso(parse_dt("2026-09-20T09:00:00+08:00"))),
        )
        for rec in catalog["records"]:
            if rec["kind"] == "facility":
                db.execute("INSERT INTO facilities(id,status_fact,accepted_at) VALUES(?,?,?)",
                           (rec["id"], rec["status_fact"], None))
        for rec in catalog["records"]:
            if rec["kind"] == "work_unit":
                db.execute(
                    "INSERT INTO units(id,contract_id,parcel,area_m2,amount_cents,current_m2) "
                    "VALUES(?,?,?,?,?,?)",
                    (rec["id"], rec["refs"][0], rec["parcel"], rec["area_m2"],
                     int(rec["amount"]) * 100, rec["area_m2"]),
                )
                for fac in rec.get("dependencies", []):
                    db.execute("INSERT INTO unit_dependencies(unit_id,facility_id) VALUES(?,?)",
                               (rec["id"], fac))
            elif rec["kind"] == "planting":
                db.execute("INSERT INTO plantings(id,unit_id,planted_at) VALUES(?,?,?)",
                           (rec["id"], rec["refs"][0], iso(parse_dt(rec["at"]))))

        # 预置 unit-A 栽植申报（施工方按作业单申报），挂接 catalog 两组抽查，等待验收签认
        unit_a = records["unit-A"]
        decl_id = "decl-seed-A"
        planted_at = iso(parse_dt(records["plant-A"]["at"]))
        db.execute(
            "INSERT INTO declarations(id,unit_id,contract_id,stage,declared_m2,submitted_by,"
            "submitted_at,status) VALUES(?,?,?,?,?,?,?,?)",
            (decl_id, "unit-A", "contract-A", "planting", unit_a["area_m2"],
             "builder-A", planted_at, "submitted"),
        )
        for idx, obs_id in enumerate(("obs-A1", "obs-A2"), start=1):
            obs = records[obs_id]
            survey_id = f"srvy-seed-A{idx}"
            alive, dead = len(obs["alive"]), len(obs["dead"])
            db.execute(
                "INSERT INTO surveys(id,declaration_id,unit_id,surveyor_id,surveyed_at,group_no,"
                "sample_size,alive_count,dead_count,measured_m2,spot_check_ratio,note,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (survey_id, decl_id, "unit-A", "survey", iso(parse_dt(obs["at"])), idx,
                 alive + dead, alive, dead, unit_a["area_m2"], None,
                 f"合成资料 {obs_id}", iso(parse_dt(obs["at"]))),
            )
            for plant_no in obs["dead"]:
                db.execute("INSERT INTO survey_dead_plants(survey_id,plant_no) VALUES(?,?)",
                           (survey_id, plant_no))
        db.execute("INSERT INTO kv(key,value) VALUES('seed.version','1')")
        db.execute("COMMIT")
        return {"seeded": True, "version": "1"}
    except Exception:
        db.execute("ROLLBACK")
        raise
