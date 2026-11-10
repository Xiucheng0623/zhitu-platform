# 06 · 财务域与采集域运行手册

> 本文对应 `deploy/migrations/005_finance.sql`、`006_collection.sql`、
> `007_rls_ext.sql`、`009_backfill_finance.sql`，以及后端
> `app/finance.py`、`app/collection.py`、`app/ops.py`。
>
> 为什么单独写一份：ERPNext 移除后，开票与付款不再有任何外部系统兜底。
> 这两块一旦出错是**会计事故**，不是页面 bug。所以约束写进数据库，
> 而不是写在 UI 校验里 —— UI 可以被绕过，CHECK 不能。

---

## 1. 财务域

### 1.1 表与职责

| 表 | 职责 | 关键约束 |
|---|---|---|
| `forge.invoices` | 发票台账（进项） | 价税合计自洽、税额与税率容差 ±0.01、同供应商发票号唯一（排除作废）、核验必须留人留时间 |
| `forge.payments` | 付款单（一次付款指令 / 一笔银行流水） | 成功必须有回单号与到账时间、审批必须有审批人、同租户回单号唯一（排除冲销） |
| `forge.payment_allocations` | 付款 ↔ 结算单核销（多对多） | 不得超付、不得超付款单金额、草稿/待确认结算单不可核销 |
| `forge.v_settlement_match` | 三单匹配（对账单 / 发票 / 付款） | 只读视图，`security_invoker=on` |
| `forge.v_payables_aging` | 应付账龄分档 | 只读视图 |

### 1.2 状态机

**发票**（`fsm.INVOICE_TRANSITIONS`）

```
draft ──issue──▶ issued ──receive──▶ received ──verify──▶ verified ──void──▶ void
                                      │
                                      └──reject──▶ rejected ──issue──▶ issued
```

- `verified` 不能退回 `received`，只能作废（红冲）。已核验的票参与付款，
  退回去会让核销记录指向一张状态不明的票。
- 表里没写的转移一律 409。

**付款**（`fsm.PAYMENT_TRANSITIONS`）

```
pending ──approve──▶ approved ──settle──▶ succeeded ──reverse──▶ reversed
   │                    │
   └────fail────────────┴──▶ failed ──retry──▶ pending
```

**结算单**（`fsm.SETTLEMENT_FLOW`）

```
draft → submitted → confirmed → invoiced → paid
```

后两步不再由人工按钮推进：

- `confirmed → invoiced`：由 `POST /invoices/{id}/verify` 触发（核验通过即已开票）
- `invoiced → paid`：由 `payment_allocations` 触发器 `sync_settlement_paid()` 推导

这样做的原因很直接：靠人点「已付款」必然出现「状态说已付、流水说没付」。

### 1.3 三单匹配的判定顺序

```
not_submitted → missing_invoice → amount_mismatch
  → awaiting_verification → ready_to_pay → partial_paid → settled
```

先命中先返回。两个容易误解的点：

1. **已付清但无发票仍报 `missing_invoice`。** 无票付款是税务风险，
   不该被"已结清"掩盖。
2. **`allocated_amount` 不计入 failed / reversed 的付款。**
   否则一次冲销就把这笔应付永久锁死 —— 冲销后必须还能重新付款，
   `e2e_finance.py` 有专门断言覆盖这条。

### 1.4 常见运维动作

```sql
-- 某期间还有哪些应付对不上
select code, payable, invoiced_amount, verified_amount, paid_amount, match_state
  from forge.v_settlement_match where period = '2026-09'
 order by match_state, code;

-- 账龄超过 60 天的应付
select supplier_name, code, outstanding, days_overdue, aging_bucket
  from forge.v_payables_aging where aging_bucket in ('d61_90','d90_plus');

-- 发票红冲（注意：会同时影响三单匹配，走 API 更好，会写审计）
select forge.sync_settlement_paid('<settlement_id>');   -- 手工重算已付状态
```

### 1.5 五个容易踩的坑

