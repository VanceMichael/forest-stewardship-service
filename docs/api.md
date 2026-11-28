# 业务服务 HTTP 约定（合成数据）

服务仅使用 Python 标准库，默认监听环回地址。所有金额以整数“分”提交与返回，面积单位为平方米，时间为带时区 ISO 8601。

## 身份与时钟

- 每个写请求带 `X-Principal` 头，取值来自 `data/catalog.json` 的 `principals`（如 `builder-user`、`acceptor-A`、`care-user`、`project-A`、`reviewer-A`、`surveyor-A`）。服务据此映射到参与方与角色；无身份写请求返回 401，越权返回 403。
- 时间可本地控制：请求头 `X-Now: 2026-12-11T09:00:00+08:00` 仅对单次请求生效；`POST /admin/clock` 持久覆盖服务时钟，`DELETE /admin/clock` 恢复真实时间。演练无需等待真实季节。
- 错误统一为 `{"error":{"code":...,"message":...}}`，HTTP 状态码：400 报文错误、401 未认证、403 越权、404 不存在、409 状态冲突、422 业务条件不满足、502 资金协作方异常。

## 角色边界

| 角色 | 可办理 |
| --- | --- |
| builder 施工方 | 栽植登记、栽植申报；不能确认自己的验收 |
| surveyor 测量员 | 抽查记录与实测面积 |
| acceptor 验收人 | 接收/限期补植/退回签认、缺陷归责、设施验收、错误地块暂停 |
| maintainer 管护队 | 接管、巡护批次、补植实施；不能修改施工队原记录（系统无改写接口） |
| proposer 经办人 | 付款受理、派发、同步、追偿提案、时钟、结清 |
| reviewer 复核人 | 归责确认、补植开单/核验、追偿确认、暂停解除、重新交接 |

## 端点

### 目录与追溯
- `GET /health`
- `GET /parties`：参与方与演练身份映射
- `GET /contracts`、`GET /units[?contract_id=]`
- `GET /units/{id}`：单元 + 付款视图 + 完整追溯
- `GET /units/{id}/trace`：施工/管护边界、申报、抽查、签认、接管、缺陷、补植、付款、暂停、下次观察
- `GET /contracts/{id}/ledger`：合同台账，含每个单元留在旧承包方的施工责任与可付/不可付原因
- `GET /events`：仅追加领域事件；`GET /notifications`、`POST /notifications/ack`

### 申报与现场记录
- `POST /plantings` `{unit_id, planted_at}`
- `POST /declarations` `{unit_id, stage: planting|care, declared_m2}`
  同一单元同一阶段只允许一笔有效申报（`submitted/accepted/replant_required` 唯一），被退回后可重新申报。
- `GET /declarations/{id}`：含抽查列表与证据适用性检查
- `POST /surveys` `{declaration_id, surveyed_at, sample_size, alive_count, dead_count, measured_m2, dead_plants?[]}`
  响应即时返回证据检查：组数≥2、不同日期、每组≥10株、末次抽查距栽植≥30天、组成活率≥0.90。服务只检查条件，不替验收人作现场判断。
- `POST /acceptances` `{declaration_id, decision: accept|replant_required|return, accepted_m2?, replant_deadline?}`
  证据不适用或无抽查不得接收；`accepted_m2` 不得超过实测覆盖，争议/未抽查面积不并入合格量。

### 巡护、缺陷与补植闭环
- `POST /patrol-batches` `{unit_id, patrolled_at, findings:[{plant_no?, description?}]}`
  同一单元同阶段同植株的未闭缺陷自动合并（只开一笔补植费），其他植株/单元的新缺陷独立建档。
- `GET /defects[?unit_id=]`
- `POST /defects/cause` `{defect_id, cause: construction|maintenance}`：归责由有权人确认
- `POST /replants` `{defect_id, amount_cents}`：施工整改 → 施工方担费；正常养护 → 管护方担费
- `POST /replants/perform` `{replant_id, performed_at}`：记录实施并重设下次观察起点（30 日后）
- `POST /replants/verify`：核验通过后缺陷闭环

### 接管与设施
- `POST /takeovers` `{unit_id, area_m2, starts_at}`：面积不得超过已接收面积；快照未解除施工责任；观察期自接管起 30 日
- `POST /takeovers/rehandover`：范围更正后的重新交接，旧接管标记 `superseded` 并通知原管护队
- `POST /facility-inspections` `{facility_id, result: accepted|returned, inspected_at}`
  共享防护设施是单元的**显式依赖**；未接收时依赖它的单元不能借其他区域验收提前释放保留款。

### 暂停与范围更正
- `POST /corrections/wrong-plot` `{affected_unit_ids:[], reason}`：暂停受影响单元的付款与新增管护安排，向施工方/已接管人发待确认通知；**原签认保留不删**
- `POST /corrections/boundary` `{unit_id, new_area_m2, reason}`：界桩复核后的当前边界更正
- `GET /holds`、`POST /holds/resolve` `{hold_id, resolution_note}`

### 付款（受理与到账分开）
- `GET /payments/eligible[?contract_id=|unit_id=]`：四类作业项（progress/retention/care_fee/replant_fee）的可付金额、额度、已付与阻断码
- `POST /payments` `{unit_id, kind, amount_cents, basis?, idempotency_key?}`：经办人受理即占额度
- `POST /payments/dispatch` `{payment_id, command_id}`：派发至合成资金协作方
- `POST /payments/sync` `{payment_id}`：轮询原始流水，`paid` 到账、`rejected` 明确失败释放额度、`pending/504` 继续占额度
- `POST /fund/trace` `{command_id}`：按流水 ID 查原始观测事实与本地关联付款
- `GET /payments[?unit_id=]`
- `POST /recoveries` `{payment_id, amount_cents, method: recover|deduct}` 与 `POST /recoveries/confirm`
  已支付金额保留事实，追偿/抵扣另立记录，旧付款不会被改为零。

### 同步结清
- `POST /settlements/run`：按当前时钟把观察期到期、管护转交、缺陷责任与付款回执汇总为同一批事项；观察期已到期但回执未结清的在途付款列入 `inconsistencies`，不会各表分别显示完成。

## 保留款阻断码

`no_accepted_area`、`no_takeover`、`observation_open`、`open_construction_defect`（含未归责缺陷；正常养护缺陷不阻断保留款）、`facility_not_accepted`、`hold_active`、`payment_in_flight`。局部接收时保留款仅按已接收范围计额，并在 `notes` 中说明，不扩及未接收部分。

## 与资金协作方的关系

业务服务通过 `--fund-base-url` 对接 `partner/server.py` 的回放协议（`POST /commands`、`GET /commands/{id}`）。202 为已收到，504 为响应不明但可能已受理；断线项只能轮询不能重发，只有明确 `rejected` 的流水才能重新办理（`attempt` 递增），已 `paid` 的不能再付一次。业务服务自身不伪造资金事实。
