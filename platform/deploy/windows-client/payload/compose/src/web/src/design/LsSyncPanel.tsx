import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Panel, toast } from "./ui";
import { request } from "../lib/http";
import { shortHash } from "../lib/assignment";
import type { AssignmentItemsSummary, AssignmentSummary } from "../lib/assignment";

/**
 * 平台 ↔ 标注引擎（Label Studio）对账面板
 * ---------------------------------------------------------------------------
 * 为什么要有这个东西：
 *   标注的**真实发生地**在 LS，结算口径在平台。两边一旦对不上，一线看到的
 *   进度就是假的，而之前出问题时只能由工程师连库、连 LS 手工数一遍 ——
 *   既慢又容易改错。这里把差异直接摆出来，并给「一键同步」。

 * 同步的方向是刻意不对称的（这是设计，不是偷懒）：
 *   · 进度（done_count）以 LS 为准 —— 只有 LS 知道到底标了几条；
 *   · 名称（LS 项目标题）以平台为准 —— 同一批数据两个名字最容易搞混；
 *   · 条目数（item_count）**两边都不自动改** —— 那是合同口径，
 *     LS 条数偏少通常意味着「导入没做完」，自动抹平等于把真问题藏起来。
 *
 * 另一个维度是「标注员分配的任务」：平台登记 500 条、LS 里实际 2 条，
 * 两边看着永远对不上，因为合同量根本不是标注员能标的任务。现在
 * `/api/v1/me/assignment` 读时实时向 LS 取，界面显示的就是 LS 当下真值；
 * 本面板额外展示全量的分配一致性（哪些批次没接入、项目有没有被共用）。
 */

interface DiffRow {
  task_id: string;
  batch_no?: string | null;
  name?: string | null;
  project_name?: string | null;
  status?: string | null;
  item_count: number;
  done_count: number;
  ls_project_id: number | null;
  ls_title?: string | null;
  ls_tasks: number;
  ls_done: number;
  ls_annotations: number;
  flags: string[];
  in_sync: boolean;
}

interface AutoSync {
  enabled: boolean;
  interval_s: number;
  last_run_at?: number | null;
  last_ok_at?: number | null;
  last_changed?: number;
  last_error?: string;
  consecutive_errors?: number;
  runs?: number;
}

interface ReconcileResp {
  rows: DiffRow[];
  errors: { task_id: string; ls_project_id: number; error: string }[];
  in_sync: number;
  total: number;
  auto?: AutoSync;
  /** 标注员任务一致性（服务端复用本轮已取的 LS 数据，不额外请求） */
  assignment?: AssignmentSummary;
  /** 真实任务数据行的汇总：条数、指纹、不可达/截断 */
  items?: AssignmentItemsSummary;
}

const FLAG_LABEL: Record<string, string> = {
  items: "条目数不符",
  done: "进度不符",
  title: "项目名不符",
};

/** 秒级时间戳 → HH:MM（只用来看"多久之前跑过"，不做时区换算）。 */
function hhmm(ts?: number | null): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/**
 * 兜底状态的说明行。
 *
 * 为什么要显示这个：进度同步有两条路（LS 推 webhook / 平台定时拉），
 * 任何一条断了，界面看起来都完全正常 —— 只是数字停住不动。
 * 把"自动对账 300 秒一次、上次 22:52、正常"摆在明面上，
 * 运营才有依据判断到底是"真没进展"还是"同步坏了"。
 */
function AutoLine({ auto }: { auto?: AutoSync }) {
  if (!auto) return null;
  const base = "var(--fk-ink-3)";
  if (!auto.enabled) {
    return (
      <div style={{ marginTop: 8, fontSize: 12, color: "var(--fk-warn-700)" }}>
        自动对账未启用（定时拉取已关闭，只能靠 LS 推送 + 手动同步）
      </div>
    );
  }
  const bad = (auto.consecutive_errors ?? 0) > 0;
  return (
    <div style={{ marginTop: 8, fontSize: 12, color: base }}>
      自动对账每 {auto.interval_s} 秒一次 · 上次 {hhmm(auto.last_run_at)} ·{" "}
      {bad ? (
        <span style={{ color: "var(--fk-danger-600)" }}>
          连续 {auto.consecutive_errors} 轮失败：{auto.last_error || "未知错误"}
        </span>
      ) : (
        <span style={{ color: "var(--fk-success-600)" }}>正常</span>
      )}
      {auto.last_changed ? `（上轮修正 ${auto.last_changed} 个批次）` : ""}
    </div>
  );
}

/**
 * 标注员任务一致性（管理侧视图）。
 *
 * 为什么和「进度差异」分开列：进度对不上是同步问题，分配集合对不上是
 * 接入问题（批次压根没建 LS 项目 / 两个批次共用一个项目 / 票据白名单
 * 与任务清单不一致）。处置方式完全不同，混成一个红色标记只会让人瞎猜。
 */
