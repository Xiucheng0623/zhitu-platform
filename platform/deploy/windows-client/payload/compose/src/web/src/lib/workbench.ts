/**
 * 标注工作台入口 —— 整页进入，不嵌套 iframe
 * ============================================================================
 * 平台外壳只负责「选中哪个批次 + 换一张一次性票据」，然后整页导航到已经
 * 品牌化的 Label Studio 工作台。标注器占满整个浏览器窗口，不再是侧栏里
 * 一块 600px 的小窗，也不再套一层写满假数据的空壳。
 *
 * 票据由编排层按 task_id 签发，LS 侧 forge_sso.py 验签后写 Django session，
 * 员工全程只认平台账号。
 */
import { get } from "./http";
import type { AnnotationTask } from "./types";

export interface LsTicket {
  ok: boolean;
  url: string;
  expires_at: number;
  ttl_seconds: number;
}

/**
 * 默认进哪个批次。
 * 旧实现直接取 active[0]，结果管理员点「标注工作台」被送进一个已经 submitted
 * 待审的批次，而真正待作业的新批次排在后面。按「可作业状态 → 最新创建」排。
 */
const STATUS_RANK: Record<string, number> = {
  in_progress: 0,
  assigned: 1,
  rework: 2,
  pending: 3,
  submitted: 4,
  reviewing: 5,
};

export function pickWorkbenchTask(tasks: AnnotationTask[]): AnnotationTask | null {
  const withLs = tasks.filter((t) => t.lsProjectId != null);
  const rank = (t: AnnotationTask) => STATUS_RANK[t.status] ?? 9;
  return (
    [...withLs].sort(
      (a, b) =>
        rank(a) - rank(b) ||
        String(b.createdAt ?? "").localeCompare(String(a.createdAt ?? ""))
    )[0] ?? null
  );
}

/**
 * `lsTaskId` 是「从平台数据行列表里点第 N 条」用的：带上它，服务端会先
 * 核实这条 LS 任务属于该账号可见的项目，再把票据的落地地址精确到
 * `?task=<id>`。于是平台里显示的那一行，就是标注员在 LS 里打开的那一条。
 * 不带时仍是整批进标注流（老入口，行为不变）。
 */
export async function workbenchUrl(taskId: string,
                                   lsTaskId?: number): Promise<string> {
  const q = lsTaskId
    ? `&ls_task_id=${encodeURIComponent(String(lsTaskId))}`
    : "";
  const t = await get<LsTicket>(
    `/api/v1/ls/ticket?task_id=${encodeURIComponent(taskId)}${q}`);
  if (!t?.url) throw new Error("票据接口没有返回标注工作台地址");
  return t.url;
}

/** 整页跳转。调用方负责在进入前显示品牌启动态。 */
export async function enterWorkbench(taskId: string,
                                     lsTaskId?: number): Promise<void> {
  const url = await workbenchUrl(taskId, lsTaskId);
  window.location.assign(url);
}

/** LS 里「返回平台」按钮的目标，允许部署时通过 VITE_PLATFORM_URL 覆盖。 */
export function platformEntry(): string {
  const raw = (import.meta.env.VITE_PLATFORM_URL as string | undefined) ?? "";
  return raw.replace(/\/+$/, "") || "/";
}
