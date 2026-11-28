"""HTTP 业务处理：申报、现场记录确认、局部验收、接管、整改闭环、付款办理。

所有写操作要求 X-Principal 演练身份；施工方不能确认自己的验收，
管护队不能修改施工队原记录（系统不提供任何改写原始记录的接口）。
"""
from __future__ import annotations

from .database import dumps
from .errors import bad_request, conflict, forbidden, not_found, unprocessable
from .rules import (
    accepted_m2,
    active_takeover,
    evaluate_evidence,
    payment_view,
    require_amount_cents,
)
from .timeutil import OBSERVATION_DAYS, add_days, parse_dt
from .util import (
    Context,
    get_contract,
    get_declaration,
    get_defect,
    get_unit,
    new_id,
    require_fields,
)

# ---------------------------------------------------------------------------
# 目录与追溯
# ---------------------------------------------------------------------------


def list_parties(ctx: Context, payload: dict, query: dict):
    rows = ctx.db.execute("SELECT * FROM parties ORDER BY role,id").fetchall()
    return {"parties": [dict(r) for r in rows],
            "principals": [dict(r) for r in ctx.db.execute(
                "SELECT * FROM principals ORDER BY principal")]}


def list_contracts(ctx, payload, query):
    out = []
    for c in ctx.db.execute("SELECT * FROM contracts ORDER BY id").fetchall():
        item = dict(c)
        item["units"] = [r["id"] for r in ctx.db.execute(
            "SELECT id FROM units WHERE contract_id=? ORDER BY id", (c["id"],))]
        out.append(item)
    return {"contracts": out}


def list_units(ctx, payload, query):
    contract_id = query.get("contract_id")
    sql = "SELECT * FROM units"
    args = []
    if contract_id:
        sql += " WHERE contract_id=?"
        args.append(contract_id)
    units = ctx.db.execute(sql + " ORDER BY id", args).fetchall()
    return {"units": [dict(u) for u in units]}


def get_unit_detail(ctx, payload, query):
    unit = get_unit(ctx.db, query["unit_id"])
    view = payment_view(ctx.db, unit, ctx.now)
    trace = build_unit_trace(ctx, unit["id"])
    return {"unit": dict(unit), "payment_view": view, "trace": trace}


def build_unit_trace(ctx, unit_id: str) -> dict:
    unit = get_unit(ctx.db, unit_id)
    declarations = []
    for d in ctx.db.execute("SELECT * FROM declarations WHERE unit_id=? ORDER BY id", (unit_id,)):
        item = dict(d)
        item["surveys"] = [dict(s) for s in ctx.db.execute(
            "SELECT * FROM surveys WHERE declaration_id=? ORDER BY group_no", (d["id"],))]
        item["acceptances"] = [dict(a) for a in ctx.db.execute(
            "SELECT * FROM acceptances WHERE declaration_id=? ORDER BY id", (d["id"],))]
        declarations.append(item)
    takeovers = [dict(t) for t in ctx.db.execute(
        "SELECT * FROM takeovers WHERE unit_id=? ORDER BY id", (unit_id,))]
    defects = [dict(d) for d in ctx.db.execute(
        "SELECT * FROM defects WHERE unit_id=? ORDER BY id", (unit_id,))]
    replants = [dict(r) for r in ctx.db.execute(
        "SELECT * FROM replants WHERE unit_id=? ORDER BY id", (unit_id,))]
    payments = [dict(p) for p in ctx.db.execute(
        "SELECT * FROM payments WHERE unit_id=? ORDER BY id", (unit_id,))]
    holds = [dict(h) for h in ctx.db.execute(
        "SELECT * FROM holds WHERE scope='unit' AND unit_id=? ORDER BY id", (unit_id,))]
    next_observations = []
    tk = active_takeover(ctx.db, unit_id)
    if tk:
        next_observations.append({
            "reason": "care_observation_period",
            "at": add_days(parse_dt(tk["starts_at"]), OBSERVATION_DAYS).isoformat(),
        })
    for r in ctx.db.execute(
            "SELECT * FROM replants WHERE unit_id=? AND next_observation_at IS NOT NULL "
            "AND status IN ('performed','verified') ORDER BY next_observation_at", (unit_id,)):
        next_observations.append({
            "reason": f"replant_{r['fee_kind']}",
            "replant_id": r["id"],
            "at": r["next_observation_at"],
        })
    return {
        "unit_id": unit_id,
        "parcel": unit["parcel"],
        "boundary_m2": float(unit["current_m2"]),
        "accepted_m2": accepted_m2(ctx.db, unit_id),
        "declarations": declarations,
        "takeovers": takeovers,
        "defects": defects,
        "replants": replants,
        "payments": payments,
        "holds": holds,
        "next_observations": next_observations,
    }


def unit_trace(ctx, payload, query):
    return build_unit_trace(ctx, query["unit_id"])


def contract_ledger(ctx, payload, query):
    contract = get_contract(ctx.db, query["contract_id"])
    units = ctx.db.execute("SELECT * FROM units WHERE contract_id=? ORDER BY id",
                           (contract["id"],)).fetchall()
    unit_views = []
    for u in units:
        v = payment_view(ctx.db, u, ctx.now)
        trace = build_unit_trace(ctx, u["id"])
        unit_views.append({
            "unit_id": u["id"],
            "parcel": u["parcel"],
            "accepted_m2": v["accepted_m2"],
            "takeover_active": v["takeover_active"],
            "open_defects": v["open_defects"],
            "missing_facilities": v["missing_facilities"],
            "active_holds": v["active_holds"],
            "retained_responsibilities": [
                d for d in trace["defects"] if d["status"] in ("open", "cause_set", "replanting")
                and d["cause"] != "maintenance"
            ],
            "payment_items": v["items"],
        })
    facilities = [dict(f) for f in ctx.db.execute(
        "SELECT f.* FROM facilities f JOIN unit_dependencies ud ON ud.facility_id=f.id "
        "JOIN units u ON u.id=ud.unit_id WHERE u.contract_id=? GROUP BY f.id ORDER BY f.id",
        (contract["id"],))]
    return {"contract": dict(contract), "facilities": facilities, "units": unit_views}


def list_events(ctx, payload, query):
    rows = ctx.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200").fetchall()
    return {"events": [dict(r) for r in rows]}


def list_notifications(ctx, payload, query):
    rows = ctx.db.execute(
        "SELECT * FROM notifications WHERE party_id=? ORDER BY id DESC", (ctx.party_id,)).fetchall()
    return {"notifications": [dict(r) for r in rows]}


def ack_notification(ctx, payload, query):
    ctx.require_role("maintainer", "builder", "proposer", "reviewer")
    require_fields(payload, ["notification_id"])
    row = ctx.db.execute("SELECT * FROM notifications WHERE id=?",
                         (payload["notification_id"],)).fetchone()
    if not row:
        raise not_found("通知不存在")
    if row["party_id"] != ctx.party_id:
        raise forbidden("只能确认发给本身份的通知")
    ctx.db.execute("UPDATE notifications SET acknowledged_at=? WHERE id=? AND acknowledged_at IS NULL",
                   (ctx.now_iso, payload["notification_id"]))
    ctx.event("notification.ack", {"notification_id": payload["notification_id"]})
    return {"ok": True}


