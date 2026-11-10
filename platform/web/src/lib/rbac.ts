/**
 * 访问控制 · 与 services/orchestrator/app/security.py 的 ROLE_MATRIX 保持同一套规则表
 * ---------------------------------------------------------------------------
 * 原则：UI 侧只做「呈现裁剪」，真正的越权拦截在服务端。两侧共用本文件的
 * 规则枚举，是为了避免前后端规则漂移；不构成安全边界。
 *
 * 2026-11 变更：移除 ERPNext 后新增财务与采集两组动作，并新增 finance 角色。
 * 原因见 docs/02 §9（ADR-001）：开票核验与付款审批若都由运营总监一人持有，
 * 四眼原则失效；会计域的内控不能省。
 */
import type { Principal, RoleCode } from "./types";

export type Action =
  | "project:read"
  | "project:write"
  | "project:delete"
  /** 标注流程编辑（含写进标注引擎）。与 project:write 分开：
   *  甲方需要改「员工怎么标」，但不应能改合同金额与单价。 */
  | "labeling:write"
  | "supplier:read"
  | "supplier:write"
  | "supplier:qualify"
  | "member:read"
  | "member:write"
  | "task:read"
  | "task:assign"
  | "task:submit"
  | "review:read"
  | "review:decide"
  | "asset:read"
  | "asset:import"
  | "settlement:read"
  | "settlement:write"
  | "settlement:approve"
  | "invoice:read"
  | "invoice:write"
  | "invoice:verify"
  | "payment:read"
  | "payment:write"
  | "payment:approve"
  | "collection:read"
  | "collection:write"
  | "performance:read"
  | "settings:write"
  | "audit:read";

const ROLE_MATRIX: Record<RoleCode, Action[]> = {
  platform_admin: [
    "project:read", "project:write", "project:delete", "labeling:write",
    "supplier:read", "supplier:write", "supplier:qualify",
    "member:read", "member:write",
    "task:read", "task:assign", "task:submit",
    "review:read", "review:decide",
    "asset:read", "asset:import",
    "settlement:read", "settlement:write", "settlement:approve",
    "invoice:read", "invoice:write", "invoice:verify",
    "payment:read", "payment:write", "payment:approve",
    "collection:read", "collection:write",
    "performance:read", "settings:write", "audit:read",
  ],
  ops_director: [
    "project:read", "project:write", "labeling:write",
    "supplier:read", "supplier:write", "supplier:qualify",
    "member:read", "member:write",
    "task:read", "task:assign",
    "review:read", "review:decide",
    "asset:read",
    "settlement:read", "settlement:write", "settlement:approve",
    "invoice:read", "invoice:write",
    "payment:read", "payment:approve",
    "collection:read", "collection:write",
    "performance:read", "audit:read",
  ],
  finance: [
    "project:read", "supplier:read", "task:read",
    "settlement:read", "settlement:write", "settlement:approve",
    "invoice:read", "invoice:write", "invoice:verify",
    "payment:read", "payment:write", "payment:approve",
    "performance:read", "audit:read",
  ],
  project_manager: [
    "project:read", "project:write", "labeling:write",
    "supplier:read", "member:read",
    "task:read", "task:assign",
    "review:read", "review:decide",
    "asset:read", "asset:import",
    "settlement:read", "invoice:read",
    "collection:read", "collection:write",
    "performance:read", "audit:read",
  ],
  data_engineer: [
    "project:read", "task:read", "asset:read", "asset:import",
    "supplier:read", "collection:read",
  ],
  reviewer: [
    "project:read", "task:read", "review:read", "review:decide",
    "asset:read", "member:read", "collection:read",
  ],
  supplier_admin: [
    "project:read", "member:read", "member:write", "task:read", "task:assign",
    "settlement:read", "invoice:read", "invoice:write",
    "collection:read", "performance:read",
  ],
  supplier_lead: [
    "project:read", "member:read", "task:read", "task:assign",
    "invoice:read", "collection:read",
  ],
  annotator: ["task:read", "task:submit", "asset:read"],
  client_viewer: ["project:read", "performance:read", "labeling:write"],
};

export function can(p: Principal, action: Action): boolean {
  return p.roles.some((r) => ROLE_MATRIX[r]?.includes(action));
}

export function canAny(p: Principal, actions: Action[]): boolean {
  return actions.some((a) => can(p, a));
}

/** 数据可见域：内部员工看全租户；供应商侧只能看自己供应商；标注员只看自己 */
export function isInternal(p: Principal): boolean {
  return p.kind === "employee";
}

/** 供服务端契约比对使用（GET /api/v1/rbac 与本表逐条对照） */
export const ROLE_MATRIX_EXPORT = ROLE_MATRIX;
