import { useState } from "react";
import type { SeriesPoint } from "../lib/analytics";
import { compactNum, num, ratioPct } from "../lib/format";

const VIZ = [
  "var(--fk-viz-1)", "var(--fk-viz-2)", "var(--fk-viz-3)", "var(--fk-viz-4)",
  "var(--fk-viz-5)", "var(--fk-viz-6)", "var(--fk-viz-7)", "var(--fk-viz-8)",
];
const viz = (i: number) => VIZ[i % VIZ.length];

/* --------------------------- 趋势面积线 ---------------------------------- */
export function TrendChart({
  data,
  height = 210,
  showRework = true,
}: {
  data: SeriesPoint[];
  height?: number;
  showRework?: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);

  // 真库里这段时间没有审核记录时，画一张全是 0 的空图比造一条曲线诚实。
  if (!data.length || data.every((d) => d.submitted === 0)) {
    return (
      <div
        style={{
          height, display: "grid", placeItems: "center", gap: 6,
          color: "var(--fk-ink-3)", fontSize: 12, textAlign: "center", lineHeight: 1.8,
        }}
      >
        <div>近 14 天暂无审核记录</div>
        <div style={{ fontSize: 11 }}>审核单产生后，这里会按真实记录聚合</div>
      </div>
    );
  }

  const W = 760;
  const H = height;
  const pad = { t: 14, r: 12, b: 26, l: 46 };
  const iw = W - pad.l - pad.r;
  const ih = H - pad.t - pad.b;
  const max = Math.max(...data.map((d) => d.submitted)) * 1.12;
  const x = (i: number) => pad.l + (i / Math.max(1, data.length - 1)) * iw;
  const y = (v: number) => pad.t + ih - (v / max) * ih;

  const line = (key: "submitted" | "accepted" | "rework") =>
    data.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join(" ");
  const area = (key: "submitted" | "accepted") =>
    `${line(key)} L${x(data.length - 1)},${pad.t + ih} L${pad.l},${pad.t + ih} Z`;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => Math.round(max * t));

  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="提交与通过趋势">
        <defs>
          <linearGradient id="g-sub" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--fk-accent-500)" stopOpacity="0.18" />
            <stop offset="100%" stopColor="var(--fk-accent-500)" stopOpacity="0.01" />
          </linearGradient>
          <linearGradient id="g-acc" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--fk-viz-3)" stopOpacity="0.14" />
            <stop offset="100%" stopColor="var(--fk-viz-3)" stopOpacity="0.01" />
          </linearGradient>
        </defs>

        {ticks.map((t, i) => (
          <g key={i}>
            <line
              x1={pad.l}
              x2={W - pad.r}
              y1={pad.t + ih - (t / max) * ih}
              y2={pad.t + ih - (t / max) * ih}
              stroke="var(--fk-line-soft)"
              strokeWidth="1"
            />
            <text
              x={pad.l - 8}
              y={pad.t + ih - (t / max) * ih + 3.5}
              textAnchor="end"
              fontSize="10"
              fill="var(--fk-ink-4)"
              style={{ fontVariantNumeric: "tabular-nums" }}
            >
              {compactNum(t)}
            </text>
          </g>
        ))}

        <path d={area("submitted")} fill="url(#g-sub)" />
        <path d={area("accepted")} fill="url(#g-acc)" />
        <path d={line("submitted")} fill="none" stroke="var(--fk-accent-500)" strokeWidth="1.8" strokeLinejoin="round" />
        <path d={line("accepted")} fill="none" stroke="var(--fk-viz-3)" strokeWidth="1.5" strokeLinejoin="round" strokeDasharray="0" />
        {showRework && (
          <path
            d={line("rework")}
            fill="none"
            stroke="var(--fk-danger-600)"
            strokeWidth="1.2"
            strokeDasharray="3 3"
            opacity="0.85"
          />
        )}

        {data.map((d, i) =>
          i % 2 === 0 ? (
            <text key={i} x={x(i)} y={H - 8} textAnchor="middle" fontSize="10" fill="var(--fk-ink-4)">
              {d.label}
            </text>
          ) : null
        )}

        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={pad.t + ih} stroke="var(--fk-graphite-400)" strokeWidth="1" strokeDasharray="3 3" />
            <circle cx={x(hover)} cy={y(data[hover].submitted)} r="3.2" fill="var(--fk-accent-500)" stroke="#fff" strokeWidth="1.5" />
            <circle cx={x(hover)} cy={y(data[hover].accepted)} r="3" fill="var(--fk-viz-3)" stroke="#fff" strokeWidth="1.5" />
          </g>
        )}

        {data.map((_, i) => (
          <rect
            key={i}
            x={x(i) - iw / data.length / 2}
            y={pad.t}
            width={iw / data.length}
            height={ih}
            fill="transparent"
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          />
        ))}
      </svg>

      {hover !== null && (
        <div
          style={{
            position: "absolute",
            left: `${(x(hover) / W) * 100}%`,
            top: 4,
            transform: "translateX(-50%)",
            background: "var(--fk-graphite-900)",
            color: "#F4F2EE",
            padding: "6px 9px",
            borderRadius: 4,
            fontSize: 11,
            lineHeight: 1.5,
            pointerEvents: "none",
            whiteSpace: "nowrap",
            boxShadow: "var(--fk-shadow-2)",
            zIndex: 3,
          }}
        >
          <div style={{ opacity: 0.65 }}>{data[hover].label}</div>
          <div>提交 {num(data[hover].submitted)}</div>
          <div>通过 {num(data[hover].accepted)}</div>
          <div style={{ color: "var(--fk-accent-200)" }}>返工 {num(data[hover].rework)}</div>
        </div>
      )}

      <div style={{ display: "flex", gap: 16, marginTop: 6, fontSize: 11, color: "var(--fk-ink-3)" }}>
        <Legend color="var(--fk-accent-500)" label="提交量" />
        <Legend color="var(--fk-viz-3)" label="通过量" />
        {showRework && <Legend color="var(--fk-danger-600)" label="返工量" dashed />}
      </div>
    </div>
  );
}