# ---------------------------------------------------------------------------
# 栽植事实登记（施工方按作业单记录，后续抽查间隔以此为基准）
# ---------------------------------------------------------------------------


def create_planting(ctx, payload, query):
    ctx.require_role("builder")
    require_fields(payload, ["unit_id", "planted_at"])
    unit = get_unit(ctx.db, payload["unit_id"])
    contract = get_contract(ctx.db, unit["contract_id"])
    if ctx.party_id != contract["builder_id"]:
        raise forbidden("只有本合同施工方可以登记栽植事实")
    existing = ctx.db.execute("SELECT id FROM plantings WHERE unit_id=?", (unit["id"],)).fetchone()
    if existing:
        raise conflict("planting_exists", f"单元 {unit['id']} 已登记栽植事实 {existing['id']}")
    planting_id = new_id("plt")
    ctx.db.execute("INSERT INTO plantings(id,unit_id,planted_at) VALUES(?,?,?)",
                   (planting_id, unit["id"], parse_dt(payload["planted_at"]).isoformat()))
    ctx.event("planting.created", {"planting_id": planting_id, "unit_id": unit["id"],
                                   "planted_at": payload["planted_at"]})
    return {"planting_id": planting_id}


def _planting_or_error(ctx, unit_id: str):
    planting = ctx.db.execute(
        "SELECT planted_at FROM plantings WHERE unit_id=? ORDER BY id LIMIT 1",
        (unit_id,)).fetchone()
    if not planting:
        raise unprocessable("no_planting", "单元缺少栽植事实，无法计算抽查间隔与证据适用性")
    return planting


# ---------------------------------------------------------------------------
# 施工/管护分批申报
# ---------------------------------------------------------------------------


def create_declaration(ctx, payload, query):
    require_fields(payload, ["unit_id", "stage", "declared_m2"])
    unit = get_unit(ctx.db, payload["unit_id"])
    if payload["stage"] not in ("planting", "care"):
        raise bad_request("invalid_stage", "stage 只能是 planting 或 care")
    try:
        declared_m2 = float(payload["declared_m2"])
    except (TypeError, ValueError):
        raise bad_request("invalid_area", "declared_m2 必须为正数")
    if declared_m2 <= 0:
        raise bad_request("invalid_area", "declared_m2 必须为正数")
    if declared_m2 > float(unit["current_m2"]):
        raise unprocessable("area_exceeds_boundary",
                            f"申报面积 {declared_m2} 超过单元当前范围 {unit['current_m2']}")
    if payload["stage"] == "planting":
        ctx.require_role("builder")
        if ctx.party_id != get_contract(ctx.db, unit["contract_id"])["builder_id"]:
            raise forbidden("只有本合同施工方可以申报栽植")
    else:
        ctx.require_role("maintainer")
        if ctx.party_id != get_contract(ctx.db, unit["contract_id"])["maintainer_id"]:
            raise forbidden("只有本合同管护方可以申报管护作业")
        if not active_takeover(ctx.db, unit["id"]):
            raise unprocessable("no_takeover", "管护申报前必须先完成接管")
    dup = ctx.db.execute(
        "SELECT id FROM declarations WHERE unit_id=? AND stage=? AND status IN "
        "('submitted','accepted','replant_required')", (unit["id"], payload["stage"])).fetchone()
    if dup:
        raise conflict("stage_already_claimed",
                       f"单元 {unit['id']} 的 {payload['stage']} 阶段已被申报 {dup['id']} 认领，"
                       "同一阶段不能重复认领；需先退回或替代原申报")
    decl_id = new_id("decl")
    ctx.db.execute(
        "INSERT INTO declarations(id,unit_id,contract_id,stage,declared_m2,submitted_by,"
        "submitted_at,status) VALUES(?,?,?,?,?,?,?,?)",
        (decl_id, unit["id"], unit["contract_id"], payload["stage"], declared_m2,
         ctx.party_id, ctx.now_iso, "submitted"))
    ctx.event("declaration.created", {"declaration_id": decl_id, "unit_id": unit["id"],
                                      "stage": payload["stage"], "declared_m2": declared_m2})
    return {"declaration_id": decl_id, "status": "submitted"}


def get_declaration_detail(ctx, payload, query):
    decl = get_declaration(ctx.db, query["declaration_id"])
    surveys = ctx.db.execute("SELECT * FROM surveys WHERE declaration_id=? ORDER BY group_no",
                             (decl["id"],)).fetchall()
    acceptances = ctx.db.execute("SELECT * FROM acceptances WHERE declaration_id=? ORDER BY id",
                                 (decl["id"],)).fetchall()
    evidence = None
    if surveys:
        planting = ctx.db.execute(
            "SELECT planted_at FROM plantings WHERE unit_id=? ORDER BY id LIMIT 1",
            (decl["unit_id"],)).fetchone()
        if planting:
            evidence = evaluate_evidence(surveys, planting["planted_at"])
        else:
            evidence = {"applicable": False,
                        "failures": [{"code": "no_planting", "message": "缺少栽植事实"}]}
    return {"declaration": dict(decl), "surveys": [dict(s) for s in surveys],
            "acceptances": [dict(a) for a in acceptances], "evidence_check": evidence}


# ---------------------------------------------------------------------------
# 测量员现场抽查（实际面积与抽查记录）
# ---------------------------------------------------------------------------


def add_survey(ctx, payload, query):
    ctx.require_role("surveyor")
    require_fields(payload, ["declaration_id", "surveyed_at", "sample_size",
                             "alive_count", "dead_count", "measured_m2"])
    decl = get_declaration(ctx.db, payload["declaration_id"])
    if decl["status"] in ("accepted", "returned"):
        raise conflict("declaration_locked", f"申报 {decl['id']} 已{decl['status']}，不再接收抽查")
    unit = get_unit(ctx.db, decl["unit_id"])
    try:
        sample = int(payload["sample_size"])
        alive = int(payload["alive_count"])
        dead = int(payload["dead_count"])
        measured = float(payload["measured_m2"])
    except (TypeError, ValueError):
        raise bad_request("invalid_numbers", "抽查数量或面积格式不正确")
    if sample <= 0 or alive < 0 or dead < 0 or measured < 0:
        raise bad_request("invalid_numbers", "抽查数量或面积必须为非负/正数")
    if alive + dead != sample:
        raise bad_request("count_mismatch", "alive_count + dead_count 必须等于 sample_size")
    if measured > float(unit["current_m2"]):
        raise unprocessable("area_exceeds_boundary",
                            f"实测面积 {measured} 超过单元当前范围 {unit['current_m2']}")
    surveyed_at = parse_dt(payload["surveyed_at"])
    existing = ctx.db.execute("SELECT MAX(group_no) AS g FROM surveys WHERE declaration_id=?",
                              (decl["id"],)).fetchone()
    group_no = int(existing["g"] or 0) + 1
    same_date = ctx.db.execute(
        "SELECT id FROM surveys WHERE declaration_id=? AND substr(surveyed_at,1,10)=?",
        (decl["id"], surveyed_at.date().isoformat())).fetchone()
    if same_date:
        raise conflict("group_same_date", "同一申报在同一日期已有抽查组，独立抽查须为不同日期")
    survey_id = new_id("srvy")
    ratio = None
    if "spot_check_ratio" in payload and payload["spot_check_ratio"] is not None:
        ratio = float(payload["spot_check_ratio"])
    ctx.db.execute(
        "INSERT INTO surveys(id,declaration_id,unit_id,surveyor_id,surveyed_at,group_no,"
        "sample_size,alive_count,dead_count,measured_m2,spot_check_ratio,note,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (survey_id, decl["id"], unit["id"], ctx.party_id, surveyed_at.isoformat(), group_no,
         sample, alive, dead, measured, ratio, payload.get("note"), ctx.now_iso))
    for plant_no in payload.get("dead_plants", []):
        ctx.db.execute("INSERT OR IGNORE INTO survey_dead_plants(survey_id,plant_no) VALUES(?,?)",
                       (survey_id, int(plant_no)))
    # 即时给出证据适用性检查（仅检查合同条件，不作现场结论）
    surveys = ctx.db.execute("SELECT * FROM surveys WHERE declaration_id=? ORDER BY group_no",
                             (decl["id"],)).fetchall()
    planting = _planting_or_error(ctx, unit["id"])
    check = evaluate_evidence(surveys, planting["planted_at"])
    ctx.event("survey.created", {"survey_id": survey_id, "declaration_id": decl["id"],
                                 "group_no": group_no, "evidence_applicable": check["applicable"]})
    return {"survey_id": survey_id, "group_no": group_no, "evidence_check": check}


