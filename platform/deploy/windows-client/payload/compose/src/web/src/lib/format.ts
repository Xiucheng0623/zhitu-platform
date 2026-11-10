/** 格式化工具 —— 全站统一口径，避免各页面自造格式 */

export const cny = (v: number, opts: { compact?: boolean; decimals?: number } = {}) => {
  if (opts.compact) {
    if (Math.abs(v) >= 1e8) return `¥${(v / 1e8).toFixed(2)}亿`;
    if (Math.abs(v) >= 1e4) return `¥${(v / 1e4).toFixed(1)}万`;
  }
  return `¥${v.toLocaleString("zh-CN", {
    minimumFractionDigits: opts.decimals ?? 0,
    maximumFractionDigits: opts.decimals ?? 0,
  })}`;
};

export const num = (v: number) => v.toLocaleString("zh-CN");

export const pct = (v: number, digits = 1) => `${v.toFixed(digits)}%`;

export const compactNum = (v: number) => {
  if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
  return num(v);
};

const pad = (n: number) => String(n).padStart(2, "0");

export const date = (iso: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

export const dateTime = (iso: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${date(iso)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

export const shortDate = (iso: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

export const timeAgo = (iso: string) => {
  const diff = Date.now() - new Date(iso).getTime();
  const abs = Math.abs(diff);
  const min = Math.round(abs / 60000);
  const suffix = diff >= 0 ? "" : "后";
  const prefix = diff >= 0 ? "" : "";
  if (min < 1) return "刚刚";
  if (min < 60) return `${prefix}${min} 分钟${suffix}`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${prefix}${h} 小时${suffix}`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${prefix}${d} 天${suffix}`;
  return date(iso);
};

/**
 * 剩余天数：负数表示逾期，**null 表示没有交付日期**。
 *
 * 为什么返回 null 而不是 NaN：
 *   库里 due_at 是可空列，未排期的任务/项目是真实现象。原来这里直接
 *   `new Date("")` 得到 NaN，界面上就出现「交付日 —（NaNd）」——
 *   一个 NaN 混进经营看板，比空着难看得多，而且 NaN 参与任何比较都是
 *   false，会让"逾期"判断静默失效。
 */
export const daysLeft = (iso: string | null | undefined): number | null => {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.ceil((t - Date.now()) / 86400000);
};

export const bytes = (mb: number) =>
  mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;

export const modalityLabel: Record<string, string> = {
  image: "图像",
  video: "视频",
  audio: "音频",
  text: "文本",
  multimodal: "多模态",
};

export const projectStatusLabel: Record<string, string> = {
  draft: "草稿",
  planning: "筹备中",
  running: "进行中",
  paused: "已暂停",
  accepting: "验收中",
  closed: "已结项",
};

export const taskStatusLabel: Record<string, string> = {
  pending: "待分配",
  assigned: "已分配",
  in_progress: "标注中",
  submitted: "待审核",
  reviewing: "审核中",
  rework: "返工中",
  accepted: "已通过",
  rejected: "已驳回",
};

export const settlementStatusLabel: Record<string, string> = {
  draft: "草稿",
  submitted: "待确认",
  confirmed: "已确认",
  invoiced: "已开票",
  paid: "已付款",
};

export const supplierStatusLabel: Record<string, string> = {
  active: "合作中",
  probation: "观察期",
  qualifying: "资质审核",
  suspended: "已暂停",
};

export const assetStatusLabel: Record<string, string> = {
  raw: "原始",
  in_annotation: "标注中",
  in_review: "质检中",
  accepted: "已验收",
  archived: "已归档",
};

/** 状态 → 语义色 */
type Tone = "neutral" | "accent" | "success" | "warn" | "danger" | "info";

export const projectStatusTone: Record<string, Tone> = {
  draft: "neutral",
  planning: "info",
  running: "accent",
  paused: "warn",
  accepting: "info",
  closed: "neutral",
};

export const taskStatusTone: Record<string, Tone> = {
  pending: "neutral",
  assigned: "info",
  in_progress: "accent",
  submitted: "info",
  reviewing: "warn",
  rework: "danger",
  accepted: "success",
  rejected: "danger",
};

export const settlementStatusTone: Record<string, Tone> = {
  draft: "neutral",
  submitted: "info",
  confirmed: "accent",
  invoiced: "warn",
  paid: "success",
};

export const supplierStatusTone: Record<string, Tone> = {
  active: "success",
  probation: "warn",
  qualifying: "info",
  suspended: "danger",
};

export const assetStatusTone: Record<string, Tone> = {
  raw: "neutral",
  in_annotation: "accent",
  in_review: "warn",
  accepted: "success",
  archived: "neutral",
};