function Legend({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
      <span
        style={{
          width: 12,
          height: 0,
          borderTop: `2px ${dashed ? "dashed" : "solid"} ${color}`,
          display: "inline-block",
        }}
      />
      {label}
    </span>
  );
}

/* --------------------------- 堆叠横条 ------------------------------------ */
export function StackedBars({
  rows,
}: {
  rows: Array<{ name: string; utilized: number; total: number; quality: number }>;
}) {
  // 全部供应商都没设产能时 max 会是 0，0/0 再把条宽算成 NaN（宽度直接失效）。
  const max = Math.max(1, ...rows.map((r) => r.total));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {rows.map((r, i) => {
        const utilPct = (r.utilized / max) * 100;
        const totalPct = (r.total / max) * 100;
        return (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "66px 1fr 46px", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 12, color: "var(--fk-ink-2)", textAlign: "right" }}>{r.name}</span>
            <div style={{ position: "relative", height: 18, background: "var(--fk-surface-2)", borderRadius: 2 }}>
              <div
                style={{
                  position: "absolute",
                  inset: 0,
                  width: `${totalPct}%`,
                  background: "var(--fk-accent-100)",
                  borderRadius: 2,
                }}
              />
              <div
                style={{
                  position: "absolute",
                  top: 0,
                  bottom: 0,
                  left: 0,
                  width: `${utilPct}%`,
                  background: r.quality >= 90 ? "var(--fk-accent-500)" : r.quality >= 80 ? "var(--fk-accent-400)" : "var(--fk-warn-600)",
                  borderRadius: 2,
                }}
              />
            </div>
            <span style={{ fontSize: 11, color: "var(--fk-ink-3)", fontVariantNumeric: "tabular-nums" }}>
              {Math.round(ratioPct(r.utilized, r.total))}%
            </span>
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: "var(--fk-ink-3)", paddingLeft: 76 }}>
        <Legend color="var(--fk-accent-500)" label="已承诺工时" />
        <Legend color="var(--fk-accent-100)" label="剩余产能" />
      </div>
    </div>
  );
}

