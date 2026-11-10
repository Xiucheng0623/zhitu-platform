/**
 * 后端行 → 前端类型 的映射层
 * ---------------------------------------------------------------------------
 * 为什么需要这一层，而不是让后端直接吐 camelCase：
 *   服务端返回的是数据库列名（snake_case），这是有意的 —— 它让「接口返回了什么」
 *   与「表里存了什么」一一可对，排查时不用在脑子里做一次名字翻译。
 *   前端类型用的是 camelCase，因为要在 JSX 里当属性访问。
 *   两边都不该为对方让步，所以翻译放在这里，一次，显式。
 *
 * 三条硬规则：
 *   1. 后端没返回的字段，宁可给 null / 空数组，也不许编一个"看起来合理"的值。
 *      前端如果因为 null 崩了，那是前端该修的地方，不是映射层该骗人的地方。
 *   2. numeric 列一律以字符串到前端（避免 JS 浮点把 0.36 变成 0.36000000000000004），
 *      在这里统一 Number()。钱的字段误差在小数点后第 15 位也会被财务抓住。
 *   3. 任何 `?` 可选字段都写默认值并注释来源，别让后来人猜。
 */
import type {
  AnnotationTask,
  AuditEntry,
  DataAsset,
  PerformanceRecord,
  Project,
  ReviewRecord,
  RoleCode,
  Settlement,
  StaffAccount,
  Supplier,
  SupplierMember,
} from "./types";

/** 后端行：字段名不做约束，一律 unknown 取值后显式转换 */
type Row = Record<string, unknown>;

const str = (v: unknown, dft = ""): string =>
  v === null || v === undefined ? dft : String(v);

const num = (v: unknown, dft = 0): number => {
  if (v === null || v === undefined || v === "") return dft;
  const n = Number(v);
  return Number.isFinite(n) ? n : dft;
};

/** 可空数字：null 就该是 null，不要变成 0（"质检得分 0 分"与"没测过"是两件事） */
const numOrNull = (v: unknown): number | null =>
  v === null || v === undefined || v === "" ? null : num(v);

const iso = (v: unknown, dft = ""): string => {
  if (v === null || v === undefined) return dft;
  if (typeof v === "string") return v;
  if (v instanceof Date) return v.toISOString();
  return String(v);
};

const isoOrNull = (v: unknown): string | null =>
  v === null || v === undefined ? null : iso(v);

const arr = <T extends string>(v: unknown, allowed: readonly T[], dft: T[] = []): T[] => {
  if (!Array.isArray(v)) return dft;
  const out = v.filter((x): x is T => typeof x === "string" && (allowed as readonly string[]).includes(x));
  return out.length ? out : dft;
};

const oneOf = <T extends string>(v: unknown, allowed: readonly T[], dft: T): T => {
  const s = typeof v === "string" ? v : "";
  return (allowed as readonly string[]).includes(s) ? (s as T) : dft;
};

const bool = (v: unknown): boolean => v === true || v === "t" || v === 1;

const MODALITIES = ["image", "video", "audio", "text", "multimodal"] as const;

// ---------------------------------------------------------------------------
// 供应商
// ---------------------------------------------------------------------------
export function toSupplier(r: Row, tenantId: string): Supplier {
  return {
    id: str(r.id),
    tenantId: str(r.enterprise_id, tenantId),
    code: str(r.code),
    name: str(r.name),
    // short_name 在库里是非空列，但历史数据可能只有 name，兜一下
    shortName: str(r.short_name) || str(r.name).slice(0, 4),
    tier: oneOf(r.tier, ["S", "A", "B", "C"] as const, "C"),
    status: oneOf(r.status, ["active", "probation", "qualifying", "suspended"] as const, "qualifying"),
    modalities: arr(r.modalities, MODALITIES),
    city: str(r.city, "—"),
    contactName: str(r.contact_name, "—"),
    contactPhone: str(r.contact_phone, "—"),
    capacityHours: num(r.capacity_hours),
    committedHours: num(r.committed_hours),
    annotatorCount: num(r.annotator_count),
    qualityScore: num(r.quality_score),
    onTimeRate: num(r.on_time_rate),
    reworkRate: num(r.rework_rate),
    contractCount: num(r.contract_count),
    onboardedAt: iso(r.onboarded_at),
    qualification: {
      businessLicense: bool(r.qual_business_license),
      ndaSigned: bool(r.qual_nda_signed),
      dataSecurityAudit: bool(r.qual_security_audit),
      lastAuditedAt: isoOrNull(r.qual_last_audited_at),
    },
  };
}