# ---------------------------------------------------------------------------
# 独立验收人签认：接收 / 限期补植 / 退回
# ---------------------------------------------------------------------------


def create_acceptance(ctx, payload, query):
    ctx.require_role("acceptor")
    require_fields(payload, ["declaration_id", "decision"])
    decl = get_declaration(ctx.db, payload["declaration_id"])
    if decl["status"] == "accepted":
        raise conflict("declaration_locked", f"申报 {decl['id']} 已接收，不能重复签认")
    if payload["decision"] not in ("accept", "replant_required", "return"):
        raise bad_request("invalid_decision", "decision 只能是 accept/replant_required/return")
    unit = get_unit(ctx.db, decl["unit_id"])
    contract = get_contract(ctx.db, unit["contract_id"])
    # 施工方不能确认自己的验收（角色独立，且显式拦截施工身份）
    if ctx.party_id == contract["builder_id"]:
        raise forbidden("施工方不能确认自己的验收")
    surveys = ctx.db.execute("SELECT * FROM surveys WHERE declaration_id=? ORDER BY group_no",
                             (decl["id"],)).fetchall()
    if not surveys:
        raise unprocessable("no_survey", "没有测量员抽查记录，验收人无法签认；未抽查不等于全部成活")
    planting = _planting_or_error(ctx, unit["id"])
    evidence = evaluate_evidence(surveys, planting["planted_at"])

    accepted_m2_value = 0.0
    if payload["decision"] == "accept":
        if not evidence["applicable"]:
            raise unprocessable("evidence_not_applicable",
                                "抽查证据不满足合同适用条件，不能签认接收", )
        accepted_m2_value = float(payload.get("accepted_m2", 0))
        if accepted_m2_value <= 0:
            raise bad_request("invalid_accepted_m2", "接收决定必须给出正数 accepted_m2")
        if accepted_m2_value > float(unit["current_m2"]):
            raise unprocessable("area_exceeds_boundary", "接收面积不能超过单元当前范围")
        # 有争议的部分不并入合格量：合格量只来自实测覆盖范围
        max_measured = max(float(s["measured_m2"]) for s in surveys)
        if accepted_m2_value > max_measured:
            raise unprocessable("disputed_area_not_qualified",
                                f"接收面积 {accepted_m2_value} 超过实测覆盖 {max_measured}，"
                                "争议/未抽查部分不得并入合格量")
        new_status = "accepted"
        deadline = None
    elif payload["decision"] == "replant_required":
        require_fields(payload, ["replant_deadline"])
        deadline = parse_dt(payload["replant_deadline"]).isoformat()
        new_status = "replant_required"
    else:
        new_status = "returned"
        deadline = None

    acc_id = new_id("acc")
    ctx.db.execute(
        "INSERT INTO acceptances(id,declaration_id,unit_id,acceptor_id,decision,accepted_m2,"
        "replant_deadline,basis_json,signed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (acc_id, decl["id"], unit["id"], ctx.party_id, payload["decision"], accepted_m2_value,
         deadline, dumps({"evidence_check": evidence, "note": payload.get("note")}), ctx.now_iso))
    ctx.db.execute("UPDATE declarations SET status=? WHERE id=?", (new_status, decl["id"]))
    ctx.event("acceptance.signed", {"acceptance_id": acc_id, "declaration_id": decl["id"],
                                    "unit_id": unit["id"], "decision": payload["decision"],
                                    "accepted_m2": accepted_m2_value})
    return {"acceptance_id": acc_id, "declaration_status": new_status,
            "accepted_m2": accepted_m2_value, "evidence_check": evidence}


# ---------------------------------------------------------------------------
# 巡护批次与缺陷合并（同一死亡植株不重复开补植费；不吞掉其他新缺陷）
# ---------------------------------------------------------------------------


def _merge_or_create_defect(ctx, unit_id, stage, plant_no, description, reporter_id, at_iso,
                            batch_id=None, note=None):
    if plant_no is not None:
        existing = ctx.db.execute(
            "SELECT * FROM defects WHERE unit_id=? AND stage=? AND plant_no=? AND status IN "
            "('open','cause_set','replanting')", (unit_id, stage, plant_no)).fetchone()
        if existing:
            report_id = new_id("dfr")
            ctx.db.execute(
                "INSERT INTO defect_reports(id,defect_id,batch_id,reporter_id,reported_at,"
                "plant_no,note) VALUES(?,?,?,?,?,?,?)",
                (report_id, existing["id"], batch_id, reporter_id, at_iso, plant_no,
                 note or "重复报告，合并到既有缺陷，不另开补植费用"))
            return existing["id"], True
    defect_id = new_id("def")
    ctx.db.execute(
        "INSERT INTO defects(id,unit_id,stage,plant_no,description,first_reported_at,status) "
        "VALUES(?,?,?,?,?,?, 'open')",
        (defect_id, unit_id, stage, plant_no, description, at_iso))
    ctx.db.execute(
        "INSERT INTO defect_reports(id,defect_id,batch_id,reporter_id,reported_at,plant_no,note) "
        "VALUES(?,?,?,?,?,?,?)",
        (new_id("dfr"), defect_id, batch_id, reporter_id, at_iso, plant_no, note))
    return defect_id, False


