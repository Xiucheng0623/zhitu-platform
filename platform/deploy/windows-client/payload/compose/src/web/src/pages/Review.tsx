import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  Field,
  PageHead,
  Panel,
  Select,
  Stat,
  toast,
} from "../design/ui";
import { dateTime, modalityLabel, num, taskStatusLabel, taskStatusTone, timeAgo } from "../lib/format";
import { useScoped } from "../lib/hooks";
import { enterWorkbench } from "../lib/workbench";

/**
 * 审核中心：所有数字都来自 forge.reviews / forge.tasks。
 *
 * 删掉的东西（2026-11-06）：
 *   · 6 张 SVG 画出来的「道路样张」—— 与真库毫无关系，还伪装成可点击抽检
 *   · 写死的缺陷类型 43/18/27/9、抽检 120 张、缺陷率 8.1%、阈值 5%
 *   · 写死的「通过率 +1.4pt」波动
 *   · 前端按 result 反推的 defects = sampled * 0.09（store 里那段也已删除）
 *
 * 现在抽检样本数 / 缺陷条目数由审核员按实际结果填写，写进 forge.reviews；
 * 批次内容不再用假图代替，而是整页进入 Label Studio 工作台看真实标注结果。
 */
export default function Review() {
  const s = useScoped();
  const queue = useMemo(
    () => s.tasks.filter((t) => ["submitted", "reviewing"].includes(t.status)),
    [s.tasks]
  );
  const [activeId, setActiveId] = useState(queue[0]?.id ?? "");
  const active = queue.find((t) => t.id === activeId) ?? queue[0] ?? null;

  const [stage, setStage] = useState<"l1" | "l2" | "qc">("l1");
  const [note, setNote] = useState("");
  const [sampled, setSampled] = useState("");
  const [defects, setDefects] = useState("");

  const myReviews = s.reviews.slice(0, 10);
  const today = new Date().toISOString().slice(0, 10);
  const reviewedToday = s.reviews.filter(
    (r) => String(r.reviewedAt).slice(0, 10) === today
  ).length;
  const passRate = s.reviews.length
    ? ((s.reviews.filter((r) => r.result === "pass").length / s.reviews.length) * 100).toFixed(1)
    : null;

  const project = active ? s.projects.find((p) => p.id === active.projectId) : null;
  const activeReviews = active ? s.reviews.filter((r) => r.taskId === active.id) : [];
  const sampledTotal = activeReviews.reduce((a, r) => a + (r.sampled ?? 0), 0);
  const defectsTotal = activeReviews.reduce((a, r) => a + (r.defects ?? 0), 0);
  const defectRate = sampledTotal > 0
    ? ((defectsTotal / sampledTotal) * 100).toFixed(1)
    : null;

  const decide = async (result: "pass" | "rework") => {
    if (!active) return;
    const nSample = Number(sampled);
    const nDefect = Number(defects);
    if (
      !Number.isFinite(nSample) || nSample < 0 ||
      !Number.isFinite(nDefect) || nDefect < 0 || nDefect > nSample
    ) {
      toast("请填写实际抽检数与缺陷数（缺陷数不得大于抽检数）");
      return;
    }
    const r = await s.decideReview(
      active.id,
      result,
      note || (result === "pass" ? "抽检符合规范" : "需返工整改"),
      stage,
      nSample,
      nDefect
    );
    if (!r.ok) {
      toast(`判定失败：${r.reason}`);
      return;
    }
    toast(result === "pass" ? `${active.batchNo} 已通过 ${stage.toUpperCase()} 审核` : `${active.batchNo} 已退回返工`);
    setNote("");
    setSampled("");
    setDefects("");
    const rest = queue.filter((t) => t.id !== active.id);
    setActiveId(rest[0]?.id ?? "");
  };

  const openWorkbench = () => {
    if (!active) return;
    if (active.lsProjectId == null) {
      toast("该批次尚未关联标注项目，无法打开工作台");
      return;
    }
    void enterWorkbench(active.id).catch((e: unknown) =>
      toast(`打开工作台失败：${e instanceof Error ? e.message : String(e)}`)
    );
  };

  return (
    <>
      <PageHead
        title="审核中心"
        desc="三级审核：L1 全量抽检、L2 疑难复核、QC 终检。判定结果直接驱动任务状态流转与供应商质量分。"
        badge={<Badge tone="outline">{queue.length} 批待审</Badge>}
        actions={
          <Button icon="sync" variant="secondary" onClick={() => void s.reload()}>
            重新加载
          </Button>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        <Stat label="待审批次" value={queue.length} hint="按提交时间排序" />
        <Stat label="今日已审" value={reviewedToday} hint="按审核记录时间统计" />
        <Stat
          label="通过率"
          value={passRate ?? "—"}
          unit={passRate ? "%" : undefined}
          hint={`基于 ${s.reviews.length} 条真实审核记录`}
        />
        <Stat label="返工批次" value={s.tasks.filter((t) => t.status === "rework").length} hint="当前处于返工状态的批次" />
      </div>

      <div className="g-sidebar">
        <Panel title="待审队列" sub={`${queue.length}`} flush>
          <div style={{ maxHeight: 560, overflowY: "auto" }}>
            {queue.map((t) => {
              const sup = s.suppliers.find((x) => x.id === t.supplierId);
              return (
                <div
                  key={t.id}
                  onClick={() => setActiveId(t.id)}
                  style={{
                    padding: "10px 12px",
                    borderBottom: "1px solid var(--fk-line-soft)",
                    cursor: "pointer",
                    background: active?.id === t.id ? "var(--fk-accent-50)" : undefined,
                    boxShadow: active?.id === t.id ? "inset 2px 0 0 var(--fk-accent-500)" : undefined,
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                    <span style={{ fontSize: 12.5, fontWeight: 500, color: "var(--fk-ink)" }}>{t.batchNo}</span>
                    <Badge tone={taskStatusTone[t.status]}>{taskStatusLabel[t.status]}</Badge>
                    {t.reworkRound > 0 && <Badge tone="danger">返工 {t.reworkRound}</Badge>}
                  </div>
                  <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>
                    {t.projectName.slice(0, 14)} · {sup?.shortName ?? "—"}
                  </div>
                  <div style={{ fontSize: 10.5, color: "var(--fk-ink-4)", marginTop: 3, display: "flex", justifyContent: "space-between" }}>
                    <span>{num(t.itemCount)} 条</span>
                    <span>{timeAgo(t.dueAt).replace("后", "")}提交</span>
                  </div>
                </div>
              );
            })}
            {!queue.length && (
              <div style={{ padding: 28, textAlign: "center", fontSize: 12, color: "var(--fk-ink-3)" }}>
                当前没有待审批次
              </div>
            )}
          </div>
        </Panel>

        <div className="stack">
          {active && (
            <>
              <Panel
                title={
                  <span className="row" style={{ gap: 8 }}>
                    {active.name}
                    <Badge tone={taskStatusTone[active.status]} dot>{taskStatusLabel[active.status]}</Badge>
                  </span>
                }
                sub={`${active.projectName} · ${modalityLabel[active.modality]} · ${num(active.itemCount)} 条`}
                actions={
                  <Badge tone="neutral">
                    {project ? `抽检 ${project.qcSamplingRate}%` : "抽检比例未登记"}
                  </Badge>
                }
              >
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11.5, color: "var(--fk-ink-3)", marginBottom: 10 }}>
                  <span>提交人 {active.assigneeName ?? "—"}</span>
                  <span>交付日 {dateTime(active.dueAt)}</span>
                  <span>返工轮次 {active.reworkRound}</span>
                  <span>已完成 {num(active.doneCount)} / {num(active.itemCount)} 条</span>
                </div>

                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 14,
                    padding: "14px 16px",
                    border: "1px solid var(--fk-line)",
                    borderRadius: 4,
                    background: "var(--fk-surface-2)",
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, fontWeight: 500, marginBottom: 3 }}>
                      查看本批真实标注结果
                    </div>
                    <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.65 }}>
                      {active.lsProjectId != null
                        ? `已关联标注项目 #${active.lsProjectId}。点击后整页进入智途标注工作台，逐条核对标注结果再做裁定。`
                        : "该批次尚未关联标注项目，请先在任务管理中同步 Label Studio 项目。"}
                    </div>
                  </div>
                  <Button
                    variant="secondary"
                    icon="external"
                    disabled={active.lsProjectId == null}
                    onClick={openWorkbench}
                  >
                    打开工作台
                  </Button>
                </div>
              </Panel>

              <div className="g-half">
                <Panel
                  title="质量统计"
                  sub={activeReviews.length ? `${activeReviews.length} 条真实审核记录` : "本批次暂无审核记录"}
                >
                  {activeReviews.length === 0 ? (
                    <div style={{ padding: "26px 8px", textAlign: "center", fontSize: 12, color: "var(--fk-ink-3)", lineHeight: 1.8 }}>
                      暂无抽检数据<br />
                      <span style={{ fontSize: 11 }}>裁定后按实际填写的抽检数 / 缺陷数生成记录</span>
                    </div>
                  ) : (
                    <>
                      {[
                        ["累计抽检", `${num(sampledTotal)} 条`, "var(--fk-ink)"],
                        ["累计缺陷", `${num(defectsTotal)} 条`, "var(--fk-danger-600)"],
                        ["缺陷率", defectRate != null ? `${defectRate}%` : "—", "var(--fk-ink)"],
                        ["通过 / 返工", `${activeReviews.filter((r) => r.result === "pass").length} / ${activeReviews.filter((r) => r.result !== "pass").length}`, "var(--fk-ink)"],
                      ].map(([k, v, c]) => (
                        <div key={k} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "7px 0", borderBottom: "1px solid var(--fk-line-soft)" }}>
                          <span style={{ color: "var(--fk-ink-3)" }}>{k}</span>
                          <span className="fk-num" style={{ color: c, fontWeight: 500 }}>{v}</span>
                        </div>
                      ))}
                      <div style={{ fontSize: 11, color: "var(--fk-ink-4)", marginTop: 10, lineHeight: 1.6 }}>
                        以上数字由审核记录聚合，未做任何推算或补齐。
                      </div>
                    </>
                  )}
                </Panel>

                <Panel title="审核裁定">
                  <div className="stack" style={{ gap: 12 }}>
                    <Field label="审核层级">
                      <Select
                        value={stage}
                        onChange={(v) => setStage(v as typeof stage)}
                        options={[
                          { value: "l1", label: "L1 · 全量抽检" },
                          { value: "l2", label: "L2 · 疑难复核" },
                          { value: "qc", label: "QC · 终检放行" },
                        ]}
                      />
                    </Field>
                    <div className="row" style={{ gap: 8 }}>
                      <Field label="实际抽检数" hint="按你实际看过的条目填写">
                        <input
                          className="fk-input fk-num"
                          type="number"
                          min="0"
                          value={sampled}
                          onChange={(e) => setSampled(e.target.value)}
                          placeholder="0"
                        />
                      </Field>
                      <Field label="缺陷条目数" hint="缺陷数不得大于抽检数">
                        <input
                          className="fk-input fk-num"
                          type="number"
                          min="0"
                          value={defects}
                          onChange={(e) => setDefects(e.target.value)}
                          placeholder="0"
                        />
                      </Field>
                    </div>
                    <Field label="裁定说明" hint="说明将同步至供应商门户，供应商可见">
                      <textarea
                        className="fk-textarea"
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        placeholder="描述具体缺陷位置与整改要求…"
                      />
                    </Field>
                    <div className="row" style={{ gap: 8 }}>
                      <Button variant="primary" icon="check" block onClick={() => decide("pass")}>
                        通过并放行
                      </Button>
                      <Button variant="danger" icon="sync" block onClick={() => decide("rework")}>
                        退回返工
                      </Button>
                    </div>
                    <div style={{ fontSize: 11, color: "var(--fk-ink-4)", lineHeight: 1.6 }}>
                      裁定后任务状态由服务端变更并写入审核记录与审计日志，前端不再推算任何指标。
                    </div>
                  </div>
                </Panel>
              </div>
            </>
          )}

          {!active && (
            <Panel title="审核记录">
              <div style={{ padding: 30, textAlign: "center", fontSize: 12.5, color: "var(--fk-ink-3)" }}>
                当前没有待审批次，以下是最近审核记录。
              </div>
            </Panel>
          )}
        </div>
      </div>

      <div style={{ marginTop: 14 }}>
        <Panel title="最近审核记录" sub={myReviews.length ? `${myReviews.length} 条 · 服务端真实记录` : "暂无记录"} flush>
          {myReviews.length === 0 ? (
            <div style={{ padding: 30, textAlign: "center", fontSize: 12, color: "var(--fk-ink-3)" }}>
              尚无审核记录。产生裁定后这里会显示真实数据。
            </div>
          ) : (
            <DataTable
              rows={myReviews}
              columns={[
                { key: "t", header: "批次", render: (r) => <div><div className="fk-cell-strong" style={{ fontSize: 12.5 }}>{r.taskName}</div><div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{r.projectName}</div></div> },
                { key: "s", header: "层级", width: 66, render: (r) => <Badge tone="outline">{r.stage.toUpperCase()}</Badge> },
                { key: "r", header: "结果", width: 80, render: (r) => <Badge tone={r.result === "pass" ? "success" : r.result === "rework" ? "danger" : "warn"} dot>{r.result === "pass" ? "通过" : r.result === "rework" ? "返工" : "驳回"}</Badge> },
                { key: "sm", header: "抽检", width: 70, align: "right", render: (r) => <span className="fk-num">{r.sampled}</span> },
                { key: "df", header: "缺陷", width: 66, align: "right", render: (r) => <span className="fk-num" style={{ color: r.defects > 20 ? "var(--fk-danger-600)" : "var(--fk-ink-2)" }}>{r.defects}</span> },
                { key: "rv", header: "审核人", width: 84, render: (r) => <span style={{ fontSize: 12 }}>{r.reviewerName || "—"}</span> },
                { key: "at", header: "时间", width: 116, align: "right", render: (r) => <span className="fk-num" style={{ fontSize: 11.5 }}>{dateTime(r.reviewedAt)}</span> },
                { key: "note", header: "说明", render: (r) => <span style={{ fontSize: 11.5, color: "var(--fk-ink-3)" }}>{r.note}</span> },
              ]}
            />
          )}
        </Panel>
      </div>
    </>
  );
}