// ---------------------------------------------------------------------------
// 供应商成员
// ---------------------------------------------------------------------------
export function toMember(r: Row, tenantId: string): SupplierMember {
  const name = str(r.name);
  return {
    id: str(r.id),
    tenantId: str(r.enterprise_id, tenantId),
    supplierId: str(r.supplier_id),
    name,
    // 后端存了 initials 但可能是 null；空字符串会让头像变成一个空白圆圈
    initials: str(r.initials) || name.slice(0, 1),
    role: oneOf(r.role, ["supplier_admin", "supplier_lead", "annotator"] as const, "annotator"),
    skills: arr(r.skills, MODALITIES),
    level: oneOf(r.level, ["senior", "mid", "junior"] as const, "mid"),
    status: oneOf(r.status, ["active", "inactive"] as const, "active"),
    passRate: num(r.pass_rate),
    completedTotal: num(r.completed_total),
    activeTasks: num(r.active_tasks),
    joinedAt: iso(r.joined_at),
    userId: r.user_id != null ? str(r.user_id) : null,
    userPhone: r.user_phone != null ? str(r.user_phone) : null,
  };
}

// ---------------------------------------------------------------------------
// 项目
// ---------------------------------------------------------------------------
/**
 * 注意字段来源不同：id/code/name/status/进度来自视图 v_project_progress，
 * client/budget/unit_price/labeling_* 来自 projects 表本体。
 * v_project_progress 里没有 manager_name、没有 supplier_ids、没有 start_at ——
 * 这三个在前端是必填，这里只能给安全的空值，不能编。
 */
export function toProject(r: Row, tenantId: string): Project {
  const total = num(r.total_items);
  const unitPrice = num(r.unit_price);
  return {
    id: str(r.id),
    tenantId,
    code: str(r.code),
    name: str(r.name),
    client: str(r.client, "—"),
    modality: oneOf(r.modality, MODALITIES, "text"),
    status: oneOf(r.status,
      ["draft", "planning", "running", "paused", "accepting", "closed"] as const, "draft"),
    // 视图没带出来：留空字符串，UI 上显示「—」比显示一个假的「陆一鸣」诚实
    managerId: str(r.manager_id),
    managerName: str(r.manager_name),
    totalItems: total,
    deliveredItems: num(r.delivered_items),
    supplierIds: Array.isArray(r.supplier_ids) ? r.supplier_ids.map(String) : [],
    // 视图只有 due_at，没有 start_at
    startAt: iso(r.start_at),
    dueAt: iso(r.due_at),
    unitPrice,
    // budget 库里可能有值也可能为 0；为 0 时用 单价×总量 估算没有意义
    // （预算里还含管理费），所以就用库里的值。
    budget: num(r.budget),
    passRate: num(r.pass_rate),
    labelingConfigId: str(r.labeling_config_id),
    labelingConfigName: str(r.labeling_config_name, "标准模板"),
    qcSamplingRate: num(r.qc_sampling_rate, 10),
    maxRework: num(r.max_rework, 3),
  };
}

