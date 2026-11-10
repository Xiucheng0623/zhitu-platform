import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { Donut, RankBars, StackedBars, TrendChart } from "../design/charts";
import { Icon } from "../design/icons";
import {
  Badge,
  Button,
  DataTable,
  InlineMeter,
  PageHead,
  Panel,
  Stat,
  Timeline,
  cx,
  useSession,
} from "../design/ui";
import { buildOverview, modalityMix, reviewActivitySeries, supplierLoad, taskStatusMix } from "../lib/analytics";
import { compactNum, cny, daysLeft, modalityLabel, num, shortDate, taskStatusLabel, taskStatusTone, timeAgo } from "../lib/format";
import { useScoped } from "../lib/hooks";
import type { AnnotationTask } from "../lib/types";

export default function Overview() {
  const nav = useNavigate();
  const s = useScoped();
  const { user, mode } = useSession();

  const ov = useMemo(
    () => buildOverview(s.projects, s.tasks, s.settlements, s.suppliers, s.reviews),
    [s.projects, s.tasks, s.settlements, s.suppliers, s.reviews]
  );
  const series = useMemo(() => reviewActivitySeries(s.reviews, 14), [s.reviews]);
  const load = useMemo(() => supplierLoad(s.suppliers), [s.suppliers]);
  const mix = useMemo(
    () =>
      modalityMix(s.assets.filter((a) => s.projects.some((p) => p.id === a.projectId))).map((m) => ({
        ...m,
        label: modalityLabel[m.key] ?? m.key,
      })),
    [s.assets, s.projects]
  );
  const statusMix = useMemo(() => taskStatusMix(s.tasks), [s.tasks]);

  /** 风险任务：逾期或返工。没有交付日期的任务不算"临近到期"，单独不列 ——
   *  它们该在任务页按「未排期」筛出来，混进"需关注"会挤掉真正要处理的事。 */
  const risks = useMemo(() => {
    return s.tasks
      .filter((t) => !["accepted", "rejected"].includes(t.status))
      .filter((t) => t.status === "rework" || (daysLeft(t.dueAt) ?? Infinity) <= 3)
      .map((t) => ({ t, left: daysLeft(t.dueAt) }))
      .sort((a, b) => (a.left ?? Infinity) - (b.left ?? Infinity))
      .slice(0, 6);
  }, [s.tasks]);

  const hour = new Date().getHours();
  const greet = hour < 6 ? "凌晨好" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";

  // 标注员的工作台只关心「我手里有什么活、什么时候交」。
  // 供应商产能、结算应付、审计日志都是分包管理视角 —— 作业员不需要，
  // 也不该看到（后端接口对这些角色本来就 404/403，这里同样不渲染）。
  if (mode === "annotator") {
    const myDone = s.tasks.reduce((a, b) => a + b.doneCount, 0);
    const myItems = s.tasks.reduce((a, b) => a + b.itemCount, 0);
    return (
      <>
        <PageHead
          title={`${greet}，${user.name}`}
          desc={`${new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" })} · 你手上共有 ${s.tasks.length} 个批次，${risks.length} 项需要优先处理`}
        />

        <div className="g-kpi" style={{ marginBottom: 14 }}>
          <Stat label="我的批次" value={s.tasks.length} hint={`共 ${num(myItems)} 条`} />
          <Stat label="待开工" value={s.tasks.filter((t) => t.status === "assigned").length} hint="已分配、还没开始" />
          <Stat label="已完成条目" value={num(myDone)} hint={`总进度 ${myItems ? Math.round((myDone / myItems) * 100) : 0}%`} />
          <Stat label="已逾期" value={ov.overdueTasks} hint="逾期会记入交付质量" />
        </div>

        <div className="g-half" style={{ marginBottom: 14 }}>
          <Panel
            title="需关注的任务"
            sub={`${risks.length} 项 · 逾期或临近交付`}
            actions={
              <Button variant="ghost" size="sm" iconRight="arrow-right" onClick={() => nav("/tasks")}>
                全部
              </Button>
            }
            flush
          >
            <DataTable<{ t: AnnotationTask; left: number | null }>
              rows={risks}
              onRowClick={() => nav("/tasks")}
              columns={[
                {
                  key: "name",
                  header: "批次 / 项目",
                  render: (r) => (
                    <div>
                      <div className="fk-cell-strong" style={{ fontSize: 12.5 }}>{r.t.name}</div>
                      <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{r.t.projectName}</div>
                    </div>
                  ),
                },
                {
                  key: "status",
                  header: "状态",
                  width: 84,
                  render: (r) => (
                    <Badge tone={taskStatusTone[r.t.status]} dot>
                      {taskStatusLabel[r.t.status]}
                    </Badge>
                  ),
                },
                {
                  key: "due",
                  header: "交付日",
                  width: 96,
                  align: "right",
                  render: (r) => (
                    <span
                      className="fk-num"
                      style={{
                        fontSize: 12,
                        color:
                          r.left === null
                            ? "var(--fk-ink-4)"
                            : r.left < 0
                            ? "var(--fk-danger-600)"
                            : r.left <= 3
                            ? "var(--fk-warn-700)"
                            : "var(--fk-ink-2)",
                        fontWeight: r.left !== null && r.left <= 3 ? 600 : 400,
                      }}
                    >
                      {r.left === null ? "未排期" : r.left < 0 ? `逾期 ${-r.left} 天` : `${r.left} 天`}
                    </span>
                  ),
                },
              ]}
              emptyText="当前没有需要优先处理的任务"
            />
          </Panel>

          <Panel title="我的任务状态" sub={`${num(s.tasks.length)} 个批次`}>
            <RankBars
              rows={statusMix.map((x) => ({
                label: taskStatusLabel[x.key] ?? x.key,
                value: x.value,
                note: `${((x.value / Math.max(1, s.tasks.length)) * 100).toFixed(0)}%`,
                tone:
                  x.key === "rework" ? "var(--fk-danger-600)"
                  : x.key === "accepted" ? "var(--fk-success-600)"
                  : x.key === "reviewing" ? "var(--fk-warn-600)"
                  : "var(--fk-accent-500)",
              }))}
              formatter={(v) => `${v} 批`}
            />
            <div className="hr" />
            <div className="row" style={{ justifyContent: "space-between", fontSize: 12 }}>
              <span style={{ color: "var(--fk-ink-3)" }}>我的返工批次</span>
              <span className="fk-num" style={{ fontWeight: 600 }}>
                {s.tasks.filter((t) => t.status === "rework").length}
              </span>
            </div>
          </Panel>
        </div>

        <div style={{ marginTop: 14, fontSize: 11, color: "var(--fk-ink-4)" }}>
          当前视角：标注员本人任务 · 只显示分配给你的批次
        </div>
      </>
    );
  }

  return (
    <>
      <PageHead
        title={`${greet}，${user.name}`}
        desc={`${new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" })} · 当前共 ${s.projects.filter((p) => p.status === "running").length} 个项目在产，${ov.overdueTasks} 个任务已逾期`}
        actions={
          <>
            <Button icon="download" variant="secondary">导出日报</Button>
            <Button icon="plus" variant="primary" onClick={() => nav("/projects?new=1")}>
              新建项目
            </Button>
          </>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        <Stat
          label="在制条目"
          value={compactNum(ov.inFlightItems)}
          hint="未验收批次的剩余条目"
        />
        <Stat
          label="待审核批次"
          value={ov.pendingReview}
          hint="已提交 / 审核中"
        />
        <Stat
          label="一次通过率"
          value={ov.firstPassRate.toFixed(1)}
          unit="%"
          hint={`基于 ${s.reviews.length} 条真实审核记录`}
        />
        <Stat
          label={mode === "supplier" ? "本期应付" : "未结清应付"}
          value={cny(ov.payable, { compact: true })}
          hint={`${s.settlements.filter((x) => x.status !== "paid").length} 张结算单未结清`}
          accent
        />
      </div>

      <div className="g-main" style={{ marginBottom: 14 }}>
        <Panel
          title="审核单量与通过趋势"
          sub="近 14 天 · 按真实审核记录聚合"
          actions={
            <div className="row" style={{ gap: 6 }}>
              <Badge tone="neutral" dot>实时</Badge>
              <Button variant="ghost" size="sm" icon="external" onClick={() => nav("/tasks")}>
                任务明细
              </Button>
            </div>
          }
        >
          <TrendChart data={series} height={222} />
        </Panel>

        <div className="stack">
          <Panel title="供应商产能占用" sub="工时">
            <StackedBars
              rows={load.map((l) => ({
                name: l.name,
                utilized: l.committed,
                total: l.capacity,
                quality: l.quality,
              }))}
            />
          </Panel>
          <Panel title="数据模态结构" sub="按条目">
            <Donut
              data={mix.map((m) => ({ key: m.key, value: m.value, label: m.label }))}
              centerValue={compactNum(mix.reduce((a, b) => a + b.value, 0))}
              centerLabel="总条目"
              size={140}
            />
          </Panel>
        </div>
      </div>

      <div className="g-half" style={{ marginBottom: 14 }}>
        <Panel
          title="需关注的任务"
          sub={`${risks.length} 项`}
          actions={
            <Button variant="ghost" size="sm" iconRight="arrow-right" onClick={() => nav("/tasks")}>
              全部
            </Button>
          }
          flush
        >
          <DataTable<{ t: AnnotationTask; left: number | null }>
            rows={risks}
            onRowClick={() => nav("/tasks")}
            columns={[
              {
                key: "name",
                header: "任务 / 项目",
                render: (r) => (
                  <div>
                    <div className="fk-cell-strong" style={{ fontSize: 12.5 }}>{r.t.name}</div>
                    <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>
                      {r.t.projectName} · {r.t.assigneeName ?? "未分配"}
                    </div>
                  </div>
                ),
              },
              {
                key: "status",
                header: "状态",
                width: 84,
                render: (r) => (
                  <Badge tone={taskStatusTone[r.t.status]} dot>
                    {taskStatusLabel[r.t.status]}
                  </Badge>
                ),
              },
              {
                key: "due",
                header: "交付日",
                width: 96,
                align: "right",
                render: (r) => (
                  <span
                    className="fk-num"
                    style={{
                      fontSize: 12,
                      color:
                        r.left === null
                          ? "var(--fk-ink-4)"
                          : r.left < 0
                          ? "var(--fk-danger-600)"
                          : r.left <= 3
                          ? "var(--fk-warn-700)"
                          : "var(--fk-ink-2)",
                      fontWeight: r.left !== null && r.left <= 3 ? 600 : 400,
                    }}
                  >
                    {r.left === null
                      ? "未排期"
                      : r.left < 0
                      ? `逾期 ${-r.left} 天`
                      : `${r.left} 天`}
                  </span>
                ),
              },
            ]}
            emptyText="当前视角下没有风险任务"
          />
        </Panel>

        <Panel title="任务状态分布" sub={`${num(s.tasks.length)} 个批次`}>
          <RankBars
            rows={statusMix.map((x) => ({
              label: taskStatusLabel[x.key] ?? x.key,
              value: x.value,
              note: `${((x.value / Math.max(1, s.tasks.length)) * 100).toFixed(0)}%`,
              tone:
                x.key === "rework" ? "var(--fk-danger-600)"
                : x.key === "accepted" ? "var(--fk-success-600)"
                : x.key === "reviewing" ? "var(--fk-warn-600)"
                : "var(--fk-accent-500)",
            }))}
            formatter={(v) => `${v} 批`}
          />
          <div className="hr" />
          <div className="row" style={{ justifyContent: "space-between", fontSize: 12 }}>
            <span style={{ color: "var(--fk-ink-3)" }}>平均返工率</span>
            <span className={cx("fk-num")} style={{ fontWeight: 600 }}>{ov.avgRework}%</span>
          </div>
          <div className="row" style={{ justifyContent: "space-between", fontSize: 12, marginTop: 7 }}>
            <span style={{ color: "var(--fk-ink-3)" }}>产能利用率</span>
            <span className="fk-num" style={{ fontWeight: 600 }}>{ov.capacityUtil}%</span>
          </div>
          <div style={{ marginTop: 8 }}>
            <InlineMeter value={ov.capacityUtil} />
          </div>
        </Panel>
      </div>

      <div className="g-half">
        <Panel title="供应商质量对比" sub="近 30 日" flush>
          <DataTable
            rows={load}
            onRowClick={() => nav("/suppliers")}
            columns={[
              {
                key: "name",
                header: "供应商",
                render: (r) => (
                  <div className="row" style={{ gap: 8 }}>
                    <Badge tone={r.tier === "S" ? "accent" : "neutral"}>{r.tier}</Badge>
                    <span className="fk-cell-strong">{r.fullName}</span>
                  </div>
                ),
              },
              {
                key: "quality",
                header: "质量分",
                width: 128,
                render: (r) => <InlineMeter value={r.quality} label={r.quality.toFixed(1)} />,
              },
              {
                key: "util",
                header: "产能占用",
                width: 96,
                align: "right",
                render: (r) => <span className="fk-num">{r.util}%</span>,
              },
            ]}
          />
        </Panel>

        <Panel
          title="操作审计"
          sub="全量留痕"
          actions={
            <Button variant="ghost" size="sm" iconRight="arrow-right" onClick={() => nav("/settings")}>
              日志
            </Button>
          }
        >
          <Timeline
            items={s.audit.slice(0, 5).map((a) => ({
              text: (
                <>
                  <strong style={{ fontWeight: 500 }}>{a.actorName}</strong>
                  <span style={{ color: "var(--fk-ink-3)" }}> · {a.target}</span>
                  <div style={{ fontSize: 12, color: "var(--fk-ink-2)", marginTop: 2 }}>{a.detail}</div>
                </>
              ),
              meta: (
                <span className="row" style={{ gap: 6, marginTop: 2 }}>
                  <Icon name="clock" size={10} />
                  {timeAgo(a.at)}
                  <span style={{ color: "var(--fk-ink-4)" }}>· {a.ip}</span>
                </span>
              ),
              tone: a.action.includes("rework") ? ("danger" as const) : undefined,
            }))}
          />
        </Panel>
      </div>

      <div style={{ marginTop: 14, fontSize: 11, color: "var(--fk-ink-4)", display: "flex", gap: 14, flexWrap: "wrap" }}>
        <span>数据截止 {shortDate(new Date().toISOString())} 09:20</span>
        <span>指标口径见《运营指标体系 v1.2》</span>
        <span>当前视角：{mode === "internal" ? "内部全量" : mode === "supplier" ? "供应商授权范围" : "标注员本人任务"}</span>
      </div>
    </>
  );
}
