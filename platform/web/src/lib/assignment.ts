/**
 * 标注员一致性（平台分配 ↔ 标注引擎实时可标 ↔ 标注员手里的数据行）
 * ---------------------------------------------------------------------------
 * 「我的任务」页不能用 tasks.item_count 当任务数：那是合同/交付口径
 * （客户买了 500 条），不是标注员在 LS 里真正能标的条目数。实测出现过
 * 平台 910 条 / LS 4 条、以及平台有批次但 LS 里一个项目都没有的情况。
 *
 * 用户后来把要求说得更准：**标注员手里的数据、平台页面显示的数据、LS 里的
 * 数据必须是同一批真实任务行**。所以前端标注员视角只读
 * `GET /api/v1/me/assignment/items`：服务端每次请求实时代理 LS 任务行，
 * 平台不保存、不复制、不缓存任何一行。页面上的条数、状态、指纹全部由
 * 这一份响应派生，不存在「另一个接口取回的旧数字」和列表打架的可能。
 *
 * 没有缓存、没有"上次的值兜底"：LS 不可达时该项目一行都不返回，界面必须
 * 显示「无法核对」，绝不能拿旧数据假装一致。
 */
import { get } from "./http";

export type AssignmentState =
  | "ok"
  | "not_connected"
  | "ls_missing"
  | "ls_unreachable";

export interface AssignmentRow {
  task_id: string;
  batch_no?: string | null;
  name?: string | null;
  project_id?: string | null;
  status?: string | null;
  assignee_user_id?: string | null;
  assignee_name?: string | null;
  /** 合同口径，只给商务结算用，永不被同步改写 */
  contract_items: number;
  contract_done: number;
  /** 实际口径：LS 里此刻真实可标 / 已标的条目数 */
  actual_items: number;
  actual_done: number;
  ls_project_id: number | null;
  ls_title?: string | null;
  state: AssignmentState;
  countable: boolean;
  error?: string;
}

export interface AssignmentSummary {
  in_sync: boolean;
  checked_at: number;
  source: string;
  batches: number;
  assigned: number;
  connected: number;
  not_connected: number;
  unreachable: number;
  contract_items: number;
  actual_items: number;
  actual_done: number;
  ls_projects: number;
  ticket_projects: number[];
  issues: string[];
}

export interface AssignmentResp {
  ok: boolean;
  rows: AssignmentRow[];
  summary: AssignmentSummary;
}

/** 集合/数量级清单，管理侧对账与定时自检读它；标注员页面已改用 items。 */
export function fetchAssignment(): Promise<AssignmentResp> {
  return get<AssignmentResp>("/api/v1/me/assignment");
}

// ---------------------------------------------------------------------------
// 任务数据行：LS 的任务是唯一正本
// ---------------------------------------------------------------------------
/** 一条真实的 LS 任务。data / meta 原样透传，平台不做映射也不造示例。 */
export interface AssignmentItem {
  ls_task_id: number;
  ls_project_id: number;
  inner_id: number;
  /** 平台批次只是合同/结算分组，不能替代这条 LS 数据 */
  batch_id: string;
  batch_no?: string | null;
  batch_name?: string | null;
  ls_title?: string | null;
  data: Record<string, unknown>;
  meta: Record<string, unknown>;
  data_sha256: string;
  is_labeled: boolean;
  total_annotations: number;
  cancelled_annotations: number;
  annotators: unknown[];
  drafts: number;
  has_draft: boolean;
  created_at: string;
  updated_at: string;
  /** 逐条进 LS 的相对路径，换票时服务端会核实归属才允许落地 */
  open_path: string;
}

export interface AssignmentGroup {
  batch_id: string;
  batch_no?: string | null;
  batch_name?: string | null;
  ls_project_id: number | null;
  ls_title: string;
  state: AssignmentState;
  error: string;
  items: AssignmentItem[];
  item_count: number;
  assignee_user_id?: string | null;
  contract_items: number;
}

export interface AssignmentProject {
  ls_project_id: number;
  batch_id: string;
  title: string;
  total: number;
  truncated: boolean;
  reachable: boolean;
  error: string;
}

export interface AssignmentItemsSummary {
  source: string;
  checked_at: number;
  /** 页面上「可标任务 N 条」必须由这个数派生 */
  items: number;
  labeled: number;
  drafts: number;
  batches: number;
  not_connected: number;
  ls_projects: number;
  ticket_projects: number[];
  unreachable: number;
  truncated: number;
  contract_items: number;
  fingerprint: string;
  in_sync: boolean;
  issues: string[];
}

export interface AssignmentItemsResp {
  ok: boolean;
  source: string;
  checked_at: number;
  fingerprint: string;
  items: AssignmentItem[];
  groups: AssignmentGroup[];
  projects: AssignmentProject[];
  unreachable: { ls_project_id: number; error: string }[];
  summary: AssignmentItemsSummary;
}

export function fetchAssignmentItems(): Promise<AssignmentItemsResp> {
  return get<AssignmentItemsResp>("/api/v1/me/assignment/items");
}

/** 把 LS 的 data payload 折成一行可读预览。只做展示，不改内容。 */
export function dataPreview(data: Record<string, unknown>, max = 110): string {
  const parts = Object.entries(data || {}).map(([k, v]) => {
    const s = typeof v === "string" ? v : JSON.stringify(v);
    return `${k}: ${s ?? ""}`;
  });
  const out = parts.join("   ·   ") || "（该任务没有 data 字段）";
  return out.length > max ? `${out.slice(0, max)}…` : out;
}

export function shortHash(h: string, n = 8): string {
  return (h || "").slice(0, n) || "—";
}

export function hhmm(epoch?: number): string {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