// ---------------------------------------------------------------------------
// 任务
// ---------------------------------------------------------------------------
export function toTask(r: Row, tenantId: string): AnnotationTask {
  return {
    id: str(r.id),
    tenantId: str(r.enterprise_id, tenantId),
    projectId: str(r.project_id),
    // 后端已 join 出 project_name（left join，项目被删时为空）
    projectName: str(r.project_name),
    batchNo: str(r.batch_no),
    name: str(r.name),
    modality: oneOf(r.modality, MODALITIES, "text"),
    status: oneOf(r.status,
      ["pending", "assigned", "in_progress", "submitted", "reviewing",
       "rework", "accepted", "rejected"] as const, "pending"),
    supplierId: r.supplier_id ? str(r.supplier_id) : null,
    assigneeId: r.assignee_id ? str(r.assignee_id) : null,
    assigneeName: r.assignee_name ? str(r.assignee_name) : null,
    // 账号 id 映射（012）：任务派给花名册成员（mem_*），登录按账号（usr_*）。
    // 前端「我的任务」过滤必须拿 assigneeUserId 跟 me.id 比，拿 assigneeId
    // 直比 usr_* 会恒不等，把自己的任务全滤没。
    assigneeUserId: r.assignee_user_id ? str(r.assignee_user_id) : null,
    itemCount: num(r.item_count),
    doneCount: num(r.done_count),
    reworkRound: num(r.rework_round),
    unitPrice: num(r.unit_price),
    dueAt: iso(r.due_at),
    createdAt: iso(r.created_at),
    // LS 未同步时是 null，前端用它判断"能不能点进标注"
    lsProjectId: numOrNull(r.ls_project_id),
    priority: oneOf(r.priority, ["high", "normal", "low"] as const, "normal"),
  };
}

// ---------------------------------------------------------------------------
// 审核记录
// ---------------------------------------------------------------------------
export function toReview(r: Row, tenantId: string): ReviewRecord {
  return {
    id: str(r.id),
    tenantId,
    taskId: str(r.task_id),
    taskName: str(r.task_name),
    projectName: str(r.project_name),
    reviewerId: str(r.reviewer_id),
    // ops.py 现在会 coalesce(users.name, supplier_members.name)；
    // 两个都查不到就退回 id —— 显示一串 usr_xxx 很丑，但比"审核人：—"有用
    reviewerName: str(r.reviewer_name) || str(r.reviewer_id),
    stage: oneOf(r.stage, ["l1", "l2", "qc"] as const, "l1"),
    result: oneOf(r.result, ["pass", "rework", "reject"] as const, "rework"),
    sampled: num(r.sampled),
    defects: num(r.defects),
    note: str(r.note),
    reviewedAt: iso(r.reviewed_at),
  };
}

// ---------------------------------------------------------------------------
// 结算
// ---------------------------------------------------------------------------
export function toSettlement(r: Row, tenantId: string): Settlement {
  const accepted = num(r.accepted_items);
  const amount = num(r.amount);
  const avg = numOrNull(r.unit_price_avg);
  return {
    id: str(r.id),
    tenantId,
    code: str(r.code),
    supplierId: str(r.supplier_id),
    supplierName: str(r.supplier_name),
    period: str(r.period),
    acceptedItems: accepted,
    // 库里有 unit_price_avg；老数据为 0 时用 金额/条数 兜底（条数也为 0 才给 0）
    unitPriceAvg: avg ?? (accepted > 0 ? amount / accepted : 0),
    amount,
    deduction: num(r.deduction),
    payable: num(r.payable),
    status: oneOf(r.status,
      ["draft", "submitted", "confirmed", "invoiced", "paid"] as const, "draft"),
    purchaseOrder: r.purchase_order ? str(r.purchase_order) : null,
    purchaseInvoice: r.purchase_invoice ? str(r.purchase_invoice) : null,
    paymentEntry: r.payment_entry ? str(r.payment_entry) : null,
    createdAt: iso(r.created_at),
    dueAt: iso(r.due_at),
  };
}

// ---------------------------------------------------------------------------
// 绩效
// ---------------------------------------------------------------------------
export function toPerformance(r: Row, tenantId: string): PerformanceRecord {
  return {
    id: str(r.id),
    tenantId,
    supplierId: str(r.supplier_id),
    supplierName: str(r.supplier_name),
    period: str(r.period),
    qualityScore: num(r.quality_score),
    onTimeRate: num(r.on_time_rate),
    reworkRate: num(r.rework_rate),
    throughput: num(r.throughput),
    composite: num(r.composite),
    rank: num(r.rank),
  };
}

