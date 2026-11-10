import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Badge, Button, Checkbox, Field, Modal, Select, TextInput, toast } from "./ui";
import { ApiError, get, post, put } from "../lib/http";
import { date } from "../lib/format";
import type {
  FlowApplyResult,
  FlowKindMeta,
  FlowMaterials,
  FlowPreview,
  FlowTemplate,
  LabelStep,
  ProjectFlow,
} from "../lib/types";

/**
 * 标注流程设计器 —— 甲方自己定义「员工怎么标」
 * ===========================================================================
 * 这个文件解决一个很具体的问题：过去「标注员在页面上怎么操作」是写死在
 * Label Studio 的 label_config XML 里的，改一次要找工程师。甲方看到的只是
 * 结果，提需求要等排期。
 *
 * 现在把它变成平台里的可视化编辑：数据对象 → 步骤列表 → 条件分支，
 * 编译成 XML 写进标注引擎。甲方点几下就能改，不需要碰代码。
 *
 * 三条不许妥协的设计约束
 * ---------------------------------------------------------------------------
 * 1）**不前端生成 XML**。预览和最终写进 LS 的必须是同一份实现（后端
 *    `flow.build_config`），否则"界面看着对、推上去不对"这类问题几乎无法排查。
 *    所以预览走 `POST .../flow/preview`，每次改动去后端算一遍。
 *
 * 2）**保存 ≠ 生效**。保存只落库；点「应用到标注引擎」才动 LS 的表单。
 *    改表单结构会让旧标注对不上（比如删掉一个字段，历史标注里那条结果就
 *    渲染不出来），所以必须有一步明确的确认，而且已有标注时要二次确认。
 *
 * 3）**字段名露出来但要说清代价**。`name` 是标注结果里的 key，导出、对账、
 *    验收报告都靠它。藏起来"替用户自动生成"会让改完字段名之后历史数据
 *    静默错位，比难看更糟。
 */

/* ------------------------------- 常量 ------------------------------------ */

const KIND_LABEL: Record<string, string> = {
  rectanglelabels: "矩形框选",
  polygonlabels: "多边形",
  keypointlabels: "关键点",
  labels: "文本片段标注",
  choices: "单选 / 多选",
  textarea: "多行文字",
  rating: "评分",
  number: "数字",
};

/** 预览里用字形区分控件，甲方扫一眼就能对上"这一步是在画框还是在打勾" */
const KIND_GLYPH: Record<string, string> = {
  rectanglelabels: "▭",
  polygonlabels: "⬡",
  keypointlabels: "✦",
  labels: "▮",
  choices: "☑",
  textarea: "✎",
  rating: "★",
  number: "#",
};

const KIND_PREFIX: Record<string, string> = {
  rectanglelabels: "rect",
  polygonlabels: "poly",
  keypointlabels: "key",
  labels: "span",
  choices: "choice",
  textarea: "note",
  rating: "rating",
  number: "num",
};

const OBJECT_LABEL: Record<string, string> = {
  image: "图像",
  audio: "音频",
  video: "视频",
  text: "文本",
};

/* ----------------------------- 小工具 ------------------------------------ */

let uidSeq = 0;
const nextId = () => `st${++uidSeq}`;

/** 设计器内部步骤：多带一个 `_raw`（可选值的原始文本）。
 *
 * 为什么不用 options.join("\n") 直接喂 textarea：
 * 用户想加第三行时会先敲一个换行，join 立刻把末尾空行吃掉，光标位置跳回，
 * 于是"永远加不了新选项"。这是受控组件里最常见的坑，必须留住原始文本。
 */
type DraftStep = LabelStep & { _raw?: string };

function splitOptions(raw: string): string[] {
  return raw
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
}

function fromServer(steps: LabelStep[]): DraftStep[] {
  return steps.map((s) => ({
    ...s,
    id: s.id || nextId(),
    _raw: (s.options || []).join("\n"),
  }));
}

/** 送给后端前把 `_raw` 去掉，并保证 options 是数组 */
function toPayload(steps: DraftStep[]): LabelStep[] {
  return steps.map((s) => ({
    kind: s.kind,
    name: s.name,
    title: s.title,
    required: !!s.required,
    per_region: !!s.per_region,
    options: splitOptions(s._raw ?? ""),
    depends_on: s.depends_on || "",
    depends_value: s.depends_value || "",
  }));
}

/** 字段名冲突时给一个可用名字（字母开头，项目内唯一，不等于数据对象） */
function freeName(kind: string, used: Set<string>, object: string): string {
  const base = KIND_PREFIX[kind] || "field";
  for (let i = 1; i <= 99; i++) {
    const n = `${base}_${i}`;
    if (!used.has(n) && n !== object) return n;
  }
  return `${base}_99`;
}