def create_patrol_batch(ctx, payload, query):
    ctx.require_role("maintainer")
    require_fields(payload, ["unit_id", "patrolled_at", "findings"])
    unit = get_unit(ctx.db, payload["unit_id"])
    contract = get_contract(ctx.db, unit["contract_id"])
    if ctx.party_id != contract["maintainer_id"]:
        raise forbidden("只有本合同管护队可以提交巡护批次")
    if not isinstance(payload["findings"], list) or not payload["findings"]:
        raise bad_request("invalid_findings", "findings 必须为非空数组")
    if not active_takeover(ctx.db, unit["id"]):
        raise unprocessable("no_takeover", "尚未接管的单元不能提交管护巡护")
    hold = ctx.db.execute(
        "SELECT id FROM holds WHERE status='active' AND ((scope='unit' AND unit_id=?) "
        "OR (scope='contract' AND contract_id=?))", (unit["id"], unit["contract_id"])).fetchone()
    if hold:
        raise conflict("hold_active", f"单元存在未解除的暂停 {hold['id']}，先处理待确认事项")
    at_iso = parse_dt(payload["patrolled_at"]).isoformat()
    batch_id = new_id("pat")
    ctx.db.execute(
        "INSERT INTO patrol_batches(id,unit_id,maintainer_id,patrolled_at,created_at) "
        "VALUES(?,?,?,?,?)",
        (batch_id, unit["id"], ctx.party_id, at_iso, ctx.now_iso))
    merged, created = [], []
    for finding in payload["findings"]:
        plant_no = finding.get("plant_no")
        defect_id, was_merged = _merge_or_create_defect(
            ctx, unit["id"], "care", int(plant_no) if plant_no is not None else None,
            finding.get("description"), ctx.party_id, at_iso, batch_id, finding.get("note"))
        (merged if was_merged else created).append(defect_id)
    ctx.event("patrol.batch_created", {"batch_id": batch_id, "unit_id": unit["id"],
                                       "new_defects": created, "merged_defects": merged})
    return {"batch_id": batch_id, "new_defect_ids": created, "merged_defect_ids": merged,
            "note": "重复报告的同一缺陷已合并，只保留一笔补植费用；其他单元/植株的新缺陷独立建档"}


def list_defects(ctx, payload, query):
    unit_id = query.get("unit_id")
    sql = ("SELECT d.*, (SELECT COUNT(*) FROM defect_reports r WHERE r.defect_id=d.id) "
           "AS report_count FROM defects d")
    args = []
    if unit_id:
        sql += " WHERE d.unit_id=?"
        args.append(unit_id)
    rows = ctx.db.execute(sql + " ORDER BY d.id", args).fetchall()
    return {"defects": [dict(r) for r in rows]}


def set_defect_cause(ctx, payload, query):
    """归责由有权人确认：施工缺陷归施工方（接管不免责），正常养护损耗归管护方。"""
    ctx.require_role("acceptor", "reviewer")
    require_fields(payload, ["defect_id", "cause"])
    if payload["cause"] not in ("construction", "maintenance"):
        raise bad_request("invalid_cause", "cause 只能是 construction 或 maintenance")
    defect = get_defect(ctx.db, payload["defect_id"])
    if defect["status"] in ("closed", "rejected"):
        raise conflict("defect_locked", f"缺陷 {defect['id']} 已终结")
    ctx.db.execute(
        "UPDATE defects SET cause=?, cause_set_by=?, cause_set_at=?, status='cause_set' WHERE id=?",
        (payload["cause"], ctx.party_id, ctx.now_iso, defect["id"]))
    # 归责确认后通知责任承担方
    unit = get_unit(ctx.db, defect["unit_id"])
    contract = get_contract(ctx.db, unit["contract_id"])
    payee = contract["builder_id"] if payload["cause"] == "construction" else contract["maintainer_id"]
    ctx.notify(payee, "defect.cause_set",
               {"defect_id": defect["id"], "unit_id": unit["id"], "cause": payload["cause"]})
    ctx.event("defect.cause_set", {"defect_id": defect["id"], "cause": payload["cause"]})
    return {"defect_id": defect["id"], "cause": payload["cause"], "responsible_party": payee}


def order_replant(ctx, payload, query):
    """补植作业：施工整改与正常养护的费用来源、责任人、下次观察起点不同。"""
    ctx.require_role("reviewer", "acceptor")
    require_fields(payload, ["defect_id", "amount_cents"])
    defect = get_defect(ctx.db, payload["defect_id"])
    if defect["status"] in ("closed", "rejected"):
        raise conflict("defect_locked", f"缺陷 {defect['id']} 已终结，不能再开补植")
    if not defect["cause"]:
        raise unprocessable("cause_unset", "缺陷尚未归责，不能确定补植费用来源与责任人")
    unit = get_unit(ctx.db, defect["unit_id"])
    contract = get_contract(ctx.db, unit["contract_id"])
    fee_cents = require_amount_cents(payload)
    if defect["cause"] == "construction":
        fee_kind = "construction_rectification"
        responsible = contract["builder_id"]
    else:
        fee_kind = "maintenance_care"
        responsible = contract["maintainer_id"]
    replant_id = new_id("rpl")
    ctx.db.execute(
        "INSERT INTO replants(id,defect_id,unit_id,fee_kind,responsible_party,fee_cents,"
        "ordered_by,ordered_at,status) VALUES(?,?,?,?,?,?,?,?,'ordered')",
        (replant_id, defect["id"], unit["id"], fee_kind, responsible, fee_cents,
         ctx.party_id, ctx.now_iso))
    ctx.db.execute("UPDATE defects SET status='replanting' WHERE id=?", (defect["id"],))
    ctx.event("replant.ordered", {"replant_id": replant_id, "defect_id": defect["id"],
                                  "fee_kind": fee_kind, "responsible_party": responsible})
    return {"replant_id": replant_id, "fee_kind": fee_kind, "responsible_party": responsible}


def perform_replant(ctx, payload, query):
    ctx.require_role("maintainer", "builder")
    require_fields(payload, ["replant_id", "performed_at"])
    row = ctx.db.execute("SELECT * FROM replants WHERE id=?", (payload["replant_id"],)).fetchone()
    if not row:
        raise not_found("补植作业不存在")
    if row["status"] != "ordered":
        raise conflict("replant_locked", f"补植 {row['id']} 状态为 {row['status']}")
    performed = parse_dt(payload["performed_at"])
    # 下一次观察起点：施工整改按整改后重新起算；养护按养护作业起算（来源不同，分别记录）
    next_obs = add_days(performed, OBSERVATION_DAYS).isoformat()
    ctx.db.execute(
        "UPDATE replants SET status='performed', performed_at=?, next_observation_at=? WHERE id=?",
        (performed.isoformat(), next_obs, row["id"]))
    ctx.event("replant.performed", {"replant_id": row["id"], "performed_at": performed.isoformat(),
                                    "next_observation_at": next_obs})
    return {"replant_id": row["id"], "status": "performed", "next_observation_at": next_obs}


def verify_replant(ctx, payload, query):
    ctx.require_role("acceptor", "reviewer")
    require_fields(payload, ["replant_id"])
    row = ctx.db.execute("SELECT * FROM replants WHERE id=?", (payload["replant_id"],)).fetchone()
    if not row:
        raise not_found("补植作业不存在")
    if row["status"] != "performed":
        raise conflict("replant_not_performed", "补植未实施，不能核验")
    ctx.db.execute("UPDATE replants SET status='verified', verified_at=? WHERE id=?",
                   (ctx.now_iso, row["id"]))
    ctx.db.execute("UPDATE defects SET status='closed', closed_at=?, "
                   "resolution_note=COALESCE(resolution_note,'') WHERE id=?",
                   (ctx.now_iso, row["defect_id"]))
    ctx.event("replant.verified", {"replant_id": row["id"], "defect_id": row["defect_id"]})
    return {"replant_id": row["id"], "status": "verified", "defect_status": "closed"}