function AssignmentLine({ a }: { a?: AssignmentSummary }) {
  if (!a) return null;
  return (
    <div
      style={{
        marginTop: 10,
        paddingTop: 8,
        borderTop: "1px solid var(--fk-line)",
        fontSize: 12,
        lineHeight: 1.8,
      }}
    >
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Badge tone={a.in_sync ? "success" : "warn"} dot>
          {a.in_sync ? "标注员任务一致" : "标注员任务待核对"}
        </Badge>
        <span style={{ color: "var(--fk-ink-2)" }}>
          已接入 {a.connected ?? 0} 个批次 / 实际可标 {a.actual_items ?? 0} 条
          {(a.not_connected ?? 0) > 0 ? ` · ${a.not_connected} 个批次尚未接入标注引擎` : ""}
          {(a.unreachable ?? 0) > 0 ? ` · ${a.unreachable} 个批次读不到标注引擎` : ""}
        </span>
        <span style={{ color: "var(--fk-ink-3)" }}>
          合同登记 {a.contract_items ?? 0} 条只用于结算，不计入任务量
        </span>
      </div>
      {(a.issues ?? []).length > 0 && (
        <div style={{ marginTop: 4, color: "var(--fk-warn-700)" }}>
          {a.issues.map((x, i) => <div key={i}>· {x}</div>)}
        </div>
      )}
    </div>
  );
}

/**
 * 数据行级一致性（管理侧视图）。
 *
 * 与上面的「任务集合一致」分开列：集合对得上，仍可能平台展示的是另一份
 * 数据。这里显示的是 LS 同一批行的条数与指纹 —— 平台不存行，所以只要
 * 指纹对应的是本轮 LS 读取，就不存在「两份数据不同步」这回事。
 */
function ItemsLine({ i }: { i?: AssignmentItemsSummary }) {
  if (!i) return null;
  const bad = !i.in_sync || i.unreachable > 0 || i.truncated > 0;
  return (
    <div
      style={{
        marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--fk-line)",
        fontSize: 12, lineHeight: 1.8,
      }}
    >
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Badge tone={bad ? "warn" : "success"} dot>
          {i.in_sync ? "数据行一致" : "数据行待核对"}
        </Badge>
        <span style={{ color: "var(--fk-ink-2)" }}>
          {i.ls_projects} 个项目 / {i.items} 条真实数据行 · 已标 {i.labeled} 条
          {i.drafts > 0 ? ` · ${i.drafts} 条草稿` : ""}
          {i.unreachable > 0 ? ` · ${i.unreachable} 个项目读不到` : ""}
          {i.truncated > 0 ? ` · ${i.truncated} 个项目被截断` : ""}
        </span>
        <span className="fk-mono" style={{ color: "var(--fk-ink-3)", fontSize: 11 }}>
          指纹 {shortHash(i.fingerprint)}
        </span>
        <span style={{ color: "var(--fk-ink-4)", fontSize: 11 }}>
          来源 {i.source} · 平台不存任务行
        </span>
      </div>
      {(i.issues ?? []).length > 0 && (
        <div style={{ marginTop: 4, color: "var(--fk-warn-700)" }}>
          {i.issues.map((x, k) => <div key={k}>· {x}</div>)}
        </div>
      )}
    </div>
  );
}

