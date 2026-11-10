# 01 · 源码审计与许可证合规

> 审计日期：2026-11-05 · 审计人：平台组 · 结论有效期为一次大版本升级

## 1. 底座版本与许可证（联网核实）

| 组件 | 版本 | 许可证 | 商业含义 |
|---|---|---|---|
| ERPNext | **v15.124.5**（2026-11-04 发布） | **GPL-3.0** | 见 §2 合规决策 |
| Frappe Framework | v15 | MIT | 可自由闭源扩展 |
| Label Studio CE | **1.23.2**（2026-09-29 发布） | **Apache-2.0** | 可修改、闭源分发、商用 |
| Label Studio Enterprise | — | 商业授权 | **禁止**复制其任何代码/功能实现 |

1.23.2 升级注意（官方 release notes 实测）：
- `SECRET_KEY` 文件权限收紧，升级后需轮换 key，所有用户 token 失效；
- 私网地址的图片导出默认被 SSRF 防护拦截，内网媒体服务器需配置
  `USE_DEFAULT_BANNED_SUBNETS=false` + `USER_ADDITIONAL_BANNED_SUBNETS`；
- 上传的 HTML/SVG/XML 默认 `CSP: sandbox`，自建反代需在 nginx 复制该 header。

## 2. GPL-3.0 合规决策（本项目的最重要约束）

ERPNext 是 GPL-3.0（无网络条款，非 AGPL）。三种商业路径：

| 路径 | 是否合规 | 说明 |
|---|---|---|
| A. 自用 / 内部 SaaS 托管 | ✅ | SaaS 服务不属于"分发"，无开源义务 |
| B. 整体交付客户自部署 + 开源 | ✅ | 提供完整对应源码（含 Custom App） |
| C. 交付客户自部署 + 闭源 Custom App | ❌ | Custom App 与 ERPNext 构成衍生作品，必须开源 |

**当前决策：走路径 A（第一阶段单企业内部部署）。** 未来转 SaaS 仍走 A；
仅当客户要求私有化交付时，切换到路径 B 并公开 Custom App 源码。
自研业务中间层（orchestrator）与统一前端不 import ERPNext 代码，仅通过 HTTP API
交互，构成"独立程序"，可保持闭源（Apache-2.0 自有授权）。

## 3. ERPNext 复用清单（实测 API 能力）

直接复用（REST API `/api/resource/{doctype}` + Workflow）：

| DocType | 用途 | 备注 |
|---|---|---|
| Supplier | 供应商主档 | 扩展字段用 Custom Field，不改源码 |
| Supplier Group | 供应商分级（S/A/B/C） | 用 group 映射 tier |
| Supplier Quotation | 供应商报价 | |
| Purchase Order | 项目分包合同 / 结算依据 | 由结算单生成 |
| Purchase Invoice | 结算发票 | 税额、账期 |
| Payment Entry | 付款 | 结算终态凭证 |
| Employee / User / Role | 内部员工 / 统一账号 | |
| Workflow | 供应商资质审核流 | 用 Frappe Workflow 引擎，不自研 |

不复用 / 自建：项目、任务分配、质检、绩效 —— ERPNext 的 Project 模块与数据标注
业务模型不匹配，强行复用会造成实体污染（见 02-architecture §4）。

## 4. Label Studio 复用清单

保留原生能力（不重复开发）：
- 五类模态标注组件 + labeling config XML（模板管理见系统设置页）
- `/api/projects` `/api/tasks` `/api/annotations` 数据管理与导入导出
- Webhook（annotationSubmitted）驱动审核流
- ML Backend 预标注接口

前端重构范围：登录入口、项目列表、任务管理、统计 —— 通过
**iframe 嵌入原生标注界面 + 外壳替换**实现（`/projects/{lsId}/label`），
不 fork Label Studio 前端源码，保证可升级。

CE 版缺失的企业功能（评审工作流、SSO、组织级活动日志）由 orchestrator 补齐，
**不得**反向参考 Enterprise 代码。

## 5. 版本锁定策略

- ERPNext / Frappe 锁定 `version-15` 分支，跟随 patch 升级（周频）；
- Label Studio 锁定 `1.23.x`，大版本升级需回归标注格式兼容性；
- 升级前跑标注回归集（200 条样本任务全模态）+ 数据隔离测试（见测试报告）。
