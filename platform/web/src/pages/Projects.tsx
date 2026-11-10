import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Icon } from "../design/icons";
import { LsDesignEntry } from "../design/LsDesignEntry";
import {
  Badge,
  Button,
  Checkbox,
  DataTable,
  DList,
  Drawer,
  Field,
  InlineMeter,
  Modal,
  PageHead,
  Panel,
  Progress,
  Select,
  Stat,
  Tabs,
  TextInput,
  toast,
  useSession,
} from "../design/ui";
import { cny, date, daysLeft, modalityLabel, num, projectStatusLabel, projectStatusTone, ratioPct, shortDate, taskStatusLabel, taskStatusTone } from "../lib/format";
import { useScoped } from "../lib/hooks";
import { get, post } from "../lib/http";
import { can } from "../lib/rbac";
import type { Modality, Project } from "../lib/types";

const MODALITY_OPTIONS = [
  { value: "image", label: "图像" },
  { value: "video", label: "视频" },
  { value: "audio", label: "音频" },
  { value: "text", label: "文本" },
  { value: "multimodal", label: "多模态" },
];

export default function Projects() {
  const s = useScoped();
  const { me } = useSession();
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState("");
  const [tab, setTab] = useState("all");
  const [modality, setModality] = useState("all");
  const [active, setActive] = useState<Project | null>(null);
  const [creating, setCreating] = useState(params.get("new") === "1");

  const writable = can(me, "project:write");

  const rows = useMemo(() => {
    let list = s.projects;
    if (q) {
      const k = q.toLowerCase();
      list = list.filter((p) => p.name.toLowerCase().includes(k) || p.code.toLowerCase().includes(k) || p.client.includes(q));
    }
    if (tab !== "all") list = list.filter((p) => p.status === tab);
    if (modality !== "all") list = list.filter((p) => p.modality === modality);
    return list;
  }, [s.projects, q, tab, modality]);

  const totalBudget = rows.reduce((a, b) => a + b.budget, 0);
  const totalItems = rows.reduce((a, b) => a + b.totalItems, 0);
  const delivered = rows.reduce((a, b) => a + b.deliveredItems, 0);

  const closeCreate = () => {
    setCreating(false);
    if (params.get("new")) {
      params.delete("new");
      setParams(params, { replace: true });
    }
  };

  return (
    <>
      <PageHead
        title="项目管理"
        desc="立项、分包、排期与交付验收的统一入口。项目是数据归属与结算的最小核算单元。"
        badge={<Badge tone="outline">{rows.length} 个项目</Badge>}
        actions={
          <>
            <Button icon="download" variant="secondary">导出清单</Button>
            {writable && (
              <Button icon="plus" variant="primary" onClick={() => setCreating(true)}>
                新建项目
              </Button>
            )}
          </>
        }
      />

      <div className="metric-strip" style={{ marginBottom: 14 }}>
        <div>
          <div className="mini-label">合同总额</div>
          <div className="fk-num" style={{ fontSize: 16, fontWeight: 600, marginTop: 3 }}>
            {cny(totalBudget, { compact: true })}
          </div>
        </div>
        <div>
          <div className="mini-label">总条目</div>
          <div className="fk-num" style={{ fontSize: 16, fontWeight: 600, marginTop: 3 }}>
            {num(totalItems)}
          </div>
        </div>
        <div>
          <div className="mini-label">已验收</div>
          <div className="fk-num" style={{ fontSize: 16, fontWeight: 600, marginTop: 3 }}>
            {num(delivered)}
          </div>
        </div>
        <div>
          <div className="mini-label">整体完成度</div>
          <div className="fk-num" style={{ fontSize: 16, fontWeight: 600, marginTop: 3, color: "var(--fk-accent-600)" }}>
            {((delivered / Math.max(1, totalItems)) * 100).toFixed(1)}%
          </div>
        </div>
        <div>
          <div className="mini-label">进行中</div>
          <div className="fk-num" style={{ fontSize: 16, fontWeight: 600, marginTop: 3 }}>
            {rows.filter((p) => p.status === "running").length}
          </div>
        </div>
      </div>

      <Panel flush>
        <div style={{ padding: "10px 16px 0" }}>
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { key: "all", label: "全部", count: s.projects.length },
              { key: "running", label: "进行中", count: s.projects.filter((p) => p.status === "running").length },
              { key: "accepting", label: "验收中", count: s.projects.filter((p) => p.status === "accepting").length },
              { key: "planning", label: "筹备中", count: s.projects.filter((p) => p.status === "planning").length },
              { key: "paused", label: "已暂停", count: s.projects.filter((p) => p.status === "paused").length },
              { key: "draft", label: "草稿", count: s.projects.filter((p) => p.status === "draft").length },
            ]}
          />
        </div>

        <div className="fk-toolbar" style={{ padding: "12px 16px" }}>
          <div className="fk-search" style={{ width: 280 }}>
            <Icon name="search" size={14} />
            <input
              className="fk-input"
              value={q}
              placeholder="搜索项目名称 / 编号 / 客户"
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          <Select
            value={modality}
            onChange={setModality}
            options={[{ value: "all", label: "全部模态" }, ...MODALITY_OPTIONS]}
          />
          <div className="fk-spacer" />
          <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>共 {rows.length} 条</span>
        </div>

        <DataTable<Project>
          rows={rows}
          onRowClick={setActive}
          columns={[
            {
              key: "code",
              header: "编号",
              width: 92,
              render: (p) => <span className="fk-mono" style={{ color: "var(--fk-ink-3)" }}>{p.code}</span>,
              sortValue: (p) => p.code,
            },
            {
              key: "name",
              header: "项目 / 客户",
              render: (p) => (
                <div>
                  <div className="fk-cell-strong">{p.name}</div>
                  <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{p.client}</div>
                </div>
              ),
              sortValue: (p) => p.name,
            },
            {
              key: "modality",
              header: "模态",
              width: 78,
              render: (p) => <Badge tone="neutral">{modalityLabel[p.modality]}</Badge>,
            },
            {
              key: "progress",
              header: "交付进度",
              width: 168,
              render: (p) => (
                <div>
                  <InlineMeter value={ratioPct(p.deliveredItems, p.totalItems)} tone="accent" />
                  <div style={{ fontSize: 10.5, color: "var(--fk-ink-4)", marginTop: 2, fontVariantNumeric: "tabular-nums" }}>
                    {num(p.deliveredItems)} / {num(p.totalItems)}
                  </div>
                </div>
              ),
              sortValue: (p) => ratioPct(p.deliveredItems, p.totalItems),
            },
            {
              key: "suppliers",
              header: "承接供应商",
              width: 132,
              render: (p) =>
                p.supplierIds.length ? (
                  <div className="row" style={{ gap: 5 }}>
                    {p.supplierIds.map((id) => {
                      const sup = s.suppliers.find((x) => x.id === id);
                      return (
                        <span
                          key={id}
                          style={{
                            fontSize: 10.5,
                            padding: "2px 5px",
                            borderRadius: 3,
                            background: "var(--fk-surface-2)",
                            border: "1px solid var(--fk-line)",
                            color: "var(--fk-ink-2)",
                            whiteSpace: "nowrap",
                          }}
                          title={sup?.name}
                        >
                          {sup?.shortName ?? id}
                        </span>
                      );
                    })}
                  </div>
                ) : (
                  <span style={{ color: "var(--fk-ink-4)", fontSize: 11.5 }}>待分包</span>
                ),
            },
            {
              key: "pass",
              header: "通过率",
              width: 76,
              align: "right",
              render: (p) => (
                <span
                  className="fk-num"
                  style={{ color: p.passRate === 0 ? "var(--fk-ink-4)" : p.passRate < 90 ? "var(--fk-warn-700)" : "var(--fk-ink)" }}
                >
                  {p.passRate === 0 ? "—" : `${p.passRate}%`}
                </span>
              ),
              sortValue: (p) => p.passRate,
            },
            {
              key: "due",
              header: "交付日",
              width: 104,
              align: "right",
              render: (p) => {
                const d = daysLeft(p.dueAt);
                if (d === null) {
                  return <span className="fk-num" style={{ fontSize: 12, color: "var(--fk-ink-4)" }}>未排期</span>;
                }
                return (
                  <span className="fk-num" style={{ fontSize: 12, color: d < 14 && p.status === "running" ? "var(--fk-warn-700)" : "var(--fk-ink-2)" }}>
                    {shortDate(p.dueAt)}
                    <span style={{ color: "var(--fk-ink-4)", marginLeft: 4 }}>({d}d)</span>
                  </span>
                );
              },
              sortValue: (p) => (p.dueAt ? new Date(p.dueAt).getTime() : Number.MAX_SAFE_INTEGER),
            },
            {
              key: "status",
              header: "状态",
              width: 84,
              render: (p) => (
                <Badge tone={projectStatusTone[p.status]} dot>
                  {projectStatusLabel[p.status]}
                </Badge>
              ),
            },
          ]}
          emptyText="没有匹配的项目"
          emptyHint="调整筛选条件，或新建一个项目"
        />
      </Panel>

      <ProjectDrawer project={active} onClose={() => setActive(null)} />
      <CreateProjectModal open={creating} onClose={closeCreate} />
    </>
  );
}