const errList = (msg: string): string[] =>
  msg
    .split(/；|;/)
    .map((x) => x.trim())
    .filter(Boolean);

/* ============================ 面板（项目详情里） ========================== */

export function FlowPanel({
  projectId,
  modality,
  writable,
}: {
  projectId: string;
  /** 项目的数据模态：没配过流程时作为默认数据对象，省一步选择 */
  modality: string;
  /** 有 labeling:write 才能打开编辑器；否则只读展示 */
  writable: boolean;
}) {
  const [flow, setFlow] = useState<ProjectFlow | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    setErr("");
    get<ProjectFlow>(`/api/v1/projects/${projectId}/flow`)
      .then((d) => {
        if (alive) setFlow(d);
      })
      .catch((e) => {
        if (alive) setErr((e as Error).message || "读取失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(() => load(), [load]);

  if (loading) {
    return <div style={{ padding: 16, fontSize: 12.5, color: "var(--fk-ink-3)" }}>正在读取标注流程…</div>;
  }
  if (err) {
    return (
      <div style={{ padding: 16, fontSize: 12.5, color: "var(--fk-ink-3)", lineHeight: 1.9 }}>
        流程读取失败：{err}
        <br />
        当前身份可能没有读项目权限，或编排层未连通。
      </div>
    );
  }

  const steps = flow?.steps?.steps ?? [];
  const object = flow?.steps?.object || modality || "image";
  const lsId = flow?.ls_project_id;

  return (
    <div style={{ padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <Badge tone="outline">{OBJECT_LABEL[object] || object}</Badge>
          <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>
            {steps.length ? `${steps.length} 步` : "还没有配流程"}
            {flow?.updated_at ? ` · 最近保存 ${date(flow.updated_at)}` : ""}
          </span>
        </div>
        <Button
          size="sm"
          variant={steps.length ? "secondary" : "primary"}
          icon={steps.length ? "edit" : "plus"}
          onClick={() => setOpen(true)}
        >
          {steps.length ? (writable ? "编辑流程" : "查看流程") : writable ? "配标注流程" : "查看"}
        </Button>
      </div>

      <div style={{ marginTop: 12 }}>
        <StepTimeline steps={steps} compact emptyText={writable ? "还没配流程。点右上角「配标注流程」，从一个模板起步最快。" : "还没配流程。"} />
      </div>

      <div style={{ marginTop: 12, fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.8 }}>
        {lsId ? (
          <>
            已绑定标注引擎 <span className="fk-mono">project #{String(lsId)}</span>
            。保存流程不会立刻影响标注员，要点「应用到标注引擎」才生效。
          </>
        ) : (
          <>还没绑定标注引擎。第一次点「应用到标注引擎」会自动建一个标注项目。</>
        )}
        {!writable && (
          <>
            <br />
            你的角色只能查看，改动需要「项目负责人 / 运营总监 / 平台管理员」或客户方账号。
          </>
        )}
      </div>

      {open && (
        <FlowDesigner
          projectId={projectId}
          modality={modality}
          flow={flow}
          writable={writable}
          onClose={() => setOpen(false)}
          onSaved={(f) => setFlow(f)}
        />
      )}
    </div>
  );
}

/* ============================ 流程展示（只读） =========================== */

/** 把步骤按"标注员会遇到的顺序"画出来。面板摘要和设计器预览共用同一份，
 *  避免"摘要说 3 步、预览说 2 步"这种对不上的情况。 */
function StepTimeline({
  steps,
  compact = false,
  emptyText = "还没有步骤",
}: {
  steps: LabelStep[];
  compact?: boolean;
  emptyText?: string;
}) {
  if (!steps.length) {
    return <div style={{ fontSize: 12.5, color: "var(--fk-ink-3)" }}>{emptyText}</div>;
  }
  return (
    <div style={{ display: "grid", gap: compact ? 9 : 11 }}>
      {steps.map((s, i) => {
        const dep = s.depends_on ? steps.find((x) => x.name === s.depends_on) : null;
        return (
          <div key={s.id || s.name || i} style={{ display: "flex", gap: 9, alignItems: "flex-start" }}>
            <span
              className="fk-num"
              style={{
                flex: "none",
                width: 20,
                height: 20,
                borderRadius: "var(--fk-radius-pill)",
                background: "var(--fk-surface-2)",
                color: "var(--fk-ink-3)",
                fontSize: 11,
                fontWeight: 600,
                display: "grid",
                placeItems: "center",
                marginTop: 1,
              }}
            >
              {i + 1}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                <span style={{ color: "var(--fk-accent-500)", fontSize: 13, lineHeight: 1 }}>
                  {KIND_GLYPH[s.kind] || "•"}
                </span>
                <span style={{ fontWeight: 560, fontSize: compact ? 12.5 : 13 }}>
                  {s.title || "未命名步骤"}
                </span>
                {s.required && <Badge tone="warn">必填</Badge>}
                {s.per_region && <Badge tone="neutral">每区域一次</Badge>}
                {!compact && (
                  <span className="fk-mono" style={{ fontSize: 10.5, color: "var(--fk-ink-3)" }}>
                    {s.name}
                  </span>
                )}
              </div>
              {dep && (
                <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", marginTop: 3 }}>
                  仅当「{dep.title || dep.name}」选了「{s.depends_value}」时才出现
                </div>
              )}
              {!!(s.options || []).length && (
                <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 5 }}>
                  {(s.options || []).map((o) => (
                    <span
                      key={o}
                      style={{
                        fontSize: 11,
                        padding: "1px 6px",
                        border: "1px solid var(--fk-line)",
                        borderRadius: "var(--fk-radius-sm)",
                        color: "var(--fk-ink-2)",
                        background: "var(--fk-surface)",
                      }}
                    >
                      {o}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ============================== 设计器 ================================== */

function FlowDesigner({
  projectId,
  modality,
  flow,
  writable,
  onClose,
  onSaved,
}: {
  projectId: string;
  modality: string;
  flow: ProjectFlow | null;
  writable: boolean;
  onClose: () => void;
  onSaved: (f: ProjectFlow) => void;
}) {
  const [materials, setMaterials] = useState<FlowMaterials | null>(null);
  const [draft, setDraft] = useState<{ object: string; steps: DraftStep[] }>(() => ({
    object: flow?.steps?.object || modality || "image",
    steps: fromServer(flow?.steps?.steps || []),
  }));
  const [templateKey, setTemplateKey] = useState<string>(flow?.template_key || "");
  const [rawMode, setRawMode] = useState<boolean>(!!flow?.raw_override);
  const [rawXml, setRawXml] = useState<string>(flow?.label_config || "");
  const [preview, setPreview] = useState<FlowPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [errs, setErrs] = useState<string[]>([]);
  const [busy, setBusy] = useState<"" | "save" | "apply">("");
  const [confirm, setConfirm] = useState<string>("");
  const [forceAck, setForceAck] = useState(false);
  const [pane, setPane] = useState<"visual" | "xml">("visual");
  const [importNote, setImportNote] = useState<string[]>([]);
  const seqRef = useRef(0);

  /* 素材（模板 + 控件类型）只在打开时取一次 */
  useEffect(() => {
    get<FlowMaterials>("/api/v1/label-step-templates")
      .then(setMaterials)
      .catch((e) => setErrs([`素材读取失败：${(e as Error).message}`]));
  }, []);

  const kinds: FlowKindMeta[] = materials?.kinds || [];
  const kindMeta = (k: string) => kinds.find((x) => x.kind === k);

  const draftKey = useMemo(
    () => JSON.stringify({ o: draft.object, s: toPayload(draft.steps), r: rawMode ? rawXml : "" }),
    [draft, rawMode, rawXml]
  );

  /* 实时预览：改动后 450ms 去后端编译一次。
     为什么不在前端算 XML：两份实现迟早漂移，而漂移的表现是"预览是对的、
     标注员看到的是错的"，这类问题甲方根本没法自己排查。 */
  useEffect(() => {
    if (!draftKey) return;
    const seq = ++seqRef.current;
    setPreviewing(true);
    const t = window.setTimeout(() => {
      const body = rawMode
        ? { object: draft.object, steps: [], label_config_raw: rawXml }
        : { object: draft.object, steps: toPayload(draft.steps) };
      post<FlowPreview>(`/api/v1/projects/${projectId}/flow/preview`, body)
        .then((r) => {
          if (seq === seqRef.current) setPreview(r);
        })
        .catch((e) => {
          if (seq === seqRef.current) {
            setPreview({
              ok: false,
              errors: [`预览请求失败：${(e as Error).message}`],
              label_config: "",
              steps: null,
            });
          }
        })
        .finally(() => {
          if (seq === seqRef.current) setPreviewing(false);
        });
    }, 450);
    return () => window.clearTimeout(t);
  }, [draftKey, projectId, rawMode, rawXml, draft.object, draft.steps]);

  /* ------------------------------ 步骤操作 ------------------------------ */

  const usedNames = () => new Set(draft.steps.map((s) => s.name));

  function patchStep(idx: number, patch: Partial<DraftStep>) {
    setDraft((d) => {
      const steps = d.steps.slice();
      steps[idx] = { ...steps[idx], ...patch };
      return { ...d, steps };
    });
  }

  function addStep(kind = "choices") {
    const meta = kindMeta(kind);
    const needOpts = meta ? meta.need_options : true;
    const name = freeName(kind, usedNames(), draft.object);
    const opts = needOpts ? (kind === "choices" ? ["选项一", "选项二"] : ["标签一"]) : [];
    setDraft((d) => ({
      ...d,
      steps: [
        ...d.steps,
        {
          id: nextId(),
          kind,
          name,
          title: `第 ${d.steps.length + 1} 步`,
          required: true,
          options: opts,
          _raw: opts.join("\n"),
        },
      ],
    }));
  }

  function removeStep(idx: number) {
    setDraft((d) => {
      const victim = d.steps[idx];
      const steps = d.steps
        .filter((_, i) => i !== idx)
        // 删掉被依赖的步骤后，挂在它下面的条件分支会变成"依赖一个不存在的步骤"，
        // 后端会报错、甲方却看不出为什么。这里顺手把条件清掉。
        .map((s) => (s.depends_on === victim.name ? { ...s, depends_on: "", depends_value: "" } : s));
      return { ...d, steps };
    });
  }

  function moveStep(idx: number, dir: -1 | 1) {
    setDraft((d) => {
      const j = idx + dir;
      if (j < 0 || j >= d.steps.length) return d;
      const steps = d.steps.slice();
      [steps[idx], steps[j]] = [steps[j], steps[idx]];
      return { ...d, steps };
    });
  }

  function applyTemplate(key: string) {
    setTemplateKey(key);
    const tpl: FlowTemplate | undefined = materials?.templates.find((x) => x.key === key);
    if (!tpl) return;
    setDraft({ object: tpl.object, steps: fromServer(tpl.steps) });
    setRawMode(false);
    setErrList([]);
    toast(`已套用模板「${tpl.name}」，可以直接改`);
  }

  function setErrList(v: string[]) {
    setErrs(v);
  }

  /** 从标注引擎读回现有表单，当作设计器的起点。
   *
   * 这条路径存在的理由是"别让人静默覆盖自己看不懂的东西"：
   * 老项目是在 LS 后台直接配的，平台里没有流程。让甲方从空白开始编，
   * 他一点应用就把原来的配置盖掉了，而且全程没有任何提示。
   */
  async function importFromLs() {
    setBusy("save");
    setErrs([]);
    setImportNote([]);
    try {
      const r = await get<{
        ok: boolean;
        reason?: string;
        message?: string;
        steps: { object: string; steps: LabelStep[] } | null;
        label_config: string;
        unsupported: string[];
        parse_error?: boolean;
      }>(`/api/v1/projects/${projectId}/flow/from-ls`);
      if (!r.ok) {
        toast(r.message || "这个项目还没有可导入的配置");
        return;
      }
      if (r.parse_error || (r.unsupported || []).length) {
        // 有平台不认识的控件：直接切手写模式，让原文完整保留在这。
        // 硬塞进图形编辑器再保存，等于把那些控件删了。
        setRawXml(r.label_config);
        setRawMode(true);
        setImportNote(
          r.parse_error
            ? ["这份 XML 解析不了（可能被手工改坏了），已切到手写模式，请直接改原文"]
            : (r.unsupported || []).map((t) => `<${t}> 平台不认识的控件，已保留在手写模式里`)
        );
        toast("现有配置里有平台不认识的控件，已切到手写 XML 模式");
        return;
      }
      setDraft({ object: r.steps?.object || draft.object, steps: fromServer(r.steps?.steps || []) });
      setRawMode(false);
      setTemplateKey("");
      toast(`已导入标注引擎里的现有配置，共 ${(r.steps?.steps || []).length} 步`);
    } catch (e) {
      setErrs([`导入失败：${(e as Error).message}`]);
    } finally {
      setBusy("");
    }
  }

  /* ------------------------------ 保存 / 应用 ---------------------------- */

  function bodyForSave() {
    return rawMode
      ? { object: draft.object, steps: [], template_key: templateKey || null, label_config_raw: rawXml }
      : { object: draft.object, steps: toPayload(draft.steps), template_key: templateKey || null };
  }

  async function doSave(thenApply: boolean) {
    if (!writable) return;
    setBusy(thenApply ? "apply" : "save");
    setErrs([]);
    try {
      const r = await put<{ ok: boolean; flow: ProjectFlow }>(
        `/api/v1/projects/${projectId}/flow`,
        bodyForSave()
      );
      onSaved(r.flow);
      if (!thenApply) {
        toast("流程已保存。标注员那边还是旧表单，要「应用到标注引擎」才生效");
        return;
      }
      await doApply(false);
    } catch (e) {
      const er = e as ApiError;
      setErrs(errList(er.message || "保存失败"));
      toast(er.status === 404 ? "没有权限或项目不存在" : "流程没保存成功，看右侧红字");
    } finally {
      setBusy("");
    }
  }

  async function doApply(force: boolean) {
    setBusy("apply");
    setErrs([]);
    try {
      const r = await post<FlowApplyResult>(
        `/api/v1/projects/${projectId}/flow/apply`,
        { force }
      );
      if (r.mode === "created") {
        // 新建的是空项目，必须把"数据还没导"如实说出来，
        // 否则甲方以为建好了就能标，点进去一条数据都没有。
        toast(`已创建标注项目 #${r.ls_project_id}。${r.hint || ""}`);
      } else {
        toast(
          r.annotations > 0
            ? `已更新标注引擎项目 #${r.ls_project_id}，覆盖了 ${r.annotations} 条已有标注的表单结构`
            : `已更新标注引擎项目 #${r.ls_project_id}`
        );
      }
      setConfirm("");
      setForceAck(false);
      onClose();
    } catch (e) {
      const er = e as ApiError;
      if (er.status === 409) {
        // 409 不是"失败"，是"要你确认"。原样把后端的文案给甲方看，
        // 里面带着"N 条标注"这个具体数字，比任何通用提示都有说服力。
        setConfirm(er.message);
        return;
      }
      setErrs(errList(er.message || "应用失败"));
      toast(er.status === 502 ? "标注引擎调用失败，流程已保存但没生效" : "应用失败，看右侧红字");
    } finally {
      setBusy("");
    }
  }

  /* -------------------------------- 渲染 -------------------------------- */

  const steps = draft.steps;
  const liveErrs = preview && !preview.ok ? preview.errors : [];
  const shownErrs = errs.length ? errs : liveErrs;

  return (
    <>
      <Modal
        open
        width={1120}
        title="标注流程设计器"
        onClose={onClose}
        foot={
          <>
            <span style={{ marginRight: "auto", fontSize: 11.5, color: "var(--fk-ink-3)" }}>
              {previewing
                ? "正在编译…"
                : preview?.ok
                  ? "配置可编译，可以保存"
                  : shownErrs.length
                    ? "有需要先修的问题"
                    : ""}
            </span>
            <Button variant="ghost" onClick={onClose} disabled={!!busy}>
              取消
            </Button>
            <Button
              variant="secondary"
              onClick={() => doSave(false)}
              disabled={!writable || !!busy || !preview?.ok}
              title="只保存，不影响正在标注的人"
            >
              {busy === "save" ? "保存中…" : "保存"}
            </Button>
            <Button
              variant="primary"
              onClick={() => doSave(true)}
              disabled={!writable || !!busy || !preview?.ok}
              title="保存并写进标注引擎，标注员刷新后就是新表单"
            >
              {busy === "apply" ? "应用中…" : "保存并应用到标注引擎"}
            </Button>
          </>
        }
      >
        {/* ---------- 顶部：数据对象 / 模板 / 手写开关 ---------- */}
        <div
          className="row"
          style={{ gap: 10, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 14 }}
        >
          <div style={{ width: 150 }}>
            <Field label="数据对象" hint="标注的是什么">
              <Select
                value={draft.object}
                onChange={(v) =>
                  setDraft((d) => ({
                    ...d,
                    object: v,
                    // 字段名不能和数据对象同名（LS 会拒绝），改名时顺手避开
                    steps: d.steps.map((s) => (s.name === v ? { ...s, name: freeName(s.kind, usedNames(), v) } : s)),
                  }))
                }
                options={(materials?.objects || [{ object: "image", tag: "Image" }]).map((o) => ({
                  value: o.object,
                  label: `${OBJECT_LABEL[o.object] || o.object}（${o.tag}）`,
                }))}
              />
            </Field>
          </div>
          <div style={{ width: 230 }}>
            <Field label="起手模板" hint="套用会替换当前步骤">
              <Select
                value={templateKey}
                onChange={applyTemplate}
                options={[
                  { value: "", label: "不用模板（自己配）" },
                  ...(materials?.templates || []).map((t) => ({
                    value: t.key,
                    label: `${t.name} · ${OBJECT_LABEL[t.object] || t.object}`,
                  })),
                ]}
              />
            </Field>
          </div>
          <div style={{ flex: 1, minWidth: 200 }}>
            <Field label=" ">
              <Checkbox
                checked={rawMode}
                onChange={(v) => {
                  if (v && !rawXml.trim()) setRawXml(preview?.label_config || flow?.label_config || "");
                  setRawMode(v);
                }}
                label="直接手写 XML（高级）"
              />
            </Field>
          </div>
        </div>

        {rawMode && (
          <div
            style={{
              marginBottom: 12,
              padding: "8px 10px",
              background: "var(--fk-warn-bg, #FDF6EC)",
              border: "1px solid var(--fk-line)",
              borderRadius: "var(--fk-radius-md)",
              fontSize: 12,
              lineHeight: 1.8,
              color: "var(--fk-ink-2)",
            }}
          >
            手写模式下图形编辑器不参与生成。切回图形模式会用步骤重新编译，
            <b>手写内容会被覆盖</b>。只有要用平台不认识的控件时才需要走这里。
          </div>
        )}

        {shownErrs.length > 0 && (
          <div
            style={{
              marginBottom: 12,
              padding: "9px 11px",
              background: "var(--fk-danger-bg, #FDF1F0)",
              border: "1px solid var(--fk-line)",
              borderRadius: "var(--fk-radius-md)",
              fontSize: 12,
              lineHeight: 1.85,
              color: "var(--fk-ink)",
            }}
          >
            <b>还需要改 {shownErrs.length} 处：</b>
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {shownErrs.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </div>
        )}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: rawMode ? "1fr" : "minmax(0, 1.15fr) minmax(0, 0.85fr)",
            gap: 18,
            alignItems: "start",
          }}
        >
          {/* ---------- 左：步骤编辑 ---------- */}
          {!rawMode && (
            <div>
              <div
                className="row"
                style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}
              >
                <span style={{ fontSize: 12, fontWeight: 600 }}>
                  标注步骤 <span style={{ color: "var(--fk-ink-3)", fontWeight: 400 }}>（标注员从上往下做）</span>
                </span>
                <div className="row" style={{ gap: 6 }}>
                  {!!flow?.ls_project_id && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon="download"
                      onClick={importFromLs}
                      disabled={!!busy}
                      title="把这个项目在标注引擎里已经配好的表单读回来当起点"
                    >
                      从标注引擎导入
                    </Button>
                  )}
                  <Button size="sm" variant="secondary" icon="plus" onClick={() => addStep("choices")}>
                    加一步
                  </Button>
                </div>
              </div>

              {steps.length === 0 && (
                <div
                  style={{
                    padding: 20,
                    border: "1px dashed var(--fk-line-strong)",
                    borderRadius: "var(--fk-radius-md)",
                    fontSize: 12.5,
                    color: "var(--fk-ink-3)",
                    lineHeight: 1.9,
                  }}
                >
                  还没有步骤。最快的路子是上面选一个「起手模板」，然后在它的基础上改文案和选项。
                </div>
              )}

              <div style={{ display: "grid", gap: 10 }}>
                {steps.map((s, i) => (
                  <StepCard
                    key={s.id || i}
                    step={s}
                    index={i}
                    total={steps.length}
                    steps={steps}
                    kinds={kinds}
                    canWrite={writable}
                    onPatch={(p) => patchStep(i, p)}
                    onRemove={() => removeStep(i)}
                    onMove={(dir) => moveStep(i, dir)}
                  />
                ))}
              </div>

              <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
                {(materials?.kinds || []).map((k) => (
                  <button
                    key={k.kind}
                    type="button"
                    onClick={() => addStep(k.kind)}
                    disabled={!writable}
                    style={{
                      fontSize: 11.5,
                      padding: "4px 9px",
                      border: "1px solid var(--fk-line)",
                      borderRadius: "var(--fk-radius-sm)",
                      background: "var(--fk-surface)",
                      color: "var(--fk-ink-2)",
                      cursor: writable ? "pointer" : "not-allowed",
                    }}
                  >
                    + {KIND_LABEL[k.kind] || k.tag}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* ---------- 右：预览 ---------- */}
          <div>
            <div className="row" style={{ gap: 4, marginBottom: 8 }}>
              <PaneTab active={pane === "visual"} onClick={() => setPane("visual")}>
                标注员看到的样子
              </PaneTab>
              <PaneTab active={pane === "xml"} onClick={() => setPane("xml")}>
                编译出的 XML
              </PaneTab>
            </div>

            {rawMode || pane === "xml" ? (
              <textarea
                className="fk-mono"
                value={rawMode ? rawXml : preview?.label_config || ""}
                onChange={(e) => rawMode && setRawXml(e.target.value)}
                readOnly={!rawMode}
                spellCheck={false}
                style={{
                  width: "100%",
                  minHeight: 320,
                  maxHeight: "48vh",
                  padding: 10,
                  fontSize: 11.5,
                  lineHeight: 1.65,
                  border: "1px solid var(--fk-line)",
                  borderRadius: "var(--fk-radius-md)",
                  background: "var(--fk-surface-2)",
                  color: "var(--fk-ink)",
                  resize: "vertical",
                  whiteSpace: "pre",
                  overflow: "auto",
                }}
              />
            ) : (
              <div
                style={{
                  border: "1px solid var(--fk-line)",
                  borderRadius: "var(--fk-radius-md)",
                  padding: 12,
                  background: "var(--fk-surface-2)",
                  maxHeight: "48vh",
                  overflow: "auto",
                }}
              >
                <div style={{ fontSize: 11, color: "var(--fk-ink-3)", marginBottom: 9 }}>
                  标注页左侧表单 · {OBJECT_LABEL[draft.object] || draft.object}
                </div>
                <div style={{ background: "var(--fk-surface)", borderRadius: "var(--fk-radius-sm)", padding: 12 }}>
                  <StepTimeline steps={steps} emptyText="还没有步骤，右边会是空白" />
                </div>
                <div style={{ fontSize: 11, color: "var(--fk-ink-3)", marginTop: 9, lineHeight: 1.8 }}>
                  带「必填」的控件没做完，点提交会被标注引擎拦下；带条件的步骤只在条件成立时出现。
                </div>
              </div>
            )}
          </div>
        </div>
      </Modal>

      {/* ---------- 409 确认覆盖 ---------- */}
      {confirm && (
        <Modal
          open
          width={520}
          title="这个改动会影响已有标注"
          onClose={() => {
            setConfirm("");
            setForceAck(false);
          }}
          foot={
            <>
              <Button
                variant="ghost"
                onClick={() => {
                  setConfirm("");
                  setForceAck(false);
                }}
              >
                先不改
              </Button>
              <Button variant="danger" disabled={!forceAck || !!busy} onClick={() => doApply(true)}>
                {busy === "apply" ? "应用中…" : "确认覆盖"}
              </Button>
            </>
          }
        >
          <div style={{ fontSize: 12.5, lineHeight: 1.9 }}>{confirm}</div>
          <div style={{ marginTop: 12 }}>
            <Checkbox
              checked={forceAck}
              onChange={setForceAck}
              label="我确认影响已有标注，仍要覆盖表单结构"
            />
          </div>
        </Modal>
      )}
    </>
  );
}

/* ---------------------------- 单个步骤卡片 ------------------------------- */

function StepCard({
  step,
  index,
  total,
  steps,
  kinds,
  canWrite,
  onPatch,
  onRemove,
  onMove,
}: {
  step: DraftStep;
  index: number;
  total: number;
  steps: DraftStep[];
  kinds: FlowKindMeta[];
  canWrite: boolean;
  onPatch: (p: Partial<DraftStep>) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const meta = kinds.find((k) => k.kind === step.kind);
  const needOpts = meta ? meta.need_options : true;
  // 天然按区域打标的控件（矩形/多边形/关键点/文本片段）不需要 perRegion：
  // 它们本来就是"一个区域一份结果"，再加上去只会让甲方困惑。
  const naturalRegion = meta ? meta.region : false;

  // 条件分支的候选：前面那些"单选/多选"步骤，且本身不能再带条件（只允许一级）
  const depCandidates = steps.filter(
    (s, i) => i < index && s.kind === "choices" && !s.depends_on
  );
  const depTarget = step.depends_on ? steps.find((s) => s.name === step.depends_on) : null;

  return (
    <div
      style={{
        border: "1px solid var(--fk-line)",
        borderRadius: "var(--fk-radius-md)",
        background: "var(--fk-surface)",
        padding: 11,
      }}
    >
      <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8 }}>
        <span
          className="fk-num"
          style={{
            flex: "none",
            width: 20,
            height: 20,
            borderRadius: "var(--fk-radius-pill)",
            background: "var(--fk-accent-200, #D9CFF4)",
            color: "var(--fk-accent-900, #36276B)",
            fontSize: 11,
            fontWeight: 700,
            display: "grid",
            placeItems: "center",
          }}
        >
          {index + 1}
        </span>
        <span style={{ color: "var(--fk-accent-500)", fontSize: 13 }}>{KIND_GLYPH[step.kind] || "•"}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <TextInput
            value={step.title}
            onChange={(v) => onPatch({ title: v })}
            placeholder="这一步让标注员做什么"
            maxLength={60}
          />
        </div>
        <div className="row" style={{ gap: 2, flex: "none" }}>
          <Button size="sm" variant="ghost" onClick={() => onMove(-1)} disabled={index === 0} title="上移">
            ↑
          </Button>
          <Button size="sm" variant="ghost" onClick={() => onMove(1)} disabled={index === total - 1} title="下移">
            ↓
          </Button>
          <Button size="sm" variant="ghost" icon="x" onClick={onRemove} title="删掉这一步" />
        </div>
      </div>

      <div className="row" style={{ gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
        <div style={{ width: 158 }}>
          <Field label="控件">
            <Select
              value={step.kind}
              onChange={(v) => {
                const m = kinds.find((k) => k.kind === v);
                const next: Partial<DraftStep> = { kind: v };
                // 从"有选项"切到"没选项"时把选项清空，否则 XML 里会留一堆
                // 用不上的 <Choice>，甲方看着莫名其妙。
                if (m && !m.need_options) {
                  next.options = [];
                  next._raw = "";
                } else if (!(step.options || []).length) {
                  next.options = ["选项一", "选项二"];
                  next._raw = "选项一\n选项二";
                }
                if (m && m.region) next.per_region = false;
                onPatch(next);
              }}
              options={kinds.map((k) => ({ value: k.kind, label: KIND_LABEL[k.kind] || k.tag }))}
            />
          </Field>
        </div>
        <div style={{ width: 150 }}>
          <Field label="字段名" hint="结果里的 key，别乱改">
            <input
              className="fk-input fk-mono"
              style={{ fontSize: 11.5 }}
              value={step.name}
              maxLength={40}
              onChange={(e) => onPatch({ name: e.target.value.replace(/[^A-Za-z0-9_]/g, "") })}
            />
          </Field>
        </div>
        <div style={{ paddingBottom: 6 }}>
          <Checkbox checked={!!step.required} onChange={(v) => onPatch({ required: v })} label="必填" />
        </div>
        {!naturalRegion && (
          <div style={{ paddingBottom: 6 }}>
            <Checkbox
              checked={!!step.per_region}
              onChange={(v) => onPatch({ per_region: v })}
              label="每区域一次"
            />
          </div>
        )}
      </div>

      {needOpts && (
        <div style={{ marginTop: 8 }}>
          <Field label="可选值（一行一个）" hint="标注员只能从这里挑，不能自己写">
            <textarea
              className="fk-input"
              style={{ minHeight: 54, padding: 8, fontSize: 12, lineHeight: 1.7, resize: "vertical" }}
              value={step._raw ?? ""}
              onChange={(e) => {
                const raw = e.target.value;
                // 同时更新 _raw 与 options：_raw 留给输入框（保住末尾空行），
                // options 给预览和后端（已经是干净数组）。
                onPatch({ _raw: raw, options: splitOptions(raw) });
              }}
              placeholder={"划痕\n破损\n污渍"}
            />
          </Field>
        </div>
      )}

      {index > 0 && (
        <div className="row" style={{ gap: 8, alignItems: "flex-end", marginTop: 6, flexWrap: "wrap" }}>
          <div style={{ width: 200 }}>
            <Field label="条件分支" hint="只在某一步选了某个值时才出现">
              <Select
                value={step.depends_on || ""}
                onChange={(v) => {
                  const t = steps.find((s) => s.name === v);
                  onPatch({ depends_on: v, depends_value: v ? (t?.options || [])[0] || "" : "" });
                }}
                options={[
                  { value: "", label: "总是显示" },
                  ...depCandidates.map((s, i) => ({
                    value: s.name,
                    label: `${i + 1}. ${s.title || s.name}`,
                  })),
                ]}
              />
            </Field>
          </div>
          {depTarget && (
            <div style={{ width: 160 }}>
              <Field label="当它选了">
                <Select
                  value={step.depends_value || ""}
                  onChange={(v) => onPatch({ depends_value: v })}
                  options={(depTarget.options || []).map((o) => ({ value: o, label: o }))}
                />
              </Field>
            </div>
          )}
          {depCandidates.length === 0 && (
            <div style={{ fontSize: 11, color: "var(--fk-ink-3)", paddingBottom: 8 }}>
              上面还没有「单选/多选」步骤，做不了条件分支
            </div>
          )}
        </div>
      )}

      {!canWrite && (
        <div style={{ fontSize: 11, color: "var(--fk-ink-3)", marginTop: 6 }}>
          只读：你的角色没有标注标准的编辑权限
        </div>
      )}
    </div>
  );
}

function PaneTab({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        fontSize: 11.5,
        padding: "4px 10px",
        borderRadius: "var(--fk-radius-sm)",
        border: "1px solid " + (active ? "var(--fk-line-strong)" : "transparent"),
        background: active ? "var(--fk-surface)" : "transparent",
        color: active ? "var(--fk-ink)" : "var(--fk-ink-3)",
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}