export function LsSyncPanel({ canSync, onDone }: { canSync: boolean; onDone?: () => void }) {
  const [data, setData] = useState<ReconcileResp | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState(true);

  const load = useCallback(async () => {
    setErr("");
    try {
      const r = await request<ReconcileResp>("/api/v1/ls/reconcile");
      setData(r);
    } catch (e) {
      // 读不到就说读不到，不拿上一次的结果冒充「已一致」
      setData(null);
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const sync = async (ids: string[]) => {
    if (!ids.length) return;
    setBusy(ids.length > 1 ? "all" : ids[0]);
    let ok = 0;
    const failed: string[] = [];
    for (const id of ids) {
      try {
        await request(`/api/v1/tasks/${id}/ls-sync`, { method: "POST", body: "{}" });
        ok++;
      } catch {
        failed.push(id);
      }
    }
    setBusy("");
    toast(
      `同步完成 ${ok} 个批次${failed.length ? `，${failed.length} 个失败（${failed.join("、")}）` : ""}`
    );
    await load();
    onDone?.();
  };

  if (err) {
    return (
      <Panel title="标注引擎对账" sub="平台与 Label Studio 的进度是否一致">
        <div style={{ fontSize: 13, color: "var(--fk-ink-2)", lineHeight: 1.7 }}>
          读取失败：{err}
          <br />
          标注引擎不可达时无法对账（不会用缓存假装一致）。
          <div style={{ marginTop: 10 }}>
            <Button size="sm" onClick={() => void load()}>重试</Button>
          </div>
        </div>
      </Panel>
    );
  }

  if (!data) {
    return (
      <Panel title="标注引擎对账">
        <div style={{ fontSize: 13, color: "var(--fk-ink-2)" }}>正在读取…</div>
      </Panel>
    );
  }

  const diffs = data.rows.filter((r) => !r.in_sync);
  // 只有「条目数不符」的批次是**不该**自动同步的（合同口径），
  // 所以可同步集合要单独算 —— 否则会渲染出一个「同步 0 个」的按钮，
  // 让人以为功能坏了。
  const syncable = diffs.filter((r) => r.flags.some((f) => f !== "items"));
  const onlyItems = diffs.length > 0 && syncable.length === 0;

  return (
    <Panel
      title="标注引擎对账"
      sub="进度以 Label Studio 为准，名称以平台为准；条目数不自动改（合同口径）"
      actions={
        <>
          {data.errors.length > 0 && (
            <Badge tone="warn" dot>{data.errors.length} 个读取失败</Badge>
          )}
          {diffs.length === 0 ? (
            <Badge tone="success" dot>全部一致</Badge>
          ) : syncable.length > 0 ? (
            <Badge tone="warn" dot>{syncable.length} 个批次待同步</Badge>
          ) : (
            <Badge tone="warn" dot>{diffs.length} 个批次条目数待核对</Badge>
          )}
          <Button size="sm" onClick={() => setOpen((v) => !v)}>
            {open ? "收起" : "展开"}
          </Button>
          <Button size="sm" onClick={() => void load()} disabled={!!busy}>
            重新对账
          </Button>
          {canSync && syncable.length > 0 && (
            <Button
              size="sm"
              variant="primary"
              disabled={!!busy}
              onClick={() => void sync(syncable.map((r) => r.task_id))}
            >
              {busy === "all" ? "同步中…" : `同步 ${syncable.length} 个`}
            </Button>
          )}
        </>
      }
    >
      {open && (
        <>
          {diffs.length === 0 ? (
            <div style={{ fontSize: 13, color: "var(--fk-ink-2)", lineHeight: 1.7 }}>
              {data.total === 0
                ? "还没有关联标注项目的批次。"
                : `已核对 ${data.total} 个批次，进度与名称都一致。`}
            </div>
          ) : (
            <table className="fk-table" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  <th style={{ textAlign: "left" }}>批次</th>
                  <th style={{ textAlign: "right" }}>条目（平台 / LS）</th>
                  <th style={{ textAlign: "right" }}>已完成（平台 / LS）</th>
                  <th style={{ textAlign: "left" }}>LS 项目名</th>
                  <th style={{ textAlign: "left" }}>差异</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {diffs.map((r) => (
                  <tr key={r.task_id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{r.batch_no || r.task_id}</div>
                      <div style={{ color: "var(--fk-ink-3)", fontSize: 12 }}>{r.name}</div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {r.item_count} / {r.ls_tasks}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {r.done_count} / {r.ls_done}
                    </td>
                    <td style={{ color: "var(--fk-ink-2)" }}>{r.ls_title || "—"}</td>
                    <td>
                      {r.flags.map((f) => (
                        <span key={f} style={{ display: "inline-block", marginRight: 4 }}>
                          <Badge tone={f === "items" ? "warn" : "danger"}>
                            {FLAG_LABEL[f] ?? f}
                          </Badge>
                        </span>
                      ))}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {canSync && r.flags.some((f) => f !== "items") && (
                        <Button
                          size="sm"
                          disabled={!!busy}
                          onClick={() => void sync([r.task_id])}
                        >
                          {busy === r.task_id ? "同步中…" : "同步"}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {onlyItems && (
            <div
              style={{
                marginTop: 10,
                padding: "8px 10px",
                borderRadius: 8,
                background: "var(--fk-warn-50)",
                fontSize: 12.5,
                lineHeight: 1.7,
                color: "var(--fk-warn-700)",
              }}
            >
              条目数不一致、进度却一致：通常是<strong>数据还没导全</strong>
              （平台按合同登记条数，LS 里实际只导入了这些）。
              这里不自动改数字 —— 要么补导入，要么确认合同条数变更。
            </div>
          )}

          {data.errors.length > 0 && (
            <div style={{ marginTop: 10, fontSize: 12.5, color: "var(--fk-danger-600)" }}>
              {data.errors.map((e) => (
                <div key={e.task_id}>
                  {e.task_id}（LS #{e.ls_project_id}）：{e.error}
                </div>
              ))}
            </div>
          )}

          <AutoLine auto={data.auto} />
          <AssignmentLine a={data.assignment} />
          <ItemsLine i={data.items} />
        </>
      )}
    </Panel>
  );
}
