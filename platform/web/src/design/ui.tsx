import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ChangeEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Icon, type IconName } from "./icons";

export const cx = (...c: Array<string | false | null | undefined>) => c.filter(Boolean).join(" ");

/* ------------------------------- Button ---------------------------------- */
type BtnVariant = "primary" | "secondary" | "ghost" | "danger" | "link";

export function Button({
  children,
  variant = "secondary",
  size = "md",
  icon,
  iconRight,
  onClick,
  disabled,
  block,
  type = "button",
  title,
}: {
  children?: ReactNode;
  variant?: BtnVariant;
  size?: "sm" | "md" | "lg";
  icon?: IconName;
  iconRight?: IconName;
  onClick?: () => void;
  disabled?: boolean;
  block?: boolean;
  type?: "button" | "submit";
  title?: string;
}) {
  // 没有 onClick 的按钮过去是纯装饰：点下去一点反应都没有，看起来却像
  // 一个真实功能。现在统一给一条诚实反馈，并挂上说明，绝不假装成功。
  const handleClick = onClick ?? (type === "button" ? () => toast("该能力尚未接入，当前版本不提供") : undefined);
  return (
    <button
      type={type}
      className={cx(
        "fk-btn",
        `fk-btn--${variant}`,
        size !== "md" && `fk-btn--${size}`,
        block && "fk-btn--block",
        !children && "fk-btn--icon"
      )}
      onClick={handleClick}
      disabled={disabled}
      title={title ?? (onClick ? undefined : "该能力尚未接入")}
    >
      {icon && <Icon name={icon} size={size === "sm" ? 13 : 15} />}
      {children}
      {iconRight && <Icon name={iconRight} size={size === "sm" ? 13 : 15} />}
    </button>
  );
}

/* ------------------------------- Badge ----------------------------------- */
export type Tone = "neutral" | "accent" | "success" | "warn" | "danger" | "info" | "outline";

export function Badge({
  children,
  tone = "neutral",
  dot,
}: {
  children: ReactNode;
  tone?: Tone;
  dot?: boolean;
}) {
  return (
    <span className={cx("fk-badge", `fk-badge--${tone}`)}>
      {dot && <span className="fk-badge__dot" />}
      {children}
    </span>
  );
}

/* ------------------------------- Panel ----------------------------------- */
export function Panel({
  title,
  sub,
  actions,
  children,
  flush,
  foot,
  className,
  noBorder,
}: {
  title?: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  foot?: ReactNode;
  className?: string;
  noBorder?: boolean;
}) {
  return (
    <section
      className={cx("fk-panel", className)}
      style={noBorder ? { border: "none", background: "transparent" } : undefined}
    >
      {(title || actions) && (
        <header className="fk-panel__head">
          <div className="fk-panel__title">
            {title}
            {sub && <span className="fk-panel__sub">{sub}</span>}
          </div>
          {actions && <div className="fk-toolbar">{actions}</div>}
        </header>
      )}
      <div className={cx("fk-panel__body", flush && "fk-panel__body--flush")}>{children}</div>
      {foot && <footer className="fk-panel__foot">{foot}</footer>}
    </section>
  );
}

/* ------------------------------- DataTable ------------------------------- */
export interface Column<T> {
  key: string;
  header: string;
  width?: number | string;
  align?: "left" | "right";
  render: (row: T, index: number) => ReactNode;
  sortValue?: (row: T) => string | number;
}