# ---------------------------------------------------------------------------
# 管护接管与更正后重新交接
# ---------------------------------------------------------------------------


def create_takeover(ctx, payload, query):
    ctx.require_role("maintainer")
    require_fields(payload, ["unit_id", "area_m2", "starts_at"])
    unit = get_unit(ctx.db, payload["unit_id"])
    contract = get_contract(ctx.db, unit["contract_id"])
    if ctx.party_id != contract["maintainer_id"]:
        raise forbidden("只有本合同管护队可以接管")
    accepted = accepted_m2(ctx.db, unit["id"])
    if accepted <= 0:
        raise unprocessable("no_accepted_area", "单元没有已接收面积，不能接管")
    try:
        area = float(payload["area_m2"])
    except (TypeError, ValueError):
        raise bad_request("invalid_area", "area_m2 必须为正数")
    if area <= 0 or area > accepted + 1e-9:
        raise unprocessable("takeover_area_exceeds_accepted",
                            f"接管面积 {area} 不能超过已接收面积 {accepted}")
    hold = ctx.db.execute(
        "SELECT id FROM holds WHERE status='active' AND ((scope='unit' AND unit_id=?) "
        "OR (scope='contract' AND contract_id=?))", (unit["id"], unit["contract_id"])).fetchone()
    if hold:
        raise conflict("hold_active", f"单元存在未解除的暂停 {hold['id']}，暂缓接管")
    if active_takeover(ctx.db, unit["id"]):
        raise conflict("takeover_active",
                       "单元已有生效中的接管；范围更正请使用重新交接接口，不能重复接管")
    starts = parse_dt(payload["starts_at"])
    open_con = ctx.db.execute(
        "SELECT id,plant_no,cause FROM defects WHERE unit_id=? AND status IN "
        "('open','cause_set','replanting') AND (cause IS NULL OR cause != 'maintenance')", (unit["id"],)).fetchall()
    tk_id = new_id("tk")
    ctx.db.execute(
        "INSERT INTO takeovers(id,unit_id,contract_id,maintainer_id,area_m2,starts_at,"
        "accepted_m2_snapshot,open_construction_defects_json,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'active',?)",
        (tk_id, unit["id"], contract["id"], ctx.party_id, area, starts.isoformat(), accepted,
         dumps([dict(r) for r in open_con]), ctx.now_iso))
    ctx.notify(contract["builder_id"], "takeover.created",
               {"unit_id": unit["id"], "takeover_id": tk_id,
                "note": "接管不免除施工缺陷责任，未结整改仍由施工方承担"})
    ctx.event("takeover.created", {"takeover_id": tk_id, "unit_id": unit["id"], "area_m2": area,
                                   "starts_at": starts.isoformat(),
                                   "open_construction_defects": [r["id"] for r in open_con]})
    return {"takeover_id": tk_id, "starts_at": starts.isoformat(),
            "observation_due_at": add_days(starts, OBSERVATION_DAYS).isoformat(),
            "open_construction_defects": [r["id"] for r in open_con]}


def rehandover(ctx, payload, query):
    """现场更正作业范围后的重新交接：原管护队必须知道接过哪部分、哪部分重新交接。"""
    ctx.require_role("acceptor", "reviewer")
    require_fields(payload, ["unit_id", "area_m2", "starts_at"])
    unit = get_unit(ctx.db, payload["unit_id"])
    current = active_takeover(ctx.db, unit["id"])
    if not current:
        raise unprocessable("no_active_takeover", "单元没有生效中的接管，不能重新交接")
    accepted = accepted_m2(ctx.db, unit["id"])
    try:
        area = float(payload["area_m2"])
    except (TypeError, ValueError):
        raise bad_request("invalid_area", "area_m2 必须为正数")
    if area <= 0 or area > accepted + 1e-9:
        raise unprocessable("takeover_area_exceeds_accepted",
                            f"重新交接面积 {area} 不能超过已接收面积 {accepted}")
    starts = parse_dt(payload["starts_at"])
    new_id_ = new_id("tk")
    ctx.db.execute("UPDATE takeovers SET status='superseded', superseded_by=? WHERE id=?",
                   (new_id_, current["id"]))
    open_con = ctx.db.execute(
        "SELECT id,plant_no,cause FROM defects WHERE unit_id=? AND status IN "
        "('open','cause_set','replanting') AND (cause IS NULL OR cause != 'maintenance')", (unit["id"],)).fetchall()
    ctx.db.execute(
        "INSERT INTO takeovers(id,unit_id,contract_id,maintainer_id,area_m2,starts_at,"
        "accepted_m2_snapshot,open_construction_defects_json,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'active',?)",
        (new_id_, unit["id"], unit["contract_id"], current["maintainer_id"], area,
         starts.isoformat(), accepted, dumps([dict(r) for r in open_con]), ctx.now_iso))
    ctx.notify(current["maintainer_id"], "takeover.rehandover",
               {"unit_id": unit["id"], "superseded": current["id"], "new_takeover_id": new_id_,
                "previous_area_m2": current["area_m2"], "new_area_m2": area,
                "note": "作业范围已更正：原接管部分留痕，超出/变化部分须按新边界重新交接"})
    ctx.event("takeover.rehandover", {"unit_id": unit["id"], "superseded": current["id"],
                                      "new_takeover_id": new_id_, "new_area_m2": area})
    return {"new_takeover_id": new_id_, "superseded_takeover_id": current["id"]}


def inspect_facility(ctx, payload, query):
    ctx.require_role("acceptor")
    require_fields(payload, ["facility_id", "result", "inspected_at"])
    if payload["result"] not in ("accepted", "returned"):
        raise bad_request("invalid_result", "result 只能是 accepted 或 returned")
    fac = ctx.db.execute("SELECT * FROM facilities WHERE id=?", (payload["facility_id"],)).fetchone()
    if not fac:
        raise not_found("防护设施不存在")
    at_iso = parse_dt(payload["inspected_at"]).isoformat()
    insp_id = new_id("finsp")
    ctx.db.execute(
        "INSERT INTO facility_inspections(id,facility_id,acceptor_id,result,inspected_at,note) "
        "VALUES(?,?,?,?,?,?)",
        (insp_id, fac["id"], ctx.party_id, payload["result"], at_iso, payload.get("note")))
    if payload["result"] == "accepted":
        ctx.db.execute("UPDATE facilities SET status_fact='accepted', accepted_at=? WHERE id=?",
                       (at_iso, fac["id"]))
    else:
        ctx.db.execute("UPDATE facilities SET status_fact='returned' WHERE id=?", (fac["id"],))
    ctx.event("facility.inspected", {"facility_id": fac["id"], "result": payload["result"]})
    return {"inspection_id": insp_id, "facility_id": fac["id"], "result": payload["result"]}


# ---------------------------------------------------------------------------
# 错误地块引用 / 界桩更正：暂停真正受影响安排，不删除原签认
# ---------------------------------------------------------------------------


