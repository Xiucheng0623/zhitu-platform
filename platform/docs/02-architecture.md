# 02 · 系统架构设计

## 1. 总体拓扑

```
┌────────────────────────────────────────────────────────────┐
│                    统一前端（forge-web）                     │
│        React 18 + TS · Design System · Hash 路由            │
└──────────────┬─────────────────────────────┬───────────────┘
               │ REST (JSON)                 │ iframe / 静态资源
┌──────────────▼──────────────┐   ┌──────────▼───────────────┐
│     orchestrator 自研中间层   │   │   Label Studio CE 1.23   │
│  FastAPI · 状态机 · RBAC     │   │   标注引擎（不改造核心）    │
│  审计 · 隔离 · 结算口径       │   │                          │
└──────┬───────────┬──────────┘   └──────────┬───────────────┘
       │ REST      │ SQL(仅自建表)            │ Webhook
┌──────▼──────┐  ┌─▼────────────┐  ┌──────────▼───────────────┐
│  ERPNext    │  │ PostgreSQL    │  │  orchestrator 接收事件    │
│  (Frappe)   │  │  业务实体库    │  │  → 状态机推进             │
│  财务单据    │  │  (独立 schema)│  └──────────────────────────┘
└─────────────┘  └──────────────┘
```

关键原则：
1. **三个系统三个库**。orchestrator 绝不直连 ERPNext/Label Studio 数据库，
   全部走 API —— 升级互不阻塞。
2. **财务以 ERPNext 为准**。本平台生成应付口径与结算单，PO/PINV/PE 单向推送。
3. **标注以 Label Studio 为准**。标注结果不落业务库，仅存引用 ID + 快照摘要。

## 2. 实体归属（数据归属表）

| 实体 | 归属系统 | 同步方式 |
|---|---|---|
| Enterprise / Supplier / SupplierMember | orchestrator | Supplier 档案推送 ERPNext |
| Project / AnnotationTask / TaskAssignment | orchestrator | Project 映射 LS project |
| AnnotationSubmission / QualityInspection / ReviewRecord | orchestrator | 由 LS Webhook 触发 |
| DeliveryRecord / Settlement / PerformanceRecord | orchestrator | 结算单推送 ERPNext |
| User 身份 | Keycloak (OIDC) | 角色声明映射 RBAC |
| 发票 / 付款 / 总账 | ERPNext | 单向，不回写 |

## 3. 多租户预留

- 所有业务表带 `tenant_id`，ORM 层（SQLAlchemy session filter）强制注入，
  跨租户查询抛 `TenantIsolationError`；
- Phase 1 单租户运行；Phase 5 SaaS 化时仅增加 `tenant` 注册流程与行级配额，
  不改表结构；
- 对象存储路径 `s3://bucket/{tenant}/{project}/{batch}/`，签名 URL 内嵌租户。

## 4. 状态机（服务端唯一权威实现）

```
AnnotationTask:
  pending → assigned → in_progress → submitted → reviewing
  reviewing → accepted            （通过，生成 ReviewRecord）
  reviewing → rework              （返工，rework_round += 1）
  rework → assigned → ... → submitted（上限 max_rework，超限转人工仲裁）

Settlement:
  draft → submitted → confirmed → invoiced → paid
  每步由不同角色执行（draft:PM / confirmed:财务 / paid:财务总监）

Supplier:
  qualifying → active | suspended；active ↔ probation（绩效触发）
```

前端 `lib/store.tsx` 复刻同一约束用于即时反馈，冲突时以服务端 409 为准。

## 5. 权限模型

RBAC（`web/src/lib/rbac.ts` 与 `orchestrator/app/rbac.py` 共用同一规则表）：
9 个角色 × 21 个动作。四道隔离边界：租户 / 供应商 / 任务 / 文件（签名 URL）。
越权访问返回 **404**（不泄露资源存在性），而非 403。

## 6. 统一前端与 Label Studio 的集成

- 登录后由 orchestrator 签发 LS 的一次性 token（`/api/bridge/ls-token`）；
- 标注工作台外壳提供条目列表 / 标签面板 / 提交动作，
  中心画布 iframe 加载 LS 原生标注界面（PostMessage 双向通信）；
- 当前仓库内置了等效交互视图（SVG 场景）用于无 LS 环境的前端基线验证，
  生产部署后自动切换（检测 `VITE_LS_EMBED=1`）。

## 7. 部署形态

单机 Docker Compose（见 deploy/docker-compose.yml）：
- caddy 反代（TLS 终止）
- erpnext: mariadb + redis(3 实例) + frappe-bench
- label-studio: postgres + ls web
- orchestrator: FastAPI + uvicorn
- minio（S3 兼容，内网数据湖）

