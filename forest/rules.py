"""领域规则：抽查证据适用性、成活事实、接管/观察期、分段付款可付性。

服务只检查证据是否满足合同约定的适用条件，不替验收人作现场判断；
没有观察值不等于死亡，未抽查面积不并入合格量。
"""
from __future__ import annotations

from decimal import Decimal

from .errors import unprocessable
from .timeutil import OBSERVATION_DAYS, add_days, parse_dt

# 合成合同固定抽查规则（见 docs/contracts.md）
RULES = {
    "min_groups": 2,          # 至少两组不同日期的独立抽查
    "min_sample_per_group": 10,  # 每组覆盖 10 株
    "min_planting_interval_days": 30,  # 第二次抽查距栽植不少于 30 天
    "survival_threshold": Decimal("0.90"),  # 成活率达到 90% 方可签认
    "observation_days": OBSERVATION_DAYS,
}
RETENTION_RATE = Decimal("0.20")

# 付款状态：仍占用额度（受理后未明确失败/追偿结清）
IN_FLIGHT = ("created", "dispatched")
PAYABLE_DONE = ("paid", "partially_recovered")


def group_survival(survey) -> Decimal:
    total = survey["alive_count"] + survey["dead_count"]
    if total <= 0:
        return Decimal(0)
    return Decimal(survey["alive_count"]) / Decimal(total)


def evaluate_evidence(surveys: list, planted_at) -> dict:
    """按合同规则检查一组抽查记录的适用性，返回失败原因明细而不是现场结论。"""
    failures: list[dict] = []
    groups = sorted(surveys, key=lambda s: (s["surveyed_at"], s["id"]))
    if len(groups) < RULES["min_groups"]:
        failures.append({
            "code": "insufficient_groups",
            "message": f"抽查组数不足：需要至少 {RULES['min_groups']} 组，现有 {len(groups)} 组",
        })
    dates = [parse_dt(g["surveyed_at"]).date() for g in groups]
    if len(set(dates)) != len(dates):
        failures.append({"code": "groups_same_date", "message": "两组抽查必须为不同日期的独立抽查"})
    for g in groups:
        sample = g["alive_count"] + g["dead_count"]
        if sample < RULES["min_sample_per_group"]:
            failures.append({
                "code": "sample_too_small",
                "survey_id": g["id"],
                "message": f"抽查 {g['id']} 仅覆盖 {sample} 株，每组至少 {RULES['min_sample_per_group']} 株",
            })
        ratio = group_survival(g)
        if ratio < RULES["survival_threshold"]:
            failures.append({
                "code": "survival_below_threshold",
                "survey_id": g["id"],
                "survival_ratio": str(ratio.quantize(Decimal("0.01"))),
                "message": f"抽查 {g['id']} 成活率 {ratio} 低于合同约定的 0.90",
            })
    if groups:
        planting = parse_dt(planted_at)
        last_at = parse_dt(groups[-1]["surveyed_at"])
        gap_days = (last_at.date() - planting.date()).days
        if gap_days < RULES["min_planting_interval_days"]:
            failures.append({
                "code": "interval_not_met",
                "message": f"末次抽查距栽植 {gap_days} 天，不足 {RULES['min_planting_interval_days']} 天",
            })
    return {
        "applicable": not failures,
        "failures": failures,
        "group_count": len(groups),
        "rules": {
            "min_groups": RULES["min_groups"],
            "min_sample_per_group": RULES["min_sample_per_group"],
            "min_planting_interval_days": RULES["min_planting_interval_days"],
            "survival_threshold": str(RULES["survival_threshold"]),
        },
    }


def money(value) -> int:
    """元（支持字符串/数值）转分，整数分。"""
    return int((Decimal(str(value)) * 100).to_integral_value())


def rate_of(rate_text: str) -> Decimal:
    return Decimal(rate_text)


def accepted_m2(db, unit_id: str):
    row = db.execute(
        "SELECT COALESCE(SUM(accepted_m2),0) AS m FROM acceptances "
        "JOIN declarations d ON d.id=acceptances.declaration_id "
        "WHERE acceptances.unit_id=? AND d.status='accepted' AND acceptances.decision='accept'",
        (unit_id,),
    ).fetchone()
    return float(row["m"])


def active_takeover(db, unit_id: str):
    return db.execute(
        "SELECT * FROM takeovers WHERE unit_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (unit_id,),
    ).fetchone()


def open_defects(db, unit_id: str):
    return db.execute(
        "SELECT * FROM defects WHERE unit_id=? AND status IN ('open','cause_set','replanting') ORDER BY id",
        (unit_id,),
    ).fetchall()


