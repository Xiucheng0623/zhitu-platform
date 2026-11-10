# 03 · API 契约（orchestrator REST）

Base URL：`/api/v1` · 认证：`Authorization: Bearer <JWT>`
响应信封：`{"rows": [...], "total": n, "page": p, "page_size": s}`

本文件的「状态」列是**实测结果**，不是计划：
- ✅ 已实现且有测试覆盖（`services/orchestrator/tests/smoke_http.py`）
- ◐ 已实现但仅部分字段/分支
- ⬜ 规划中，当前调用会 404

前端 `web/src/lib/http.ts` 是唯一的请求入口，`web/src/lib/adapters.ts` 负责把后端行
（snake_case）翻译成前端类型（camelCase）。页面不直接 fetch，也不自己拼字段映射 ——
契约改动只需同步 adapters 一处。

> 2026-11 更新：原来的 `web/src/lib/api.ts`（一个包装 seed 的假门面）已删除。
> 它留在仓库里只会让后来人误以为页面还在读种子数据，或者更糟 ——
> 不小心把它接回去，让演示数字重新上界面。

## 认证

两种模式，由 `FORGE_AUTH_MODE` 决定：

| 模式 | 行为 | 用途 |
|---|---|---|
| `jwt` | 校验 OIDC JWT（RS256 via JWKS 或 HS256 共享密钥），取 `sub`/`exp`/`roles`/`tenant_id`/`supplier_id` 声明 | **生产唯一允许的模式** |
| `dev` | 接受演示 token，形如 `tenant::role[|role][@supplier_id][#sub]` | 仅本地联调；任何人可伪造角色，禁止上公网 |

角色取值见 `app/security.py` 的 `ROLE_MATRIX`，与 `web/src/lib/rbac.ts` 逐条镜像
（9 角色 × 21 动作）。两侧漂移会被契约测试的 `/api/v1/rbac` 比对发现。

## 元信息

| Method | Path | 权限 | 状态 | 说明 |
|---|---|---|---|---|
| GET | /healthz | 公开 | ✅ | `{ok, version, auth_mode, db:{ok,pg,tables,rls_forced,can_bypass_rls,pool}}`；`?deep=1` 追加外部系统探测 |
| GET | /api/v1/me | 任意 | ✅ | 主体 + 角色 + 有效权限列表 |
| GET | /api/v1/rbac | 公开 | ✅ | 规则表镜像，供前端/CI 比对 |
| GET | /api/v1/integrations/health | audit:read | ✅ | 逐系统状态，**外部系统不可达时仍返回 200**（降级为 `ok:false`） |

> `/healthz` 刻意不报告业务行数：该查询无租户上下文，而所有业务表 `force RLS`，
> 任何计数都会是 0，极易被误读成「库是空的」。

## 项目与供应商

| Method | Path | 权限 | 状态 | 说明 |
|---|---|---|---|---|
| GET | /projects | project:read | ✅ | 含 `progress_pct`/`task_count`/`accepted_tasks`/`overdue_tasks`（来自 `v_project_progress`） |
| POST | /projects | project:write | ⬜ | 创建项目（当前由前端 store 实现） |
| GET | /suppliers | supplier:read | ✅ | 可选 `?status=` |
| POST | /suppliers/{id}/status | supplier:qualify | ✅ | `{status: qualifying\|active\|probation\|suspended}`，非法转移 409 |
| GET | /suppliers/{id}/members | member:read | ⬜ | |
| POST | /suppliers/{id}/members | member:write | ⬜ | |

## 任务