资源需求实测估算：ERPNext 栈 ~3.5GB 内存，LS ~1GB，orchestrator ~300MB，
总计约 5GB / 磁盘 25GB 起。

---

## 9. 架构决策记录（ADR-001）：移除 ERPNext

**日期**：2026-11-05　**状态**：已采纳　**影响面**：架构、部署、许可证、模块边界

### 背景

初始方案把 ERPNext v15 作为供应商主档、采购订单、发票、付款的权威系统，
本平台通过 Frappe REST 单向推送单据。实际推进到 Phase 2 时暴露三个硬问题：

1. **运行门槛不可接受**。ERPNext 需要 MariaDB + Redis×2 + bench + 站点初始化，
   镜像约 4GB；Frappe 不支持 Windows 原生运行。本机（及多数国内交付现场）
   无法把它跑起来，导致「集成」长期停留在代码层面，从未真实联调。
2. **许可证约束转嫁给客户**。ERPNext 是 GPL-3.0，私有化交付必须向客户开源
   全部对应源码。对一个要打包卖给数据服务商的商业产品，这是长期枷锁。
3. **跨系统对账成本高于收益**。供应商、结算单、发票分散在两个库，
   任何一次结算异常都要两边比对；而 ERPNext 提供的这几个模块（供应商档案、
   采购单、发票、付款）本质是常规 CRUD + 状态机，自研成本远低于长期对账成本。

### 决策

**移除 ERPNext。** 供应商、合同、任务、质检、结算、开票、付款全部由本平台
自有 PostgreSQL 承载，单一权威源，事务内闭环。

### 保留与迁移

| 原 ERPNext 职责 | 现在由谁承担 | 状态 |
|---|---|---|
| Supplier 主档 / 资质 | `forge.suppliers`（含 3 项资质列） | 已实现 |
| Supplier Quotation / Contract | `forge.project_suppliers`（承接关系 + 配额） | 已实现 |
| Purchase Order | 结算单的「应付」侧（`settlements.amount`） | 已实现 |
| Purchase Invoice | `forge.invoices`（6 态 + 价税自洽 + 防重票唯一索引） | 已实现 |
| Payment Entry | `forge.payments` + `forge.payment_allocations`（付款流水 + 核销） | 已实现 |
| 三单匹配 | `forge.v_settlement_match`（对账单 / 发票 / 付款逐单核对） | 已实现 |
| 应付账龄 | `forge.v_payables_aging`（current / d1_30 / d31_60 / d61_90 / d90+） | 已实现 |
| Workflow 审批流 | `fsm.SETTLEMENT_FLOW` / `INVOICE_TRANSITIONS` / `PAYMENT_TRANSITIONS` + 分步权限 | 已实现 |

`settlements` 表里原有的 `purchase_order / purchase_invoice / payment_entry`
三个自由文本列保留为「外部单据号引用」，用于对接客户已有的财务系统（用友/金蝶等），
不再指向 ERPNext。

### 后果

**收益**
- 部署从 13 个服务降到 9 个，内存占用约 5GB → 2GB，Windows 本机可跑全栈
- 自有代码无 GPL 分发义务，商业交付合同可签
- 结算链路单库事务，不再有跨系统对账

**代价（必须自己扛）**
- 开票、付款流水、三单匹配需要自研 —— 已于 2026-11 补齐（`005_finance.sql`），
  包含会计内控约束：发票价税合计自洽、同一供应商发票号唯一、核销不得超付、
  结算单 paid 状态由核销金额自动推导（不允许人工点按钮改状态）
- 付款执行仍走线下银行，平台只登记流水与回单号；若需银企直连，
  需对接银行 API 或第三方支付，这是另一期工程
- 与客户既有财务系统对接时，需要出导入/导出格式（CSV/Excel 或对方 API）

### 参考对象

功能矩阵对标海天瑞声（Speechocean）等专业数据服务平台，而非 ERP 系统：

| 能力域 | 平台侧实现 |
|---|---|
| 数据采集 | `collectors` / `collection_tasks` / `collection_scripts` / `collection_sessions`（独立产线，不与标注共用状态机） |
| 标注生产 | Label Studio 原生引擎 + 本平台派单/批次 |
| 质量体系 | 三级抽检（L1/L2/QC）、缺陷分类、返工轮次、SLA |
| 交付验收 | 交付记录 + 客户验收状态 |
| 供应商协同 | 供应商门户 + 产能/质量评分 |
| 结算计费 | 按件计费、扣款规则、期间结算、发票核验、付款核销、三单匹配 |
| 合规安全 | 租户隔离 + 审计留痕 + 对象存储前缀隔离 |

**ERP 的价值在「记录发生了什么」，数据平台的价值在「控制生产怎么发生」。**
后者才是本产品的核心，前者自研一个够用的子集即可。
