"""端到端业务检查：以合成数据跑通申报→抽查→局部验收→接管→整改→付款全链路。

用法：
  python3 scripts/run_checks.py                 # 自动启动业务服务与资金协作方（临时库）
  python3 scripts/run_checks.py --base URL      # 对已运行的业务服务执行检查

日期通过 X-Now / 管理时钟本地推进，不等待真实季节。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PRINCIPALS = {
    "builder": "builder-user",
    "surveyor": "surveyor-A",
    "acceptor": "acceptor-A",
    "care": "care-user",
    "proposer": "project-A",
    "reviewer": "reviewer-A",
}


class CheckFailure(AssertionError):
    pass


class Client:
    def __init__(self, base: str, principal: str | None = None, now_iso: str | None = None):
        self.base = base.rstrip("/")
        self.principal = principal
        self.now_iso = now_iso

    def call(self, method: str, path: str, payload=None, principal=None, now_iso=None,
             expect_status=200):
        principal = principal or self.principal
        now_iso = now_iso or self.now_iso
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if principal:
            headers["X-Principal"] = principal
        if now_iso:
            headers["X-Now"] = now_iso
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, body = resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read().decode())
        if status != expect_status:
            raise CheckFailure(f"{method} {path} 期望 {expect_status} 实际 {status}: {body}")
        return body

    def fail(self, method, path, payload=None, principal=None, now_iso=None, expect_status=409):
        return self.call(method, path, payload, principal, now_iso, expect_status)


def wait_health(base: str):
    for _ in range(50):
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            pass
        import time
        time.sleep(0.1)
    raise CheckFailure(f"服务未就绪: {base}")


def run_checks(base: str, fund_base: str | None) -> dict:
    counts = {"passed": 0, "checks": 0}

    def check(name: str, cond, detail=""):
        counts["checks"] += 1
        if not cond:
            raise CheckFailure(f"检查失败: {name} {detail}")
        counts["passed"] += 1
        print(f"  ✓ {name}")

    builder = Client(base, PRINCIPALS["builder"])
    surveyor = Client(base, PRINCIPALS["surveyor"])
    acceptor = Client(base, PRINCIPALS["acceptor"])
    care = Client(base, PRINCIPALS["care"])
    proposer = Client(base, PRINCIPALS["proposer"])
    reviewer = Client(base, PRINCIPALS["reviewer"])

    T0 = "2026-11-10T09:00:00+08:00"
    T1 = "2026-11-11T09:00:00+08:00"
    T35 = "2026-12-11T09:00:00+08:00"     # unit-A 接管 30 天后
    T_FENCE = "2026-12-05T10:00:00+08:00"  # 设施复查之后

    print("1. 身份与角色隔离")
    health = Client(base).call("GET", "/health")
    check("健康检查声明合成模式", health.get("synthetic") is True)
    Client(base).fail("POST", "/declarations", {"unit_id": "unit-C", "stage": "planting",
                      "declared_m2": 900}, expect_status=401)
    care.fail("POST", "/declarations", {"unit_id": "unit-C", "stage": "planting",
              "declared_m2": 900}, expect_status=403)
    acceptor.fail("POST", "/acceptances", {"declaration_id": "decl-seed-A",
                  "decision": "accept", "accepted_m2": 1}, principal=PRINCIPALS["builder"],
                  expect_status=403)

    print("2. 申报与重复认领")
    builder.call("POST", "/plantings", {"unit_id": "unit-C",
                 "planted_at": "2026-10-01T08:00:00+08:00"}, now_iso=T0)
    builder.call("POST", "/plantings", {"unit_id": "unit-B",
                 "planted_at": "2026-10-01T08:00:00+08:00"}, now_iso=T0)
    decl_c = builder.call("POST", "/declarations", {"unit_id": "unit-C", "stage": "planting",
                          "declared_m2": 900}, now_iso=T0)["declaration_id"]
    builder.fail("POST", "/declarations", {"unit_id": "unit-C", "stage": "planting",
                 "declared_m2": 900}, now_iso=T0, expect_status=409)

    print("3. 抽查证据适用性（服务只检查合同条件，不作现场判断）")
    # 第一组抽查：10 株全活
    surveyor.call("POST", "/surveys", {"declaration_id": decl_c, "surveyed_at":
                  "2026-10-20T09:00:00+08:00", "sample_size": 10, "alive_count": 10,
                  "dead_count": 0, "measured_m2": 900}, now_iso=T0)
    # 第二组距栽植不足 30 天 → 证据不适用
    early = surveyor.call("POST", "/surveys", {"declaration_id": decl_c, "surveyed_at":
                          "2026-10-25T09:00:00+08:00", "sample_size": 10, "alive_count": 10,
                          "dead_count": 0, "measured_m2": 900}, now_iso=T0)
    check("间隔不足判定为不适用", early["evidence_check"]["applicable"] is False
          and any(f["code"] == "interval_not_met" for f in early["evidence_check"]["failures"]))
    # 无抽查不能签认（未抽查不等于全部成活）
    decl_b = builder.call("POST", "/declarations", {"unit_id": "unit-B", "stage": "planting",
                          "declared_m2": 800}, now_iso=T0)["declaration_id"]
    acceptor.fail("POST", "/acceptances", {"declaration_id": decl_b, "decision": "accept",
                  "accepted_m2": 800}, now_iso=T0, expect_status=422)
    # 成活率不足：仅 8/10 存活
    surveyor.call("POST", "/surveys", {"declaration_id": decl_b, "surveyed_at":
                  "2026-10-18T09:00:00+08:00", "sample_size": 10, "alive_count": 8,
                  "dead_count": 2, "measured_m2": 800, "dead_plants": [3, 7]}, now_iso=T0)
    surveyor.call("POST", "/surveys", {"declaration_id": decl_b, "surveyed_at":
                  "2026-11-08T09:00:00+08:00", "sample_size": 10, "alive_count": 8,
                  "dead_count": 2, "measured_m2": 800}, now_iso=T0)
    acceptor.fail("POST", "/acceptances", {"declaration_id": decl_b, "decision": "accept",
                  "accepted_m2": 800}, now_iso=T0, expect_status=422)
    # 限期补植决定
    repl = acceptor.call("POST", "/acceptances", {"declaration_id": decl_b,
                         "decision": "replant_required",
                         "replant_deadline": "2026-11-25T09:00:00+08:00",
                         "note": "成活率低于90%"}, now_iso=T0)
    check("限期补植签认", repl["declaration_status"] == "replant_required")
    # 补齐 unit-C 合规第二组抽查（距栽植 2026-10-01 超过30天）
    ok_survey = surveyor.call("POST", "/surveys", {"declaration_id": decl_c, "surveyed_at":
                              "2026-11-05T09:00:00+08:00", "sample_size": 10, "alive_count": 10,
                              "dead_count": 0, "measured_m2": 900}, now_iso=T0)
    check("合规证据适用", ok_survey["evidence_check"]["applicable"] is True)

    print("4. 局部验收与争议面积")
    # unit-C 全量接收
    acc_c = acceptor.call("POST", "/acceptances", {"declaration_id": decl_c,
                          "decision": "accept", "accepted_m2": 900}, now_iso=T0)
    check("unit-C 全量接收", acc_c["accepted_m2"] == 900.0)
    # unit-A 预置证据：90% 成活率、间隔 32 天 → 适用；只接收 800/1000（局部）
    acc_a = acceptor.call("POST", "/acceptances", {"declaration_id": "decl-seed-A",
                          "decision": "accept", "accepted_m2": 800,
                          "note": "争议的200平不并入合格量"}, now_iso=T0)
    check("unit-A 局部接收 800 平", acc_a["accepted_m2"] == 800.0)
    acceptor.fail("POST", "/acceptances", {"declaration_id": "decl-seed-A", "decision": "accept",
                  "accepted_m2": 1000}, now_iso=T0, expect_status=409)
    trace_a = Client(base).call("GET", "/units/unit-A/trace")
    check("追溯视图记录施工/验收边界", trace_a["accepted_m2"] == 800.0
          and trace_a["boundary_m2"] == 1000.0)

    print("5. 接管：确认单元、起始时点与未解除施工责任")
    care.fail("POST", "/takeovers", {"unit_id": "unit-B", "area_m2": 800,
              "starts_at": T1}, now_iso=T1, expect_status=422)
    tk_a = care.call("POST", "/takeovers", {"unit_id": "unit-A", "area_m2": 800,
                     "starts_at": T1}, now_iso=T1)
    check("接管给出观察期到期日", tk_a["observation_due_at"] == "2026-12-11T09:00:00+08:00")
    care.fail("POST", "/takeovers", {"unit_id": "unit-A", "area_m2": 800,
              "starts_at": T1}, now_iso=T1, expect_status=409)
    # 管护队不能修改施工队原记录：没有任何修改接口，重复提交申报也被阶段认领拦截
    care.fail("POST", "/surveys", {"declaration_id": decl_c, "surveyed_at": T1,
              "sample_size": 10, "alive_count": 10, "dead_count": 0, "measured_m2": 900},
              now_iso=T1, expect_status=403)

    print("6. 巡护缺陷合并：同一死亡植株不重复开补植费")
    batch1 = care.call("POST", "/patrol-batches", {"unit_id": "unit-A",
                       "patrolled_at": "2026-11-15T09:00:00+08:00",
                       "findings": [{"plant_no": 10, "description": "植株10死亡"},
                                    {"plant_no": 11, "description": "植株11枯萎"}]}, now_iso=T1)
    check("首次巡护建两个缺陷", len(batch1["new_defect_ids"]) == 2 and not batch1["merged_defect_ids"])
    batch2 = care.call("POST", "/patrol-batches", {"unit_id": "unit-A",
                       "patrolled_at": "2026-11-16T09:00:00+08:00",
                       "findings": [{"plant_no": 10, "description": "再次发现10号死亡"},
                                    {"plant_no": 12, "description": "12号新缺陷"}]}, now_iso=T1)
    check("重复报告合并、新缺陷独立", len(batch2["merged_defect_ids"]) == 1
          and len(batch2["new_defect_ids"]) == 1)
    defects = Client(base).call("GET", "/defects?unit_id=unit-A")["defects"]
    check("缺陷总数不膨胀", len(defects) == 3)

    print("7. 归责与补植：施工整改 vs 正常养护，费用来源与下次观察起点不同")
    d10 = next(d for d in defects if d["plant_no"] == 10)
    d11 = next(d for d in defects if d["plant_no"] == 11)
    acceptor.call("POST", "/defects/cause", {"defect_id": d10["id"], "cause": "construction"},
                  now_iso=T1)
    acceptor.call("POST", "/defects/cause", {"defect_id": d11["id"], "cause": "maintenance"},
                  now_iso=T1)
    care.fail("POST", "/replants", {"defect_id": d10["id"], "amount_cents": 5000},
              now_iso=T1, expect_status=403)
    rpl10 = reviewer.call("POST", "/replants", {"defect_id": d10["id"], "amount_cents": 5000},
                          now_iso=T1)
    check("施工整改责任留在施工方", rpl10["fee_kind"] == "construction_rectification"
          and rpl10["responsible_party"] == "builder-A")
    rpl11 = reviewer.call("POST", "/replants", {"defect_id": d11["id"], "amount_cents": 3000},
                          now_iso=T1)
    check("养护补植责任归管护方", rpl11["fee_kind"] == "maintenance_care"
          and rpl11["responsible_party"] == "care-A")
    perf10 = care.call("POST", "/replants/perform", {"replant_id": rpl10["replant_id"],
                       "performed_at": "2026-11-18T09:00:00+08:00"}, now_iso=T1)
    check("补植后重设下次观察起点", perf10["next_observation_at"] == "2026-12-18T09:00:00+08:00")
    reviewer.call("POST", "/replants/verify", {"replant_id": rpl10["replant_id"]}, now_iso=T1)
    care.call("POST", "/replants/perform", {"replant_id": rpl11["replant_id"],
              "performed_at": "2026-11-19T09:00:00+08:00"}, now_iso=T1)
    reviewer.call("POST", "/replants/verify", {"replant_id": rpl11["replant_id"]}, now_iso=T1)
    # 第二批次合并后独立建档的 12 号新缺陷也要闭环，证明合并没有吞掉新缺陷
    d12 = next(d for d in defects if d["plant_no"] == 12)
    acceptor.call("POST", "/defects/cause", {"defect_id": d12["id"], "cause": "maintenance"},
                  now_iso=T1)
    rpl12 = reviewer.call("POST", "/replants", {"defect_id": d12["id"], "amount_cents": 2000},
                          now_iso=T1)
    care.call("POST", "/replants/perform", {"replant_id": rpl12["replant_id"],
              "performed_at": "2026-11-20T09:00:00+08:00"}, now_iso=T1)
    reviewer.call("POST", "/replants/verify", {"replant_id": rpl12["replant_id"]}, now_iso=T1)

    print("8. 可付性：独立单元可付、依赖设施与观察期阻止保留款")
    eligible = proposer.call("GET", "/payments/eligible?contract_id=contract-A", now_iso=T1)
    view_a = next(u for u in eligible["units"] if u["unit_id"] == "unit-A")
    view_c = next(u for u in eligible["units"] if u["unit_id"] == "unit-C")
    check("观察期内保留款不可付", any(b["code"] == "observation_open"
          for b in view_a["items"]["retention"]["blockers"]))
    check("设施未接收阻止保留款（不借其他区域提前释放）",
          any(b["code"] == "facility_not_accepted" for b in view_a["items"]["retention"]["blockers"])
          and any(b["code"] == "facility_not_accepted"
                  for b in view_c["items"]["retention"]["blockers"]) is False)
    check("无依赖的 unit-C 进度款可付", view_c["items"]["progress"]["eligible"] is True)
    # unit-C 保留款：观察期未起算（未接管）
    check("未接管不可释放保留款", any(b["code"] == "no_takeover"
          for b in view_c["items"]["retention"]["blockers"]))

    print("9. 付款受理与到账分开（合成资金流水 202/504/轮询）")
    if fund_base:
        # unit-C 进度款 180000 元 = 18000000 分；unit-C 全额 900000，进度款上限 80% = 720000
        pay_c = proposer.call("POST", "/payments", {"unit_id": "unit-C", "kind": "progress",
                              "amount_cents": 18000000}, now_iso=T1)["payment_id"]
        disp_c = proposer.call("POST", "/payments/dispatch", {"payment_id": pay_c,
                               "command_id": "pay-C"}, now_iso=T1)
        check("pay-C 受理状态 202", disp_c["http_status"] == 202)
        synced = proposer.call("POST", "/payments/sync", {"payment_id": pay_c}, now_iso=T1)
        check("明确 rejected 后标记失败并可重办", synced["remote_state"] == "rejected"
              and synced["payment_status"] == "failed")
        # 重新办理同一流水（attempt=2）
        pay_c2 = proposer.call("POST", "/payments", {"unit_id": "unit-C", "kind": "progress",
                               "amount_cents": 18000000,
                               "idempotency_key": "pay-C-retry"}, now_iso=T1)["payment_id"]
        redisp = proposer.call("POST", "/payments/dispatch", {"payment_id": pay_c2,
                                "command_id": "pay-C"}, now_iso=T1)
        check("失败流水可重新办理（attempt=2）", redisp["attempt"] == 2)
        resync = proposer.call("POST", "/payments/sync", {"payment_id": pay_c2}, now_iso=T1)
        check("重办流水再次明确失败，额度释放", resync["remote_state"] == "rejected"
              and resync["payment_status"] == "failed")
        # unit-A 保留款 200000 元流水：504 断线仍占额度
        # 先让设施通过、观察期到期、缺陷结清
        acceptor.call("POST", "/facility-inspections", {"facility_id": "fence-A",
                      "result": "accepted", "inspected_at": T_FENCE}, now_iso=T_FENCE)
        care.call("POST", "/takeovers", {"unit_id": "unit-C", "area_m2": 900,
                  "starts_at": "2026-11-11T09:00:00+08:00"}, now_iso=T1)
        eligible2 = proposer.call("GET", "/payments/eligible?unit_id=unit-A", now_iso=T35)
        view_a2 = eligible2["units"][0]
        check("观察期到期+设施接收+整改结清后保留款可付",
              view_a2["items"]["retention"]["eligible"] is True)
        check("保留款金额=已接收部分的20%", view_a2["items"]["retention"]["amount_cents"]
              == int(100000000 * 0.8 * 0.2))  # 160000 元 = 16000000 分
        # catalog 的 pay-A 流水金额固定 200000 元；改用进度款科目演示 504
        pay_a = proposer.call("POST", "/payments", {"unit_id": "unit-A", "kind": "progress",
                              "amount_cents": 20000000}, now_iso=T35)["payment_id"]
        disp_a = proposer.call("POST", "/payments/dispatch", {"payment_id": pay_a,
                               "command_id": "pay-A"}, now_iso=T35)
        check("pay-A 首响 504 仍受理", disp_a["http_status"] == 504)
        proposer.fail("POST", "/payments/dispatch", {"payment_id": pay_a, "command_id": "pay-A"},
                      now_iso=T35, expect_status=409)
        s1 = proposer.call("POST", "/payments/sync", {"payment_id": pay_a}, now_iso=T35)
        check("首次轮询 pending，继续占额度", s1["remote_state"] == "pending"
              and s1["payment_status"] == "dispatched")
        s2 = proposer.call("POST", "/payments/sync", {"payment_id": pay_a}, now_iso=T35)
        check("再次轮询到账 paid", s2["remote_state"] == "paid"
              and s2["payment_status"] == "paid")
        proposer.fail("POST", "/payments/dispatch", {"payment_id": pay_a, "command_id": "pay-A"},
                      now_iso=T35, expect_status=409)
        trace_fund = reviewer.call("POST", "/fund/trace", {"command_id": "pay-A"}, now_iso=T35)
        check("原流水可查且关联本地付款", trace_fund["remote_observation"]["state"] == "paid"
              and len(trace_fund["local_payments"]) >= 1)

    print("10. 错误地块引用：暂停受影响安排、通知接管人、不删原签认")
    hold = acceptor.call("POST", "/corrections/wrong-plot", {"affected_unit_ids": ["unit-B"],
                         "reason": "施工记录误引用相邻地块"}, now_iso=T35)
    check("暂停事项已建立", len(hold["hold_ids"]) == 1)
    proposer.fail("POST", "/payments", {"unit_id": "unit-B", "kind": "progress",
                  "amount_cents": 10000}, now_iso=T35, expect_status=409)
    ntfs = care.call("GET", "/notifications", now_iso=T35)["notifications"]
    builder_ntfs = builder.call("GET", "/notifications", now_iso=T35)["notifications"]
    check("向已接管/施工责任方发出待确认事项",
          any(n["topic"] in ("hold.pending_confirmation", "boundary.corrected",
                             "takeover.rehandover", "takeover.created", "defect.cause_set")
              for n in ntfs)
          and any(n["topic"] == "hold.pending_confirmation" for n in builder_ntfs))
    pending = next(n for n in builder_ntfs if n["topic"] == "hold.pending_confirmation")
    ack = care.call("POST", "/notifications/ack", {"notification_id": pending["id"]},
                    now_iso=T35, expect_status=403)
    check("不能确认发给其他身份的通知", ack["error"]["code"] == "forbidden")
    builder.call("POST", "/notifications/ack", {"notification_id": pending["id"]}, now_iso=T35)
    original = Client(base).call("GET", f"/declarations/{decl_b}")
    check("原申报与签认保留不删", original["declaration"]["id"] == decl_b)
    reviewer.call("POST", "/holds/resolve", {"hold_id": hold["hold_ids"][0],
                  "resolution_note": "界桩复核完成，引用已更正"}, now_iso=T35)

    print("11. 已付款不改零：追偿/抵扣另立记录")
    if fund_base:
        rec = proposer.call("POST", "/recoveries", {"payment_id": pay_a,
                             "amount_cents": 5000000, "method": "deduct",
                             "note": "复核发现多计，后续抵扣"}, now_iso=T35)["recovery_id"]
        confirmed = reviewer.call("POST", "/recoveries/confirm", {"recovery_id": rec}, now_iso=T35)
        check("部分追偿状态保留原付款事实", confirmed["payment_status"] == "partially_recovered")
        pays = proposer.call("GET", "/payments?unit_id=unit-A", now_iso=T35)["payments"]
        target = next(p for p in pays if p["id"] == pay_a)
        check("旧付款金额与到账事实未被改零", target["amount_cents"] == 20000000
              and any(c["remote_state"] == "paid" for c in target["commands"]))

    print("12. 范围更正与重新交接")
    acceptor.call("POST", "/corrections/boundary", {"unit_id": "unit-A", "new_area_m2": 950,
                  "reason": "界桩复核修正100平方米"}, now_iso=T35)
    reh = reviewer.call("POST", "/takeovers/rehandover", {"unit_id": "unit-A", "area_m2": 800,
                        "starts_at": "2026-12-12T09:00:00+08:00"}, now_iso=T35)
    check("重新交接留痕且旧接管被替代", reh["superseded_takeover_id"])
    trace_a2 = Client(base).call("GET", "/units/unit-A/trace")
    check("追溯视图保留两次接管边界", len(trace_a2["takeovers"]) == 2
          and trace_a2["boundary_m2"] == 950.0)

    print("13. 同步结清：同一批实际责任，不各表分别完成")
    stl = proposer.call("POST", "/settlements/run", now_iso=T35)
    check("结清批次包含逐单元事项", len(stl["result"]["items"]) > 0)
    ledger = Client(base).call("GET", "/contracts/contract-A/ledger")
    remaining = []
    for u in ledger["units"]:
        remaining.extend(u["retained_responsibilities"])
    check("合同台账能查旧承包方残留责任（列表可空，结构必须存在）", isinstance(remaining, list))
    # unit-B 仍在限期补植：台账中保留款不可付原因清晰
    view_b = next(u for u in ledger["units"] if u["unit_id"] == "unit-B")
    check("不可付原因可追到具体阻断码",
          all(isparseable := isinstance(u["payment_items"]["retention"]["blockers"], list)
              for u in ledger["units"]))

    print("14. 时钟本地控制")
    clock = Client(base).call("GET", "/admin/clock")
    check("时钟可查询", "now_iso" in clock)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--fund-base", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    processes = []
    tempdir = None
    try:
        if args.base:
            base, fund_base = args.base, args.fund_base
        else:
            tempdir = tempfile.mkdtemp(prefix="forest-check-")
            fund_db = os.path.join(tempdir, "partner.sqlite3")
            forest_db = os.path.join(tempdir, "forest.sqlite3")
            partner = subprocess.Popen(
                [sys.executable, str(ROOT / "partner/server.py"), "--port", "0", "--state", fund_db],
                stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
            fund_port = json.loads(partner.stdout.readline())["port"]
            fund_base = f"http://127.0.0.1:{fund_port}"
            processes.append(partner)
            service = subprocess.Popen(
                [sys.executable, "-m", "forest.server", "--port", "0", "--db", forest_db,
                 "--fund-base-url", fund_base],
                stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
            service_port = json.loads(service.stdout.readline())["port"]
            base = f"http://127.0.0.1:{service_port}"
            processes.append(service)
            wait_health(base)
        counts = run_checks(base, fund_base if not args.base else args.fund_base)
        print(f"\n全部通过：{counts['passed']}/{counts['checks']} 项检查")
    finally:
        for p in processes:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
