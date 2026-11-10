import { useMemo, useState } from "react";
import { RankBars } from "../design/charts";
import { Icon } from "../design/icons";
import {
  Badge,
  Button,
  Checkbox,
  DataTable,
  DList,
  Drawer,
  InlineMeter,
  PageHead,
  Panel,
  Select,
  Stat,
  Tabs,
  Timeline,
  toast,
  useSession,
} from "../design/ui";
import { cny, date, modalityLabel, num, ratioPct, settlementStatusLabel, supplierStatusLabel, supplierStatusTone } from "../lib/format";
import { useScoped } from "../lib/hooks";
import { can } from "../lib/rbac";
import type { Supplier } from "../lib/types";

const STATUS_TABS = [
  { key: "all", label: "全部" },
  { key: "active", label: "合作中" },
  { key: "probation", label: "观察期" },
  { key: "qualifying", label: "资质审核" },
  { key: "suspended", label: "已暂停" },
];

export default function Suppliers() {
  const s = useScoped();
  const { me } = useSession();
  const [q, setQ] = useState("");
  const [tab, setTab] = useState("all");
  const [modality, setModality] = useState("all");
  const [active, setActive] = useState<Supplier | null>(null);

  const writable = can(me, "supplier:write");

  const rows = useMemo(() => {
    let list = s.suppliers;
    if (q) {
      const k = q.toLowerCase();
      list = list.filter((x) => x.name.toLowerCase().includes(k) || x.code.toLowerCase().includes(k) || x.city.includes(q));
    }
    if (tab !== "all") list = list.filter((x) => x.status === tab);
    if (modality !== "all") list = list.filter((x) => x.modalities.includes(modality as never));
    return list;
  }, [s.suppliers, q, tab, modality]);

  const avgQuality = rows.length ? rows.reduce((a, b) => a + b.qualityScore, 0) / rows.length : 0;
  const avgOnTime = rows.length ? rows.reduce((a, b) => a + b.onTimeRate, 0) / rows.length : 0;
  const totalAnnotators = rows.reduce((a, b) => a + b.annotatorCount, 0);
  const totalCapacity = rows.reduce((a, b) => a + b.capacityHours, 0);
  const committed = rows.reduce((a, b) => a + b.committedHours, 0);

  return (
    <>
      <PageHead
        title="供应商管理"
        desc="供应商是产能与质量的供给方。资质、等级、产能、历史交付与质量评分在同一档案内沉淀，直接驱动任务分配与结算定价。"
        badge={<Badge tone="outline">{s.suppliers.length} 家在册</Badge>}
        actions={
          <>
            <Button icon="download" variant="secondary">导出档案</Button>
            {writable && (
              <Button icon="plus" variant="primary" onClick={() => toast("供应商邀请链接能力尚未接入；请先通过注册页创建账号，再由管理员授权")}>
                邀请供应商
              </Button>
            )}
          </>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        <Stat label="在册供应商" value={s.suppliers.length} hint={`${s.suppliers.filter((x) => x.status === "active").length} 家合作中`} />
        <Stat label="标注员总数" value={num(totalAnnotators)} hint="含供应商侧全部成员" />
        <Stat
          label="平均质量分"
          value={s.suppliers.length ? avgQuality.toFixed(1) : "—"}
          hint={s.suppliers.length ? `准时率 ${avgOnTime.toFixed(1)}%` : "暂无供应商记录"}
        />
        <Stat
          label="产能占用"
          value={((committed / Math.max(1, totalCapacity)) * 100).toFixed(1)}
          unit="%"
          hint={`${num(committed)} / ${num(totalCapacity)} 工时`}
        />
      </div>

      <Panel flush>
        <div style={{ padding: "10px 16px 0" }}>
          <Tabs value={tab} onChange={setTab} items={STATUS_TABS.map((t) => ({ ...t, count: t.key === "all" ? s.suppliers.length : s.suppliers.filter((x) => x.status === t.key).length }))} />
        </div>
        <div className="fk-toolbar" style={{ padding: "12px 16px" }}>
          <div className="fk-search" style={{ width: 280 }}>
            <Icon name="search" size={14} />
            <input className="fk-input" value={q} placeholder="搜索供应商名称 / 编号 / 城市" onChange={(e) => setQ(e.target.value)} />
          </div>
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
          <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>共 {rows.length} 家</span>
        </div>

        <DataTable<Supplier>
          rows={rows}
          onRowClick={setActive}
          columns={[
            { key: "code", header: "编号", width: 84, render: (x) => <span className="fk-mono">{x.code}</span>, sortValue: (x) => x.code },
            {
              key: "name",
              header: "供应商",
              render: (x) => (
                <div>
                  <div className="fk-cell-strong">{x.name}</div>
                  <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>
                    {x.city} · {x.contactName} · 签约 {x.contractCount} 次
                  </div>
                </div>
              ),
              sortValue: (x) => x.name,
            },
            {
              key: "tier",
              header: "等级",
              width: 62,
              render: (x) => <Badge tone={x.tier === "S" ? "accent" : x.tier === "C" ? "warn" : "neutral"}>{x.tier} 级</Badge>,
            },
            { key: "mod", header: "模态", width: 120, render: (x) => <span style={{ fontSize: 11.5, color: "var(--fk-ink-2)" }}>{x.modalities.map((m) => modalityLabel[m]).join(" / ")}</span> },
            {
              key: "quality",
              header: "质量分",
              width: 122,
              render: (x) => <InlineMeter value={x.qualityScore} label={x.qualityScore.toFixed(1)} />,
              sortValue: (x) => x.qualityScore,
            },
            {
              key: "cap",
              header: "产能占用",
              width: 128,
              render: (x) => <InlineMeter value={ratioPct(x.committedHours, x.capacityHours)} tone="accent" />,
              sortValue: (x) => ratioPct(x.committedHours, x.capacityHours),
            },
            {
              key: "people",
              header: "标注员",
              width: 72,
              align: "right",
              render: (x) => <span className="fk-num">{x.annotatorCount}</span>,
              sortValue: (x) => x.annotatorCount,
            },
            {
              key: "status",
              header: "状态",
              width: 84,
              render: (x) => <Badge tone={supplierStatusTone[x.status]} dot>{supplierStatusLabel[x.status]}</Badge>,
            },
            {
              key: "act",
              header: "",
              width: 40,
              align: "right",
              render: () => <Icon name="chevron-right" size={14} />,
            },
          ]}
          emptyText="没有匹配的供应商"
        />
      </Panel>

      <SupplierDrawer supplier={active} onClose={() => setActive(null)} />
    </>
  );
}

function SupplierDrawer({ supplier, onClose }: { supplier: Supplier | null; onClose: () => void }) {
  const s = useScoped();
  const store = useScoped();
  const [tab, setTab] = useState("profile");
  if (!supplier) return null;
  const x = supplier;

  const members = s.members.filter((m) => m.supplierId === x.id);
  const tasks = s.tasks.filter((t) => t.supplierId === x.id);
  const settles = s.settlements.filter((t) => t.supplierId === x.id);
  const perf = s.performance.find((p) => p.supplierId === x.id);
  const projects = s.projects.filter((p) => p.supplierIds.includes(x.id));

  const q = x.qualification;

  return (
    <Drawer
      open
      onClose={onClose}
      width={720}
      title={
        <span className="row" style={{ gap: 8 }}>
          {x.name}
          <Badge tone={x.tier === "S" ? "accent" : "neutral"}>{x.tier} 级</Badge>
          <Badge tone={supplierStatusTone[x.status]} dot>{supplierStatusLabel[x.status]}</Badge>
        </span>
      }
      sub={<span className="fk-mono">{x.code} · {x.city} · 入驻 {date(x.onboardedAt)}</span>}
      foot={
        <>
          <Button variant="ghost" onClick={onClose}>关闭</Button>
          <Button variant="secondary" icon="file-text">查看合同</Button>
          {x.status === "qualifying" ? (
            <Button
              variant="primary"
              icon="check"
              onClick={async () => {
                const r = await store.setSupplierStatus(x.id, "active");
                if (!r.ok) {
                  toast(`审核失败：${r.reason}`);
                  return;
                }
                toast(`${x.name} 资质审核已通过`);
                onClose();
              }}
            >
              通过资质审核
            </Button>
          ) : (
            <Button variant="secondary" icon="edit" disabled title="档案编辑接口尚未接入">
              编辑档案
            </Button>
          )}
        </>
      }
    >
      <div style={{ marginBottom: 14 }}>
        <Tabs
          value={tab}
          onChange={setTab}
          items={[
            { key: "profile", label: "档案" },
            { key: "qual", label: "资质" },
            { key: "team", label: "团队", count: members.length },
            { key: "tasks", label: "交付", count: tasks.length },
            { key: "settle", label: "结算", count: settles.length },
          ]}
        />
      </div>

      {tab === "profile" && (
        <div className="stack-lg">
          <div className="g-3">
            <div className="tile">
              <div className="mini-label">质量分</div>
              <div className="fk-num" style={{ fontSize: 20, fontWeight: 600, marginTop: 4 }}>{x.qualityScore}</div>
              <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>行业均值 84.6</div>
            </div>
            <div className="tile">
              <div className="mini-label">准时交付</div>
              <div className="fk-num" style={{ fontSize: 20, fontWeight: 600, marginTop: 4 }}>{x.onTimeRate}%</div>
              <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>返工率 {x.reworkRate}%</div>
            </div>
            <div className="tile">
              <div className="mini-label">综合排名</div>
              <div className="fk-num" style={{ fontSize: 20, fontWeight: 600, marginTop: 4 }}>#{perf?.rank ?? "—"}</div>
              <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>共 {s.suppliers.length} 家</div>
            </div>
          </div>

          <Panel title="基础信息">
            <DList
              items={[
                ["联系人", `${x.contactName} · ${x.contactPhone}`],
                ["主营模态", x.modalities.map((m) => modalityLabel[m]).join(" / ")],
                ["标注员", `${x.annotatorCount} 人`],
                ["月度产能", `${num(x.capacityHours)} 工时（已承诺 ${num(x.committedHours)}）`],
                ["在研项目", projects.map((p) => p.code).join("、") || "—"],
                ["累计结算", cny(settles.reduce((a, b) => a + b.payable, 0), { compact: true })],
              ]}
            />
          </Panel>

          <Panel title="产能构成">
            <RankBars
              rows={[
                { label: "已承诺工时", value: x.committedHours, note: `${ratioPct(x.committedHours, x.capacityHours).toFixed(0)}%` },
                { label: "剩余可分配", value: x.capacityHours - x.committedHours, note: `${(100 - ratioPct(x.committedHours, x.capacityHours)).toFixed(0)}%`, tone: "var(--fk-accent-200)" },
              ]}
              formatter={(v) => `${num(v)} h`}
            />
          </Panel>
        </div>
      )}

      {tab === "qual" && (
        <div className="stack">
          {[
            ["营业执照", q.businessLicense, "注册满 2 年，经营范围含数据处理服务"],
            ["保密协议（NDA）", q.ndaSigned, "双方签署，覆盖数据不可留存条款"],
            ["数据安全审计", q.dataSecurityAudit, q.lastAuditedAt ? `上次审计 ${date(q.lastAuditedAt)}` : "尚未完成第三方审计"],
          ].map(([label, ok, note], i) => (
            <div
              key={i}
              className="tile"
              style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 14px" }}
            >
              <span
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: 4,
                  display: "grid",
                  placeContent: "center",
                  background: ok ? "var(--fk-success-50)" : "var(--fk-warn-50)",
                  color: ok ? "var(--fk-success-700)" : "var(--fk-warn-700)",
                  border: `1px solid ${ok ? "var(--fk-success-100)" : "var(--fk-warn-100)"}`,
                  flex: "none",
                }}
              >
                <Icon name={ok ? "check" : "alert"} size={14} strokeWidth={2} />
              </span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>{label as string}</div>
                <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", marginTop: 2 }}>{note as string}</div>
              </div>
              <Badge tone={ok ? "success" : "warn"}>{ok ? "已通过" : "待补"}</Badge>
            </div>
          ))}
          <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.7, marginTop: 4 }}>
            资质未全项通过时，供应商无法承接新项目分包；已进行中的项目不受影响，但会被禁止扩充新批次。
          </div>
        </div>
      )}

      {tab === "team" && (
        <DataTable
          rows={members}
          columns={[
            { key: "n", header: "成员", render: (m) => <div className="row" style={{ gap: 8 }}><span className="fk-avatar">{m.initials}</span><span className="fk-cell-strong">{m.name}</span></div> },
            { key: "r", header: "角色", width: 96, render: (m) => <Badge tone="neutral">{m.role === "supplier_admin" ? "管理员" : m.role === "supplier_lead" ? "组长" : "标注员"}</Badge> },
            { key: "l", header: "等级", width: 66, render: (m) => <span style={{ fontSize: 12 }}>{m.level === "senior" ? "资深" : m.level === "mid" ? "中级" : "初级"}</span> },
            { key: "p", header: "通过率", width: 88, align: "right", render: (m) => <span className="fk-num">{m.passRate}%</span> },
            { key: "t", header: "在手任务", width: 80, align: "right", render: (m) => <span className="fk-num">{m.activeTasks}</span> },
            { key: "c", header: "累计完成", width: 96, align: "right", render: (m) => <span className="fk-num">{num(m.completedTotal)}</span> },
          ]}
          emptyText="该供应商暂无成员"
        />
      )}

      {tab === "tasks" && (
        <DataTable
          rows={tasks}
          columns={[
            { key: "n", header: "批次", render: (t) => <div><div className="fk-cell-strong" style={{ fontSize: 12.5 }}>{t.name}</div><div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{t.assigneeName ?? "未分配"}</div></div> },
            { key: "c", header: "条目", width: 86, align: "right", render: (t) => <span className="fk-num">{num(t.itemCount)}</span> },
            { key: "d", header: "进度", width: 120, render: (t) => <InlineMeter value={(t.doneCount / t.itemCount) * 100} tone="accent" /> },
            { key: "r", header: "返工", width: 62, align: "right", render: (t) => <span className="fk-num" style={{ color: t.reworkRound > 0 ? "var(--fk-danger-600)" : "var(--fk-ink-3)" }}>{t.reworkRound}</span> },
            { key: "due", header: "交付日", width: 92, align: "right", render: (t) => <span className="fk-num">{date(t.dueAt)}</span> },
          ]}
          emptyText="暂无交付记录"
        />
      )}

      {tab === "settle" && (
        <DataTable
          rows={settles}
          columns={[
            { key: "c", header: "结算单", render: (x2) => <span className="fk-mono">{x2.code}</span> },
            { key: "p", header: "期间", width: 86, render: (x2) => <span className="fk-num">{x2.period}</span> },
            { key: "a", header: "应付", width: 110, align: "right", render: (x2) => <span className="fk-num">{cny(x2.payable)}</span> },
            { key: "s", header: "状态", width: 84, render: (x2) => <Badge tone={x2.status === "paid" ? "success" : x2.status === "draft" ? "neutral" : "info"}>{settlementStatusLabel[x2.status]}</Badge> },
          ]}
          emptyText="暂无结算单"
        />
      )}
    </Drawer>
  );
}
