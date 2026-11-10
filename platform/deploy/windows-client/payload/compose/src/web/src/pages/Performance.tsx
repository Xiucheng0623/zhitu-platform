import { useEffect, useMemo, useState } from "react";
import { MiniBars, RankBars, Sparkline } from "../design/charts";
import {
  Badge,
  Button,
  DataTable,
  PageHead,
  Panel,
  Select,
  Stat,
  Tabs,
} from "../design/ui";
import { cny, num, pct } from "../lib/format";
import { useScoped } from "../lib/hooks";
import { get } from "../lib/http";

const WEIGHTS = [
  { key: "质量分", w: 0.42, desc: "一次通过率与抽检缺陷率加权" },
  { key: "准时率", w: 0.26, desc: "按批次交付日计算，逾期按天扣减" },
  { key: "返工控制", w: 0.18, desc: "返工轮次与返工条目占比" },
  { key: "吞吐量", w: 0.14, desc: "月度验收条目数，按最高值归一" },
];

export default function Performance() {
  const s = useScoped();
  const [selected, setSelected] = useState("");

  // 周期从真库记录里取，不再写死「2026-08/09/10」三个假 Tab。
  const periods = useMemo(
    () => [...new Set(s.performance.map((r) => r.period))].filter(Boolean).sort().reverse(),
    [s.performance]
  );
  const [tab, setTab] = useState("");
  const activePeriod = tab || periods[0] || "";

  // 历史曲线只由真实多期记录构成；单期就是没有趋势，不画假线。
  const history = useMemo(() => {
    const m = new Map<string, number[]>();
    s.performance.forEach((r) => {
      const arr = m.get(r.supplierId) ?? [];
      arr.push(r.composite);
      m.set(r.supplierId, arr);
    });
    return m;
  }, [s.performance]);

  const rows = useMemo(
    () => [...s.performance]
      .filter((r) => !activePeriod || r.period === activePeriod)
      .sort((a, b) => a.rank - b.rank),
    [s.performance, activePeriod]
  );
  const cur = rows.find((r) => r.supplierId === selected) ?? rows[0];

  const avgComposite = rows.length ? rows.reduce((a, b) => a + b.composite, 0) / rows.length : 0;
  const best = rows[0];
  const worst = rows[rows.length - 1];

  const grade = (v: number) => (v >= 90 ? "A+" : v >= 85 ? "A" : v >= 78 ? "B" : v >= 70 ? "C" : "D");
  const gradeTone = (v: number): "success" | "accent" | "warn" | "danger" =>
    v >= 85 ? "success" : v >= 78 ? "accent" : v >= 70 ? "warn" : "danger";

  if (!rows.length) {
    return (
      <>
        <PageHead title="绩效中心" desc="绩效评分直接决定任务分配的优先权与结算议价空间。" />
        <div
          style={{
            padding: "56px 24px", textAlign: "center", color: "var(--fk-ink-3)",
            border: "1px dashed var(--fk-line)", borderRadius: 8, lineHeight: 1.9,
          }}
        >
          <div style={{ fontSize: 13.5, color: "var(--fk-ink-2)" }}>暂无绩效数据</div>
          <div style={{ fontSize: 11.5 }}>绩效快照由结算与审核结果生成，产生记录后这里才会出现排名与趋势。</div>
        </div>
        {/* 单题耗时是标注页的实测流水，与绩效快照是两套数据：
            绩效还没生成时它往往已经有了，所以空态下也要露出来，
            否则「标注员干了活却看不到任何数字」，也没法反推产能。 */}
        <div style={{ marginTop: 14 }}>
          <TaskTimePanel />
        </div>
      </>
    );
  }

  const breakdown = cur
    ? [
        { label: "质量分", value: cur.qualityScore, weight: 0.42, tone: "var(--fk-viz-1)" },
        { label: "准时率", value: cur.onTimeRate, weight: 0.26, tone: "var(--fk-viz-2)" },
        { label: "返工控制", value: Math.max(0, 100 - cur.reworkRate * 4), weight: 0.18, tone: "var(--fk-viz-3)" },
        { label: "吞吐量", value: Math.min(100, (cur.throughput / 148000) * 100), weight: 0.14, tone: "var(--fk-viz-4)" },
      ]
    : [];

  return (
    <>
      <PageHead
        title="绩效中心"
        desc="绩效评分直接决定任务分配的优先权与结算议价空间。评分口径固定、可追溯，供应商可查看自身每一项得分来源。"
        badge={<Badge tone="outline">评分周期 月度</Badge>}
        actions={
          <>
            <Button icon="download" variant="secondary">导出绩效报告</Button>
            <Button icon="settings" variant="secondary">调整权重</Button>
          </>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        <Stat label="平均综合分" value={avgComposite.toFixed(1)} hint={`${activePeriod || "当前周期"} · ${rows.length} 家供应商`} />
        <Stat label="最高分供应商" value={best?.composite ?? "—"} hint={best?.supplierName.slice(0, 12)} accent />
        <Stat label="低于阈值" value={rows.filter((r) => r.composite < 78).length} unit="家" hint="综合分 < 78 触发整改" />
        <Stat
          label="月度验收吞吐"
          value={num(rows.reduce((a, b) => a + b.throughput, 0))}
          hint="全部供应商合计条目"
        />
      </div>

      <div className="g-main">
        <Panel
          title="综合排名"
          sub={activePeriod ? `${activePeriod} 评分周期` : "当前评分周期"}
          actions={
            periods.length > 1 ? (
              <Tabs
                value={activePeriod}
                onChange={setTab}
                items={periods.map((p) => ({ key: p, label: p.slice(5) + " 月" }))}
              />
            ) : undefined
          }
          flush
        >
          <DataTable
            rows={rows}
            onRowClick={(r) => setSelected(r.supplierId)}
            columns={[
              {
                key: "rank",
                header: "排名",
                width: 56,
                render: (r) => (
                  <span
                    className="fk-num"
                    style={{
                      fontWeight: 600,
                      color: r.rank <= 2 ? "var(--fk-accent-600)" : "var(--fk-ink-2)",
                    }}
                  >
                    #{r.rank}
                  </span>
                ),
              },
              {
                key: "name",
                header: "供应商",
                render: (r) => (
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span className="fk-cell-strong">{r.supplierName}</span>
                    <Badge tone={gradeTone(r.composite)}>{grade(r.composite)}</Badge>
                  </div>
                ),
                sortValue: (r) => r.supplierName,
              },
              { key: "q", header: "质量", width: 66, align: "right", render: (r) => <span className="fk-num">{r.qualityScore}</span>, sortValue: (r) => r.qualityScore },
              { key: "o", header: "准时", width: 70, align: "right", render: (r) => <span className="fk-num">{pct(r.onTimeRate)}</span>, sortValue: (r) => r.onTimeRate },
              {
                key: "rw",
                header: "返工率",
                width: 70,
                align: "right",
                render: (r) => (
                  <span className="fk-num" style={{ color: r.reworkRate > 6 ? "var(--fk-danger-600)" : "var(--fk-ink-2)" }}>
                    {pct(r.reworkRate)}
                  </span>
                ),
                sortValue: (r) => r.reworkRate,
              },
              { key: "t", header: "吞吐", width: 84, align: "right", render: (r) => <span className="fk-num">{num(r.throughput)}</span>, sortValue: (r) => r.throughput },
              {
                key: "trend",
                header: "历史",
                width: 84,
                render: (r) => {
                  const h = history.get(r.supplierId) ?? [];
                  return h.length >= 2
                    ? <Sparkline values={h} />
                    : <span style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>—</span>;
                },
              },
              {
                key: "c",
                header: "综合分",
                width: 92,
                align: "right",
                render: (r) => (
                  <span className="fk-num" style={{ fontWeight: 600, fontSize: 13 }}>{r.composite}</span>
                ),
                sortValue: (r) => r.composite,
              },
            ]}
          />
        </Panel>

        <div className="stack">
          {cur && (
            <Panel
              title="评分分解"
              sub={cur.supplierName.slice(0, 14)}
              actions={<Badge tone={gradeTone(cur.composite)}>{grade(cur.composite)} · {cur.composite}</Badge>}
            >
              <div style={{ display: "flex", flexDirection: "column", gap: 13 }}>
                {breakdown.map((b) => (
                  <div key={b.label}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                      <span style={{ color: "var(--fk-ink-2)" }}>
                        {b.label}
                        <span style={{ color: "var(--fk-ink-4)", marginLeft: 5, fontSize: 10.5 }}>权重 {b.weight * 100}%</span>
                      </span>
                      <span className="fk-num" style={{ color: "var(--fk-ink)" }}>{b.value.toFixed(1)}</span>
                    </div>
                    <div style={{ height: 6, background: "var(--fk-surface-2)", borderRadius: 999 }}>
                      <div style={{ height: "100%", width: `${b.value}%`, background: b.tone, borderRadius: 999 }} />
                    </div>
                    <div style={{ fontSize: 10.5, color: "var(--fk-ink-4)", marginTop: 3 }}>
                      贡献 {((b.value * b.weight) / 100 * 100).toFixed(1)} 分
                    </div>
                  </div>
                ))}
              </div>
              <div className="hr" />
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <span style={{ fontSize: 11.5, color: "var(--fk-ink-3)" }}>加权综合</span>
                <span className="fk-num" style={{ fontSize: 20, fontWeight: 600 }}>{cur.composite}</span>
              </div>
            </Panel>
          )}

          <Panel title="权重口径" sub="可按项目覆写">
            <div className="stack" style={{ gap: 10 }}>
              {WEIGHTS.map((w) => (
                <div key={w.key} style={{ display: "flex", gap: 10 }}>
                  <span style={{ fontSize: 11, color: "var(--fk-accent-600)", fontVariantNumeric: "tabular-nums", minWidth: 32, fontWeight: 600 }}>
                    {w.w * 100}%
                  </span>
                  <div>
                    <div style={{ fontSize: 12, color: "var(--fk-ink-2)" }}>{w.key}</div>
                    <div style={{ fontSize: 10.5, color: "var(--fk-ink-4)", lineHeight: 1.5 }}>{w.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>

      <div style={{ marginTop: 14 }} className="g-half">
        <TaskTimePanel />
        <Panel title="吞吐量对比" sub="本月验收条目">
          <RankBars
            rows={rows.map((r) => ({
              label: r.supplierName.slice(0, 10),
              value: r.throughput,
              note: `${r.composite} 分`,
              tone: r.composite >= 85 ? "var(--fk-accent-500)" : r.composite >= 78 ? "var(--fk-warn-600)" : "var(--fk-danger-600)",
            }))}
            formatter={(v) => num(v)}
          />
        </Panel>
        <Panel title="历史评分" sub="按真实周期">
          {periods.length >= 2 ? (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 14 }}>
              {rows.slice(0, 4).map((r) => {
                const h = history.get(r.supplierId) ?? [];
                return (
                  <div key={r.supplierId} className="tile" style={{ padding: "10px 12px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: 11.5, color: "var(--fk-ink-2)" }}>{r.supplierName.slice(0, 8)}</span>
                      <span className="fk-num" style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{r.composite}</span>
                    </div>
                    {h.length >= 2 && <div style={{ marginTop: 6 }}><MiniBars values={h} /></div>}
                  </div>
                );
              })}
            </div>
          ) : (
            <div style={{ padding: "28px 8px", textAlign: "center", fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.8 }}>
              目前只有 {periods.length || 1} 个评分周期，不足以生成趋势。<br />累计两个周期后自动显示。
            </div>
          )}
          <div className="hr" />
          <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.7 }}>
            最低分：{worst?.supplierName.slice(0, 12)}（{worst?.composite}）
          </div>
        </Panel>
      </div>
    </>
  );
}

/**
 * 单题耗时（标注页实测）。
 *
 * 数据来源：标注页的增强层每进入/离开/提交一条题都会回传一条流水
 * （forge.task_time_logs），平台这里按批次聚合。它是**计价与效率模型的
 * 原始事实**：谈单价要看人均日产出，判返工要看异常慢的题。
 *
 * 没数据时显示「暂无实测数据」而不是画一条假曲线 —— 上线初期本来就是空的，
 * 把空态画成 0 分钟会让人误以为「大家 0 秒就做完了」。
 */
function TaskTimePanel() {
  const s = useScoped();
  // 只有同步到 Label Studio 的批次才有耗时可言（没 LS 项目就没有标注页）
  const batches = useMemo(
    () => s.tasks.filter((t) => t.lsProjectId).sort((a, b) => a.batchNo.localeCompare(b.batchNo)),
    [s.tasks]
  );
  const [picked, setPicked] = useState("");
  const cur = picked || batches[0]?.id || "";
  const [data, setData] = useState<{
    items: number; avg_seconds: number; total_seconds: number; total_minutes: number;
    p50_seconds: number; p90_seconds: number;
    by_user: { user_id: string; user_name: string; items: number; seconds: number; minutes: number }[];
  } | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!cur) return;
    let alive = true;
    setErr("");
    get<any>(`/api/v1/tasks/${cur}/efficiency`)
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setErr((e as Error).message || "读取失败"); });
    return () => { alive = false; };
  }, [cur]);

  const fmt = (sec: number) => (sec >= 60 ? `${(sec / 60).toFixed(1)} 分` : `${sec.toFixed(1)} 秒`);

  return (
    <Panel
      title="单题耗时"
      sub="标注页实测，回传自每题的实际作业时间"
      actions={
        batches.length > 0 ? (
          <div style={{ minWidth: 168 }}>
            <Select
              value={cur}
              onChange={setPicked}
              options={batches.map((t) => ({ value: t.id, label: `${t.batchNo} · ${t.name}` }))}
            />
          </div>
        ) : undefined
      }
    >
      {batches.length === 0 ? (
        <div style={{ padding: "28px 8px", textAlign: "center", fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.8 }}>
          还没有同步到标注引擎的批次，无法统计耗时。
        </div>
      ) : err ? (
        <div style={{ padding: "20px 8px", fontSize: 11.5, color: "var(--fk-ink-3)" }}>
          耗时读取失败：{err}
        </div>
      ) : !data ? (
        <div style={{ padding: "20px 8px", fontSize: 11.5, color: "var(--fk-ink-3)" }}>正在读取…</div>
      ) : data.items === 0 ? (
        <div style={{ padding: "28px 8px", textAlign: "center", fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.9 }}>
          这个批次还没有实测数据。
          <br />
          标注员每完成一题、或离开一题时，标注页会自动回传耗时；下次作业后这里就有数了。
        </div>
      ) : (
        <>
          <div className="g-3" style={{ gap: 10 }}>
            <div className="tile">
              <div className="mini-label">已统计题数</div>
              <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4 }}>{data.items}</div>
            </div>
            <div className="tile">
              <div className="mini-label">平均单题</div>
              <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4 }}>{fmt(data.avg_seconds)}</div>
            </div>
            <div className="tile">
              <div className="mini-label">累计工时</div>
              {/* 用 seconds 而不是 minutes*60：后端的分被 round 到 0.1（6 秒粒度），
                  2 题各 2 秒会显示成 6 秒。seconds 是精确值。 */}
              <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4 }}>{fmt(data.total_seconds)}</div>
            </div>
          </div>
          <div className="hr" />
          <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.9 }}>
            P50 {fmt(data.p50_seconds)} · P90 {fmt(data.p90_seconds)}
            <br />
            按人：{data.by_user.map((u) => `${u.user_name} ${u.items} 题 / ${fmt(u.seconds)}`).join("　")}
          </div>
        </>
      )}
    </Panel>
  );
}