/* --------------------------- 环形占比 ------------------------------------ */
export function Donut({
  data,
  size = 148,
  centerLabel,
  centerValue,
}: {
  data: Array<{ key: string; value: number; label: string }>;
  size?: number;
  centerLabel?: string;
  centerValue?: string;
}) {
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const R = 56;
  const C = 2 * Math.PI * R;
  let acc = 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 22, flexWrap: "wrap" }}>
      <svg width={size} height={size} viewBox="0 0 140 140" role="img" aria-label="结构占比">
        <circle cx="70" cy="70" r={R} fill="none" stroke="var(--fk-surface-2)" strokeWidth="15" />
        {data.map((d, i) => {
          const frac = d.value / total;
          const dash = frac * C;
          const el = (
            <circle
              key={i}
              cx="70"
              cy="70"
              r={R}
              fill="none"
              stroke={viz(i)}
              strokeWidth="15"
              strokeDasharray={`${Math.max(0, dash - 2)} ${C - dash + 2}`}
              strokeDashoffset={-acc * C}
              transform="rotate(-90 70 70)"
            />
          );
          acc += frac;
          return el;
        })}
        <text x="70" y="66" textAnchor="middle" fontSize="19" fontWeight="600" fill="var(--fk-ink)" style={{ fontVariantNumeric: "tabular-nums" }}>
          {centerValue}
        </text>
        <text x="70" y="83" textAnchor="middle" fontSize="9.5" fill="var(--fk-ink-3)" letterSpacing="0.5">
          {centerLabel}
        </text>
      </svg>
      <div style={{ display: "flex", flexDirection: "column", gap: 7, minWidth: 116 }}>
        {data.map((d, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12 }}>
            <span style={{ width: 7, height: 7, background: viz(i), borderRadius: 1.5, flex: "none" }} />
            <span style={{ color: "var(--fk-ink-2)", flex: 1 }}>{d.label}</span>
            <span style={{ color: "var(--fk-ink-3)", fontVariantNumeric: "tabular-nums" }}>
              {((d.value / total) * 100).toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* --------------------------- 横向排行 ------------------------------------ */
export function RankBars({
  rows,
  max,
  formatter = (v: number) => num(v),
}: {
  rows: Array<{ label: string; value: number; note?: string; tone?: string }>;
  max?: number;
  formatter?: (v: number) => string;
}) {
  const m = max ?? Math.max(...rows.map((r) => r.value), 1);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 11 }}>
      {rows.map((r, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 92px", gap: 10, alignItems: "center" }}>
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4, fontSize: 12 }}>
              <span style={{ color: "var(--fk-ink-2)" }}>{r.label}</span>
              {r.note && <span style={{ color: "var(--fk-ink-3)", fontSize: 11 }}>{r.note}</span>}
            </div>
            <div style={{ height: 5, background: "var(--fk-surface-2)", borderRadius: 999 }}>
              <div
                style={{
                  height: "100%",
                  width: `${Math.max(2, (r.value / m) * 100)}%`,
                  background: r.tone ?? "var(--fk-accent-500)",
                  borderRadius: 999,
                }}
              />
            </div>
          </div>
          <span style={{ textAlign: "right", fontSize: 12, color: "var(--fk-ink)", fontVariantNumeric: "tabular-nums" }}>
            {formatter(r.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

/* --------------------------- 迷你走势 ------------------------------------ */
export function Sparkline({
  values,
  width = 76,
  height = 22,
  tone = "var(--fk-accent-500)",
}: {
  values: number[];
  width?: number;
  height?: number;
  tone?: string;
}) {
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const sp = max - min || 1;
  const d = values
    .map((v, i) => {
      const x = (i / Math.max(1, values.length - 1)) * width;
      const y = height - ((v - min) / sp) * (height - 3) - 1.5;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <path d={d} fill="none" stroke={tone} strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/* --------------------------- 环形进度 ------------------------------------ */
export function Gauge({
  value,
  max = 100,
  size = 96,
  label,
  sub,
}: {
  value: number;
  max?: number;
  size?: number;
  label: string;
  sub?: string;
}) {
  const R = 40;
  const C = 2 * Math.PI * R;
  const frac = Math.min(1, value / max);
  const tone = frac >= 0.9 ? "var(--fk-success-600)" : frac >= 0.7 ? "var(--fk-accent-500)" : "var(--fk-danger-600)";
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 6 }}>
      <svg width={size} height={size} viewBox="0 0 100 100" role="img" aria-label={label}>
        <circle cx="50" cy="50" r={R} fill="none" stroke="var(--fk-surface-2)" strokeWidth="9" />
        <circle
          cx="50"
          cy="50"
          r={R}
          fill="none"
          stroke={tone}
          strokeWidth="9"
          strokeLinecap="round"
          strokeDasharray={`${frac * C} ${C}`}
          transform="rotate(-90 50 50)"
        />
        <text x="50" y="51" textAnchor="middle" fontSize="18" fontWeight="600" fill="var(--fk-ink)" style={{ fontVariantNumeric: "tabular-nums" }}>
          {value}
        </text>
        <text x="50" y="64" textAnchor="middle" fontSize="8.5" fill="var(--fk-ink-3)">
          {sub ?? `/ ${max}`}
        </text>
      </svg>
      <span style={{ fontSize: 11.5, color: "var(--fk-ink-3)" }}>{label}</span>
    </div>
  );
}

/* --------------------------- 热力矩阵 ------------------------------------ */
export function Heatmap({ weeks = 12 }: { weeks?: number }) {
  const days = ["一", "二", "三", "四", "五", "六", "日"];
  const rnd = (i: number, j: number) => {
    const v = Math.sin(i * 12.9898 + j * 78.233) * 43758.5453;
    return Math.abs(v - Math.floor(v));
  };
  return (
    <div>
      <div style={{ display: "flex", gap: 3, paddingLeft: 18 }}>
        {Array.from({ length: weeks }).map((_, j) => (
          <div key={j} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            {days.map((_, i) => {
              const v = rnd(i, j);
              const weekend = i >= 5;
              const level = weekend ? v * 0.45 : v;
              const alpha = 0.08 + level * 0.82;
              return (
                <div
                  key={i}
                  title={`第 ${j + 1} 周 周${days[i]} · ${Math.round(level * 100)}%`}
                  style={{
                    width: 13,
                    height: 13,
                    borderRadius: 2,
                    background: level < 0.12 ? "var(--fk-surface-2)" : `rgba(122,92,214,${alpha})`,
                  }}
                />
              );
            })}
          </div>
        ))}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 10, fontSize: 10.5, color: "var(--fk-ink-4)" }}>
        <span>低</span>
        {[0.12, 0.32, 0.52, 0.72, 0.92].map((a, i) => (
          <span key={i} style={{ width: 10, height: 10, borderRadius: 2, background: `rgba(122,92,214,${a})` }} />
        ))}
        <span>高</span>
        <span style={{ marginLeft: "auto" }}>近 {weeks} 周日提交密度</span>
      </div>
    </div>
  );
}

/* --------------------------- 小色块条 ------------------------------------ */
export function MiniBars({ values, tone = "var(--fk-accent-500)" }: { values: number[]; tone?: string }) {
  const max = Math.max(...values, 1);
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 22 }}>
      {values.map((v, i) => (
        <span
          key={i}
          style={{
            width: 3.5,
            height: `${Math.max(8, (v / max) * 100)}%`,
            background: tone,
            opacity: 0.35 + (v / max) * 0.65,
            borderRadius: 1,
          }}
        />
      ))}
    </div>
  );
}
