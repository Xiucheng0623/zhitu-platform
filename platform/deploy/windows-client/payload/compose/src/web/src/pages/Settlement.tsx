import { useMemo, useState } from "react";
import { Icon } from "../design/icons";
import {
  Badge,
  Button,
  DataTable,
  DList,
  Drawer,
  PageHead,
  Panel,
  Progress,
  Stat,
  Tabs,
  Timeline,
  toast,
} from "../design/ui";
import { cny, date, num, settlementStatusLabel, settlementStatusTone } from "../lib/format";
import { settlementTrend } from "../lib/analytics";
import { useScoped } from "../lib/hooks";
import type { Settlement } from "../lib/types";

const FLOW: Array<{ key: Settlement["status"]; label: string; next: string | null }> = [
  { key: "draft", label: "草稿", next: "提交结算单" },
  { key: "submitted", label: "待确认", next: "确认金额" },
  { key: "confirmed", label: "已确认", next: "登记发票" },
  { key: "invoiced", label: "已开票", next: "登记付款" },
  { key: "paid", label: "已付款", next: null },
];

export default function SettlementPage() {
  const s = useScoped();
  const [tab, setTab] = useState("all");
  const [active, setActive] = useState<Settlement | null>(null);

  const rows = useMemo(
    () => (tab === "all" ? s.settlements : s.settlements.filter((x) => x.status === tab)),
    [s.settlements, tab]
  );

  const sum = (f: (x: Settlement) => number) => rows.reduce((a, b) => a + f(b), 0);
  const trend = useMemo(() => settlementTrend(s.settlements), [s.settlements]);

  return (
    <>
      <PageHead
        title="结算中心"
        desc="验收条目 → 结算单 → 发票核验 → 付款核销，全部由本平台自有财务表承载。三单匹配（对账单 / 发票 / 付款）在 /api/v1/finance/match 实时核对。"
        badge={<Badge tone="outline">{s.settlements.length} 张结算单</Badge>}
        actions={
          <>
            <Button icon="download" variant="secondary">导出对账单</Button>
            <Button
              icon="plus"
              variant="primary"
              onClick={() => toast("结算草案生成接口尚未接入；当前结算单来自服务端验收记录")}
            >
              生成结算草案
            </Button>
          </>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        <Stat label="本期应付" value={cny(sum((x) => x.payable), { compact: true })} hint={`${rows.length} 张结算单`} accent />
        <Stat label="已结清" value={cny(rows.filter((x) => x.status === "paid").reduce((a, b) => a + b.payable, 0), { compact: true })} hint="含税" />
        <Stat
          label="未结清"
          value={cny(rows.filter((x) => x.status !== "paid").reduce((a, b) => a + b.payable, 0), { compact: true })}
          hint="跨 3 个结算周期"
        />
        <Stat
          label="质量扣款"
          value={cny(sum((x) => x.deduction), { compact: true })}
          hint={`占应付 ${((sum((x) => x.deduction) / Math.max(1, sum((x) => x.amount))) * 100).toFixed(1)}%`}
        />
      </div>

      <div className="g-main" style={{ marginBottom: 14 }}>
        <Panel flush>
          <div style={{ padding: "10px 16px 0" }}>
            <Tabs
              value={tab}
              onChange={setTab}
              items={[
                { key: "all", label: "全部", count: s.settlements.length },
                { key: "draft", label: "草稿", count: s.settlements.filter((x) => x.status === "draft").length },
                { key: "submitted", label: "待确认", count: s.settlements.filter((x) => x.status === "submitted").length },
                { key: "confirmed", label: "已确认", count: s.settlements.filter((x) => x.status === "confirmed").length },
                { key: "invoiced", label: "已开票", count: s.settlements.filter((x) => x.status === "invoiced").length },
                { key: "paid", label: "已付款", count: s.settlements.filter((x) => x.status === "paid").length },
              ]}
            />
          </div>

          <DataTable<Settlement>
            rows={rows}
            onRowClick={setActive}
            columns={[
              { key: "c", header: "结算单", width: 118, render: (x) => <span className="fk-mono" style={{ fontSize: 11.5, color: "var(--fk-ink)", whiteSpace: "nowrap" }}>{x.code}</span>, sortValue: (x) => x.code },
              {
                key: "sup",
                header: "供应商",
                render: (x) => (
                  <div style={{ whiteSpace: "nowrap" }}>
                    <div className="fk-cell-strong">{x.supplierName}</div>
                    <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>期间 {x.period}</div>
                  </div>
                ),
                sortValue: (x) => x.supplierName,
              },
              { key: "items", header: "验收条目", width: 92, align: "right", render: (x) => <span className="fk-num">{num(x.acceptedItems)}</span>, sortValue: (x) => x.acceptedItems },
              { key: "avg", header: "均价", width: 70, align: "right", render: (x) => <span className="fk-num">{x.unitPriceAvg.toFixed(2)}</span> },
              { key: "amt", header: "金额", width: 100, align: "right", render: (x) => <span className="fk-num">{cny(x.amount, { compact: true })}</span>, sortValue: (x) => x.amount },
              {
                key: "ded",
                header: "扣款",
                width: 84,
                align: "right",
                render: (x) => <span className="fk-num" style={{ color: "var(--fk-danger-600)" }}>-{cny(x.deduction, { compact: true })}</span>,
                sortValue: (x) => x.deduction,
              },
              {
                key: "pay",
                header: "应付",
                width: 104,
                align: "right",
                render: (x) => <span className="fk-num" style={{ fontWeight: 600, color: "var(--fk-ink)" }}>{cny(x.payable, { compact: true })}</span>,
                sortValue: (x) => x.payable,
              },
              { key: "due", header: "账期", width: 96, align: "right", render: (x) => <span className="fk-num" style={{ fontSize: 11.5 }}>{date(x.dueAt)}</span> },
              { key: "st", header: "状态", width: 84, render: (x) => <Badge tone={settlementStatusTone[x.status]} dot>{settlementStatusLabel[x.status]}</Badge> },
              { key: "a", header: "", width: 34, align: "right", render: () => <Icon name="chevron-right" size={14} /> },
            ]}
            emptyText="该状态下没有结算单"
          />
        </Panel>

        <div className="stack">
          <Panel title="结算进度" sub="按状态">
            <div className="stack" style={{ gap: 12 }}>
              {FLOW.map((f) => {
                const list = rows.filter((x) => x.status === f.key);
                const amount = list.reduce((a, b) => a + b.payable, 0);
                const pct = (list.length / Math.max(1, rows.length)) * 100;
                return (
                  <div key={f.key}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11.5, marginBottom: 4 }}>
                      <span style={{ color: "var(--fk-ink-2)" }}>{f.label}</span>
                      <span className="fk-num" style={{ color: "var(--fk-ink-3)" }}>
                        {list.length} 张 · {cny(amount, { compact: true })}
                      </span>
                    </div>
                    <Progress value={pct} tone={f.key === "paid" ? "success" : f.key === "draft" ? "warn" : "accent"} />
                  </div>
                );
              })}
            </div>
          </Panel>

          <Panel title="期间走势" sub="近 3 期">
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {trend.map((t) => {
                const max = Math.max(...trend.map((x) => x.amount));
                return (
                  <div key={t.period}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11.5, marginBottom: 3 }}>
                      <span style={{ color: "var(--fk-ink-2)" }}>{t.period}</span>
                      <span className="fk-num" style={{ color: "var(--fk-ink-3)" }}>{cny(t.payable, { compact: true })}</span>
                    </div>
                    <div style={{ height: 6, background: "var(--fk-surface-2)", borderRadius: 999 }}>
                      <div style={{ height: "100%", width: `${(t.amount / max) * 100}%`, background: "var(--fk-accent-500)", borderRadius: 999 }} />
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="hr" />
            <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.7 }}>
              结算金额以核验通过的发票为准；应付口径与质量扣款由本平台计算，付款到账后由核销流水自动推导结算单状态。
            </div>
          </Panel>
        </div>
      </div>

      <SettlementDrawer
        settlement={active}
        onClose={() => setActive(null)}
        onAdvance={async (id) => {
          const r = await s.advanceSettlement(id);
          toast(r.ok ? "结算单状态已推进" : `流转失败：${r.reason}`);
        }}
      />
    </>
  );
}