def wrong_plot_correction(ctx, payload, query):
    """发现施工记录引用错误地块：暂停受影响单元的付款与管护安排并发出待确认事项。

    原始签认保留不删，责任不悬空；解除暂停需要经办人+复核人确认更正结论。
    """
    ctx.require_role("acceptor", "reviewer", "proposer")
    require_fields(payload, ["affected_unit_ids", "reason"])
    affected = payload["affected_unit_ids"]
    if not isinstance(affected, list) or not affected:
        raise bad_request("invalid_affected", "affected_unit_ids 必须为非空数组")
    hold_ids = []
    for unit_id in affected:
        unit = get_unit(ctx.db, unit_id)
        hold_id = new_id("hold")
        ctx.db.execute(
            "INSERT INTO holds(id,scope,unit_id,contract_id,reason_code,reason,status,raised_by,"
            "raised_at) VALUES(?,?,?,?,?,?,'active',?,?)",
            (hold_id, "unit", unit_id, unit["contract_id"], "wrong_plot_reference",
             payload["reason"], ctx.party_id, ctx.now_iso))
        hold_ids.append(hold_id)
        contract = get_contract(ctx.db, unit["contract_id"])
        # 向已接管人发出待确认事项
        tk = active_takeover(ctx.db, unit_id)
        if tk:
            ctx.notify(tk["maintainer_id"], "hold.pending_confirmation",
                       {"hold_id": hold_id, "unit_id": unit_id, "reason": payload["reason"],
                        "note": "施工记录地块引用存疑，请确认已接管边界；原签认保留不删"})
        ctx.notify(contract["builder_id"], "hold.pending_confirmation",
                   {"hold_id": hold_id, "unit_id": unit_id, "reason": payload["reason"]})
        ctx.event("hold.raised", {"hold_id": hold_id, "unit_id": unit_id,
                                  "reason_code": "wrong_plot_reference"})
    return {"hold_ids": hold_ids, "affected_unit_ids": affected,
            "note": "受影响单元的付款与新增管护安排已暂停；原始记录与签认保留"}


def correct_unit_boundary(ctx, payload, query):
    """界桩复核后的范围更正：更新当前边界并通知原管护队重新交接。"""
    ctx.require_role("acceptor", "reviewer")
    require_fields(payload, ["unit_id", "new_area_m2", "reason"])
    unit = get_unit(ctx.db, payload["unit_id"])
    try:
        new_area = float(payload["new_area_m2"])
    except (TypeError, ValueError):
        raise bad_request("invalid_area", "new_area_m2 必须为正数")
    if new_area <= 0:
        raise bad_request("invalid_area", "new_area_m2 必须为正数")
    if new_area > float(unit["area_m2"]) * 1.5:
        raise unprocessable("area_change_unreasonable", "更正面积偏离设计面积过大，先暂停并走错误地块流程")
    old_area = float(unit["current_m2"])
    ctx.db.execute("UPDATE units SET current_m2=? WHERE id=?", (new_area, unit["id"]))
    tk = active_takeover(ctx.db, unit["id"])
    if tk:
        ctx.notify(tk["maintainer_id"], "boundary.corrected",
                   {"unit_id": unit["id"], "old_m2": old_area, "new_m2": new_area,
                    "reason": payload["reason"],
                    "note": "作业范围已更正：已接部分继续有效，变化部分必须重新交接"})
    ctx.event("unit.boundary_corrected", {"unit_id": unit["id"], "old_m2": old_area,
                                          "new_m2": new_area, "reason": payload["reason"]})
    return {"unit_id": unit["id"], "previous_m2": old_area, "current_m2": new_area}


def list_holds(ctx, payload, query):
    rows = ctx.db.execute("SELECT * FROM holds ORDER BY id DESC").fetchall()
    return {"holds": [dict(r) for r in rows]}


def resolve_hold(ctx, payload, query):
    ctx.require_role("proposer", "reviewer")
    require_fields(payload, ["hold_id", "resolution_note"])
    hold = ctx.db.execute("SELECT * FROM holds WHERE id=?", (payload["hold_id"],)).fetchone()
    if not hold:
        raise not_found("暂停事项不存在")
    if hold["status"] == "resolved":
        raise conflict("hold_resolved", f"暂停 {hold['id']} 已解除")
    ctx.db.execute("UPDATE holds SET status='resolved', resolved_at=?, resolution_note=? WHERE id=?",
                   (ctx.now_iso, payload["resolution_note"], hold["id"]))
    ctx.event("hold.resolved", {"hold_id": hold["id"], "note": payload["resolution_note"]})
    return {"hold_id": hold["id"], "status": "resolved"}


# ---------------------------------------------------------------------------
# 付款：可付性查询、逐作业项受理、资金派发与回执同步
# ---------------------------------------------------------------------------


def eligible_payments(ctx, payload, query):
    if query.get("contract_id"):
        contract = get_contract(ctx.db, query["contract_id"])
        units = ctx.db.execute("SELECT * FROM units WHERE contract_id=? ORDER BY id",
                               (contract["id"],)).fetchall()
    elif query.get("unit_id"):
        units = [get_unit(ctx.db, query["unit_id"])]
    else:
        units = ctx.db.execute("SELECT * FROM units ORDER BY id").fetchall()
    return {"units": [payment_view(ctx.db, u, ctx.now) for u in units], "at": ctx.now_iso}


def _idempotency_key(payload, unit_id, kind):
    if payload.get("idempotency_key"):
        return str(payload["idempotency_key"])
    return f"{unit_id}:{kind}:{dumps(payload.get('basis', {}))}"


