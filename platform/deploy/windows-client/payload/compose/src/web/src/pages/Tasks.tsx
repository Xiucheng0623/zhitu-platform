import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "../design/icons";
import {
  Badge,
  Button,
  DataTable,
  Field,
  InlineMeter,
  Modal,
  PageHead,
  Panel,
  Select,
  Stat,
  Tabs,
  toast,
  useSession,
} from "../design/ui";
import { WorkbenchLaunch } from "../design/WorkbenchLaunch";
import { LsSyncPanel } from "../design/LsSyncPanel";
import {
  dataPreview, fetchAssignmentItems, hhmm, shortHash,
} from "../lib/assignment";
import type { AssignmentItem, AssignmentItemsResp, AssignmentGroup } from "../lib/assignment";
import { cny, daysLeft, modalityLabel, num, shortDate, taskStatusLabel, taskStatusTone } from "../lib/format";
import { useScoped } from "../lib/hooks";
import { can } from "../lib/rbac";
import { enterWorkbench } from "../lib/workbench";
import type { AnnotationTask } from "../lib/types";

export default function Tasks() {
  const s = useScoped();
  const { me, mode } = useSession();
  const nav = useNavigate();
  const [tab, setTab] = useState("all");
  const [q, setQ] = useState("");
  const [projectId, setProjectId] = useState("all");
  const [supplierId, setSupplierId] = useState("all");
  const [picked, setPicked] = useState<string[]>([]);
  const [assignTarget, setAssignTarget] = useState<AnnotationTask | null>(null);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState("");
  const [launchBatch, setLaunchBatch] = useState("");
  // 标注员视角的**唯一数据源**：本次请求从 LS 实时代理回来的任务数据行。
  // 页面上的条数、已完成数、状态、指纹全部由这一份响应派生，不再另外读
  // 一个「统计接口」——两个来源就可能出现两个不同的数字。
  const [view, setView] = useState<AssignmentItemsResp | null>(null);
  const [viewErr, setViewErr] = useState("");
  const [viewLoading, setViewLoading] = useState(false);

  /** 从具体批次整页进入对应的 LS 项目，不走 iframe。 */
  const launchTask = (t: AnnotationTask) => {
    if (t.lsProjectId == null) {
      toast("该批次尚未关联标注项目");
      return;
    }
    setLaunchBatch(`${t.batchNo} · ${t.projectName}`);
    setLaunchError("");
    setLaunching(true);
    void enterWorkbench(t.id).catch((e: unknown) =>
      setLaunchError(e instanceof Error ? e.message : String(e))
    );
  };

  /** 从平台数据行列表里点「打开这一条」：落地到 LS 的同一个 task id。 */
  const launchItem = (it: AssignmentItem) => {
    setLaunchBatch(`${it.batch_no || it.batch_id} · 第 ${it.inner_id} 条 · LS #${it.ls_task_id}`);
    setLaunchError("");
    setLaunching(true);
    void enterWorkbench(it.batch_id, it.ls_task_id).catch((e: unknown) =>
      setLaunchError(e instanceof Error ? e.message : String(e))
    );
  };

  const assignable = can(me, "task:assign");
  // 标注员视角：整页收成「我的任务」。供应商是谁、分给了谁、多少钱 ——
  // 这些是分包管理信息，作业员不需要也不该看到（服务端也已把这些字段剥掉）。
  const mine = mode === "annotator";

  const rows = useMemo(() => {
    let list = s.tasks;
    if (tab !== "all") {
      if (tab === "open") list = list.filter((t) => !["accepted", "rejected"].includes(t.status));
      else if (tab === "review") list = list.filter((t) => ["submitted", "reviewing"].includes(t.status));
      else list = list.filter((t) => t.status === tab);
    }
    if (projectId !== "all") list = list.filter((t) => t.projectId === projectId);
    if (supplierId !== "all") list = list.filter((t) => t.supplierId === supplierId);
    if (q) {
      const k = q.toLowerCase();
      list = list.filter(
        (t) => t.name.toLowerCase().includes(k) || t.projectName.toLowerCase().includes(k) || (t.assigneeName ?? "").toLowerCase().includes(k)
      );
    }
    return list;
  }, [s.tasks, tab, projectId, supplierId, q]);

  // 标注员视角：真实数据行必须来自 LS 实时读取，不能拿合同 item_count 冒充。
  // 读失败时清空整份视图并明确显示「无法核对」，绝不拿上一次的数据假装一致。
  const loadView = useCallback(async () => {
    if (!mine) return;
    setViewLoading(true);
    setViewErr("");
    try {
      setView(await fetchAssignmentItems());
    } catch (e) {
      setView(null);
      setViewErr((e as Error).message || "读取失败");
    } finally {
      setViewLoading(false);
    }
  }, [mine]);

  useEffect(() => { void loadView(); }, [loadView]);

  // LS 里增删/标注一条后，一个开着的平台页面也要能追上：页面可见时每 60 秒
  // 重新取一次，切回标签页时立刻取一次。平台本身不缓存任何行。
  // 频率刻意保守：这个接口会实时代理 LS 全量任务行，人一多、项目一大，
  // 30 秒一轮会把标注引擎压出延迟。真要承载大项目得改成「指纹优先 + 按需取行」。
  useEffect(() => {
    if (!mine) return;
    const tick = () => {
      if (document.visibilityState === "visible") void loadView();
    };
    const id = window.setInterval(tick, 60_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [mine, loadView]);

  const gMap = useMemo(
    () => new Map((view?.groups ?? []).map((g) => [g.batch_id, g])),
    [view]
  );

  const totalItems = rows.reduce((a, b) => a + b.itemCount, 0);
  const totalValue = rows.reduce((a, b) => a + b.itemCount * b.unitPrice, 0);
  const unassigned = s.tasks.filter((t) => !t.assigneeId).length;
  const overdue = s.tasks.filter((t) => {
    if (["accepted", "rejected"].includes(t.status)) return false;
    const d = daysLeft(t.dueAt);
    return d !== null && d < 0; // 没有交付日期的不算逾期，也不能因为 null 被当成逾期
  }).length;

  const allPicked = rows.length > 0 && picked.length === rows.length;
  const toggleAll = () => setPicked(allPicked ? [] : rows.map((r) => r.id));
  const toggleOne = (id: string) => setPicked((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));

  const runBulkAssign = async (supId: string, memberId: string) => {
    let ok = 0;
    let fail = 0;
    let firstReason = "";
    for (const id of picked) {
      const r = await s.assignTask(id, supId, memberId);
      if (r.ok) ok++;
      else {
        fail++;
        if (!firstReason) firstReason = r.reason ?? "";
      }
    }
    toast(
      `已分配 ${ok} 个批次${fail ? `，${fail} 个被拒绝（${firstReason}）` : ""}`
    );
    setPicked([]);
    setBulkOpen(false);
  };

  return (
    <>
      <PageHead
        title={mine ? "我的标注任务" : "任务管理"}
        desc={
          mine
            ? "下面每一条都是标注引擎里此刻真实存在的数据，与你在工作台打开的是同一条。平台不另存一份，刷新即与标注引擎同步。"
            : "批次是任务分配与结算的最小单位。分配动作受状态机约束：仅待分配 / 已分配 / 返工中的批次可重新指派。"
        }
        badge={<Badge tone="outline">{num(s.tasks.length)} 个批次</Badge>}
        actions={
          <>
            {!mine && (
              <Button icon="download" variant="secondary">导出批次明细</Button>
            )}
            {assignable && (
              <Button icon="plus" variant="primary" onClick={() => toast("请在项目内创建新批次，或从数据管理导入后生成")}>
                新建批次
              </Button>
            )}
          </>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        {mine ? (
          <>
            <Stat
              label="可标任务"
              value={view ? num(view.summary.items) : viewLoading ? "…" : "—"}
              hint={view
                ? `${view.summary.ls_projects} 个标注项目 · 标注引擎实时`
                : viewErr ? "无法核对（见下方提示）" : "正在核对…"}
            />
            <Stat label="待接入" value={view?.summary.not_connected ?? 0} hint="已分配但未接入标注引擎" />
            <Stat
              label="已完成条目"
              value={view ? num(view.summary.labeled) : "—"}
              hint="标注引擎里实际已标"
            />
            <Stat label="已逾期" value={overdue} hint="逾期会记入你的交付质量" />
          </>
        ) : (
          <>
            <Stat label="当前筛选批次" value={rows.length} hint={`共 ${num(totalItems)} 条`} />
            <Stat label="未分配批次" value={unassigned} hint="需尽快指派供应商" />
            <Stat label="已逾期" value={overdue} hint="逾期将影响供应商绩效" />
            <Stat label="批次合同额" value={cny(totalValue, { compact: true })} hint="条目 × 单价" />
          </>
        )}
      </div>

      {mine && (
        <AssignmentNotice
          data={view}
          err={viewErr}
          loading={viewLoading}
          onReload={() => void loadView()}
        />
      )}

      {/* 对账面板只给管理侧看：标注员的「我的任务」页里不该出现供应商/合同口径。
          同步按钮按 task:assign 权限渲染，与后端 require("task:assign") 对齐。 */}
      {!mine && (
        <div style={{ marginBottom: 14 }}>
          <LsSyncPanel canSync={assignable} onDone={() => void s.reload()} />
        </div>
      )}

      {/* 标注员的主视图：LS 里那一条条真实数据，不是平台批次的合同条目数。 */}
      {mine && (
        <MyDataPanel
          data={view}
          err={viewErr}
          loading={viewLoading}
          onReload={() => void loadView()}
          onOpen={launchItem}
        />
      )}

      <Panel flush>
        <div style={{ padding: "10px 16px 0" }}>
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { key: "all", label: "全部", count: s.tasks.length },
              { key: "open", label: "在制", count: s.tasks.filter((t) => !["accepted", "rejected"].includes(t.status)).length },
              ...(mine ? [] : [{ key: "pending", label: "待分配", count: s.tasks.filter((t) => t.status === "pending").length }]),
              { key: "in_progress", label: "标注中", count: s.tasks.filter((t) => t.status === "in_progress").length },
              { key: "review", label: "待审核", count: s.tasks.filter((t) => ["submitted", "reviewing"].includes(t.status)).length },
              { key: "rework", label: "返工中", count: s.tasks.filter((t) => t.status === "rework").length },
              { key: "accepted", label: "已通过", count: s.tasks.filter((t) => t.status === "accepted").length },
            ]}
          />
        </div>

        <div className="fk-toolbar" style={{ padding: "12px 16px" }}>
          <div className="fk-search" style={{ width: 240 }}>
            <Icon name="search" size={14} />
            <input
              className="fk-input"
              value={q}
              placeholder={mine ? "搜索我的批次 / 项目" : "搜索批次 / 项目 / 标注员"}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          <Select
            value={projectId}
            onChange={setProjectId}
            options={[{ value: "all", label: "全部项目" }, ...s.projects.map((p) => ({ value: p.id, label: p.code }))]}
          />
          {mode !== "annotator" && (
            <Select
              value={supplierId}
              onChange={setSupplierId}
              options={[{ value: "all", label: "全部供应商" }, ...s.suppliers.map((x) => ({ value: x.id, label: x.shortName }))]}
            />
          )}
          <div className="fk-spacer" />
          {picked.length > 0 ? (
            <div className="row" style={{ gap: 8 }}>
              <span style={{ fontSize: 12, color: "var(--fk-ink-2)" }}>已选 {picked.length} 项</span>
              {assignable && (
                <Button size="sm" variant="primary" icon="people" onClick={() => setBulkOpen(true)}>
                  批量分配
                </Button>
              )}
              <Button size="sm" variant="ghost" onClick={() => setPicked([])}>取消</Button>
            </div>
          ) : (
            <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>{rows.length} 条</span>
          )}
        </div>

        <div className="fk-tablewrap">
          <table className="fk-table">
            <thead>
              <tr>
                {!mine && (
                  <th style={{ width: 34 }}>
                    <input type="checkbox" checked={allPicked} onChange={toggleAll} aria-label="全选" />
                  </th>
                )}
                <th>批次 / 项目</th>
                <th style={{ width: 70 }}>模态</th>
                {!mine && <th style={{ width: 118 }}>供应商</th>}
                {!mine && <th style={{ width: 100 }}>标注员</th>}
                <th style={{ width: 176 }}>{mine ? "进度（标注引擎实时）" : "进度"}</th>
                <th style={{ width: 58, textAlign: "right" }}>返工</th>
                {!mine && <th style={{ width: 104, textAlign: "right" }}>金额</th>}
                <th style={{ width: 96, textAlign: "right" }}>交付日</th>
                <th style={{ width: 86 }}>状态</th>
                <th style={{ width: mine ? 96 : 158, textAlign: "right" }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 40).map((t) => {
                const left = daysLeft(t.dueAt);
                const sup = s.suppliers.find((x) => x.id === t.supplierId);
                // 标注员视角：一切「条数」都换成 LS 实时值。
                // 未接入 / 读不到的批次不显示合同数，避免把 500 当成可标任务。
                const g: AssignmentGroup | undefined = mine ? gMap.get(t.id) : undefined;
                // 连批次表里的进度也由同一份 LS 数据行派生，不再另取统计数
                const liveTotal = mine && view
                  ? (g?.state === "ok" ? g.item_count : null)
                  : t.itemCount;
                const liveDone = mine && view
                  ? (g?.state === "ok" ? g.items.filter((i) => i.is_labeled).length : null)
                  : t.doneCount;
                const canLaunch = view ? g?.state === "ok" : t.lsProjectId != null;
                return (
                  <tr key={t.id} className="is-clickable">
                    {!mine && (
                      <td onClick={(e) => e.stopPropagation()}>
                        <input type="checkbox" checked={picked.includes(t.id)} onChange={() => toggleOne(t.id)} aria-label="选择" />
                      </td>
                    )}
                    <td>
                      <div className="fk-cell-strong" style={{ fontSize: 12.5 }}>{t.name}</div>
                      <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>
                        {t.projectName}
                        {t.priority === "high" && <span style={{ color: "var(--fk-danger-600)", marginLeft: 6 }}>· 高优</span>}
                        {mine && g && g.state !== "ok" && (
                          <span style={{ marginLeft: 6, color: "var(--fk-warn-700)" }}>
                            · {g.state === "not_connected" ? "未接入标注引擎" : "标注引擎不可核对"}
                          </span>
                        )}
                      </div>
                    </td>
                    <td><Badge tone="neutral">{modalityLabel[t.modality]}</Badge></td>
                    {!mine && (
                      <td style={{ fontSize: 11.5, color: "var(--fk-ink-2)" }}>{sup?.shortName ?? <span style={{ color: "var(--fk-ink-4)" }}>待分包</span>}</td>
                    )}
                    {!mine && (
                      <td style={{ fontSize: 11.5, color: t.assigneeName ? "var(--fk-ink-2)" : "var(--fk-ink-4)" }}>
                        {t.assigneeName ?? "未指派"}
                      </td>
                    )}
                    <td>
                      <InlineMeter
                        value={liveTotal ? ((liveDone ?? 0) / liveTotal) * 100 : 0}
                        tone="accent"
                      />
                      <div style={{ fontSize: 10.5, color: "var(--fk-ink-4)", marginTop: 2, fontVariantNumeric: "tabular-nums" }}>
                        {liveTotal === null
                          ? <span style={{ color: "var(--fk-warn-700)" }}>不计入可标任务</span>
                          : `${num(liveDone ?? 0)} / ${num(liveTotal)}`}
                      </div>
                    </td>
                    <td className="fk-col-num">
                      <span style={{ color: t.reworkRound > 0 ? "var(--fk-danger-600)" : "var(--fk-ink-4)", fontWeight: t.reworkRound > 0 ? 600 : 400 }}>
                        {t.reworkRound}
                      </span>
                    </td>
                    {!mine && <td className="fk-col-num">{cny(t.itemCount * t.unitPrice, { compact: true })}</td>}
                    <td className="fk-col-num">
                      <span
                        style={{
                          color:
                            left !== null && left < 0 && !["accepted", "rejected"].includes(t.status)
                              ? "var(--fk-danger-600)"
                              : "var(--fk-ink-2)",
                          fontWeight: left !== null && left < 0 ? 600 : 400,
                        }}
                      >
                        {left === null ? "未排期" : shortDate(t.dueAt)}
                      </span>
                    </td>
                    <td><Badge tone={taskStatusTone[t.status]} dot>{taskStatusLabel[t.status]}</Badge></td>
                    <td style={{ textAlign: "right" }}>
                      <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                        {mine ? (
                          canLaunch && (
                            <Button size="sm" variant="primary" icon="external" onClick={() => launchTask(t)}>
                              标注
                            </Button>
                          )
                        ) : (
                          <>
                            {t.lsProjectId != null && (
                              <Button size="sm" variant="primary" icon="external" onClick={() => launchTask(t)}>
                                标注
                              </Button>
                            )}
                            {["pending", "assigned", "rework"].includes(t.status) && assignable ? (
                              <Button size="sm" variant="secondary" onClick={() => setAssignTarget(t)}>分配</Button>
                            ) : ["submitted", "reviewing"].includes(t.status) ? (
                              <Button size="sm" variant="ghost" onClick={() => nav("/review")}>审核</Button>
                            ) : (
                              <Button size="sm" variant="ghost" onClick={() => nav("/data")}>查看</Button>
                            )}
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {rows.length > 40 && (
            <div style={{ padding: "10px 16px", fontSize: 12, color: "var(--fk-ink-3)", borderTop: "1px solid var(--fk-line-soft)" }}>
              仅显示前 40 条，共 {rows.length} 条。使用筛选条件缩小范围，或导出完整明细。
            </div>
          )}
          {!rows.length && (
            <div style={{ padding: 40, textAlign: "center", color: "var(--fk-ink-3)", fontSize: 12.5 }}>
              当前筛选条件下没有批次
            </div>
          )}
        </div>
      </Panel>

      <AssignModal
        task={assignTarget}
        onClose={() => setAssignTarget(null)}
        onDone={(msg) => {
          toast(msg);
          setAssignTarget(null);
        }}
      />

      {launching && (
        <WorkbenchLaunch
          batchLabel={launchBatch}
          error={launchError}
          onRetry={launchError ? () => { setLaunchError(""); const t = s.tasks.find((x) => `${x.batchNo} · ${x.projectName}` === launchBatch); if (t) launchTask(t); } : undefined}
          onBack={() => { setLaunching(false); setLaunchError(""); }}
        />
      )}

      <Modal open={bulkOpen} onClose={() => setBulkOpen(false)} title={`批量分配 ${picked.length} 个批次`}>
        <BulkForm
          onSubmit={runBulkAssign}
          suppliers={s.suppliers}
          members={s.members}
        />
      </Modal>
    </>
  );
}

function BulkForm({
  onSubmit,
  suppliers,
  members,
}: {
  onSubmit: (sup: string, mem: string) => void;
  suppliers: { id: string; name: string; tier: string }[];
  members: { id: string; supplierId: string; name: string; role: string }[];
}) {
  const [sup, setSup] = useState(suppliers[0]?.id ?? "");
  const cands = members.filter((m) => m.supplierId === sup && m.role === "annotator");
  const [mem, setMem] = useState(cands[0]?.id ?? "");
  return (
    <div className="stack">
      <Field label="目标供应商">
        <Select
          value={sup}
          onChange={(v) => {
            setSup(v);
            const first = members.find((m) => m.supplierId === v && m.role === "annotator");
            setMem(first?.id ?? "");
          }}
          options={suppliers.map((x) => ({ value: x.id, label: `${x.name}（${x.tier} 级）` }))}
        />
      </Field>
      <Field label="标注员" hint="仅列出该供应商在册标注员">
        <Select value={mem} onChange={setMem} options={cands.map((m) => ({ value: m.id, label: m.name }))} />
      </Field>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Button variant="primary" onClick={() => mem && onSubmit(sup, mem)} disabled={!mem}>
          确认分配
        </Button>
      </div>
    </div>
  );
}

function AssignModal({
  task,
  onClose,
  onDone,
}: {
  task: AnnotationTask | null;
  onClose: () => void;
  onDone: (msg: string) => void;
}) {
  const s = useScoped();
  const [sup, setSup] = useState("");
  const [mem, setMem] = useState("");

  if (!task) return null;
  const currentSup = sup || task.supplierId || s.suppliers[0]?.id || "";
  const cands = s.members.filter((m) => m.supplierId === currentSup && m.role === "annotator");
  const currentMem = mem || "";

  const submit = async () => {
    if (!currentMem) return;
    const r = await s.assignTask(task.id, currentSup, currentMem);
    onDone(
      r.ok
        ? `${task.name} 已分配给 ${s.members.find((m) => m.id === currentMem)?.name}`
        : `分配失败：${r.reason}`
    );
  };

  return (
    <Modal
      open
      onClose={onClose}
      width={540}
      title="分配标注任务"
      foot={
        <>
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={submit} disabled={!currentMem}>确认分配</Button>
        </>
      }
    >
      <div className="stack">
        <div style={{ background: "var(--fk-surface-2)", border: "1px solid var(--fk-line)", borderRadius: 4, padding: "10px 12px" }}>
          <div style={{ fontSize: 12.5, fontWeight: 500 }}>{task.name}</div>
          <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", marginTop: 3 }}>
            {task.projectName} · {num(task.itemCount)} 条 · 单价 {cny(task.unitPrice, { decimals: 2 })} · 状态{" "}
            <span style={{ color: "var(--fk-ink-2)" }}>{taskStatusLabel[task.status]}</span>
          </div>
        </div>

        <Field label="承接供应商" hint="仅可分配给已通过资质审核的供应商">
          <Select
            value={currentSup}
            onChange={(v) => {
              setSup(v);
              setMem("");
            }}
            options={s.suppliers
              .filter((x) => x.status === "active" || x.id === task.supplierId)
              .map((x) => ({ value: x.id, label: `${x.name}（${x.tier} 级 · 占用 ${Math.round((x.committedHours / x.capacityHours) * 100)}%）` }))}
          />
        </Field>

        <Field label="指定标注员" required hint={`该供应商有 ${cands.length} 名在册标注员`}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, maxHeight: 220, overflowY: "auto" }}>
            {cands.map((m) => (
              <button
                key={m.id}
                onClick={() => setMem(m.id)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "7px 9px",
                  border: `1px solid ${currentMem === m.id ? "var(--fk-accent-200)" : "var(--fk-line)"}`,
                  background: currentMem === m.id ? "var(--fk-accent-50)" : "var(--fk-surface)",
                  borderRadius: 4,
                  cursor: "pointer",
                  textAlign: "left",
                }}
              >
                <span className="fk-avatar">{m.initials}</span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 12, color: "var(--fk-ink)" }}>{m.name}</span>
                  <span style={{ display: "block", fontSize: 10.5, color: "var(--fk-ink-3)" }}>
                    通过率 {m.passRate}% · 在手 {m.activeTasks}
                  </span>
                </span>
              </button>
            ))}
            {!cands.length && <span style={{ fontSize: 12, color: "var(--fk-ink-3)", padding: 8 }}>该供应商暂无可用标注员</span>}
          </div>
        </Field>
      </div>
    </Modal>
  );
}

/**
 * 「我的任务」页的一致性状态条。
 *
 * 为什么必须有一条明面上的说明：平台登记的 500/400 是合同量，LS 里是
 * 真实能标的数据行。以前把合同量直接显示给标注员，两边看起来永远对不上。
 * 现在数据行来自 LS 实时读取，但「哪些批次还没接入」「刚才是不是没读到」
 * 依然要说清楚 —— 静默才是一致性事故最容易藏身的地方。
 */
function AssignmentNotice({
  data, err, loading, onReload,
}: {
  data: AssignmentItemsResp | null;
  err: string;
  loading: boolean;
  onReload: () => void;
}) {
  const s = data?.summary;
  const bad = !!err || !s || !s.in_sync || s.unreachable > 0 || s.truncated > 0;
  const label = err ? "无法核对" : loading && !s ? "核对中" : !s ? "尚未核对" : s.in_sync ? "两边一致" : "有批次待接入";
  return (
    <div
      style={{
        marginBottom: 14,
        padding: "10px 14px",
        border: "1px solid var(--fk-line)",
        borderRadius: 6,
        background: bad ? "var(--fk-warn-50)" : "var(--fk-surface)",
        fontSize: 12.5,
        lineHeight: 1.85,
        color: "var(--fk-ink-2)",
      }}
    >
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Badge tone={bad ? "warn" : "success"} dot>{label}</Badge>
        <span style={{ flex: 1, minWidth: 240 }}>
          {err
            ? `读取标注引擎失败：${err}。这里不会用旧数据冒充一致，请稍后重试。`
            : !s
              ? "正在从标注引擎读取你实际能标的数据…"
              : `任务数来自标注引擎实时：${s.ls_projects} 个项目 / ${s.items} 条数据行，核对于 ${hhmm(s.checked_at)}。合同登记 ${s.contract_items} 条只用于结算，不计入你的任务量。`}
        </span>
        <Button size="sm" onClick={onReload} disabled={loading}>重新核对</Button>
      </div>
      {s && (
        <div style={{ marginTop: 4, color: "var(--fk-ink-3)", fontSize: 11.5 }}>
          本页数据指纹 <span className="fk-mono">{shortHash(s.fingerprint)}</span>
          {" · "}平台不存任务行，刷新即从标注引擎重取；标注引擎不可达时这里不会留下旧数据。
        </div>
      )}
      {s && s.issues.length > 0 && (
        <div style={{ marginTop: 6, paddingLeft: 2, color: "var(--fk-warn-700)" }}>
          {s.issues.map((x, i) => <div key={i}>· {x}</div>)}
        </div>
      )}
    </div>
  );
}

/**
 * 标注员的主视图：LS 里逐条真实存在的数据。
 *
 * 这是「标注员手里的数据 == 平台看到的数据 == LS 看到的数据」落地的地方：
 * 每一行都是从 LS 实时读回来的同一个 task id 与同一份 data，点这一行
 * 由服务端换一张精确到 ?task=<id> 的票据，落到的就是这一条，不是任务队列头。
 * 未标注行进 LS 标注流；已标注行进 LS 数据管理视图（标注流队列只排未标注
 * 任务，带 labeling=1 会被静默跳到下一题，那样平台第 N 行就会开成第 N+1 行）。
 */
function MyDataPanel({
  data, err, loading, onReload, onOpen,
}: {
  data: AssignmentItemsResp | null;
  err: string;
  loading: boolean;
  onReload: () => void;
  onOpen: (it: AssignmentItem) => void;
}) {
  // 每个批次默认只渲染前 20 行。后端单次最多带回 2000 行/项目，
  // 全量铺进 DOM 会直接卡死浏览器 —— 数据仍全部来自 LS，只是分页展示。
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const VISIBLE = 20;
  const s = data?.summary;
  const groups = data?.groups ?? [];
  return (
    <Panel
      title="我的数据"
      sub="逐条来自标注引擎 · 点开的就是 LS 里的同一条"
      flush
    >
      <div
        className="row"
        style={{
          gap: 8, padding: "9px 16px", borderBottom: "1px solid var(--fk-line-soft)",
          fontSize: 12, color: "var(--fk-ink-2)", flexWrap: "wrap",
        }}
      >
        <Badge tone={err ? "warn" : "outline"} dot>
          {err ? "无法核对" : loading && !s ? "读取中" : "标注引擎实时"}
        </Badge>
        <span style={{ flex: 1, minWidth: 200 }}>
          {s
            ? `${num(s.items)} 条数据行 · ${s.ls_projects} 个项目 · 已标 ${s.labeled} 条 · 指纹 ${shortHash(s.fingerprint)}`
            : err ? `读取失败：${err}` : "正在读取标注引擎里的数据行…"}
        </span>
        <Button size="sm" onClick={onReload} disabled={loading}>刷新数据</Button>
      </div>

      {groups.map((g) => (
        <div key={g.batch_id}>
          <div
            style={{
              padding: "8px 16px", background: "var(--fk-surface-2)",
              borderBottom: "1px solid var(--fk-line-soft)",
              display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
              fontSize: 12,
            }}
          >
            <span style={{ fontWeight: 600, color: "var(--fk-ink)" }}>
              {g.batch_no || g.batch_id} · {g.batch_name || "—"}
            </span>
            <span className="fk-mono" style={{ color: "var(--fk-ink-3)", fontSize: 11 }}>
              {g.ls_project_id ? `LS project #${g.ls_project_id}` : "未绑定"}
            </span>
            <span style={{ color: "var(--fk-ink-3)", fontSize: 11 }}>
              {g.state === "ok"
                ? `${g.item_count} 条真实数据`
                : g.state === "not_connected" ? "未接入标注引擎" : "标注引擎不可核对"}
            </span>
            <div className="fk-spacer" />
            {g.contract_items > 0 && (
              <span style={{ color: "var(--fk-ink-4)", fontSize: 11 }}>
                合同登记 {num(g.contract_items)} 条只用于结算
              </span>
            )}
          </div>
          {(expanded[g.batch_id] ? g.items : g.items.slice(0, VISIBLE)).map((it) => (
            <div
              key={`${it.ls_project_id}-${it.ls_task_id}`}
              className="row"
              style={{
                gap: 10, padding: "9px 16px",
                borderBottom: "1px solid var(--fk-line-soft)", flexWrap: "wrap",
              }}
            >
              <span className="fk-mono" style={{ fontSize: 11.5, color: "var(--fk-ink-3)", width: 74 }}>
                #{it.ls_task_id}
              </span>
              <span style={{ fontSize: 11, color: "var(--fk-ink-4)", width: 52 }}>
                第 {it.inner_id} 条
              </span>
              <span
                className="fk-mono"
                style={{
                  flex: 1, minWidth: 220, fontSize: 11.5, color: "var(--fk-ink)",
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}
                title={dataPreview(it.data, 400)}
              >
                {dataPreview(it.data)}
              </span>
              <Badge tone={it.is_labeled ? "success" : it.has_draft ? "warn" : "neutral"} dot>
                {it.is_labeled ? "已标" : it.has_draft ? "草稿" : "待标"}
              </Badge>
              <Button size="sm" variant="ghost" icon="external" onClick={() => onOpen(it)}>
                {it.is_labeled ? "查看这一条" : "打开这一条"}
              </Button>
            </div>
          ))}
          {g.items.length > VISIBLE && (
            <div
              className="row"
              style={{
                gap: 10, padding: "9px 16px",
                borderBottom: "1px solid var(--fk-line-soft)",
                fontSize: 11.5, color: "var(--fk-ink-3)",
              }}
            >
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setExpanded((v) => ({ ...v, [g.batch_id]: !v[g.batch_id] }))}
              >
                {expanded[g.batch_id] ? "收起" : `展开其余 ${g.items.length - VISIBLE} 条`}
              </Button>
              <span>默认只显示前 {VISIBLE} 条，避免大项目卡住浏览器；数据仍逐条来自标注引擎。</span>
            </div>
          )}
          {g.state !== "ok" && (
            <div style={{ padding: "12px 16px", fontSize: 12, color: "var(--fk-warn-700)" }}>
              {g.state === "not_connected"
                ? "该批次尚未接入标注引擎，不计入可标任务；平台不会造一批假数据行给你。"
                : `${g.error || "读取标注引擎失败"}，本批次一行都不显示（不拿旧数据冒充一致）。`}
            </div>
          )}
        </div>
      ))}

      {!err && data && groups.length === 0 && (
        <div style={{ padding: 32, textAlign: "center", color: "var(--fk-ink-3)", fontSize: 12.5 }}>
          当前没有分给你的标注数据
        </div>
      )}
    </Panel>
  );
}
