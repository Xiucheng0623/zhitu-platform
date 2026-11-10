/**
 * 真实后端客户端（Phase 2 接线点）
 * ---------------------------------------------------------------------------
 * 页面数据经 store.tsx 的 reload() 并发拉取 orchestrator 列表接口。
 * 本文件负责「真的连到 orchestrator」：请求、鉴权、连通性探测。
 *
 * 约定：
 *   - VITE_API_BASE 为空 → 视为未配置后端，探测返回 not_configured，
 *     页面保持空态，绝不伪装成「已连接」。
 *   - token 存 localStorage（forge.platform.token），业务数据不落本地。
 */
const RAW_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
export const API_BASE = RAW_BASE.replace(/\/+$/, "");
export const TOKEN_KEY = "forge.platform.token";

export type ProbeState = "ok" | "degraded" | "fail" | "unknown";

export interface ProbeRow {
  /** database | labelstudio | storage | payments */
  key: string;
  state: ProbeState;
  detail: string;
}

export interface ProbeResult {
  configured: boolean;
  version?: string;
  authMode?: string;
  rows: ProbeRow[];
  at: number;
}

export function isBackendConfigured(): boolean {
  return API_BASE.length > 0;
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式下 localStorage 不可写，忽略 */
  }
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

/**
 * 全局 401 回调。
 * 会话层注册后，任何一次请求拿到 401 都能把界面切回登录页，
 * 而不是让每个页面各自处理一遍「token 过期」。
 */
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!isBackendConfigured()) {
    throw new ApiError(0, "not_configured", "未配置 VITE_API_BASE");
  }
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // 网关/代理返回的 HTML 错误页（502/504 常见），不能直接 JSON.parse，
    // 否则前端会抛 SyntaxError 而完全看不到 502 这个真正的原因。
    throw new ApiError(res.status, `http_${res.status}`,
      text.slice(0, 200) || res.statusText);
  }
  if (!res.ok) {
    const err = body?.error;
    if (res.status === 401 && !path.startsWith("/api/v1/auth/login")) {
      // 登录接口自己的 401 是「密码错误」，不该触发登出
      setToken("");
      onUnauthorized?.();
    }
    throw new ApiError(res.status, err?.code ?? `http_${res.status}`,
      err?.message ?? res.statusText);
  }
  return body as T;
}

/** 便捷动词。页面里写 `get<T>("/api/v1/projects")` 比每次拼 RequestInit 干净。 */
export function get<T>(path: string): Promise<T> {
  return request<T>(path);
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function patch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function clearToken(): void {
  setToken("");
}

/** 探测外部系统 + 数据库 + 对象存储的真实状态 */
export async function probeBackend(): Promise<ProbeResult> {
  const at = Date.now();
  if (!isBackendConfigured()) {
    return { configured: false, rows: [], at };
  }

  const rows: ProbeRow[] = [];
  let version: string | undefined;
  let authMode: string | undefined;

  // 1) 数据库（/healthz 的 db 段）
  try {
    const h = await request<{ ok: boolean; version: string; auth_mode: string; db: any }>("/healthz");
    version = h.version;
    authMode = h.auth_mode;
    rows.push({
      key: "database",
      state: h.db?.ok ? "ok" : "fail",
      detail: h.db?.ok
        ? `PG ${h.db.pg} · ${h.db.rls_forced}/${h.db.tables} 表强制 RLS`
        : h.db?.error ?? "不可用",
    });
  } catch (e) {
    rows.push({ key: "database", state: "fail", detail: (e as Error).message });
  }

  // 2) 外部系统（需要 audit:read 权限，失败时降级为 unknown 而非伪装成功）
  //    2026-11 起 ERPNext 已移除（docs/02 §9 ADR-001），
  //    集成面只剩 Label Studio 与对象存储 —— 不要再去读 r.rows.erpnext，
  //    取 undefined.ok 会直接抛 TypeError，把一个「降级」变成「白屏」。
  try {
    const r = await request<{
      rows: { labelstudio?: { ok: boolean; user?: string; error?: string } };
      storage: { ok: boolean; bucket?: string; error?: string };
    }>("/api/v1/integrations/health");

    const ls = r.rows.labelstudio;
    rows.push({
      key: "labelstudio",
      state: ls?.ok ? "ok" : "fail",
      detail: ls?.ok ? `用户 ${ls.user ?? "—"}` : ls?.error ?? "不可达",
    });
    rows.push({
      key: "storage",
      state: r.storage.ok ? "ok" : "fail",
      detail: r.storage.ok ? `bucket ${r.storage.bucket}` : r.storage.error ?? "不可达",
    });
    // 未做任何标注引擎改造时的现状：供应商门户与结算等模块仍在本地存储层，
    // 这里显式暴露出来，避免运维以为「全都连上了」。
    rows.push({
      key: "payments",
      state: "unknown",
      detail: "付款执行走线下银行，平台只登记流水与回单号",
    });
  } catch (e) {
    const err = e as ApiError;
    const detail = err.status === 404 ? "当前身份无 audit:read 权限" : err.message;
    for (const key of ["labelstudio", "storage"]) {
      rows.push({ key, state: "unknown", detail });
    }
  }

  return { configured: true, version, authMode, rows, at };
}
