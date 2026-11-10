/**
 * 前端状态层（Writable Store）
 * ---------------------------------------------------------------------------
 * 2026-11 起本层真正接上了 orchestrator：
 *   · 挂载时并发拉 9 个 GET 接口，把结果映射成前端类型填进 state
 *   · 写操作先调后端，成功后再用服务端返回的结果刷新本地（服务端为准）
 *   · 任何一个接口 403/404（角色没这个权限）都只影响那一个模块，
 *     不会把整页打回演示数据 —— 审核员看不到结算，不代表项目列表也看不了
 *
 * 2026-11-06：删掉 seed 兜底。
 *   旧版初始 state 里塞了一整套虚构项目/供应商/结算单，等真库回来再覆盖。
 *   只要后端慢一点、某个接口 403、token 过期，界面就会顶着「已连真库」的
 *   顶栏显示一批看起来完全真实的假数字。现在初始一律空数组，拉不到就显示
 *   空态 + 顶栏告警，"降级但说实话"，绝不"降级但装作没事"。
 *
 * 状态机约束在此处先拦一次（改善手感），服务端会再拦一次（最终为准）。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { FALLBACK_TENANT_ID } from "./brand";
import {
  rowsOf,
  toAsset,
  toAudit,
  toMember,
  toPerformance,
  toProject,
  toReview,
  toSettlement,
  toStaff,
  toSupplier,
  toTask,
} from "./adapters";
import { getToken, get, post, request, isBackendConfigured } from "./http";
import { can } from "./rbac";
import type {
  AnnotationTask,
  AuditEntry,
  DataAsset,
  PerformanceRecord,
  Principal,
  Project,
  ReviewRecord,
  RoleCode,
  Settlement,
  StaffAccount,
  Supplier,
  SupplierMember,
} from "./types";

const STORAGE_KEY = "forge.platform.state.v1";

/** 数据来自哪里 —— 顶栏据此决定要不要挂「后端未连接」告警 */
export type DataSource = "loading" | "api" | "offline";

export interface StoreState {
  projects: Project[];
  tasks: AnnotationTask[];
  reviews: ReviewRecord[];
  settlements: Settlement[];
  suppliers: Supplier[];
  members: SupplierMember[];
  /** 内部员工账号（forge.users）。与 members 是两套主体，别混。 */
  employees: StaffAccount[];
  assets: DataAsset[];
  performance: PerformanceRecord[];
  audit: AuditEntry[];
}

/**
 * 初始态一律为空。
 * 旧实现把 seed 里的虚构项目/供应商/结算单塞进初始 state，等真库拉回来再覆盖。
 * 后果很坏：后端慢一点、某个接口 403、甚至 token 过期，界面上就会显示一批
 * 看起来完全真实的假数据，还顶着「已连真库」的顶栏。空态 + 加载态才诚实。
 */
function initialState(): StoreState {
  return {
    projects: [],
    tasks: [],
    reviews: [],
    settlements: [],
    suppliers: [],
    members: [],
    employees: [],
    assets: [],
    performance: [],
    audit: [],
  };
}

/**
 * 只读后端缓存，不写 localStorage。
 *
 * 旧实现把整个 StoreState 序列化进 localStorage。接真库之后这条路必须断掉：
 * 一旦把服务端数据写进浏览器，同事看到的就是「上次打开时的快照」——
 * 任务已经改派了、结算已经付款了，界面还停在几天前，而且完全没有提示。
 * 缓存该由服务端和 HTTP 层负责，本地只缓存 token。
 */
function loadLegacyCache(): void {
  try {
    // 清掉历史版本留下的快照，避免新代码误读旧数据
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* 隐私模式下不可写，忽略 */
  }
}

type Result = { ok: boolean; reason?: string };
const ok = (): Result => ({ ok: true });
const fail = (reason: string): Result => ({ ok: false, reason });

/** 把 ApiError 翻成能给用户看的一句话 */
function reasonOf(e: unknown): string {
  const err = e as { status?: number; message?: string };
  if (err?.status === 403) return "当前角色没有这个操作的权限";
  if (err?.status === 404) return "对象不存在或无权访问";
  if (err?.status === 0) return "未连接后端";
  return err?.message || "操作失败";
}