| Method | Path | 权限 | 状态 | 说明 |
|---|---|---|---|---|
| GET | /tasks | task:read | ✅ | 过滤 `project_id`/`status`/`supplier_id`，分页 `limit`(≤500)/`offset` |
| GET | /tasks/{id} | task:read | ✅ | 不可见即 404 |
| POST | /tasks/{id}/assign | task:assign | ✅ | `{supplier_id, assignee_id, note}`；成员须属该供应商且在岗，否则 422；关闭上一条分配记录并留痕 |
| POST | /tasks/{id}/submit | task:submit | ✅ | `{item_count, ls_annotation_ref}`；越权提交 404 |
| POST | /tasks/{id}/review | review:decide | ✅ | `{stage: l1\|l2\|qc, result: pass\|rework\|reject, note, sampled, defects}`；返工轮次 +1 |
| GET | /reviews | review:read | ✅ | `?task_id=&stage=&result=`，按裁定时间倒序 |

### 任务状态机（唯一权威实现在 `app/fsm.py`）

```
pending ─┬→ assigned ─┬→ in_progress → submitted → reviewing ─┬→ accepted（终态）
         │            ├→ submitted                          ├→ rework ─┬→ assigned
         │            └→ rejected（终态）                     │          └→ submitted
         └→ rejected（终态）                                  └→ rejected（终态）
```

表外转移一律 409 `state_conflict`。`assigned → assigned` 表示改派，允许但会
关闭旧分配记录，不退化成 pending。

## 结算

| Method | Path | 权限 | 状态 | 说明 |
|---|---|---|---|---|
| GET | /settlements | settlement:read | ✅ | 可选 `?status=&supplier_id=` |
| POST | /settlements/{id}/advance | settlement:read（按步校验） | ✅ | 见下表；权限不足返回 **404** |

`draft → submitted → confirmed → invoiced → paid`

| 步骤 | 所需权限 |
|---|---|
| draft → submitted | settlement:write |
| submitted → confirmed | settlement:approve |
| confirmed → invoiced | settlement:approve |
| invoiced → paid | settlement:approve |

> 财务动作对项目经理不可见（其无 approve 权限），推进时同样返回 404 —— 与「不存在」
> 无法区分，避免探测结算单状态。

## 财务域（发票 / 付款 / 三单匹配）

ERPNext 移除后由自有表承载，见 `docs/02 §9`。金额字段一律返回**字符串**（Decimal 序列化，
避免 JS 浮点丢精度）。

| 方法 | 路径 | 权限 | 实现 | 说明 |
|---|---|---|---|---|
| GET | /invoices | invoice:read | ✅ | `?status=&supplier_id=&settlement_id=` |
| POST | /invoices | invoice:write | ✅ | `{supplier_id, settlement_id?, invoice_no, invoice_type, amount, tax_rate}`；税额与价税合计由服务端算，超应付 422，重票 422 |
| POST | /invoices/{id}/issue | invoice:write | ✅ | draft → issued |
| POST | /invoices/{id}/receive | invoice:write | ✅ | issued → received |
| POST | /invoices/{id}/verify | invoice:verify | ✅ | received → verified，并联动结算单 confirmed → invoiced |
| POST | /invoices/{id}/reject | invoice:verify | ✅ | `{reason}`；received → rejected |
| POST | /invoices/{id}/void | invoice:verify | ✅ | 任意非终态 → void（红冲） |
| GET | /payments | payment:read | ✅ | `?status=&supplier_id=`，含已核销合计 |
| POST | /payments | payment:write | ✅ | `{supplier_id, amount, method, payee_*, allocations:[{settlement_id, amount}]}`；核销超付由库内触发器拒绝 |
| POST | /payments/{id}/approve | payment:approve | ✅ | pending → approved，记录审批人 |
| POST | /payments/{id}/settle | payment:approve | ✅ | `{voucher_no}`；approved → succeeded，**强制要求回单号** |
| POST | /payments/{id}/fail | payment:approve | ✅ | `{reason}` |
| POST | /payments/{id}/reverse | payment:approve | ✅ | succeeded → reversed；结算单自动退回 invoiced |
| GET | /finance/match | settlement:read | ✅ | 三单匹配 `?match_state=&period=` |
| GET | /finance/aging | settlement:read | ✅ | 应付账龄分档 |
| GET | /finance/summary | settlement:read | ✅ | 应付/已付/未付 + 各种异常计数 |