def create_payment(ctx, payload, query):
    """项目经办人按合同逐作业项办理；受理即占额度，到账与受理分开。"""
    ctx.require_role("proposer")
    require_fields(payload, ["unit_id", "kind", "amount_cents"])
    unit = get_unit(ctx.db, payload["unit_id"])
    kind = payload["kind"]
    if kind not in ("progress", "retention", "care_fee", "replant_fee"):
        raise bad_request("invalid_kind", "未知付款类型")
    view = payment_view(ctx.db, unit, ctx.now)
    item = view["items"][kind]
    holds = ctx.db.execute(
        "SELECT id FROM holds WHERE status='active' AND ((scope='unit' AND unit_id=?) "
        "OR (scope='contract' AND contract_id=?))", (unit["id"], unit["contract_id"])).fetchall()
    if holds:
        raise conflict("hold_active", f"单元存在未解除暂停 {[h['id'] for h in holds]}，暂停付款")
    amount = require_amount_cents(payload)
    basis = dict(payload.get("basis", {}))
    replant_id = None
    care_period_index = None

    if kind in ("progress", "retention"):
        if item["blockers"]:
            raise unprocessable("not_payable", dumps(item["blockers"]))
        if amount > item["amount_cents"]:
            raise unprocessable("amount_exceeds_eligible",
                                f"可付余额 {item['amount_cents']} 分，申请 {amount} 分")
    elif kind == "care_fee":
        period_index = basis.get("period_index")
        period = next((p for p in item["periods"] if p["period_index"] == period_index), None)
        if item["blockers"] or not period or period["billed"]:
            raise unprocessable("not_payable", "该管护观察周期不可付或已计费")
        care_period_index = int(period_index)
        basis["period_start"] = period["period_start"]
        basis["period_end"] = period["period_end"]
    elif kind == "replant_fee":
        replant_id = basis.get("replant_id")
        replant = next((r for r in item["replants"] if r["replant_id"] == replant_id), None)
        if item["blockers"] or not replant:
            raise unprocessable("not_payable", "该补植作业不可付或已计费")
        if amount != replant["amount_cents"]:
            raise unprocessable("amount_mismatch", "补植费金额必须与补植作业记录一致")
        basis["fee_kind"] = replant["fee_kind"]
        basis["responsible_party"] = replant["responsible_party"]

    contract = get_contract(ctx.db, unit["contract_id"])
    payee = contract["builder_id"]
    if kind == "care_fee" or (kind == "replant_fee" and basis.get("fee_kind") == "maintenance_care"):
        payee = contract["maintainer_id"]
    key = _idempotency_key(payload, unit["id"], kind)
    dup = ctx.db.execute("SELECT * FROM payments WHERE idempotency_key=?", (key,)).fetchone()
    if dup:
        raise conflict("duplicate_payment_request", f"相同付款申请已受理: {dup['id']}")
    payment_id = new_id("pay")
    ctx.db.execute(
        "INSERT INTO payments(id,unit_id,contract_id,kind,payee_id,amount_cents,status,basis_json,"
        "replant_id,care_period_index,idempotency_key,created_by,created_at) "
        "VALUES(?,?,?,?,?,?,'created',?,?,?,?,?,?)",
        (payment_id, unit["id"], unit["contract_id"], kind, payee, amount,
         dumps(basis), replant_id, care_period_index, key, ctx.party_id, ctx.now_iso))
    ctx.event("payment.created", {"payment_id": payment_id, "unit_id": unit["id"], "kind": kind,
                                  "amount_cents": amount})
    return {"payment_id": payment_id, "status": "created", "amount_cents": amount,
            "note": "已受理并占用付款额度，尚未派发；已成功的付款不能再付一次"}


def list_payments(ctx, payload, query):
    sql = "SELECT * FROM payments"
    args = []
    if query.get("unit_id"):
        sql += " WHERE unit_id=?"
        args.append(query["unit_id"])
    rows = ctx.db.execute(sql + " ORDER BY id", args).fetchall()
    out = []
    for p in rows:
        item = dict(p)
        item["commands"] = [dict(c) for c in ctx.db.execute(
            "SELECT * FROM payment_commands WHERE payment_id=? ORDER BY attempt", (p["id"],))]
        out.append(item)
    return {"payments": out}


def dispatch_payment(ctx, payload, query):
    """派发到合成资金接口。504 断线项保持 dispatched 并继续占额度，不重复派发。"""
    ctx.require_role("proposer")
    require_fields(payload, ["payment_id", "command_id"])
    payment = ctx.db.execute("SELECT * FROM payments WHERE id=?",
                             (payload["payment_id"],)).fetchone()
    if not payment:
        raise not_found("付款不存在")
    if payment["status"] in ("paid",):
        raise conflict("already_paid", f"付款 {payment['id']} 已到账，不能再付一次")
    existing = ctx.db.execute(
        "SELECT * FROM payment_commands WHERE command_id=? ORDER BY attempt",
        (payload["command_id"],)).fetchall()
    for c in existing:
        if c["remote_state"] in (None, "pending", "paid"):
            if c["payment_id"] != payment["id"]:
                raise conflict("command_bound_to_other_payment",
                               f"流水 {payload['command_id']} 已被付款 {c['payment_id']} 占用且未明确失败")
            raise conflict("dispatch_in_flight",
                           "该流水仍在受理/到账链路中，断线项只能轮询不能重发")
        if c["remote_state"] == "rejected" and c["payment_id"] != payment["id"]:
            # 明确失败的流水允许用于新付款的重新办理，attempt 继续递增
            continue
    if payment["status"] in ("dispatched",):
        raise conflict("dispatch_in_flight", "付款已派发且结果未明，请先同步回执")
    request_payload = {"unit": payment["unit_id"], "amount": payment["amount_cents"] // 100}
    attempt = (max((c["attempt"] for c in existing), default=0)) + 1
    result = ctx.fund.send(payload["command_id"], "payment", request_payload)
    cmd_id = new_id("pcmd")
    ctx.db.execute(
        "INSERT INTO payment_commands(id,payment_id,command_id,attempt,http_status,remote_state,"
        "detail_json,dispatched_at,synced_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (cmd_id, payment["id"], payload["command_id"], attempt, result["http_status"], None,
         dumps(result["body"]), ctx.now_iso, ctx.now_iso))
    ctx.db.execute("UPDATE payments SET status='dispatched' WHERE id=?", (payment["id"],))
    ctx.event("payment.dispatched", {"payment_id": payment["id"],
                                     "command_id": payload["command_id"],
                                     "http_status": result["http_status"], "attempt": attempt})
    return {"payment_id": payment["id"], "command_id": payload["command_id"], "attempt": attempt,
            "http_status": result["http_status"], "status": "dispatched",
            "note": "504 表示响应不明，协作方可能已受理；本项继续占用额度，请用同步接口轮询"}


def sync_payment(ctx, payload, query):
    """轮询原始流水，以资金接口回执更新到账/失败；受理与到账分开。"""
    ctx.require_role("proposer", "reviewer")
    require_fields(payload, ["payment_id"])
    payment = ctx.db.execute("SELECT * FROM payments WHERE id=?",
                             (payload["payment_id"],)).fetchone()
    if not payment:
        raise not_found("付款不存在")
    cmd = ctx.db.execute(
        "SELECT * FROM payment_commands WHERE payment_id=? ORDER BY attempt DESC, id DESC LIMIT 1",
        (payment["id"],)).fetchone()
    if not cmd:
        raise conflict("not_dispatched", "付款尚未派发，无流水可同步")
    observation = ctx.fund.fetch(cmd["command_id"])
    state = observation.get("state")
    ctx.db.execute(
        "UPDATE payment_commands SET remote_state=?, voucher=?, detail_json=?, synced_at=? WHERE id=?",
        (state, observation.get("voucher"), dumps(observation), ctx.now_iso, cmd["id"]))
    if state == "paid":
        if payment["status"] != "paid":
            ctx.db.execute("UPDATE payments SET status='paid' WHERE id=?", (payment["id"],))
        ctx.event("payment.paid", {"payment_id": payment["id"], "voucher": observation.get("voucher")})
    elif state == "rejected":
        # 仅明确失败的项释放额度、允许重新办理
        ctx.db.execute("UPDATE payments SET status='failed' WHERE id=? AND status!='paid'",
                       (payment["id"],))
        ctx.event("payment.failed", {"payment_id": payment["id"],
                                     "reason": observation.get("reason")})
    return {"payment_id": payment["id"], "command_id": cmd["command_id"],
            "remote_state": state, "observation": observation,
            "payment_status": ctx.db.execute(
                "SELECT status FROM payments WHERE id=?", (payment["id"],)).fetchone()["status"]}