function ProjectDrawer({ project, onClose }: { project: Project | null; onClose: () => void }) {
  const s = useScoped();
  const { me } = useSession();
  if (!project) return null;

  const p = project;
  const tasks = s.tasks.filter((t) => t.projectId === p.id);
  // 批次实际导入到哪个 LS 项目，与「标注流程」面板里的项目级绑定是两回事：
  // 一个平台项目可以有多批数据、各自导到不同的 LS 项目。两处都要写清楚，
  // 否则甲方看到「已绑定 #70」和「未同步」会以为是数据坏了。
  const batchLsId = tasks.find((t) => t.lsProjectId)?.lsProjectId;
  const suppliers = p.supplierIds.map((id) => s.suppliers.find((x) => x.id === id)).filter(Boolean);
  const done = tasks.filter((t) => t.status === "accepted").length;
  // 未排期的项目 totalItems 是 0，直接除会渲染成 "NaN%"（已实测踩到）。
  const donePct = p.totalItems > 0 ? (p.deliveredItems / p.totalItems) * 100 : 0;

  return (
    <Drawer
      open={!!project}
      onClose={onClose}
      width={680}
      title={
        <span className="row" style={{ gap: 8 }}>
          {p.name}
          <Badge tone={projectStatusTone[p.status]} dot>{projectStatusLabel[p.status]}</Badge>
        </span>
      }
      sub={
        <span className="row" style={{ gap: 8 }}>
          <span className="fk-mono">{p.code}</span>
          <span>·</span>
          <span>{p.client}</span>
          <span>·</span>
          <span>负责人 {p.managerName}</span>
        </span>
      }
      foot={
        <>
          <Button variant="ghost" onClick={onClose}>关闭</Button>
          <Button variant="secondary" icon="external">查看批次</Button>
          <Button variant="primary" icon="edit">编辑项目</Button>
        </>
      }
    >
      <div className="stack-lg">
        <div>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>交付进度</span>
            <span className="fk-num" style={{ fontSize: 12, fontWeight: 600 }}>
              {num(p.deliveredItems)} / {num(p.totalItems)}（{donePct.toFixed(1)}%）
            </span>
          </div>
          <Progress value={donePct} />
        </div>

        <div className="g-3">
          <div className="tile">
            <div className="mini-label">合同金额</div>
            <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4 }}>{cny(p.budget, { compact: true })}</div>
            <div style={{ fontSize: 11, color: "var(--fk-ink-3)", marginTop: 2 }}>单条 {cny(p.unitPrice, { decimals: 2 })}</div>
          </div>
          <div className="tile">
            <div className="mini-label">批次</div>
            <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4 }}>{tasks.length}</div>
            <div style={{ fontSize: 11, color: "var(--fk-ink-3)", marginTop: 2 }}>已通过 {done} 批</div>
          </div>
          <div className="tile">
            <div className="mini-label">一次通过率</div>
            <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4 }}>{p.passRate}%</div>
            <div style={{ fontSize: 11, color: "var(--fk-ink-3)", marginTop: 2 }}>抽检 {p.qcSamplingRate}%</div>
          </div>
        </div>

        <Panel title="项目档案" flush>
          <div style={{ padding: 16 }}>
            <DList
              items={[
                ["数据模态", modalityLabel[p.modality]],
                ["标注界面", <span className="fk-mono">{p.labelingConfigName}</span>],
                ["启动 / 交付", `${date(p.startAt)} → ${date(p.dueAt)}`],
                ["质量标准", `抽检 ${p.qcSamplingRate}% · 最多返工 ${p.maxRework} 次`],
                ["批次同步目标", <span className="fk-mono">
                  {batchLsId ? `project #${batchLsId}` : "尚无批次接入"}
                </span>],
              ]}
            />
          </div>
        </Panel>

        <Panel
          title="标注流程"
          sub="甲方在标注引擎里直接编辑：员工按什么顺序、用什么控件"
          flush
        >
          <LsDesignEntry projectId={p.id} writable={can(me, "labeling:write")} />
        </Panel>

        <Panel title="标注规范" sub="标注页右侧侧栏会实时显示这份内容" flush>
          <GuidelinePanel projectId={p.id} writable={can(me, "labeling:write")} />
        </Panel>

        <Panel title="承接供应商" sub={`${suppliers.length} 家`} flush>
          <DataTable
            rows={suppliers as NonNullable<(typeof suppliers)[number]>[]}
            columns={[
              { key: "name", header: "供应商", render: (x) => <span className="fk-cell-strong">{x.name}</span> },
              { key: "tier", header: "等级", width: 60, render: (x) => <Badge tone={x.tier === "S" ? "accent" : "neutral"}>{x.tier}</Badge> },
              { key: "q", header: "质量分", width: 80, align: "right", render: (x) => <span className="fk-num">{x.qualityScore}</span> },
              {
                key: "tasks",
                header: "本项批次",
                width: 80,
                align: "right",
                render: (x) => <span className="fk-num">{s.tasks.filter((t) => t.projectId === p.id && t.supplierId === x.id).length}</span>,
              },
            ]}
          />
        </Panel>

        <Panel title="最近批次" flush>
          <DataTable
            rows={tasks.slice(0, 6)}
            columns={[
              { key: "name", header: "批次", render: (t) => <span style={{ fontSize: 12.5 }}>{t.batchNo} · {t.assigneeName ?? "未分配"}</span> },
              { key: "cnt", header: "条目", width: 76, align: "right", render: (t) => <span className="fk-num">{num(t.itemCount)}</span> },
              { key: "st", header: "状态", width: 80, render: (t) => <Badge tone={taskStatusTone[t.status]} dot>{taskStatusLabel[t.status]}</Badge> },
            ]}
            emptyText="尚无批次"
          />
        </Panel>
      </div>
    </Drawer>
  );
}

