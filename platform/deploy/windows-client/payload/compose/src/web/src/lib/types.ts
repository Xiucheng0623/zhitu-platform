/**
 * 领域模型 · 与 services/orchestrator 的 canonical schema 一一对应
 * 所有实体携带 tenantId —— 多租户隔离在数据层强制，不在 UI 层过滤。
 */

export type ID = string;

export type TenantId = string;

/** 主体类型：决定数据可见域 */
export type PrincipalKind = "employee" | "supplier_member" | "annotator" | "reviewer" | "client";

export type RoleCode =
  | "platform_admin"
  | "ops_director"
  | "finance"
  | "project_manager"
  | "data_engineer"
  | "reviewer"
  | "supplier_admin"
  | "supplier_lead"
  | "annotator"
  | "client_viewer";

export interface Principal {
  id: ID;
  tenantId: TenantId;
  name: string;
  kind: PrincipalKind;
  roles: RoleCode[];
  /** 供应商侧主体的归属供应商，内部员工为 null */
  supplierId: ID | null;
  email: string;
  title: string;
  initials: string;
}

export interface Enterprise {
  id: TenantId;
  name: string;
  shortName: string;
  plan: "internal" | "saas";
  seats: number;
}

export type SupplierTier = "S" | "A" | "B" | "C";
export type SupplierStatus = "active" | "probation" | "qualifying" | "suspended";

export interface Supplier {
  id: ID;
  tenantId: TenantId;
  code: string;
  name: string;
  shortName: string;
  tier: SupplierTier;
  status: SupplierStatus;
  /** 主营模态 */
  modalities: Modality[];
  city: string;
  contactName: string;
  contactPhone: string;
  /** 月度产能（标注工时/月） */
  capacityHours: number;
  /** 当前在制工时 */
  committedHours: number;
  annotatorCount: number;
  qualityScore: number;
  onTimeRate: number;
  reworkRate: number;
  contractCount: number;
  onboardedAt: string;
  /** 资质文件审核状态 */
  qualification: {
    businessLicense: boolean;
    ndaSigned: boolean;
    dataSecurityAudit: boolean;
    lastAuditedAt: string | null;
  };
}

export interface SupplierMember {
  id: ID;
  tenantId: TenantId;
  supplierId: ID;
  name: string;
  initials: string;
  role: "supplier_admin" | "supplier_lead" | "annotator";
  skills: Modality[];
  level: "senior" | "mid" | "junior";
  status: "active" | "inactive";
  /** 30 日通过率 */
  passRate: number;
  /** 累计完成条数 */
  completedTotal: number;
  /** 当前在手任务数 */
  activeTasks: number;
  joinedAt: string;
  /** 绑定的平台登录账号（forge.users.id）。null = 未开通登录，
   *  分给他的批次没人能作业 —— 分配前先在人员页绑定。 */
  userId: string | null;
  /** 绑定账号的手机号（脱敏展示由后端决定，这里存原值供管理端核对） */
  userPhone: string | null;
}

export type Modality = "image" | "video" | "audio" | "text" | "multimodal";

export type ProjectStatus =
  | "draft"
  | "planning"
  | "running"
  | "paused"
  | "accepting"
  | "closed";

export interface Project {
  id: ID;
  tenantId: TenantId;
  code: string;
  name: string;
  client: string;
  modality: Modality;
  status: ProjectStatus;
  managerId: ID;
  managerName: string;
  /** 总条数 */
  totalItems: number;
  /** 已验收条数 */
  deliveredItems: number;
  supplierIds: ID[];
  startAt: string;
  dueAt: string;
  /** 单条单价（元） */
  unitPrice: number;
  /** 预算（元） */
  budget: number;
  /** 一次通过率 % */
  passRate: number;
  /** 标注模板（Label Studio labeling config 引用） */
  labelingConfigId: string;
  labelingConfigName: string;
  /** 质量标准：抽检比例 % */
  qcSamplingRate: number;
  /** 允许返工次数上限 */
  maxRework: number;
}