function SettlementDrawer({
  settlement,
  onClose,
  onAdvance,
}: {
  settlement: Settlement | null;
  onClose: () => void;
  onAdvance: (id: string) => Promise<void>;
}) {
  const s = useScoped();
  if (!settlement) return null;
  const x = s.settlements.find((i) => i.id === settlement.id) ?? settlement;
  const step = FLOW.find((f) => f.key === x.status);

  // 移除 ERPNext 后不再有采购订单这一层（见 docs/02 §9 ADR-001）。
  // 真实链路是：结算单 → 发票 → 付款核销 → 到账，对应 forge.invoices /
  // forge.payments / forge.payment_allocations。
  const chain = [
    { k: "结算单", v: x.code, ok: true },
    { k: "发票", v: x.purchaseInvoice ?? "待开票", ok: !!x.purchaseInvoice },
    { k: "付款核销", v: x.paymentEntry ?? "未核销", ok: !!x.paymentEntry },
  ];

  return (
    <Drawer
      open
      onClose={onClose}
      width={640}
      title={
        <span className="row" style={{ gap: 8 }}>
          {x.supplierName}
          <Badge tone={settlementStatusTone[x.status]} dot>{settlementStatusLabel[x.status]}</Badge>
        </span>
      }
      sub={<span className="fk-mono">{x.code} · 期间 {x.period} · 账期 {date(x.dueAt)}</span>}
      foot={
        <>
          <Button variant="ghost" onClick={onClose}>关闭</Button>
          <Button variant="secondary" icon="download">下载对账明细</Button>
          {step?.next && (
            <Button variant="primary" onClick={() => onAdvance(x.id)}>
              {step.next}
            </Button>
          )}
        </>
      }
    >
      <div className="stack-lg">
        <div className="g-3">
          <div className="tile">
            <div className="mini-label">标注金额</div>
            <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4 }}>{cny(x.amount)}</div>
            <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{num(x.acceptedItems)} 条 × {x.unitPriceAvg}</div>
          </div>
          <div className="tile">
            <div className="mini-label">质量扣款</div>
            <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4, color: "var(--fk-danger-600)" }}>-{cny(x.deduction)}</div>
            <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>按返工率与缺陷率计</div>
          </div>
          <div className="tile" style={{ borderColor: "var(--fk-accent-200)", background: "var(--fk-accent-50)" }}>
            <div className="mini-label">应付金额</div>
            <div className="fk-num" style={{ fontSize: 17, fontWeight: 600, marginTop: 4, color: "var(--fk-accent-700)" }}>{cny(x.payable)}</div>
            <div style={{ fontSize: 11, color: "var(--fk-accent-700)", opacity: 0.7 }}>含税</div>
          </div>
        </div>

        <Panel title="单据链与核销状态" sub="发票 → 核销 → 到账，逐级校验">
          <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
            {chain.map((c, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "9px 0",
                  borderBottom: i < chain.length - 1 ? "1px solid var(--fk-line-soft)" : undefined,
                }}
              >
                <span
                  style={{
                    width: 20,
                    height: 20,
                    borderRadius: 3,
                    display: "grid",
                    placeContent: "center",
                    background: c.ok ? "var(--fk-success-50)" : "var(--fk-surface-2)",
                    color: c.ok ? "var(--fk-success-700)" : "var(--fk-ink-4)",
                    border: `1px solid ${c.ok ? "var(--fk-success-100)" : "var(--fk-line)"}`,
                    flex: "none",
                  }}
                >
                  <Icon name={c.ok ? "check" : "clock"} size={11} strokeWidth={2.2} />
                </span>
                <span style={{ fontSize: 12, color: "var(--fk-ink-2)", width: 110 }}>{c.k}</span>
                <span className="fk-mono" style={{ fontSize: 11.5, color: c.ok ? "var(--fk-ink)" : "var(--fk-ink-4)" }}>{c.v}</span>
                {c.ok && <Badge tone="success" >已同步</Badge>}
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="流转记录">
          <Timeline
            items={[
              { text: `结算单创建（${settlementStatusLabel.draft}）`, meta: date(x.createdAt), tone: "accent" },
              ...(x.purchaseOrder ? [{ text: `生成采购订单 ${x.purchaseOrder}`, meta: "依据验收条目自动汇总" }] : []),
              ...(x.purchaseInvoice ? [{ text: `登记采购发票 ${x.purchaseInvoice}`, meta: "税额按 6% 计算" }] : []),
              ...(x.paymentEntry ? [{ text: `完成付款 ${x.paymentEntry}`, meta: "银行转账 · 已核销" }] : []),
              ...(!step?.next ? [] : [{ text: `待办：${step.next}`, meta: "由财务或运营总监操作" }]),
            ]}
          />
        </Panel>

        <Panel title="计算明细">
          <DList
            items={[
              ["验收条目", `${num(x.acceptedItems)} 条（已扣除非合格批次）`],
              ["加权均价", `${x.unitPriceAvg.toFixed(2)} 元 / 条`],
              ["扣款依据", `返工率 ${(x.deduction / Math.max(1, x.amount) * 100).toFixed(2)}% 对应质量条款 4.2`],
              ["付款条件", "验收通过后 30 日内"],
              ["数据隔离", "本结算单仅对供应商本人与内部财务可见"],
            ]}
          />
        </Panel>
      </div>
    </Drawer>
  );
}