1. **金额永远用 Decimal。** API 返回字符串而非数字。前端把它 `Number()`
   再算，就已经丢精度了 —— 显示可以格式化，运算必须在服务端。
2. **税额容差是 ±0.01。** 真发票按行舍入后汇总，强行要求分毫不差会把
   合法票据挡在门外。但容差只能有一分钱，不能放宽。
3. **`ck_payment_succeeded_evidence` 会挡人。** 想直接 `update status='succeeded'`
   一定会失败：必须同时给 `voucher_no` 与 `paid_at`。这是设计，不是 bug。
4. **重票唯一索引是部分索引**（`where status <> 'void'`）。作废后可以重开同号，
   因为红冲后重开是正常业务。
5. **`009_backfill_finance.sql` 会给历史 `paid` 结算单补发票与流水。**
   如果从旧系统迁移过来的数据不想造凭证，把该脚本从 migrate 链里去掉 ——
   但那样三单匹配会永远报 `missing_invoice`。

---

## 2. 采集域

### 2.1 为什么独立成域

海天瑞声这类平台里，采集与标注是**两条并列产线**，不是同一流程的两个阶段：

- 采集有自己的对象：采集方案、采集脚本、现场、设备参数、采集员、场次
- 结算口径不同：采集按**有效片段**（`unit_price × valid_clips`），标注按**条目**
- 质量口径不同：采集看底噪、漏录、场景噪声；标注看标错、漏标

把采集塞进 `tasks` 会让「任务」同时表示标注批次与采集场次，状态机互相污染。

### 2.2 表与职责

| 表 | 职责 | 关键约束 |
|---|---|---|
| `forge.collectors` | 采集员档案 | 未实名或未签授权协议不得 `active`（`ck_collector_can_work`） |
| `forge.collection_tasks` | 采集方案 | 目标场次/片段、设备与环境要求、单价与预算 |
| `forge.collection_scripts` | 采集脚本条目 | `(task_id, seq)` 唯一，支持反复 upsert 迭代方案 |
| `forge.collection_sessions` | 采集场次 | 一位采集员同时只能有一场进行中；有效+无效 ≤ 总数 |

### 2.3 自动回写（触发器，不是应用层）

场次 `accepted` 后：

- `collection_tasks.done_sessions / valid_clips / spent` 重算
- `collectors.sessions_done / valid_clips` 重算

放在库里而不是 API 里，是因为场次还可能被运维脚本、数据修复、批量导入改动 ——
只要走了 SQL 就一定回写，不存在「从后台改完数字不对」这种事。

### 2.4 状态机

采集任务：`draft → recruiting → running ⇄ paused → completed | cancelled`

场次：`scheduled → recording → submitted → qc → accepted | rejected`

场次质检用 `review:decide` 权限（`POST /collection/sessions/{id}/qc`），
不开放给采集管理岗 —— 自己采自己检就是没有质检。

### 2.5 三个坑

1. **不能直接创建 `active` 采集员。** 创建接口固定落 `screening`，
   必须走 `POST /collectors/{id}/status`，且档案里已实名 + 已签协议。
2. **并行场次是唯一索引挡的**，不是应用判断。同一采集员已有
   `scheduled`/`recording` 场次时再排期返回 422。
3. **删场次会让累计量回退**（触发器 `after delete` 也会重算）。
   如果这是期望行为就没问题，若要做"作废但保留统计"，请改状态而不是删行。

---

## 3. 验收方式

```bash
# 库层：13 个场景，全部做真写入后 rollback，可重复执行
psql -U forge_app -d forge -f deploy/migrations/008_finance_verify.sql
# 期望：全部 PASS，无 FAIL

# 接口层：58 项断言，覆盖两条完整业务闭环
python services/orchestrator/tests/e2e_finance.py
# 期望：58/58 通过

# 回归：既有 39 项契约与隔离测试
python services/orchestrator/tests/smoke_http.py
# 期望：39/39 通过
```

三个套件都可重复执行：fixture 用固定单据号，开场先清残留，结束再清一次。
审计日志是 append-only，故意不清理 —— 这是设计而非疏漏。