export type TaskStatus =
  | "pending"
  | "assigned"
  | "in_progress"
  | "submitted"
  | "reviewing"
  | "rework"
  | "accepted"
  | "rejected";

export interface AnnotationTask {
  id: ID;
  tenantId: TenantId;
  projectId: ID;
  projectName: string;
  batchNo: string;
  name: string;
  modality: Modality;
  status: TaskStatus;
  supplierId: ID | null;
  assigneeId: ID | null;
  assigneeName: string | null;
  /** 花名册成员绑定的登录账号 id（usr_*）。assigneeId 是 mem_*，两者不可直接混比 */
  assigneeUserId: ID | null;
  itemCount: number;
  doneCount: number;
  /** 返工轮次 */
  reworkRound: number;
  unitPrice: number;
  dueAt: string;
  createdAt: string;
  /** Label Studio 侧项目 ID，null 表示尚未同步 */
  lsProjectId: number | null;
  priority: "high" | "normal" | "low";
}

export interface ReviewRecord {
  id: ID;
  tenantId: TenantId;
  taskId: ID;
  taskName: string;
  projectName: string;
  reviewerId: ID;
  reviewerName: string;
  /** 审核层级 */
  stage: "l1" | "l2" | "qc";
  result: "pass" | "rework" | "reject";
  /** 抽检样本数 */
  sampled: number;
  /** 缺陷条目数 */
  defects: number;
  note: string;
  reviewedAt: string;
}

export type SettlementStatus = "draft" | "submitted" | "confirmed" | "invoiced" | "paid";

export interface Settlement {
  id: ID;
  tenantId: TenantId;
  code: string;
  supplierId: ID;
  supplierName: string;
  period: string;
  acceptedItems: number;
  unitPriceAvg: number;
  amount: number;
  /** 质量扣款 */
  deduction: number;
  /** 应付 */
  payable: number;
  status: SettlementStatus;
  purchaseOrder: string | null;
  purchaseInvoice: string | null;
  paymentEntry: string | null;
  createdAt: string;
  dueAt: string;
}

export interface PerformanceRecord {
  id: ID;
  tenantId: TenantId;
  supplierId: ID;
  supplierName: string;
  period: string;
  qualityScore: number;
  onTimeRate: number;
  reworkRate: number;
  throughput: number;
  /** 综合评级分 0-100 */
  composite: number;
  rank: number;
}

export type DataAssetStatus = "raw" | "in_annotation" | "in_review" | "accepted" | "archived";

export interface DataAsset {
  id: ID;
  tenantId: TenantId;
  projectId: ID;
  batchNo: string;
  name: string;
  modality: Modality;
  status: DataAssetStatus;
  sizeMb: number;
  itemCount: number;
  importedAt: string;
  /** 质检抽样结果 */
  qcScore: number | null;
  storage: "s3" | "oss" | "local";
}

export interface AuditEntry {
  id: ID;
  tenantId: TenantId;
  actorName: string;
  action: string;
  target: string;
  detail: string;
  at: string;
  ip: string;
}

/**
 * 内部员工账号 —— 来自 forge.users（GET /api/v1/admin/users）
 *
 * 只放 forge.users 里真有的字段：库里没有部门、职称、在研项目数、本月审核数，
 * 就不在模型里造这些字段。宁可表格列少几列，也不能出现凭空数字。
 */
export interface StaffAccount {
  id: ID;
  tenantId: TenantId;
  name: string;
  /** 服务端已脱敏（138****8000），前端拿不到完整号码 */
  phone: string;
  roles: RoleCode[];
  status: "pending" | "active" | "suspended";
  mustChangePassword: boolean;
  lastLoginAt: string | null;
  createdAt: string;
}

export interface DeliveryRecord {  id: ID;
  tenantId: TenantId;
  projectId: ID;
  projectName: string;
  version: string;
  format: string;
  itemCount: number;
  deliveredAt: string;
  acceptedAt: string | null;
  clientSignOff: boolean;
}