export function DataTable<T,>({
  columns,
  rows,
  onRowClick,
  emptyText = "无匹配记录",
  emptyHint,
}: {
  columns: Column<T>[];
  rows: T[];
  onRowClick?: (row: T) => void;
  emptyText?: string;
  emptyHint?: string;
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    return [...rows].sort((a, b) => {
      const va = col.sortValue!(a);
      const vb = col.sortValue!(b);
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * sort.dir;
      return String(va).localeCompare(String(vb), "zh-CN") * sort.dir;
    });
  }, [rows, sort, columns]);

  if (!rows.length) {
    return <Empty title={emptyText} hint={emptyHint} />;
  }

  return (
    <div className="fk-tablewrap">
      <table className="fk-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={{ width: c.width, textAlign: c.align ?? "left" }}
                className={c.sortValue ? "is-sortable" : undefined}
                onClick={
                  c.sortValue
                    ? () =>
                        setSort((s) =>
                          s?.key === c.key ? { key: c.key, dir: s.dir === 1 ? -1 : 1 } : { key: c.key, dir: 1 }
                        )
                    : undefined
                }
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  {c.header}
                  {sort?.key === c.key && (
                    <Icon name={sort.dir === 1 ? "arrow-up" : "arrow-down"} size={10} strokeWidth={2.2} />
                  )}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, i) => (
            <tr
              key={i}
              className={onRowClick ? "is-clickable" : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  style={{ textAlign: c.align ?? "left" }}
                  className={c.align === "right" ? "fk-col-num" : undefined}
                >
                  {c.render(row, i)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------- Tabs ------------------------------------ */
export function Tabs({
  items,
  value,
  onChange,
}: {
  items: Array<{ key: string; label: string; count?: number }>;
  value: string;
  onChange: (k: string) => void;
}) {
  return (
    <div className="fk-tabs" role="tablist">
      {items.map((it) => (
        <button
          key={it.key}
          role="tab"
          className="fk-tab"
          data-active={value === it.key}
          aria-selected={value === it.key}
          onClick={() => onChange(it.key)}
        >
          {it.label}
          {it.count !== undefined && <span className="fk-tab__count">{it.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Segmented({
  items,
  value,
  onChange,
}: {
  items: Array<{ key: string; label: string }>;
  value: string;
  onChange: (k: string) => void;
}) {
  return (
    <div className="fk-segmented">
      {items.map((it) => (
        <button key={it.key} data-active={value === it.key} onClick={() => onChange(it.key)}>
          {it.label}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------- Form ------------------------------------ */
export function Field({
  label,
  hint,
  error,
  required,
  children,
}: {
  label?: string;
  hint?: string;
  error?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="fk-field">
      {label && (
        <label className="fk-label">
          {label}
          {required && <span className="fk-label__req">*</span>}
        </label>
      )}
      {children}
      {error ? <span className="fk-error">{error}</span> : hint ? <span className="fk-hint">{hint}</span> : null}
    </div>
  );
}

export function TextInput({
  value,
  onChange,
  placeholder,
  type = "text",
  invalided,
  autoFocus,
  onKeyDown,
  maxLength,
  inputMode,
  autoComplete,
  name,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  invalided?: boolean;
  autoFocus?: boolean;
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  maxLength?: number;
  /** 手机号用 "numeric"：移动端弹数字键盘，桌面端仍可粘贴 */
  inputMode?: "text" | "numeric" | "tel" | "email";
  /** 让浏览器/密码管理器知道这是登录名还是密码，避免自动填充填错框 */
  autoComplete?: string;
  name?: string;
}) {
  return (
    <input
      className="fk-input"
      type={type}
      name={name}
      value={value}
      placeholder={placeholder}
      autoFocus={autoFocus}
      maxLength={maxLength}
      inputMode={inputMode}
      autoComplete={autoComplete}
      aria-invalid={invalided || undefined}
      onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
    />
  );
}

export function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <select className="fk-select" value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function SearchInput({
  value,
  onChange,
  placeholder = "搜索…",
  width = 260,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  width?: number;
}) {
  return (
    <div className="fk-search" style={{ width }}>
      <Icon name="search" size={14} />
      <input
        className="fk-input"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

export function Checkbox({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: ReactNode;
}) {
  return (
    <label className="fk-check">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

export function Switch({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      className="fk-switch"
      data-on={on}
      onClick={() => onChange(!on)}
    />
  );
}

/* ------------------------------- Overlays -------------------------------- */
export function Modal({
  open,
  title,
  children,
  onClose,
  foot,
  width = 560,
}: {
  open: boolean;
  title: ReactNode;
  children: ReactNode;
  onClose: () => void;
  foot?: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);

  if (!open) return null;
  return createPortal(
    <div className="fk-overlay fk-overlay--modal" onClick={onClose}>
      <div className="fk-modal" style={{ maxWidth: width }} onClick={(e) => e.stopPropagation()}>
        <header className="fk-overlay__head">
          <h2 className="fk-overlay__title">{title}</h2>
          <Button variant="ghost" size="sm" icon="x" onClick={onClose} />
        </header>
        <div className="fk-overlay__body">{children}</div>
        {foot && <footer className="fk-overlay__foot">{foot}</footer>}
      </div>
    </div>,
    document.body
  );
}

export function Drawer({
  open,
  title,
  sub,
  children,
  onClose,
  foot,
  width = 620,
}: {
  open: boolean;
  title: ReactNode;
  sub?: ReactNode;
  children: ReactNode;
  onClose: () => void;
  foot?: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);

  if (!open) return null;
  return createPortal(
    <div className="fk-overlay fk-overlay--drawer" onClick={onClose}>
      <aside className="fk-drawer" style={{ maxWidth: width }} onClick={(e) => e.stopPropagation()}>
        <header className="fk-overlay__head">
          <div>
            <h2 className="fk-overlay__title">{title}</h2>
            {sub && <div style={{ marginTop: 4, fontSize: 12, color: "var(--fk-ink-3)" }}>{sub}</div>}
          </div>
          <Button variant="ghost" size="sm" icon="x" onClick={onClose} />
        </header>
        <div className="fk-overlay__body">{children}</div>
        {foot && <footer className="fk-overlay__foot">{foot}</footer>}
      </aside>
    </div>,
    document.body
  );
}

/* ------------------------------- Data ------------------------------------ */
export function Stat({
  label,
  value,
  unit,
  delta,
  hint,
  accent,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  delta?: { value: string; dir: "up" | "down" | "flat"; good?: boolean };
  hint?: string;
  accent?: boolean;
}) {
  const tone = delta ? (delta.dir === "flat" ? "flat" : delta.dir) : null;
  return (
    <div className="fk-stat">
      <span className="fk-stat__label">{label}</span>
      <span className="fk-stat__value" style={accent ? { color: "var(--fk-accent-600)" } : undefined}>
        {value}
        {unit && <span className="fk-stat__unit">{unit}</span>}
      </span>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {delta && (
          <span className={cx("fk-stat__delta", `fk-stat__delta--${tone}`)}>
            <Icon
              name={delta.dir === "up" ? "arrow-up" : delta.dir === "down" ? "arrow-down" : "activity"}
              size={11}
              strokeWidth={2.2}
            />
            {delta.value}
          </span>
        )}
        {hint && <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>{hint}</span>}
      </div>
    </div>
  );
}

export function Progress({ value, tone = "accent" }: { value: number; tone?: "accent" | "success" | "warn" | "danger" }) {
  const bg =
    tone === "success"
      ? "var(--fk-success-600)"
      : tone === "warn"
        ? "var(--fk-warn-600)"
        : tone === "danger"
          ? "var(--fk-danger-600)"
          : "var(--fk-accent-500)";
  return (
    <div className="fk-progress">
      <div className="fk-progress__fill" style={{ width: `${Math.min(100, Math.max(0, value))}%`, background: bg }} />
    </div>
  );
}

export function InlineMeter({
  value,
  label,
  tone = "auto",
}: {
  value: number;
  label?: string;
  /** auto: 按数值语义着色（质量/达标率）；accent: 中性进度（交付进度/产能占用） */
  tone?: "auto" | "accent";
}) {
  // 调用方传进来的可能是 NaN（0/0）——这里统一兜成 0，别让 "NaN%" 上屏。
  const v = Number.isFinite(value) ? value : 0;
  const color =
    tone === "accent"
      ? "var(--fk-accent-500)"
      : v >= 90
        ? "var(--fk-success-600)"
        : v >= 70
          ? "var(--fk-accent-500)"
          : "var(--fk-danger-600)";
  return (
    <div className="fk-inline-meter">
      <div className="fk-inline-meter__track">
        <div className="fk-inline-meter__fill" style={{ width: `${Math.min(100, Math.max(0, v))}%`, background: color }} />
      </div>
      <span className="fk-inline-meter__val">{label ?? `${Math.round(v)}%`}</span>
    </div>
  );
}

export function Avatar({ name, size = "md" }: { name: string; size?: "md" | "lg" }) {
  return <span className={cx("fk-avatar", size === "lg" && "fk-avatar--lg")}>{name.slice(0, 1)}</span>;
}

export function AvatarStack({ names, max = 4 }: { names: string[]; max?: number }) {
  const shown = names.slice(0, max);
  return (
    <div className="fk-avatar-stack">
      {shown.map((n, i) => (
        <Avatar key={i} name={n} />
      ))}
      {names.length > max && (
        <span className="fk-avatar" style={{ background: "var(--fk-surface-2)", color: "var(--fk-ink-3)" }}>
          +{names.length - max}
        </span>
      )}
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="fk-empty">
      <div
        style={{
          width: 34,
          height: 34,
          border: "1px dashed var(--fk-line-strong)",
          borderRadius: 6,
          display: "grid",
          placeContent: "center",
          color: "var(--fk-ink-4)",
        }}
      >
        <Icon name="layers" size={16} />
      </div>
      <div className="fk-empty__title">{title}</div>
      {hint && <div style={{ fontSize: 12, maxWidth: 380 }}>{hint}</div>}
      {action}
    </div>
  );
}

export function PageHead({
  title,
  desc,
  actions,
  badge,
  crumb,
}: {
  title: ReactNode;
  desc?: ReactNode;
  actions?: ReactNode;
  badge?: ReactNode;
  crumb?: ReactNode;
}) {
  return (
    <div className="fk-pagehead">
      <div>
        {crumb && <div style={{ marginBottom: 8 }}>{crumb}</div>}
        <h1 className="fk-pagehead__title">
          {title}
          {badge}
        </h1>
        {desc && <p className="fk-pagehead__desc">{desc}</p>}
      </div>
      {actions && <div className="fk-pagehead__actions">{actions}</div>}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <span className="fk-kbd">{children}</span>;
}

export function DList({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="fk-dl">
      {items.map(([k, v], i) => (
        <div key={i} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Timeline({
  items,
}: {
  items: Array<{ text: ReactNode; meta: ReactNode; tone?: "accent" | "danger" }>;
}) {
  return (
    <div className="fk-timeline">
      {items.map((it, i) => (
        <div key={i} className="fk-timeline__item" data-tone={it.tone}>
          <div className="fk-timeline__text">{it.text}</div>
          <div className="fk-timeline__meta">{it.meta}</div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------- Toast ----------------------------------- */
let toastSubs: Array<(m: string) => void> = [];
export function toast(msg: string) {
  toastSubs.forEach((s) => s(msg));
}

export function ToastHost() {
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    const fn = (m: string) => {
      setMsg(m);
      window.setTimeout(() => setMsg((cur) => (cur === m ? null : cur)), 2400);
    };
    toastSubs.push(fn);
    return () => {
      toastSubs = toastSubs.filter((x) => x !== fn);
    };
  }, []);
  if (!msg) return null;
  return createPortal(
    <div className="fk-toast" role="status">
      <Icon name="check" size={14} strokeWidth={2.2} />
      {msg}
    </div>,
    document.body
  );
}

/* ------------------------------- Session --------------------------------- */
import type { Principal } from "../lib/types";

export type ViewMode = "internal" | "supplier" | "annotator";

export interface Session {
  enterprise: { name: string; shortName: string; plan: "internal" | "saas"; seats: number };
  /** phone 是登录名（掩码），email 仅作展示，可能为空 */
  user: { name: string; title: string; email: string; phone: string; roles: string[] };
  me: Principal;
  /**
   * 视角由身份推导（见 lib/auth.tsx deriveMode），**不是可切换的开关**。
   * 早期版本在这里放 setMode 让用户自己在内部/供应商/标注员之间跳，
   * 那只适合演示数据；接真后端后保留它等于把越权入口摆在界面上。
   */
  mode: ViewMode;
  logout: () => void;
}

export const SessionCtx = createContext<Session | null>(null);
export const useSession = () => {
  const s = useContext(SessionCtx);
  if (!s) throw new Error("SessionCtx missing");
  return s;
};