export interface StoreApi extends StoreState {
  dataSource: DataSource;
  /** 后端连上了，但某些模块没权限/加载失败 —— 页面据此显示一条窄提示 */
  partialErrors: string[];
  reload(): Promise<void>;

  createProject(input: {
    name: string;
    client: string;
    modality: Project["modality"];
    totalItems: number;
    unitPrice: number;
    supplierIds: string[];
    dueAt: string;
    qcSamplingRate: number;
    labelingConfigName: string;
  }): Promise<{ project: Project | null; result: Result }>;
  createSupplier(input: {
    name: string;
    city: string;
    capacityHours: number;
    modalities: Supplier["modalities"];
  }): Promise<{ supplier: Supplier | null; result: Result }>;
  assignTask(taskId: string, supplierId: string, assigneeId: string): Promise<Result>;
  submitTask(taskId: string): Promise<Result>;
  decideReview(
    taskId: string,
    result: "pass" | "rework",
    note: string,
    stage: ReviewRecord["stage"],
    sampled?: number,
    defects?: number
  ): Promise<Result>;
  advanceSettlement(id: string): Promise<Result>;
  importAsset(input: {
    projectId: string;
    name: string;
    modality: DataAsset["modality"];
    itemCount: number;
    sizeMb: number;
  }): Promise<Result>;
  addMember(input: {
    supplierId: string;
    name: string;
    role: SupplierMember["role"];
    level: SupplierMember["level"];
  }): Promise<Result>;
  setSupplierStatus(id: string, status: Supplier["status"]): Promise<Result>;
  updateMemberRole(id: string, role: RoleCode | SupplierMember["role"]): Promise<Result>;
  /** 花名册成员绑定/解绑平台登录账号（绑上后该账号登录才看得见分给他的批次） */
  bindMemberUser(memberId: string, phone: string): Promise<Result>;
  unbindMemberUser(memberId: string): Promise<Result>;
  reset(): Promise<void>;
}

const StoreCtx = createContext<StoreApi | null>(null);

