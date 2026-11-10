import { useMemo, useState } from "react";
import { Donut } from "../design/charts";
import { Icon } from "../design/icons";
import {
  Badge,
  Button,
  DataTable,
  Field,
  Modal,
  PageHead,
  Panel,
  Select,
  Stat,
  Tabs,
  TextInput,
  toast,
} from "../design/ui";
import { assetStatusLabel, assetStatusTone, bytes, compactNum, dateTime, modalityLabel, num } from "../lib/format";
import { useScoped } from "../lib/hooks";
import type { DataAsset } from "../lib/types";

export default function DataPage() {
  const s = useScoped();
  const [tab, setTab] = useState("all");
  const [q, setQ] = useState("");
  const [projectId, setProjectId] = useState("all");
  const [modality, setModality] = useState("all");
  const [importing, setImporting] = useState(false);

  const rows = useMemo(() => {
    let list = s.assets.filter((a) => s.projects.some((p) => p.id === a.projectId));
    if (tab !== "all") list = list.filter((a) => a.status === tab);
    if (projectId !== "all") list = list.filter((a) => a.projectId === projectId);
    if (modality !== "all") list = list.filter((a) => a.modality === modality);
    if (q) list = list.filter((a) => a.name.toLowerCase().includes(q.toLowerCase()));
    return list;
  }, [s.assets, s.projects, tab, projectId, modality, q]);

  const totalItems = rows.reduce((a, b) => a + b.itemCount, 0);
  const totalSize = rows.reduce((a, b) => a + b.sizeMb, 0);
  const qcRows = rows.filter((a) => a.qcScore !== null);
  const avgQc = qcRows.length ? qcRows.reduce((a, b) => a + (b.qcScore ?? 0), 0) / qcRows.length : 0;

  const mix = useMemo(() => {
    const m = new Map<string, number>();
    rows.forEach((a) => m.set(a.modality, (m.get(a.modality) ?? 0) + a.itemCount));
    return [...m.entries()].map(([k, v]) => ({ key: k, value: v, label: modalityLabel[k] ?? k }));
  }, [rows]);

  return (
    <>
      <PageHead
        title="数据管理"
        desc="原始数据按批次入湖，标注、质检、验收状态与存储位置全程可查。每个批次绑定唯一项目归属，跨项目不可复用。"
        badge={<Badge tone="outline">{s.assets.length} 个批次</Badge>}
        actions={
          <>
            <Button icon="sync" variant="secondary" onClick={() => { void s.reload(); toast("已重新拉取服务端数据"); }}>
              重新加载
            </Button>
            <Button icon="upload" variant="primary" onClick={() => setImporting(true)}>
              导入数据
            </Button>
          </>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        <Stat label="批次条目" value={compactNum(totalItems)} hint={`${rows.length} 个批次`} />
        <Stat label="登记数据量" value={bytes(totalSize)} hint="按 forge.assets.size_mb 汇总" />
        <Stat
          label="平均质检分"
          value={qcRows.length ? avgQc.toFixed(1) : "—"}
          hint={`${qcRows.length} 批已质检`}
        />
        <Stat
          label="待质检批次"
          value={rows.filter((a) => a.status === "in_review").length}
          hint={`${rows.filter((a) => a.status === "in_annotation").length} 批标注中`}
        />
      </div>

      <div className="g-main" style={{ marginBottom: 14 }}>
        <Panel flush>
          <div style={{ padding: "10px 16px 0" }}>
            <Tabs
              value={tab}
              onChange={setTab}
              items={[
                { key: "all", label: "全部", count: s.assets.length },
                { key: "raw", label: "原始", count: s.assets.filter((a) => a.status === "raw").length },
                { key: "in_annotation", label: "标注中", count: s.assets.filter((a) => a.status === "in_annotation").length },
                { key: "in_review", label: "质检中", count: s.assets.filter((a) => a.status === "in_review").length },
                { key: "accepted", label: "已验收", count: s.assets.filter((a) => a.status === "accepted").length },
                { key: "archived", label: "已归档", count: s.assets.filter((a) => a.status === "archived").length },
              ]}
            />
          </div>

          <div className="fk-toolbar" style={{ padding: "12px 16px" }}>
            <div className="fk-search" style={{ width: 240 }}>
              <Icon name="search" size={14} />
              <input className="fk-input" value={q} placeholder="搜索文件名" onChange={(e) => setQ(e.target.value)} />
            </div>
            <Select
              value={projectId}
              onChange={setProjectId}
              options={[{ value: "all", label: "全部项目" }, ...s.projects.map((p) => ({ value: p.id, label: p.code }))]}
            />
            <Select
              value={modality}
              onChange={setModality}
              options={[
                { value: "all", label: "全部模态" },
                { value: "image", label: "图像" },
                { value: "video", label: "视频" },
                { value: "audio", label: "音频" },
                { value: "text", label: "文本" },
                { value: "multimodal", label: "多模态" },
              ]}
            />
            <div className="fk-spacer" />
            <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>{rows.length} 个批次</span>
          </div>

          <DataTable<DataAsset>
            rows={rows.slice(0, 30)}
            columns={[
              {
                key: "name",
                header: "批次 / 文件",
                render: (a) => (
                  <div className="row" style={{ gap: 8 }}>
                    <span style={{ color: "var(--fk-ink-4)", display: "grid", placeContent: "center" }}>
                      <Icon
                        name={a.modality === "video" ? "video" : a.modality === "audio" ? "audio" : a.modality === "text" ? "file-text" : "image"}
                        size={14}
                      />
                    </span>
                    <div>
                      <div className="fk-mono" style={{ color: "var(--fk-ink)", fontSize: 11.5 }}>{a.name}</div>
                      <div style={{ fontSize: 10.5, color: "var(--fk-ink-3)" }}>
                        {s.projects.find((p) => p.id === a.projectId)?.code} · {a.batchNo}
                      </div>
                    </div>
                  </div>
                ),
                sortValue: (a) => a.name,
              },
              { key: "m", header: "模态", width: 70, render: (a) => <Badge tone="neutral">{modalityLabel[a.modality]}</Badge> },
              { key: "c", header: "条目", width: 82, align: "right", render: (a) => <span className="fk-num">{num(a.itemCount)}</span>, sortValue: (a) => a.itemCount },
              { key: "s", header: "大小", width: 80, align: "right", render: (a) => <span className="fk-num">{bytes(a.sizeMb)}</span>, sortValue: (a) => a.sizeMb },
              {
                key: "q",
                header: "质检分",
                width: 74,
                align: "right",
                render: (a) =>
                  a.qcScore === null ? (
                    <span style={{ color: "var(--fk-ink-4)" }}>—</span>
                  ) : (
                    <span className="fk-num" style={{ color: a.qcScore < 88 ? "var(--fk-warn-700)" : "var(--fk-ink)", fontWeight: a.qcScore < 88 ? 600 : 400 }}>
                      {a.qcScore.toFixed(1)}
                    </span>
                  ),
                sortValue: (a) => a.qcScore ?? 0,
              },
              { key: "st", header: "存储", width: 62, render: (a) => <span className="fk-mono" style={{ fontSize: 10.5, color: "var(--fk-ink-3)" }}>{a.storage.toUpperCase()}</span> },
              { key: "t", header: "导入时间", width: 122, align: "right", render: (a) => <span className="fk-num" style={{ fontSize: 11.5 }}>{dateTime(a.importedAt)}</span>, sortValue: (a) => new Date(a.importedAt).getTime() },
              { key: "state", header: "状态", width: 78, render: (a) => <Badge tone={assetStatusTone[a.status]} dot>{assetStatusLabel[a.status]}</Badge> },
            ]}
            emptyText="没有匹配的数据批次"
            emptyHint="调整筛选条件，或导入新批次"
          />
        </Panel>

        <div className="stack">
          <Panel title="模态分布" sub="按条目">
            <Donut data={mix} centerValue={compactNum(totalItems)} centerLabel="条目" size={132} />
          </Panel>
          <Panel title="导入校验规则">
            <div className="stack" style={{ gap: 11, fontSize: 12, color: "var(--fk-ink-2)" }}>
              {[
                ["格式校验", "按模态白名单校验扩展名与 MIME，非法文件整批拒绝"],
                ["去重校验", "基于内容 SHA-256 与项目内已有批次比对，重复条目剔除"],
                ["归属校验", "批次必须绑定唯一项目，跨项目复用需管理员二次确认"],
                ["脱敏校验", "文本类数据执行 PII 规则扫描，命中即阻断入湖"],
              ].map(([k, v], i) => (
                <div key={i} style={{ display: "flex", gap: 9 }}>
                  <span style={{ color: "var(--fk-success-600)", marginTop: 2 }}>
                    <Icon name="check" size={12} strokeWidth={2.4} />
                  </span>
                  <div>
                    <div style={{ fontWeight: 500 }}>{k}</div>
                    <div style={{ color: "var(--fk-ink-3)", lineHeight: 1.6, marginTop: 1 }}>{v}</div>
                  </div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>

      <ImportModal open={importing} onClose={() => setImporting(false)} />
    </>
  );
}

function ImportModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const s = useScoped();
  const [projectId, setProjectId] = useState(s.projects[0]?.id ?? "");
  const [name, setName] = useState("");
  const [items, setItems] = useState("5000");
  const [size, setSize] = useState("1200");
  const [modality, setModality] = useState("image");
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!projectId) return setErr("请选择归属项目");
    if (!name.trim()) return setErr("请填写批次文件名");
    if (Number(items) <= 0) return setErr("条目数必须大于 0");
    const r = await s.importAsset({
      projectId,
      name: name.trim(),
      modality: modality as DataAsset["modality"],
      itemCount: Number(items),
      sizeMb: Number(size),
    });
    if (!r.ok) {
      setErr(r.reason ?? "导入失败");
      return;
    }
    toast(`已导入 ${name}，状态为原始，待质检`);
    setName("");
    setErr("");
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={560}
      title="导入数据批次"
      foot={
        <>
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={submit}>开始导入</Button>
        </>
      }
    >
      <div className="stack">
        {err && <div className="fk-error">{err}</div>}
        <Field label="归属项目" required hint="批次入库后不可变更归属">
          <Select value={projectId} onChange={setProjectId} options={s.projects.map((p) => ({ value: p.id, label: `${p.code} · ${p.name}` }))} />
        </Field>
        <Field label="文件名 / 批次标识" required>
          <TextInput value={name} onChange={setName} placeholder="例如 AD-2606_batch_12.zip" />
        </Field>
        <div className="g-3" style={{ gap: 12 }}>
          <Field label="模态">
            <Select
              value={modality}
              onChange={setModality}
              options={[
                { value: "image", label: "图像" },
                { value: "video", label: "视频" },
                { value: "audio", label: "音频" },
                { value: "text", label: "文本" },
                { value: "multimodal", label: "多模态" },
              ]}
            />
          </Field>
          <Field label="条目数">
            <TextInput value={items} onChange={setItems} type="number" />
          </Field>
          <Field label="大小（MB）">
            <TextInput value={size} onChange={setSize} type="number" />
          </Field>
        </div>
        <div
          style={{
            border: "1px dashed var(--fk-line-strong)",
            borderRadius: 4,
            padding: "16px 14px",
            textAlign: "center",
            fontSize: 12,
            color: "var(--fk-ink-3)",
            background: "var(--fk-surface-2)",
          }}
        >
          将文件拖拽至此，或从 S3 / OSS 引入（生产环境启用）
        </div>
      </div>
    </Modal>
  );
}