### 匹配状态取值

`not_submitted`（结算单还是草稿）→ `missing_invoice` → `amount_mismatch`
→ `awaiting_verification` → `ready_to_pay` → `partial_paid` → `settled`

判定顺序即上面的顺序，先命中的先返回。**已付清但缺发票仍报 `missing_invoice`** ——
这是有意为之：无票付款是税务风险，不该被"已结清"掩盖。

## 采集域

| 方法 | 路径 | 权限 | 实现 | 说明 |
|---|---|---|---|---|
| GET | /collectors | collection:read | ✅ | `?status=&city=&accent=` |
| POST | /collectors | collection:write | ✅ | 初始状态恒为 `screening`，不能直接 active |
| POST | /collectors/{id}/status | collection:write | ✅ | 未实名或未签协议转 active 返回 422 |
| GET | /collection/tasks | collection:read | ✅ | 来自 `v_collection_progress` |
| POST | /collection/tasks | collection:write | ✅ | 项目必须可见，否则 404 |
| POST | /collection/tasks/{id}/status | collection:write | ✅ | draft → recruiting → running ⇄ paused → completed / cancelled |
| GET | /collection/tasks/{id}/scripts | collection:read | ✅ | 按 seq 升序 |
| POST | /collection/tasks/{id}/scripts | collection:write | ✅ | 批量 upsert（`on conflict (task_id, seq)`），可反复迭代方案 |
| GET | /collection/sessions | collection:read | ✅ | `?task_id=&collector_id=&status=` |
| POST | /collection/sessions | collection:write | ✅ | 同一采集员已有进行中场次时 422 |
| POST | /collection/sessions/{id}/submit | collection:write | ✅ | 有效+无效片段数不得超过总数（422） |
| POST | /collection/sessions/{id}/qc | review:decide | ✅ | `{result: pass\|partial\|fail}`；通过后回写任务与采集员累计 |
| GET | /collection/summary | collection:read | ✅ | 任务 / 采集员 / 场次三组计数 |

## 审核与绩效

| 方法 | 路径 | 权限 | 实现 | 说明 |
|---|---|---|---|---|
| GET | /reviews/quality | review:read | ✅ | 按阶段（l1/l2/qc）算缺陷率，`?period=YYYY-MM` |
| GET | /performance | performance:read | ✅ | 快照表 `performance_records` |
| GET | /performance/live | performance:read | ✅ | 从任务与审核现算，供应商当面质疑时用这个 |
| GET | /assets | asset:read | ✅ | `?project_id=&status=` |
| POST | /assets/{id}/status | asset:import | ✅ | 资产生命周期 raw → in_annotation → in_review → accepted → archived |

## 审计

| Method | Path | 权限 | 状态 | 说明 |
|---|---|---|---|---|
| GET | /audit | audit:read | ✅ | `?action=&limit=&offset=`，按时间倒序 |

审计与业务写入**同事务**：业务成功则审计必在，反之一起回滚。表上有
`before update or delete` 触发器，物理上不可篡改。

## 文件存储

| Method | Path | 权限 | 状态 | 说明 |
|---|---|---|---|---|
| POST | /assets/presign | asset:import | ✅ | `{project_id, batch_no, filename, content_type}` → 预签名 PUT URL（15 分钟） |
| GET | /assets/sign | asset:read | ✅ | `?key=` → 预签名 GET URL；key 必须在本租户前缀内，否则 404 |
| POST | /assets/import | asset:import | ✅ | 入库登记；同 project+batch+name 重复登记返回既有 id（`duplicate:true`） |

Key 由服务端拼：`{enterprise_id}/{project_id}/{batch_no}/{filename}`，
**不接受客户端传入完整 key**。文件直传对象存储，不经过中间层。

## Webhook（Label Studio → orchestrator）