// ---------------------------------------------------------------------------
// 数据资产
// ---------------------------------------------------------------------------
export function toAsset(r: Row, tenantId: string): DataAsset {
  return {
    id: str(r.id),
    tenantId,
    projectId: str(r.project_id),
    batchNo: str(r.batch_no),
    name: str(r.name),
    modality: oneOf(r.modality, MODALITIES, "image"),
    status: oneOf(r.status,
      ["raw", "in_annotation", "in_review", "accepted", "archived"] as const, "raw"),
    sizeMb: num(r.size_mb),
    itemCount: num(r.item_count),
    importedAt: iso(r.imported_at),
    qcScore: numOrNull(r.qc_score),
    // 后端只存 storage_key，没存 storage 类型；当前实际部署是 MinIO(S3 兼容)
    storage: "s3",
  };
}

// ---------------------------------------------------------------------------
// 审计
// ---------------------------------------------------------------------------
export function toAudit(r: Row, tenantId: string): AuditEntry {
  return {
    id: String(r.id ?? ""),
    tenantId: str(r.enterprise_id, tenantId),
    actorName: str(r.actor_name, "—"),
    action: str(r.action),
    target: str(r.target),
    detail: str(r.detail),
    at: iso(r.at),
    ip: str(r.ip, "—"),
  };
}

// ---------------------------------------------------------------------------
// 内部员工账号
// ---------------------------------------------------------------------------
const ROLE_CODES: readonly RoleCode[] = [
  "platform_admin", "ops_director", "finance", "project_manager", "data_engineer",
  "reviewer", "supplier_admin", "supplier_lead", "annotator", "client_viewer",
];

/** 角色码 → 中文职务文案（与 lib/auth.tsx 的 ROLE_LABEL 保持一致） */
export const ROLE_TEXT: Record<string, string> = {
  platform_admin: "平台管理员",
  ops_director: "运营总监",
  finance: "财务",
  project_manager: "项目经理",
  data_engineer: "数据工程师",
  reviewer: "审核员",
  supplier_admin: "供应商管理员",
  supplier_lead: "供应商组长",
  annotator: "标注员",
  client_viewer: "客户观察员",
};

export function toStaff(r: Row, tenantId: string): StaffAccount {
  return {
    id: str(r.id),
    tenantId,
    name: str(r.name) || str(r.phone, "未命名"),
    phone: str(r.phone),
    roles: arr(r.roles, ROLE_CODES),
    status: oneOf(r.status, ["pending", "active", "suspended"] as const, "pending"),
    mustChangePassword: r.must_change_password === true,
    lastLoginAt: isoOrNull(r.last_login_at),
    createdAt: iso(r.created_at),
  };
}

// ---------------------------------------------------------------------------
// 当前登录主体
// ---------------------------------------------------------------------------
/** /api/v1/me 的返回 → 前端 Principal */
export function toPrincipal(me: Row): {
  id: string;
  tenantId: string;
  name: string;
  roles: RoleCode[];
  supplierId: string | null;
  email: string;
  isInternal: boolean;
} {
  const roles = Array.isArray(me.roles) ? me.roles.map(String) : [];
  return {
    id: str(me.sub),
    tenantId: str(me.tenant_id),
    name: str(me.name),
    roles: roles as RoleCode[],
    supplierId: me.supplier_id ? str(me.supplier_id) : null,
    email: str(me.email),
    isInternal: me.is_internal === true,
  };
}

/** 把 {rows:[...]} 这种列表响应统一取出来 —— 后端有两种分页形态，别在页面里各写一遍 */
export function rowsOf<T>(payload: { rows?: T[] } | null | undefined): T[] {
  return Array.isArray(payload?.rows) ? (payload!.rows as T[]) : [];
}