/** 资源各拉一次；各自独立 try，谁失败只记谁的账 */
async function fetchAll(tenantId: string, roles: RoleCode[]): Promise<{
  state: Partial<StoreState>;
  errors: string[];
  any: boolean;
}> {
  const state: Partial<StoreState> = {};
  const errors: string[] = [];
  let any = false;

  // 按角色裁剪拉取面：标注员登录时不该去打供应商/结算/审计接口
  // （就算打了也只是 404，但一排「加载失败」的噪音会让人以为系统坏了）。
  // 这是体验裁剪，不是安全边界 —— 每个接口自己有 require() 和 RLS。
  const allow = (action: Parameters<typeof can>[1]) =>
    can({ roles } as Principal, action);

  const jobs: [string, () => Promise<void>][] = [];

  if (allow("project:read")) jobs.push(["项目", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/projects");
    state.projects = rowsOf(r).map((x) => toProject(x, tenantId));
  }]);
  if (allow("supplier:read")) jobs.push(["供应商", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/suppliers");
    state.suppliers = rowsOf(r).map((x) => toSupplier(x, tenantId));
  }]);
  if (allow("member:read")) jobs.push(["人员", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/supplier-members");
    state.members = rowsOf(r).map((x) => toMember(x, tenantId));
  }]);
  if (allow("settings:write")) jobs.push(["内部员工", async () => {
    // 返回体是 {items:[...]} 不是 {rows:[...]}，与其它列表接口不一致；
    // 这是 auth.py 早期就定的形状，这里适配而不是去改它（改了会连带影响管理端）
    const r = await get<{ items?: Record<string, unknown>[] }>("/api/v1/admin/users");
    state.employees = (r.items ?? []).map((x) => toStaff(x, tenantId));
  }]);
  if (allow("task:read")) jobs.push(["任务", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/tasks?limit=500");
    state.tasks = rowsOf(r).map((x) => toTask(x, tenantId));
  }]);
  if (allow("review:read")) jobs.push(["审核", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/reviews?limit=500");
    state.reviews = rowsOf(r).map((x) => toReview(x, tenantId));
  }]);
  if (allow("settlement:read")) jobs.push(["结算", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/settlements");
    state.settlements = rowsOf(r).map((x) => toSettlement(x, tenantId));
  }]);
  if (allow("performance:read")) jobs.push(["绩效", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/performance");
    state.performance = rowsOf(r).map((x) => toPerformance(x, tenantId));
  }]);
  if (allow("asset:read")) jobs.push(["数据资产", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/assets");
    state.assets = rowsOf(r).map((x) => toAsset(x, tenantId));
  }]);
  if (allow("audit:read")) jobs.push(["审计", async () => {
    const r = await get<{ rows: Record<string, unknown>[] }>("/api/v1/audit?limit=200");
    state.audit = rowsOf(r).map((x) => toAudit(x, tenantId));
  }]);

  const results = await Promise.allSettled(jobs.map(([, fn]) => fn()));
  results.forEach((res, i) => {
    if (res.status === "fulfilled") {
      any = true;
    } else {
      errors.push(`${jobs[i][0]}：${reasonOf(res.reason)}`);
    }
  });

  return { state, errors, any };
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<StoreState>(initialState);
  const [dataSource, setDataSource] = useState<DataSource>(
    isBackendConfigured() && getToken() ? "loading" : "offline"
  );
  const [partialErrors, setPartialErrors] = useState<string[]>([]);
  // 防止并发 reload 互相覆盖：只认最后一次发起的请求
  const reqSeq = useRef(0);

  const reload = useCallback(async () => {
    if (!isBackendConfigured() || !getToken()) {
      setDataSource("offline");
      return;
    }
    const mine = ++reqSeq.current;
    let me: { tenant_id?: string; roles?: RoleCode[] } = {};
    try {
      // 与 lib/auth.tsx 同一个接口：身份只认 /auth/me，不另开一份。
      // main.py 里还留着一个早期的 /api/v1/me，返回字段略不同（sub 而非 id），
      // 两边各取一份迟早会漂移。
      me = await get<{ tenant_id?: string; roles?: RoleCode[] }>("/api/v1/auth/me");
    } catch {
      // /me 都失败说明 token 已经不可用了，交给 auth 层处理，这里只降级
      if (mine === reqSeq.current) setDataSource("offline");
      return;
    }
    const tenantId = String(me.tenant_id ?? FALLBACK_TENANT_ID);
    const { state: fresh, errors, any } = await fetchAll(tenantId, me.roles ?? []);
    if (mine !== reqSeq.current) return; // 有更新的一次 reload 已经跑过了
    setState((prev) => ({ ...prev, ...fresh }) as StoreState);
    setPartialErrors(errors);
    setDataSource(any ? "api" : "offline");
  }, []);

  useEffect(() => {
    loadLegacyCache();
    void reload();
  }, [reload]);

  // -------------------------------------------------------------------------
  // 项目
  // -------------------------------------------------------------------------
  const createProject = useCallback<StoreApi["createProject"]>(async (input) => {
    try {
      const res = await post<{ ok: boolean; project: Record<string, unknown> }>(
        "/api/v1/projects",
        {
          name: input.name,
          client: input.client,
          modality: input.modality,
          total_items: input.totalItems,
          unit_price: input.unitPrice,
          due_at: input.dueAt,
          qc_sampling_rate: input.qcSamplingRate,
          labeling_config_name: input.labelingConfigName,
          supplier_ids: input.supplierIds,
        }
      );
      const project = toProject(res.project, FALLBACK_TENANT_ID);
      setState((s) => ({ ...s, projects: [project, ...s.projects] }));
      // 审计记录由后端写，前端不本地捏一条（旧实现连操作人、IP 都是写死的）。
      // 同时后端返回的是列表视图的字段子集，补一次全量拉取把进度字段和真实审计带回来。
      void reload();
      return { project, result: ok() };
    } catch (e) {
      return { project: null, result: fail(reasonOf(e)) };
    }
  }, [reload]);

  // -------------------------------------------------------------------------
  // 供应商
  // -------------------------------------------------------------------------
  const createSupplier = useCallback<StoreApi["createSupplier"]>(async (input) => {
    try {
      const res = await post<{ ok: boolean; supplier: Record<string, unknown> }>(
        "/api/v1/suppliers",
        {
          name: input.name,
          city: input.city,
          capacity_hours: input.capacityHours,
          modalities: input.modalities,
        }
      );
      const supplier = toSupplier(res.supplier, FALLBACK_TENANT_ID);
      setState((s) => ({ ...s, suppliers: [supplier, ...s.suppliers] }));
      void reload();
      return { supplier, result: ok() };
    } catch (e) {
      return { supplier: null, result: fail(reasonOf(e)) };
    }
  }, [reload]);

  const setSupplierStatus = useCallback<StoreApi["setSupplierStatus"]>(async (id, status) => {
    try {
      await post(`/api/v1/suppliers/${encodeURIComponent(id)}/status`, { status });
    } catch (e) {
      return fail(reasonOf(e));
    }
    setState((s) => ({
      ...s,
      suppliers: s.suppliers.map((x) => (x.id === id ? { ...x, status } : x)),
    }));
    return ok();
  }, []);

  // -------------------------------------------------------------------------
  // 任务
  // -------------------------------------------------------------------------
  const assignTask = useCallback<StoreApi["assignTask"]>(
    async (taskId, supplierId, assigneeId) => {
      // 本地先做一次归属校验，避免无意义的往返；服务端仍会完整校验
      const member = state.members.find((m) => m.id === assigneeId);
      if (!member || member.supplierId !== supplierId) {
        return fail("标注员不属于该供应商，分配被拒绝");
      }
      try {
        await post(`/api/v1/tasks/${encodeURIComponent(taskId)}/assign`, {
          supplier_id: supplierId,
          assignee_id: assigneeId,
        });
      } catch (e) {
        return fail(reasonOf(e));
      }
      setState((s) => ({
        ...s,
        tasks: s.tasks.map((x) =>
          x.id === taskId
            ? {
                ...x,
                supplierId,
                assigneeId,
                assigneeName: member.name,
                status: "assigned",
              }
            : x
        ),
        members: s.members.map((m) =>
          m.id === assigneeId ? { ...m, activeTasks: m.activeTasks + 1 } : m
        ),
      }));
      return ok();
    },
    [state.members]
  );

  const submitTask = useCallback<StoreApi["submitTask"]>(async (taskId) => {
    const t = state.tasks.find((x) => x.id === taskId);
    try {
      // item_count 不传则后端按任务总量记，这里显式传 doneCount 更贴近实际提交数
      await post(`/api/v1/tasks/${encodeURIComponent(taskId)}/submit`, {
        item_count: t?.itemCount ?? 0,
      });
    } catch (e) {
      return fail(reasonOf(e));
    }
    setState((s) => ({
      ...s,
      tasks: s.tasks.map((x) =>
        x.id === taskId ? { ...x, status: "submitted", doneCount: x.itemCount } : x
      ),
    }));
    return ok();
  }, [state.tasks]);

  // -------------------------------------------------------------------------
  // 审核
  // -------------------------------------------------------------------------
  const decideReview = useCallback<StoreApi["decideReview"]>(
    async (taskId, result, note, stage, sampled = 0, defects = 0) => {
      // 抽检数 / 缺陷数必须由审核员按实际结果填写。
      // 旧实现在前端按 itemCount * 5% 反推 sampled、按 result 反推 defects，
      // 于是每一条「审核记录」里的质量数字都是编的，绩效与结算还会拿它当真。
      try {
        await post(`/api/v1/tasks/${encodeURIComponent(taskId)}/review`, {
          stage,
          result,
          note,
          sampled: Math.max(0, Math.round(sampled)),
          defects: Math.max(0, Math.round(defects)),
        });
      } catch (e) {
        return fail(reasonOf(e));
      }
      // 服务端会写一条 reviews 记录并把任务推进到 accepted / rework。
      // 与其在前端照抄一遍规则（一定漂移），不如直接重取。
      void reload();
      return ok();
    },
    [reload]
  );

  // -------------------------------------------------------------------------
  // 结算
  // -------------------------------------------------------------------------
  const advanceSettlement = useCallback<StoreApi["advanceSettlement"]>(async (id) => {
    try {
      await post(`/api/v1/settlements/${encodeURIComponent(id)}/advance`);
    } catch (e) {
      return fail(reasonOf(e));
    }
    void reload();
    return ok();
  }, [reload]);

  // -------------------------------------------------------------------------
  // 数据资产
  // -------------------------------------------------------------------------
  const importAsset = useCallback<StoreApi["importAsset"]>(async (input) => {
    try {
      await post("/api/v1/assets/import", {
        project_id: input.projectId,
        // 批次号由后端唯一约束 (project_id, batch_no, name) 兜重复；
        // 这里按当天日期生成，同一天同项目多批会撞 —— 用时间戳后 4 位区分
        batch_no: `B${new Date().toISOString().slice(0, 10).replace(/-/g, "")}-${String(
          Date.now() % 10000
        ).padStart(4, "0")}`,
        name: input.name,
        modality: input.modality,
        size_mb: input.sizeMb,
        item_count: input.itemCount,
      });
    } catch (e) {
      return fail(reasonOf(e));
    }
    void reload();
    return ok();
  }, [reload]);

  // -------------------------------------------------------------------------
  // 人员
  // -------------------------------------------------------------------------
  const addMember = useCallback<StoreApi["addMember"]>(async (input) => {
    // 后端还没有 POST /supplier-members —— 不假装成功，明确告诉调用方
    void input;
    return fail("后端尚未提供新增成员接口，请在 Label Studio 或数据库中维护");
  }, []);

  const updateMemberRole = useCallback<StoreApi["updateMemberRole"]>(async (id, role) => {
    void id;
    void role;
    return fail("后端尚未提供修改成员角色接口");
  }, []);

  const bindMemberUser = useCallback<StoreApi["bindMemberUser"]>(async (memberId, phone) => {
    try {
      const res = await post<{ ok: boolean; user_id: string; user_phone: string }>(
        `/api/v1/supplier-members/${encodeURIComponent(memberId)}/bind-user`,
        { phone }
      );
      setState((s) => ({
        ...s,
        members: s.members.map((m) =>
          m.id === memberId ? { ...m, userId: res.user_id, userPhone: res.user_phone } : m
        ),
      }));
      return ok();
    } catch (e: unknown) {
      return fail(reasonOf(e));
    }
  }, []);

  const unbindMemberUser = useCallback<StoreApi["unbindMemberUser"]>(async (memberId) => {
    try {
      await request<{ ok: boolean }>(
        `/api/v1/supplier-members/${encodeURIComponent(memberId)}/bind-user`,
        { method: "DELETE" }
      );
      setState((s) => ({
        ...s,
        members: s.members.map((m) =>
          m.id === memberId ? { ...m, userId: null, userPhone: null } : m
        ),
      }));
      return ok();
    } catch (e: unknown) {
      return fail(reasonOf(e));
    }
  }, []);

  const reset = useCallback(async () => {
    // 「重置」原先只清 localStorage。接真库后本地没有可清的业务数据了，
    // 语义变成「重新拉一遍」，否则用户会以为按了就会恢复演示数据。
    await reload();
  }, [reload]);

  const value = useMemo<StoreApi>(
    () => ({
      ...state,
      dataSource,
      partialErrors,
      reload,
      createProject,
      createSupplier,
      assignTask,
      submitTask,
      decideReview,
      advanceSettlement,
      importAsset,
      addMember,
      setSupplierStatus,
      updateMemberRole,
      bindMemberUser,
      unbindMemberUser,
      reset,
    }),
    [
      state,
      dataSource,
      partialErrors,
      reload,
      createProject,
      createSupplier,
      assignTask,
      submitTask,
      decideReview,
      advanceSettlement,
      importAsset,
      addMember,
      setSupplierStatus,
      updateMemberRole,
      bindMemberUser,
      unbindMemberUser,
      reset,
    ]
  );

  return <StoreCtx.Provider value={value}>{children}</StoreCtx.Provider>;
}

export function useStore() {
  const s = useContext(StoreCtx);
  if (!s) throw new Error("StoreProvider missing");
  return s;
}