/**
 * 项目标注规范编辑器。
 *
 * 为什么放在项目详情里：这段内容最终显示在**标注页右侧侧栏**（Label Studio
 * 里由注入层渲染），甲方改规则、标注员按规则干活，两边必须是同一份。
 * 存平台（forge.project_guidelines）而不是存 LS 项目描述，是因为 LS 那边
 * 是纯文本、无权限、改完没人审；这里能走平台的角色与审计。
 *
 * Markdown 原样存、前端只做展示：不做富文本编辑器，也不把渲染结果回存 ——
 * 存 HTML 等于把 XSS 面存进库里，而且同一个字符串会被注入到另一个源的页面里。
 */
function GuidelinePanel({ projectId, writable }: { projectId: string; writable: boolean }) {
  const [md, setMd] = useState("");
  const [imgs, setImgs] = useState("");
  const [saved, setSaved] = useState<{ md: string; imgs: string[] }>({ md: "", imgs: [] });
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr("");
    setEditing(false);
    get<{ markdown: string; images: string[] }>(`/api/v1/projects/${projectId}/guideline`)
      .then((d) => {
        if (!alive) return;
        const m = d.markdown || "";
        const list = d.images || [];
        setMd(m);
        setImgs(list.join("\n"));
        setSaved({ md: m, imgs: list });
      })
      .catch((e) => {
        // 读失败要说清是「没配」还是「接口不通」，别都显示成空
        if (alive) setErr((e as Error).message || "读取失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  async function save() {
    const images = imgs.split("\n").map((x) => x.trim()).filter(Boolean);
    const bad = images.find((u) => !/^https?:\/\//i.test(u));
    if (bad) {
      toast("示例图只支持 http/https 绝对地址");
      return;
    }
    setBusy(true);
    try {
      await post(`/api/v1/projects/${projectId}/guideline`, { markdown: md, images });
      setSaved({ md, imgs: images });
      setEditing(false);
      toast("标注规范已保存，标注员刷新标注页即可看到");
    } catch (e) {
      toast((e as Error).message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return <div style={{ padding: 16, fontSize: 12.5, color: "var(--fk-ink-3)" }}>正在读取规范…</div>;
  }
  if (err) {
    return (
      <div style={{ padding: 16, fontSize: 12.5, color: "var(--fk-ink-3)", lineHeight: 1.9 }}>
        规范读取失败：{err}
        <br />
        当前身份可能没有读项目权限，或编排层未连通。
      </div>
    );
  }

  const empty = !saved.md.trim() && saved.imgs.length === 0;

  return (
    <div style={{ padding: 16 }}>
      {editing ? (
        <div className="stack" style={{ gap: 10 }}>
          <Field label="规范正文（Markdown，直接换行即可）">
            <textarea
              className="fk-input"
              style={{ minHeight: 132, padding: 10, lineHeight: 1.8, resize: "vertical", fontFamily: "inherit" }}
              value={md}
              maxLength={20000}
              placeholder={"例：\n1. 每张图必须框全目标主体，边界贴着目标外沿\n2. 遮挡超过 50% 的目标标记为 NG\n3. 同一图多个目标逐个标注，不要合并"}
              onChange={(e) => setMd(e.target.value)}
            />
          </Field>
          <Field label="示例图地址（一行一个，最多 12 张）">
            <textarea
              className="fk-input"
              style={{ minHeight: 62, padding: 10, lineHeight: 1.8, resize: "vertical", fontFamily: "var(--fk-font-mono)" }}
              value={imgs}
              placeholder="https://cdn.example.cn/guideline/ok.png"
              onChange={(e) => setImgs(e.target.value)}
            />
          </Field>
          <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => { setEditing(false); setMd(saved.md); setImgs(saved.imgs.join("\n")); }}>
              取消
            </Button>
            <Button variant="primary" onClick={save} disabled={busy}>
              {busy ? "保存中…" : "保存规范"}
            </Button>
          </div>
        </div>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {empty ? (
            <div style={{ fontSize: 12.5, color: "var(--fk-ink-3)", lineHeight: 1.9 }}>
              还没有配置标注规范。
              <br />
              标注员在标注页按 G 打开的右侧侧栏会显示这里的内容；不配就是空的，不会编造示例。
            </div>
          ) : (
            <>
              {saved.imgs.length > 0 && (
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  {saved.imgs.map((u) => (
                    <a key={u} href={u} target="_blank" rel="noreferrer"
                       style={{ fontSize: 12, color: "var(--fk-accent)", wordBreak: "break-all" }}>
                      示例图 {saved.imgs.indexOf(u) + 1}
                    </a>
                  ))}
                </div>
              )}
              <pre style={{
                margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word",
                fontSize: 12.5, lineHeight: 1.85, color: "var(--fk-ink-2)",
                background: "var(--fk-surface-2)", border: "1px solid var(--fk-line)",
                borderRadius: 8, padding: 12, maxHeight: 200, overflow: "auto",
              }}>{saved.md}</pre>
            </>
          )}
          {writable && (
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <Button variant="secondary" icon="edit" onClick={() => setEditing(true)}>
                {empty ? "填写规范" : "编辑规范"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CreateProjectModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const store = useScoped();
  const [name, setName] = useState("");
  const [client, setClient] = useState("");
  const [modality, setModality] = useState<Modality>("image");
  const [items, setItems] = useState("20000");
  const [price, setPrice] = useState("2.4");
  const [due, setDue] = useState("");
  const [qc, setQc] = useState("10");
  const [cfg, setCfg] = useState("BBox + Classification");
  const [picked, setPicked] = useState<string[]>([]);
  const [err, setErr] = useState("");

  const reset = () => {
    setName(""); setClient(""); setItems("20000"); setPrice("2.4"); setDue(""); setQc("10"); setPicked([]); setErr("");
  };

  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim()) return setErr("请填写项目名称");
    if (!client.trim()) return setErr("请填写客户名称");
    if (Number(items) <= 0) return setErr("总条目数必须大于 0");
    if (Number(price) <= 0) return setErr("单价必须大于 0");
    const dueAt = due ? new Date(due).toISOString() : new Date(Date.now() + 60 * 86400000).toISOString();
    setBusy(true);
    const { project, result } = await store.createProject({
      name: name.trim(),
      client: client.trim(),
      modality,
      totalItems: Number(items),
      unitPrice: Number(price),
      supplierIds: picked,
      dueAt,
      qcSamplingRate: Number(qc),
      labelingConfigName: cfg,
    });
    setBusy(false);
    if (!result.ok || !project) {
      // 失败时停在弹窗里，把原因写在原处 —— 关掉弹窗再弹一个 toast
      // 会让用户丢掉刚填的七八个字段
      setErr(result.reason ?? "创建失败");
      return;
    }
    toast(`已创建 ${project.code}，状态为草稿`);
    reset();
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={620}
      title="新建项目"
      foot={
        <>
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={submit} disabled={busy}>
            {busy ? "创建中…" : "创建为草稿"}
          </Button>
        </>
      }
    >
      <div className="stack">
        {err && <div className="fk-error">{err}</div>}
        <div className="g-half" style={{ gap: 12 }}>
          <Field label="项目名称" required>
            <TextInput value={name} onChange={setName} placeholder="例如：自动驾驶多模态路采标注" />
          </Field>
          <Field label="客户" required>
            <TextInput value={client} onChange={setClient} placeholder="例如：蔚途汽车" />
          </Field>
        </div>

        <div className="g-half" style={{ gap: 12 }}>
          <Field label="数据模态" required>
            <Select value={modality} onChange={(v) => setModality(v as Modality)} options={MODALITY_OPTIONS} />
          </Field>
          <Field label="标注模板" hint="来自 Label Studio labeling config">
            <Select
              value={cfg}
              onChange={setCfg}
              options={[
                { value: "BBox + Classification", label: "BBox + Classification" },
                { value: "Polygon Segmentation", label: "Polygon Segmentation" },
                { value: "NER + Relation", label: "NER + Relation" },
                { value: "ASR Transcript", label: "ASR Transcript" },
                { value: "Temporal Boundary", label: "Temporal Boundary" },
              ]}
            />
          </Field>
        </div>

        <div className="g-3" style={{ gap: 12 }}>
          <Field label="总条目数" required>
            <TextInput value={items} onChange={setItems} type="number" />
          </Field>
          <Field label="单条单价（元）" required>
            <TextInput value={price} onChange={setPrice} type="number" />
          </Field>
          <Field label="质检抽样率（%）">
            <TextInput value={qc} onChange={setQc} type="number" />
          </Field>
        </div>

        <Field label="计划交付日">
          <TextInput value={due} onChange={setDue} type="date" />
        </Field>

        <Field label="预分包供应商" hint="可稍后调整；供应商仅能看到被授权的项目">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 2 }}>
            {store.suppliers.map((sup) => (
              <label
                key={sup.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "7px 10px",
                  border: `1px solid ${picked.includes(sup.id) ? "var(--fk-accent-200)" : "var(--fk-line)"}`,
                  background: picked.includes(sup.id) ? "var(--fk-accent-50)" : "var(--fk-surface)",
                  borderRadius: 4,
                  fontSize: 12,
                  cursor: "pointer",
                }}
              >
                <Checkbox
                  checked={picked.includes(sup.id)}
                  onChange={(v) => setPicked((cur) => (v ? [...cur, sup.id] : cur.filter((x) => x !== sup.id)))}
                />
                <span style={{ flex: 1 }}>{sup.shortName}</span>
                <Badge tone="outline">{sup.tier}</Badge>
              </label>
            ))}
          </div>
        </Field>
      </div>
    </Modal>
  );
}