| Method | Path | 状态 | 说明 |
|---|---|---|---|
| POST | /webhooks/labelstudio | ✅ | `ANNOTATION_CREATED/UPDATED`：按 `ls_project_id` 定位任务，`assigned/in_progress` → `submitted`，并写审计 |

校验 header `X-Forge-Secret`（对应 `FORGE_LS_WEBHOOK_SECRET`），不匹配返回 404。
Label Studio 无终端用户身份，此路径用服务主体 `svc_labelstudio` 执行。

## 平台 → Label Studio 单点登录

| Method | Path | 权限 | 状态 | 说明 |
|---|---|---|---|---|
| GET | /ls/ticket | task:read / review:read / asset:read | ✅ | `?task_id=` → 90 秒一次性票据 URL |

```json
{"ok": true, "url": "http://ls.example.cn/forge/sso/?ticket=...", "expires_at": 1793897327, "ttl_seconds": 90}
```

链路：前端带平台 JWT 换票据 → iframe 打开 `{LS_PUBLIC}/forge/sso/?ticket=...` →
LS 侧 `core/forge_sso.py` 验 HMAC 签名、时效性、`jti` 防重放 → 按合成邮箱
`<手机号>@staff.zhitu.local` 建/找 LS 用户（无可用密码、非管理员）→ 加入项目所属组织 →
写 Django session → 302 进 `/projects/{ls_project_id}/data`。

设计约束：

- **只收 `task_id`，不收 `project_id`**。LS 项目 id 由服务端从任务行反查，并经
  `db.tenant_conn` 的 RLS 过滤；否则任意有 `task:read` 的人都能改个数字换到别人项目的票据。
- `FORGE_LS_SSO_SECRET` 由编排层与 LS 共享（≥32 字符）。缺失时 `/ls/ticket` 返回 503，
  LS 侧 `/forge/sso/` 也返回 503，**不降级为免鉴权**。
- `FORGE_LS_PUBLIC_BASE_URL` 是浏览器可达的 LS 地址，与服务端内部调用用的
  `FORGE_LS_BASE_URL` 分开；不配则退回后者。
- 平台与 LS 必须同主域（SameSite=Lax 的 sessionid 才会随 iframe 发送）。
- LS 侧仍保留一行用户，用于承载 `annotation.completed_by`、任务锁与审计归属；
  员工看不到也用不了 LS 密码（`set_unusable_password`）。这不是"合并成一个账号表"，
  而是平台代管 LS 账号。
- LS 服务账号（`FORGE_LS_REFRESH_TOKEN`）继续用于建项目 / 导数据 / 收 webhook，
  是机器身份，与浏览器 SSO 是两条独立的路。

可重放补丁：`labelstudio/brand/apply_brand.py` 会把 `forge_sso.py` 复制进 LS 包并
挂到 `MIDDLEWARE`，同时替换新旧两套注册页模板。**升级 LS 后必须重跑**，否则
`/forge/sso/` 会 404，员工又会被弹回登录页。

## 错误语义

统一信封：`{"error": {"code": "...", "message": "...", ...}}`

| Code | 含义 |
|---|---|
| 400 | 参数校验失败 |
| 401 | 未认证 / token 过期（`missing bearer token` / `token expired`）|
| **404** | 越权访问 **或** 资源不存在（刻意不区分，避免存在性泄露）|
| 409 | 状态机冲突 `{code:"state_conflict", entity, current, target}` |
| 422 | 业务规则拒绝（如「该成员不属于所选供应商」）|
| 502 | 外部系统集成失败 `{code:"integration_failed", system}` |
| 503 | 依赖未配置（如对象存储未启用）|

## 幂等

所有 `POST/PATCH` 支持 `Idempotency-Key` header（UUID）：
- 命中则原样返回首次响应，并带 `X-Idempotent-Replay: true`；
- **只缓存 2xx**，4xx/5xx 不入库，允许客户端修正后复用同一 key 重试；
- 键按租户隔离，默认 TTL 24 小时（`FORGE_IDEMPOTENCY_TTL_H`），过期行由清理任务删除。