def fund_trace(ctx, payload, query):
    """按资金流水 ID 查原始观测事实（原流水查询）。"""
    ctx.require_role("proposer", "reviewer")
    require_fields(payload, ["command_id"])
    observation = ctx.fund.fetch(payload["command_id"])
    local = ctx.db.execute(
        "SELECT p.* FROM payment_commands c JOIN payments p ON p.id=c.payment_id "
        "WHERE c.command_id=? ORDER BY c.attempt", (payload["command_id"],)).fetchall()
    return {"command_id": payload["command_id"], "remote_observation": observation,
            "local_payments": [dict(r) for r in local]}


def propose_recovery(ctx, payload, query):
    """已支付金额保留事实：追偿/抵扣另立记录，不把旧付款改为零。"""
    ctx.require_role("proposer")
    require_fields(payload, ["payment_id", "amount_cents", "method"])
    if payload["method"] not in ("recover", "deduct"):
        raise bad_request("invalid_method", "method 只能是 recover 或 deduct")
    payment = ctx.db.execute("SELECT * FROM payments WHERE id=?",
                             (payload["payment_id"],)).fetchone()
    if not payment:
        raise not_found("付款不存在")
    if payment["status"] not in ("paid", "partially_recovered"):
        raise unprocessable("payment_not_paid", "只有已到账付款才能办理追偿/抵扣")
    amount = require_amount_cents(payload)
    from .rules import recovered_total
    already = recovered_total(ctx.db, payment["id"])
    if already + amount > payment["amount_cents"]:
        raise unprocessable("recovery_exceeds_payment", "追偿累计不能超过原付款金额")
    rec_id = new_id("rec")
    ctx.db.execute(
        "INSERT INTO recoveries(id,payment_id,amount_cents,method,note,proposed_by,created_at,status) "
        "VALUES(?,?,?,?,?,?,?,'proposed')",
        (rec_id, payment["id"], amount, payload["method"], payload.get("note"),
         ctx.party_id, ctx.now_iso))
    ctx.event("recovery.proposed", {"recovery_id": rec_id, "payment_id": payment["id"],
                                    "amount_cents": amount})
    return {"recovery_id": rec_id, "status": "proposed",
            "note": "原付款事实保留，等待复核人确认"}


def confirm_recovery(ctx, payload, query):
    ctx.require_role("reviewer")
    require_fields(payload, ["recovery_id"])
    rec = ctx.db.execute("SELECT * FROM recoveries WHERE id=?",
                         (payload["recovery_id"],)).fetchone()
    if not rec:
        raise not_found("追偿记录不存在")
    if rec["status"] != "proposed":
        raise conflict("recovery_locked", f"追偿 {rec['id']} 状态为 {rec['status']}")
    ctx.db.execute(
        "UPDATE recoveries SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
        (ctx.party_id, ctx.now_iso, rec["id"]))
    payment = ctx.db.execute("SELECT * FROM payments WHERE id=?", (rec["payment_id"],)).fetchone()
    from .rules import recovered_total
    total = recovered_total(ctx.db, payment["id"])
    new_status = "recovered" if total >= payment["amount_cents"] else "partially_recovered"
    ctx.db.execute("UPDATE payments SET status=? WHERE id=?", (new_status, payment["id"]))
    ctx.event("recovery.confirmed", {"recovery_id": rec["id"], "payment_id": payment["id"],
                                     "payment_status": new_status})
    return {"recovery_id": rec["id"], "status": "confirmed", "payment_status": new_status}


# ---------------------------------------------------------------------------
# 同步结清：观察期到期、管护转交、付款回执按同一批实际责任结清
# ---------------------------------------------------------------------------


def run_settlement(ctx, payload, query):
    ctx.require_role("proposer", "reviewer")
    units = ctx.db.execute("SELECT * FROM units ORDER BY id").fetchall()
    items, inconsistencies = [], []
    for u in units:
        view = payment_view(ctx.db, u, ctx.now)
        tk = active_takeover(ctx.db, u["id"])
        if tk:
            due = add_days(parse_dt(tk["starts_at"]), OBSERVATION_DAYS)
            due_passed = ctx.now >= due
            payments = ctx.db.execute(
                "SELECT id,kind,status FROM payments WHERE unit_id=? ORDER BY id", (u["id"],)).fetchall()
            # 观察期已到期却仍有在途付款 -> 各表显示不一致，列入待办而不是分别标完成
            if due_passed:
                items.append({"unit_id": u["id"], "kind": "observation", "ref_id": tk["id"],
                              "state": "due"})
                for p in payments:
                    if p["status"] in ("created", "dispatched"):
                        inconsistencies.append({
                            "unit_id": u["id"], "payment_id": p["id"], "kind": p["kind"],
                            "issue": "观察期已到期但付款回执未结清，不能分别标记完成",
                        })
                    items.append({"unit_id": u["id"], "kind": f"payment_{p['kind']}",
                                  "ref_id": p["id"], "state": p["status"]})
            else:
                items.append({"unit_id": u["id"], "kind": "observation", "ref_id": tk["id"],
                              "state": "open", "due_at": due.isoformat()})
        # 管护转交与缺陷状态一致性
        for d in view["open_defects"]:
            items.append({"unit_id": u["id"], "kind": "defect", "ref_id": d["id"],
                          "state": d["status"]})
            if tk and d["cause"] == "construction":
                items.append({"unit_id": u["id"], "kind": "builder_responsibility",
                              "ref_id": d["id"], "state": "retained"})
    settlement_id = new_id("stl")
    scope = {"at": ctx.now_iso, "unit_count": len(units)}
    result = {"items": items, "inconsistencies": inconsistencies}
    ctx.db.execute("INSERT INTO settlements(id,run_at,scope_json,result_json) VALUES(?,?,?,?)",
                   (settlement_id, ctx.now_iso, dumps(scope), dumps(result)))
    for it in items:
        ctx.db.execute(
            "INSERT OR IGNORE INTO settlement_items(settlement_id,unit_id,kind,ref_id,state) "
            "VALUES(?,?,?,?,?)",
            (settlement_id, it["unit_id"], it["kind"], it["ref_id"], it["state"]))
    ctx.event("settlement.run", {"settlement_id": settlement_id,
                                 "inconsistency_count": len(inconsistencies)})
    return {"settlement_id": settlement_id, "scope": scope, "result": result}


# ---------------------------------------------------------------------------
# 时钟控制（本地演练，不等待真实季节）
# ---------------------------------------------------------------------------


def set_clock(ctx, payload, query):
    ctx.require_role("proposer", "reviewer", "acceptor")
    require_fields(payload, ["now_iso"])
    dt = parse_dt(payload["now_iso"])
    ctx.db.execute(
        "INSERT INTO kv(key,value) VALUES('clock.now_iso',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dt.isoformat(),))
    ctx.event("clock.set", {"now_iso": dt.isoformat()})
    return {"now_iso": dt.isoformat()}


def clear_clock(ctx, payload, query):
    ctx.require_role("proposer", "reviewer", "acceptor")
    ctx.db.execute("DELETE FROM kv WHERE key='clock.now_iso'")
    ctx.event("clock.cleared", {})
    return {"ok": True}


def get_clock(ctx, payload, query):
    return {"now_iso": ctx.now_iso, "source": "header" if ctx.clock.header_value else (
        "override" if ctx.db.execute(
            "SELECT 1 FROM kv WHERE key='clock.now_iso'").fetchone() else "real")}