def active_holds(db, unit_id: str, contract_id: str):
    return db.execute(
        "SELECT * FROM holds WHERE status='active' AND "
        "((scope='unit' AND unit_id=?) OR (scope='contract' AND contract_id=?)) ORDER BY id",
        (unit_id, contract_id),
    ).fetchall()


def missing_facilities(db, unit_id: str):
    return db.execute(
        "SELECT f.* FROM unit_dependencies ud JOIN facilities f ON f.id=ud.facility_id "
        "WHERE ud.unit_id=? AND f.accepted_at IS NULL ORDER BY f.id",
        (unit_id,),
    ).fetchall()


def in_flight_payments(db, unit_id: str, kind: str | None = None):
    sql = ("SELECT * FROM payments WHERE unit_id=? AND status IN ('created','dispatched')")
    args: list = [unit_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return db.execute(sql + " ORDER BY id", args).fetchall()


def paid_total(db, unit_id: str, kinds: tuple[str, ...]):
    row = db.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS c FROM payments "
        "WHERE unit_id=? AND kind IN (%s) AND status IN ('paid','partially_recovered')"
        % ",".join("?" for _ in kinds),
        (unit_id, *kinds),
    ).fetchone()
    return int(row["c"])


def recovered_total(db, payment_id: str) -> int:
    row = db.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS c FROM recoveries "
        "WHERE payment_id=? AND status='confirmed'",
        (payment_id,),
    ).fetchone()
    return int(row["c"])


def _block_holds(holds) -> list[dict]:
    return [
        {"code": "hold_active", "hold_id": h["id"], "reason": h["reason"], "reason_code": h["reason_code"]}
        for h in holds
    ]


