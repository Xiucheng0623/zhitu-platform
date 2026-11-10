import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { BrandMark, Icon } from "../design/icons";
import { Badge, Button, DataTable, InlineMeter, Progress, useSession } from "../design/ui";
import { cny, date, daysLeft, modalityLabel, num, settlementStatusLabel, supplierStatusLabel, taskStatusLabel } from "../lib/format";
import { useScoped } from "../lib/hooks";

export default function Portal() {
  const s = useScoped();
  const { enterprise } = useSession();
  const nav = useNavigate();

  const supplier = s.suppliers[0];
  const tasks = s.tasks;
  const pending = tasks.filter((t) => ["pending", "assigned"].includes(t.status));
  const working = tasks.filter((t) => t.status === "in_progress");
  const rework = tasks.filter((t) => t.status === "rework");
  const payable = s.settlements.filter((x) => x.status !== "paid").reduce((a, b) => a + b.payable, 0);

  const members = s.members.filter((m) => m.role === "annotator");
  const capacity = supplier ? supplier.capacityHours : 0;
  const committed = supplier ? supplier.committedHours : 0;

  const projectCards = useMemo(
    () =>
      s.projects.map((p) => {
        const pt = tasks.filter((t) => t.projectId === p.id);
        return {
          p,
          count: pt.length,
          items: pt.reduce((a, b) => a + b.itemCount, 0),
          done: pt.reduce((a, b) => a + b.doneCount, 0),
        };
      }),
    [s.projects, tasks]
  );

  if (!supplier) {
    return (
      <div style={{ padding: 60, textAlign: "center", color: "var(--fk-ink-3)", fontSize: 13 }}>
        当前账号未绑定任何供应商，请联系{enterprise.shortName}运营人员开通。
      </div>
    );
  }

  return (
    <>
      {/* 身份横幅 */}
      <div
        style={{
          position: "relative",
          background: "var(--fk-graphite-950)",
          color: "#E8E5E0",
          borderRadius: 6,
          padding: "20px 24px",
          marginBottom: 16,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            position: "absolute",
            inset: 0,
            backgroundImage:
              "linear-gradient(rgba(255,255,255,.03) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.03) 1px, transparent 1px)",
            backgroundSize: "36px 36px",
            maskImage: "radial-gradient(ellipse 70% 120% at 85% 0%, #000, transparent 70%)",
          }}
        />
        <div style={{ position: "relative", display: "flex", alignItems: "flex-start", gap: 18, flexWrap: "wrap" }}>
          <span style={{ color: "var(--fk-accent-400)", display: "grid", placeContent: "center", marginTop: 2 }}>
            <BrandMark size={26} />
          </span>
          <div style={{ flex: 1, minWidth: 240 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <h1 style={{ fontSize: 18, fontWeight: 600, letterSpacing: "-0.01em" }}>{supplier.name}</h1>
              <Badge tone="accent">{supplier.tier} 级供应商</Badge>
              <Badge tone={supplier.status === "active" ? "success" : "warn"} dot>
                {supplierStatusLabel[supplier.status]}
              </Badge>
            </div>
            <div style={{ fontSize: 12, color: "var(--fk-graphite-400)", marginTop: 5 }}>
              {supplier.code} · {supplier.city} · 对接人 {supplier.contactName} · 与 {enterprise.shortName} 合作 {supplier.contractCount} 次
            </div>
            <div style={{ display: "flex", gap: 26, marginTop: 16, flexWrap: "wrap" }}>
              {[
                ["质量评分", `${supplier.qualityScore}`, "行业均值 84.6"],
                ["准时交付率", `${supplier.onTimeRate}%`, `返工率 ${supplier.reworkRate}%`],
                ["在册标注员", `${members.length}`, `产能占用 ${Math.round((committed / capacity) * 100)}%`],
                ["待结货款", cny(payable, { compact: true }), `${s.settlements.filter((x) => x.status !== "paid").length} 张结算单`],
              ].map(([k, v, note], i) => (
                <div key={i}>
                  <div style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--fk-graphite-500)" }}>{k}</div>
                  <div style={{ fontSize: 19, fontWeight: 600, fontVariantNumeric: "tabular-nums", marginTop: 2, color: k === "待结货款" ? "var(--fk-accent-400)" : "#F4F2EE" }}>
                    {v}
                  </div>
                  <div style={{ fontSize: 10.5, color: "var(--fk-graphite-500)", marginTop: 1 }}>{note}</div>
                </div>
              ))}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <Button variant="secondary" icon="file-text" onClick={() => nav("/settlement")}>结算单</Button>
            <Button variant="primary" icon="tasks" onClick={() => nav("/tasks")}>处理任务</Button>
          </div>
        </div>
      </div>

      {/* 待办 */}
      <div className="g-kpi" style={{ marginBottom: 14 }}>
        {[
          { k: "待接单批次", v: pending.length, note: "需指定标注员后开工", act: "/tasks", tone: "accent" as const, icon: "tasks" as const },
          { k: "标注进行中", v: working.length, note: `${num(working.reduce((a, b) => a + b.doneCount, 0))} 条已完成`, act: "/tasks", tone: "info" as const, icon: "annotate" as const },
          { k: "返工待处理", v: rework.length, note: "需在 48 小时内重新提交", act: "/tasks", tone: "danger" as const, icon: "sync" as const },
          { k: "待确认结算", v: s.settlements.filter((x) => x.status === "submitted").length, note: cny(payable, { compact: true }), act: "/settlement", tone: "warn" as const, icon: "settle" as const },
        ].map((c, i) => (
          <div
            key={i}
            className="tile tile--clickable"
            style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 4 }}
            onClick={() => nav(c.act)}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span className="mini-label">{c.k}</span>
              <span style={{ color: "var(--fk-ink-4)" }}>
                <Icon name={c.icon} size={14} />
              </span>
            </div>
            <span className="fk-num" style={{ fontSize: 24, fontWeight: 600, lineHeight: 1.2 }}>{c.v}</span>
            <span style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{c.note}</span>
          </div>
        ))}
      </div>

      <div className="g-main" style={{ marginBottom: 14 }}>
        <div className="stack">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <h2 style={{ fontSize: 14, fontWeight: 600 }}>授权给我的项目</h2>
            <span style={{ fontSize: 11.5, color: "var(--fk-ink-3)" }}>共 {projectCards.length} 个 · 仅显示已授权项目</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 12 }}>
            {projectCards.map(({ p, count, items, done }) => (
              <div key={p.id} className="tile tile--clickable" onClick={() => nav("/tasks")} style={{ padding: "14px 16px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{p.name}</div>
                    <div style={{ fontSize: 11, color: "var(--fk-ink-3)", marginTop: 2 }}>
                      {p.client} · {modalityLabel[p.modality]}
                    </div>
                  </div>
                  <Badge tone="neutral">{p.code}</Badge>
                </div>
                <div style={{ marginTop: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--fk-ink-3)", marginBottom: 4 }}>
                    <span>{count} 个批次 · {num(items)} 条</span>
                    <span className="fk-num">{((done / Math.max(1, items)) * 100).toFixed(0)}%</span>
                  </div>
                  <Progress value={(done / Math.max(1, items)) * 100} />
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 9, fontSize: 11, color: "var(--fk-ink-3)" }}>
                  <span>交付 {p.dueAt ? date(p.dueAt) : "未排期"}</span>
                  <span
                    style={{
                      color:
                        daysLeft(p.dueAt) !== null && (daysLeft(p.dueAt) as number) < 14
                          ? "var(--fk-warn-700)"
                          : undefined,
                    }}
                  >
                    {daysLeft(p.dueAt) === null ? "—" : `剩 ${daysLeft(p.dueAt)} 天`}
                  </span>
                </div>
              </div>
            ))}
          </div>

          <div style={{ fontSize: 11, color: "var(--fk-ink-4)", display: "flex", alignItems: "center", gap: 6 }}>
            <Icon name="lock" size={11} />
            数据隔离生效：您仅可访问上表中已授权项目的数据，无法查看其他供应商的项目、人员与结算信息。
          </div>
        </div>

        <div className="stack">
          <div className="fk-panel">
            <div className="fk-panel__head">
              <div className="fk-panel__title">团队产能</div>
              <Button variant="ghost" size="sm" onClick={() => nav("/people")}>管理</Button>
            </div>
            <div className="fk-panel__body">
              <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 6 }}>
                <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>本月承诺工时</span>
                <span className="fk-num" style={{ fontSize: 12 }}>{num(committed)} / {num(capacity)} h</span>
              </div>
              <InlineMeter value={(committed / capacity) * 100} tone="accent" />
              <div className="hr" />
              <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", marginBottom: 8 }}>活跃标注员（按在手任务）</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                {members.slice(0, 6).map((m) => (
                  <div key={m.id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                    <span className="fk-avatar">{m.initials}</span>
                    <span style={{ flex: 1, color: "var(--fk-ink-2)" }}>{m.name}</span>
                    <span className="fk-num" style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>在手 {m.activeTasks}</span>
                    <span className="fk-num" style={{ fontSize: 11, width: 40, textAlign: "right", color: m.passRate < 88 ? "var(--fk-warn-700)" : "var(--fk-ink-2)" }}>
                      {m.passRate}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="fk-panel">
            <div className="fk-panel__head">
              <div className="fk-panel__title">近期结算</div>
              <Button variant="ghost" size="sm" onClick={() => nav("/settlement")}>全部</Button>
            </div>
            <div className="fk-panel__body" style={{ padding: 0 }}>
              <DataTable
                rows={s.settlements.slice(0, 4)}
                columns={[
                  { key: "p", header: "期间", width: 74, render: (x) => <span className="fk-num" style={{ fontSize: 11.5 }}>{x.period}</span> },
                  { key: "a", header: "应付", align: "right", render: (x) => <span className="fk-num" style={{ fontSize: 12 }}>{cny(x.payable, { compact: true })}</span> },
                  { key: "s", header: "状态", width: 76, render: (x) => <Badge tone={x.status === "paid" ? "success" : "info"}>{settlementStatusLabel[x.status]}</Badge> },
                ]}
                emptyText="暂无结算单"
              />
            </div>
          </div>
        </div>
      </div>

      <div className="fk-panel">
        <div className="fk-panel__head">
          <div className="fk-panel__title">
            待办批次
            <span className="fk-panel__sub">按交付日排序</span>
          </div>
          <Button variant="ghost" size="sm" iconRight="arrow-right" onClick={() => nav("/tasks")}>任务管理</Button>
        </div>
        <div className="fk-panel__body" style={{ padding: 0 }}>
          <DataTable
            rows={[...pending, ...working, ...rework].slice(0, 6)}
            onRowClick={() => nav("/tasks")}
            columns={[
              { key: "n", header: "批次 / 项目", render: (t) => <div><div className="fk-cell-strong" style={{ fontSize: 12.5 }}>{t.name}</div><div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{t.projectName}</div></div> },
              { key: "a", header: "标注员", width: 96, render: (t) => <span style={{ fontSize: 12 }}>{t.assigneeName ?? "待指派"}</span> },
              { key: "c", header: "条目", width: 82, align: "right", render: (t) => <span className="fk-num">{num(t.itemCount)}</span> },
              { key: "p", header: "进度", width: 130, render: (t) => <InlineMeter value={(t.doneCount / t.itemCount) * 100} tone="accent" /> },
              { key: "s", header: "状态", width: 84, render: (t) => <Badge tone={t.status === "rework" ? "danger" : t.status === "in_progress" ? "accent" : "info"} dot>{taskStatusLabel[t.status]}</Badge> },
              { key: "d", header: "交付日", width: 92, align: "right", render: (t) => <span className="fk-num" style={{ fontSize: 11.5 }}>{date(t.dueAt)}</span> },
            ]}
            emptyText="暂无待办批次"
          />
        </div>
      </div>
    </>
  );
}
