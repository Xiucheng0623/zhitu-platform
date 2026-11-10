/** 聚合分析 · 所有看板指标的唯一计算来源，页面不得自行推导 */
import type {
  AnnotationTask,
  DataAsset,
  PerformanceRecord,
  Project,
  ReviewRecord,
  Settlement,
  Supplier,
} from "./types";
import { ratioPct } from "./format";

export interface Overview {
  activeProjects: number;
  totalProjects: number;
  openTasks: number;
  overdueTasks: number;
  pendingReview: number;
  inFlightItems: number;
  acceptedItems: number;
  monthOutput: number;
  mtdRevenue: number;
  payable: number;
  firstPassRate: number;
  activeSuppliers: number;
  capacityUtil: number;
  avgRework: number;
}

export function buildOverview(
  projects: Project[],
  tasks: AnnotationTask[],
  settlements: Settlement[],
  suppliers: Supplier[],
  reviews: ReviewRecord[]
): Overview {
  const active = projects.filter((p) => p.status === "running" || p.status === "accepting");
  const open = tasks.filter((t) => !["accepted", "rejected"].includes(t.status));
  const overdue = open.filter((t) => new Date(t.dueAt).getTime() < Date.now());
  const pendingReview = tasks.filter((t) => t.status === "submitted" || t.status === "reviewing");
  const inFlight = tasks.reduce((s, t) => s + (t.status === "accepted" ? 0 : t.itemCount - t.doneCount), 0);
  const accepted = tasks.reduce((s, t) => s + (t.status === "accepted" ? t.doneCount : 0), 0);
  const curMonth = new Date().toISOString().slice(0, 7);
  const cur = settlements.filter((s) => s.period === curMonth);
  const payable = settlements.filter((s) => ["submitted", "confirmed", "invoiced"].includes(s.status)).reduce((s, x) => s + x.payable, 0);
  const pass = reviews.filter((r) => r.result === "pass").length / Math.max(1, reviews.length);
  const capTotal = suppliers.reduce((s, x) => s + x.capacityHours, 0);
  const capCommitted = suppliers.reduce((s, x) => s + x.committedHours, 0);

  return {
    activeProjects: active.length,
    totalProjects: projects.length,
    openTasks: open.length,
    overdueTasks: overdue.length,
    pendingReview: pendingReview.length,
    inFlightItems: inFlight,
    acceptedItems: accepted,
    monthOutput: tasks.reduce((s, t) => s + t.doneCount, 0),
    mtdRevenue: cur.reduce((s, x) => s + x.amount, 0),
    payable,
    firstPassRate: Math.round(pass * 1000) / 10,
    activeSuppliers: suppliers.filter((s) => s.status === "active").length,
    capacityUtil: Math.round((capCommitted / Math.max(1, capTotal)) * 1000) / 10,
    avgRework: Math.round((tasks.filter((t) => t.reworkRound > 0).length / Math.max(1, tasks.length)) * 1000) / 10,
  };
}

export interface SeriesPoint {
  label: string;
  submitted: number;
  accepted: number;
  rework: number;
}

/**
 * 近 N 日真实审核单量趋势。
 *
 * 旧版用固定种子伪随机生成每天 4200~6800 条「提交」曲线，和真库毫无关系，
 * 却摆在经营看板最显眼的位置。现在只按 forge.reviews.reviewed_at 聚合：
 * 有多少条记录就画多少，没有就是全 0（TrendChart 会显示空态）。
 */
export function reviewActivitySeries(reviews: ReviewRecord[], days = 14): SeriesPoint[] {
  const buckets = new Map<string, { submitted: number; accepted: number; rework: number }>();
  const base = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(base.getTime() - i * 86400000);
    buckets.set(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`,
      { submitted: 0, accepted: 0, rework: 0 });
  }
  reviews.forEach((r) => {
    const key = String(r.reviewedAt ?? "").slice(0, 10);
    const b = buckets.get(key);
    if (!b) return;
    b.submitted += 1;
    if (r.result === "pass") b.accepted += 1;
    else b.rework += 1;
  });
  return [...buckets.entries()].map(([key, v]) => {
    const [, m, d] = key.split("-");
    return { label: `${Number(m)}/${Number(d)}`, ...v };
  });
}

/** 模态产出结构 */
export function modalityMix(assets: DataAsset[]) {
  const map = new Map<string, number>();
  assets.forEach((a) => map.set(a.modality, (map.get(a.modality) ?? 0) + a.itemCount));
  return [...map.entries()]
    .map(([k, v]) => ({ key: k, value: v }))
    .sort((a, b) => b.value - a.value);
}

/** 任务状态分布 */
export function taskStatusMix(tasks: AnnotationTask[]) {
  const map = new Map<string, number>();
  tasks.forEach((t) => map.set(t.status, (map.get(t.status) ?? 0) + 1));
  return [...map.entries()].map(([key, value]) => ({ key, value })).sort((a, b) => b.value - a.value);
}

/** 供应商质量 / 产能对比 */
export function supplierLoad(suppliers: Supplier[]) {
  return suppliers.map((s) => ({
    id: s.id,
    name: s.shortName,
    fullName: s.name,
    util: Math.round(ratioPct(s.committedHours, s.capacityHours)),
    quality: s.qualityScore,
    tier: s.tier,
    capacity: s.capacityHours,
    committed: s.committedHours,
    annotators: s.annotatorCount,
  }));
}

/** 结算走势（按期间汇总） */
export function settlementTrend(settlements: Settlement[]) {
  const map = new Map<string, { amount: number; payable: number; count: number }>();
  settlements.forEach((s) => {
    const cur = map.get(s.period) ?? { amount: 0, payable: 0, count: 0 };
    cur.amount += s.amount;
    cur.payable += s.payable;
    cur.count += 1;
    map.set(s.period, cur);
  });
  return [...map.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([period, v]) => ({ period, ...v }));
}

/** 综合评分分解（绩效中心雷达/条形） */
export function performanceBreakdown(p: PerformanceRecord) {
  return [
    { key: "质量", value: p.qualityScore, weight: 0.42 },
    { key: "准时", value: p.onTimeRate, weight: 0.26 },
    { key: "低返工", value: Math.max(0, 100 - p.reworkRate * 4), weight: 0.18 },
    { key: "吞吐", value: Math.min(100, (p.throughput / 148000) * 100), weight: 0.14 },
  ];
}