def payment_view(db, unit, now_dt) -> dict:
    """逐单元计算四类作业项的可付金额与不可付原因。只给依据，不自动放款。"""
    unit_id = unit["id"]
    contract_id = unit["contract_id"]
    current_m2 = float(unit["current_m2"] or unit["area_m2"])
    accepted = accepted_m2(db, unit_id)
    contract = db.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
    rate = rate_of(contract["retention_rate"])
    takeover = active_takeover(db, unit_id)
    holds = active_holds(db, unit_id, contract_id)
    facilities_missing = missing_facilities(db, unit_id)
    defects = open_defects(db, unit_id)
    construction_open = [d for d in defects if d["cause"] != "maintenance"]
    amount = int(unit["amount_cents"])
    share = Decimal(str(accepted)) / Decimal(str(current_m2)) if current_m2 > 0 else Decimal(0)
    gross_accepted = (Decimal(amount) * share).to_integral_value()
    retention_cap = int((gross_accepted * rate).to_integral_value())
    progress_cap = int(gross_accepted - retention_cap)

    view: dict = {
        "unit_id": unit_id,
        "parcel": unit["parcel"],
        "contract_id": contract_id,
        "current_m2": current_m2,
        "accepted_m2": accepted,
        "takeover_active": bool(takeover),
        "open_defects": [dict(d) for d in defects],
        "missing_facilities": [f["id"] for f in facilities_missing],
        "active_holds": [h["id"] for h in holds],
        "items": {},
    }

    # 1. 进度款（扣除保留款部分），按已接收比例计算
    blockers = _block_holds(holds)
    if accepted <= 0:
        blockers.append({"code": "no_accepted_area", "message": "尚无验收人签认接收的面积"})
    inflight = in_flight_payments(db, unit_id, "progress")
    if inflight:
        blockers.append({"code": "payment_in_flight", "payments": [p["id"] for p in inflight]})
    paid_progress = paid_total(db, unit_id, ("progress",))
    view["items"]["progress"] = {
        "eligible": not blockers and progress_cap > paid_progress,
        "amount_cents": max(0, progress_cap - paid_progress),
        "cap_cents": progress_cap,
        "paid_cents": paid_progress,
        "partial": 0 < accepted < current_m2,
        "blockers": blockers,
    }

    # 2. 保留款：按已接收范围计额；观察期满 + 整改结清 + 显式依赖设施已接收
    #    未接收部分不产生保留款，但不阻断已接收部分按约释放
    blockers = _block_holds(holds)
    if accepted <= 0:
        blockers.append({"code": "no_accepted_area", "message": "尚无验收人签认接收的面积"})
    notes = []
    if 0 < accepted < current_m2:
        notes.append({"code": "partial_acceptance",
                      "message": "单元仍有未接收面积，保留款仅按已接收范围计算，不扩及未接收部分"})
    if not takeover:
        blockers.append({"code": "no_takeover", "message": "管护队尚未接管，观察期未起算"})
    else:
        due = add_days(parse_dt(takeover["starts_at"]), OBSERVATION_DAYS)
        view["observation_due_at"] = due.isoformat()
        if now_dt < due:
            blockers.append({
                "code": "observation_open",
                "message": f"观察期未满：{due.isoformat()} 到期",
                "observation_due_at": due.isoformat(),
            })
    if construction_open:
        blockers.append({
            "code": "open_construction_defect",
            "defects": [d["id"] for d in construction_open],
            "message": "施工缺陷整改未结（含尚未归责的缺陷）",
        })
    if facilities_missing:
        blockers.append({
            "code": "facility_not_accepted",
            "facilities": [f["id"] for f in facilities_missing],
            "message": "单元显式依赖的共享防护设施尚未接收，不得借其他区域验收提前释放",
        })
    inflight = in_flight_payments(db, unit_id, "retention")
    if inflight:
        blockers.append({"code": "payment_in_flight", "payments": [p["id"] for p in inflight]})
    paid_retention = paid_total(db, unit_id, ("retention",))
    view["items"]["retention"] = {
        "eligible": not blockers and retention_cap > paid_retention,
        "amount_cents": max(0, retention_cap - paid_retention),
        "cap_cents": retention_cap,
        "paid_cents": paid_retention,
        "partial": 0 < accepted < current_m2,
        "notes": notes,
        "blockers": blockers,
    }

    # 3. 管护作业费：按接管后的完整观察周期计期，金额由合同经办人在办理时给出
    blockers = _block_holds(holds)
    periods: list[dict] = []
    if not takeover:
        blockers.append({"code": "no_takeover", "message": "管护队尚未接管，无管护作业可计费"})
    else:
        start = parse_dt(takeover["starts_at"])
        elapsed_days = (now_dt.date() - start.date()).days
        completed = max(0, elapsed_days // OBSERVATION_DAYS)
        paid_periods = {
            r["care_period_index"] for r in db.execute(
                "SELECT care_period_index FROM payments WHERE unit_id=? AND kind='care_fee' "
                "AND care_period_index IS NOT NULL AND status IN "
                "('paid','partially_recovered','dispatched','created')",
                (unit_id,)).fetchall()
        }
        used = {idx for idx in paid_periods if idx is not None}
        for idx in range(completed):
            period_start = add_days(start, idx * OBSERVATION_DAYS)
            periods.append({
                "period_index": idx,
                "period_start": period_start.isoformat(),
                "period_end": add_days(period_start, OBSERVATION_DAYS).isoformat(),
                "billed": idx in used,
            })
    inflight = in_flight_payments(db, unit_id, "care_fee")
    if inflight:
        blockers.append({"code": "payment_in_flight", "payments": [p["id"] for p in inflight]})
    view["items"]["care_fee"] = {
        "eligible": not blockers and any(not p["billed"] for p in periods),
        "periods": periods,
        "blockers": blockers,
    }

    # 4. 补植费：按已实施且未支付的补植作业逐笔办理，施工整改与正常养护费用来源不同
    blockers = _block_holds(holds)
    replant_rows = db.execute(
        "SELECT r.* FROM replants r WHERE r.unit_id=? AND r.status IN ('performed','verified') "
        "AND NOT EXISTS(SELECT 1 FROM payments p WHERE p.kind='replant_fee' "
        "AND p.replant_id=r.id AND p.status IN "
        "('paid','partially_recovered','dispatched','created')) ORDER BY r.id",
        (unit_id,),
    ).fetchall()
    inflight = in_flight_payments(db, unit_id, "replant_fee")
    if inflight:
        blockers.append({"code": "payment_in_flight", "payments": [p["id"] for p in inflight]})
    view["items"]["replant_fee"] = {
        "eligible": not blockers and bool(replant_rows),
        "replants": [
            {
                "replant_id": r["id"],
                "fee_kind": r["fee_kind"],
                "responsible_party": r["responsible_party"],
                "amount_cents": r["fee_cents"],
            }
            for r in replant_rows
        ],
        "blockers": blockers,
    }
    return view


def require_amount_cents(payload: dict, field: str = "amount_cents") -> int:
    if field not in payload:
        raise unprocessable("amount_required", f"缺少 {field}（整数分）")
    try:
        value = int(payload[field])
    except (TypeError, ValueError):
        raise unprocessable("amount_invalid", f"{field} 必须为整数分")
    if value <= 0:
        raise unprocessable("amount_invalid", f"{field} 必须为正数")
    return value
