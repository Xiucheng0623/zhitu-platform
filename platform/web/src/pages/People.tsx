import { useMemo, useState } from "react";
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
  useSession,
} from "../design/ui";
import { ROLE_TEXT } from "../lib/adapters";
import { date, num } from "../lib/format";
import { useScoped } from "../lib/hooks";
import type { RoleCode, StaffAccount, SupplierMember } from "../lib/types";

export default function People() {
  const s = useScoped();
  const { me } = useSession();
  const [tab, setTab] = useState("internal");
  const [q, setQ] = useState("");
  const [supplierFilter, setSupplierFilter] = useState("all");
  const [invite, setInvite] = useState(false);
  const [bindTarget, setBindTarget] = useState<SupplierMember | null>(null);
  const [unbindTarget, setUnbindTarget] = useState<SupplierMember | null>(null);
  const [busyUser, setBusyUser] = useState("");

  /** 激活/停用平台账号。未激活的账号登录会被后端拒绝（status=pending）。 */
  const changeStatus = async (e: StaffAccount, status: StaffAccount["status"]) => {
    setBusyUser(e.id);
    const r = await s.setStaffStatus(e.id, status);
    setBusyUser("");
    if (!r.ok) {
      toast(`操作失败：${r.reason ?? "未知原因"}`);
      return;
    }
    toast(
      status === "active"
        ? `${e.name} 已激活，现在可以用手机号登录`
        : `${e.name} 已停用，登录会被拒绝`
    );
  };

  // 内部员工来自 forge.users（/api/v1/admin/users）。没有 member:read 权限的
  // 角色（如财务）拿到的是空列表 + 顶栏「部分模块未加载」提示 —— 这比退回
  // 种子数据里那 10 个虚构同事要好得多。
  // 库里没有部门、职称、在研项目数这些字段，所以表格也不再显示这几列：
  // 显示一个恒为 0 的"本月审核"比空着更误导。
  const staff = useMemo(() => {
    const list = s.employees;
    if (!q) return list;
    const k = q.toLowerCase();
    return list.filter(
      (e) =>
        e.name.toLowerCase().includes(k) ||
        e.phone.includes(q) ||
        e.roles.some((r) => (ROLE_TEXT[r] ?? r).includes(q))
    );
  }, [s.employees, q]);

  const members = useMemo(() => {
    let list = s.members;
    if (supplierFilter !== "all") list = list.filter((m) => m.supplierId === supplierFilter);
    if (q) list = list.filter((m) => m.name.includes(q));
    return list;
  }, [s.members, supplierFilter, q]);

  const activeStaff = s.employees.filter((e) => e.status === "active").length;
  const pendingStaff = s.employees.filter((e) => e.status === "pending").length;
  const suspendedStaff = s.employees.filter((e) => e.status === "suspended").length;
  const reviewerStaff = s.employees.filter((e) => e.roles.includes("reviewer")).length;

  return (
    <>
      <PageHead
        title="人员管理"
        desc="内部员工与供应商成员在同一名录下分层管理。内部员工由平台账号体系维护，供应商成员由供应商管理员维护并接受本企业审批。"
        badge={<Badge tone="outline">{s.employees.length + s.members.length} 人</Badge>}
        actions={
          <>
            <Button icon="download" variant="secondary">导出名录</Button>
            <Button icon="plus" variant="primary" onClick={() => setInvite(true)}>
              {tab === "internal" ? "邀请成员" : "新增标注员"}
            </Button>
          </>
        }
      />

      <div className="g-kpi" style={{ marginBottom: 14 }}>
        <Stat label="内部员工" value={s.employees.length} hint={`${activeStaff} 人在岗`} />
        <Stat label="供应商标注员" value={num(s.members.length)} hint={`分布在 ${s.suppliers.length} 家供应商`} />
        <Stat
          label="平均通过率"
          value={
            s.members.length
              ? (s.members.reduce((a, b) => a + b.passRate, 0) / s.members.length).toFixed(1)
              : "—"
          }
          unit={s.members.length ? "%" : undefined}
          hint="近 30 日标注一次通过"
        />
        <Stat
          label="账号待处理"
          value={pendingStaff + suspendedStaff}
          hint={`待激活 ${pendingStaff} · 已停用 ${suspendedStaff} · 审核员 ${reviewerStaff}`}
        />
      </div>

      <Panel flush>
        <div style={{ padding: "10px 16px 0" }}>
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { key: "internal", label: "内部团队", count: s.employees.length },
              { key: "supplier", label: "供应商成员", count: s.members.length },
            ]}
          />
        </div>

        <div className="fk-toolbar" style={{ padding: "12px 16px" }}>
          <div className="fk-search" style={{ width: 260 }}>
            <Icon name="search" size={14} />
            <input
              className="fk-input"
              value={q}
              placeholder="搜索姓名 / 手机号 / 角色"
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          {tab === "supplier" && (
            <Select
              value={supplierFilter}
              onChange={setSupplierFilter}
              options={[{ value: "all", label: "全部供应商" }, ...s.suppliers.map((x) => ({ value: x.id, label: x.shortName }))]}
            />
          )}
          <div className="fk-spacer" />
          <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>
            {tab === "internal" ? `${staff.length} 人` : `${members.length} 人`}
          </span>
        </div>

        {tab === "internal" ? (
          <DataTable<StaffAccount>
            rows={staff}
            emptyText="没有可显示的内部员工"
            emptyHint="当前角色可能没有 member:read 权限；请用管理员账号查看，或检查后端 /api/v1/admin/users"
            columns={[
              {
                key: "n",
                header: "成员",
                render: (e) => (
                  <div className="row" style={{ gap: 9 }}>
                    <span className="fk-avatar">{e.name.slice(0, 1)}</span>
                    <div>
                      <div className="fk-cell-strong">{e.name}</div>
                      <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{e.phone || "—"}</div>
                    </div>
                  </div>
                ),
                sortValue: (e) => e.name,
              },
              {
                key: "r",
                header: "角色",
                width: 230,
                render: (e) => (
                  <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                    {e.roles.length ? (
                      e.roles.map((r) => (
                        <Badge key={r} tone={r === "platform_admin" ? "accent" : "neutral"}>
                          {ROLE_TEXT[r] ?? r}
                        </Badge>
                      ))
                    ) : (
                      <span style={{ fontSize: 12, color: "var(--fk-ink-3)" }}>未分配</span>
                    )}
                  </div>
                ),
              },
              {
                key: "l",
                header: "最后登录",
                width: 150,
                align: "right",
                render: (e) => (
                  <span
                    className="fk-num"
                    style={{ color: e.lastLoginAt ? undefined : "var(--fk-ink-3)" }}
                  >
                    {e.lastLoginAt
                      ? new Date(e.lastLoginAt).toLocaleString("zh-CN", { hour12: false }).slice(0, 16)
                      : "从未登录"}
                  </span>
                ),
              },
              { key: "j", header: "开户", width: 100, align: "right", render: (e) => <span className="fk-num">{date(e.createdAt)}</span> },
              {
                key: "s",
                header: "状态",
                width: 88,
                render: (e) => (
                  <Badge
                    tone={e.status === "active" ? "success" : e.status === "pending" ? "warn" : "danger"}
                    dot
                  >
                    {e.status === "active" ? "在岗" : e.status === "pending" ? "待激活" : "已停用"}
                  </Badge>
                ),
              },
              {
                key: "a",
                header: "操作",
                width: 84,
                align: "right",
                render: (e) =>
                  e.status === "active" ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busyUser === e.id}
                      onClick={() => changeStatus(e, "suspended")}
                    >
                      停用
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={busyUser === e.id}
                      onClick={() => changeStatus(e, "active")}
                    >
                      {busyUser === e.id ? "…" : "激活"}
                    </Button>
                  ),
              },
            ]}
          />
        ) : (
          <DataTable<SupplierMember>
            rows={members}
            columns={[
              {
                key: "n",
                header: "成员",
                render: (m) => (
                  <div className="row" style={{ gap: 9 }}>
                    <span className="fk-avatar">{m.initials}</span>
                    <div>
                      <div className="fk-cell-strong">{m.name}</div>
                      <div style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>
                        {s.suppliers.find((x) => x.id === m.supplierId)?.shortName ?? "—"} · 入驻 {date(m.joinedAt)}
                      </div>
                    </div>
                  </div>
                ),
                sortValue: (m) => m.name,
              },
              {
                key: "r",
                header: "角色",
                width: 92,
                render: (m) => (
                  <Badge tone={m.role === "supplier_admin" ? "accent" : "neutral"}>
                    {m.role === "supplier_admin" ? "管理员" : m.role === "supplier_lead" ? "组长" : "标注员"}
                  </Badge>
                ),
              },
              {
                key: "l",
                header: "等级",
                width: 68,
                render: (m) => <span style={{ fontSize: 12 }}>{m.level === "senior" ? "资深" : m.level === "mid" ? "中级" : "初级"}</span>,
              },
              {
                key: "p",
                header: "通过率",
                width: 80,
                align: "right",
                render: (m) => (
                  <span className="fk-num" style={{ color: m.passRate < 85 ? "var(--fk-warn-700)" : "var(--fk-ink)" }}>
                    {m.passRate.toFixed(1)}%
                  </span>
                ),
                sortValue: (m) => m.passRate,
              },
              { key: "a", header: "在手", width: 62, align: "right", render: (m) => <span className="fk-num">{m.activeTasks}</span> },
              { key: "c", header: "累计完成", width: 96, align: "right", render: (m) => <span className="fk-num">{num(m.completedTotal)}</span>, sortValue: (m) => m.completedTotal },
              {
                // 登录账号 = 花名册成员与平台账号的绑定。绑上后该账号登录平台
                // 才看得见分给他的批次；没绑就分活，等于把活分给了一个进不来的人。
                key: "u",
                header: "登录账号",
                width: 132,
                render: (m) =>
                  m.userPhone ? (
                    <span className="fk-num" style={{ fontSize: 11.5 }}>{m.userPhone}</span>
                  ) : (
                    <span style={{ fontSize: 11.5, color: "var(--fk-warn-700)" }}>未绑定</span>
                  ),
              },
              {
                key: "s",
                header: "状态",
                width: 72,
                render: (m) => <Badge tone={m.status === "active" ? "success" : "neutral"} dot>{m.status === "active" ? "在职" : "停用"}</Badge>,
              },
              {
                key: "act",
                header: "",
                width: 92,
                align: "right",
                render: (m) =>
                  m.userPhone ? (
                    <Button size="sm" variant="ghost" onClick={() => setUnbindTarget(m)}>解绑</Button>
                  ) : (
                    <Button size="sm" variant="secondary" onClick={() => setBindTarget(m)}>绑定账号</Button>
                  ),
              },
            ]}
            emptyText="该供应商暂无成员"
          />
        )}
      </Panel>

      <InviteModal open={invite} onClose={() => setInvite(false)} mode={tab} canWriteSupplier={me.kind === "employee"} />

      <BindModal member={bindTarget} onClose={() => setBindTarget(null)} />

      <Modal
        open={!!unbindTarget}
        onClose={() => setUnbindTarget(null)}
        width={420}
        title="解绑登录账号"
        foot={
          <>
            <Button variant="ghost" onClick={() => setUnbindTarget(null)}>取消</Button>
            <Button
              variant="primary"
              onClick={async () => {
                if (!unbindTarget) return;
                const r = await s.unbindMemberUser(unbindTarget.id);
                toast(r.ok ? "已解绑" : `解绑失败：${r.reason}`);
                setUnbindTarget(null);
              }}
            >
              确认解绑
            </Button>
          </>
        }
      >
        <div style={{ fontSize: 13, lineHeight: 1.7 }}>
          解绑 <strong>{unbindTarget?.name}</strong> 的登录账号（{unbindTarget?.userPhone ?? "—"}）？
          <div style={{ color: "var(--fk-ink-3)", marginTop: 6, fontSize: 12 }}>
            解绑后该账号登录平台将看不到任何批次（已分配的批次会留在名下，但没人能作业）。
          </div>
        </div>
      </Modal>
    </>
  );
}

/** 绑定登录账号：按手机号把花名册成员接到平台账号上。 */
function BindModal({ member, onClose }: { member: SupplierMember | null; onClose: () => void }) {
  const s = useScoped();
  const [phone, setPhone] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  if (!member) return null;

  const submit = async () => {
    if (!phone.trim()) return setErr("请填写登录手机号");
    setBusy(true);
    setErr("");
    const r = await s.bindMemberUser(member.id, phone.trim());
    setBusy(false);
    if (r.ok) {
      toast(`${member.name} 已绑定登录账号`);
      setPhone("");
      onClose();
    } else {
      setErr(r.reason ?? "绑定失败");
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      width={460}
      title="绑定登录账号"
      foot={
        <>
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={submit} disabled={busy}>
            {busy ? "绑定中…" : "确认绑定"}
          </Button>
        </>
      }
    >
      <div className="stack">
        <div style={{ background: "var(--fk-surface-2)", border: "1px solid var(--fk-line)", borderRadius: 4, padding: "10px 12px" }}>
          <div style={{ fontSize: 12.5, fontWeight: 500 }}>{member.name}</div>
          <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", marginTop: 3 }}>
            {s.suppliers.find((x) => x.id === member.supplierId)?.shortName ?? "—"} · {member.role === "annotator" ? "标注员" : member.role === "supplier_lead" ? "组长" : "供应商管理员"}
          </div>
        </div>
        <Field
          label="登录手机号"
          required
          hint="该成员登录平台用的手机号（已在「内部团队/注册」里开过户）；绑定后他才能在「我的任务」里看到分给自己的批次"
        >
          <TextInput value={phone} onChange={setPhone} placeholder="138xxxxxxxx" />
        </Field>
        {err && <div style={{ color: "var(--fk-danger-600)", fontSize: 12 }}>{err}</div>}
      </div>
    </Modal>
  );
}

function InviteModal({ open, onClose, mode, canWriteSupplier }: { open: boolean; onClose: () => void; mode: string; canWriteSupplier: boolean }) {
  const s = useScoped();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [supplierId, setSupplierId] = useState(s.suppliers[0]?.id ?? "");
  const [role, setRole] = useState("annotator");
  const [level, setLevel] = useState("mid");
  const [err, setErr] = useState("");
  // 内部邀请：后端 POST /api/v1/admin/invites 发的是**邀请码**，
  // 不是「发一封邮件」。成员拿码去登录页「凭邀请码注册」自助开户，
  // 开户后是 pending，还要管理员在本页点「激活」才能登录。
  const [inviteRole, setInviteRole] = useState<RoleCode>("annotator");
  const [days, setDays] = useState(7);
  const [uses, setUses] = useState(1);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");

  const submit = async () => {
    if (mode !== "internal") {
      if (!name.trim()) return setErr("请填写姓名");
      if (!canWriteSupplier) return setErr("当前视角无权新增供应商成员");
      if (!supplierId) return setErr("请选择供应商");
      const r = await s.addMember({
        supplierId,
        name: name.trim(),
        role: role as SupplierMember["role"],
        level: level as SupplierMember["level"],
      });
      if (!r.ok) {
        setErr(r.reason ?? "添加失败");
        return;
      }
      toast(`${name} 已加入供应商团队`);
      setName(""); setEmail(""); setErr("");
      onClose();
      return;
    }

    setBusy(true);
    setErr("");
    const r = await s.createInvite({
      roles: [inviteRole],
      note: email.trim(),
      maxUses: uses,
      expiresInDays: days,
    });
    setBusy(false);
    if (!r.ok || !r.code) {
      setErr(r.reason ?? "生成邀请码失败");
      return;
    }
    setCode(r.code);
  };

  const close = () => {
    setName(""); setEmail(""); setErr(""); setCode("");
    onClose();
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      toast("邀请码已复制");
    } catch {
      toast(`请手动复制：${code}`);
    }
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title={mode === "internal" ? "生成邀请码" : "新增供应商标注员"}
      foot={
        <>
          <Button variant="ghost" onClick={close}>{code ? "完成" : "取消"}</Button>
          {!code && (
            <Button variant="primary" onClick={submit} disabled={busy}>
              {busy ? "生成中…" : mode === "internal" ? "生成邀请码" : "添加成员"}
            </Button>
          )}
        </>
      }
    >
      <div className="stack">
        {err && <div className="fk-error">{err}</div>}

        {mode === "internal" && code && (
          <>
            <div
              style={{
                background: "var(--fk-surface-2)",
                border: "1px solid var(--fk-line)",
                borderRadius: 4,
                padding: "16px 14px",
                textAlign: "center",
              }}
            >
              <div style={{ fontSize: 11.5, letterSpacing: ".12em", color: "var(--fk-ink-3)" }}>
                邀请码
              </div>
              <div
                className="fk-num"
                style={{ fontSize: 26, fontWeight: 600, letterSpacing: ".16em", margin: "6px 0 10px" }}
              >
                {code}
              </div>
              <Button size="sm" variant="secondary" onClick={copy}>复制邀请码</Button>
            </div>
            <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, lineHeight: 1.9, color: "var(--fk-ink-2)" }}>
              <li>把邀请码发给成员（{days} 天内有效，最多 {uses} 人使用）。</li>
              <li>成员打开平台登录页，点底部「凭邀请码注册」，填手机号、姓名、密码和这个码。</li>
              <li>注册后账号是「待激活」，回到本页点<strong>激活</strong>，对方才能登录。</li>
            </ol>
          </>
        )}

        {mode === "internal" && !code && (
          <>
            <Field label="授予角色" required hint="邀请码只能开这一个角色；要多个角色就分别发码，或开户后另行调整">
              <Select
                value={inviteRole}
                onChange={(v) => setInviteRole(v as RoleCode)}
                options={[
                  { value: "annotator", label: "标注员" },
                  { value: "reviewer", label: "审核员" },
                  { value: "project_manager", label: "项目经理" },
                  { value: "data_engineer", label: "数据工程师" },
                  { value: "finance", label: "财务" },
                  { value: "ops_director", label: "运营总监" },
                  { value: "platform_admin", label: "平台管理员" },
                ]}
              />
            </Field>
            <div className="g-half" style={{ gap: 12 }}>
              <Field label="有效期">
                <Select
                  value={String(days)}
                  onChange={(v) => setDays(Number(v))}
                  options={[
                    { value: "7", label: "7 天" },
                    { value: "30", label: "30 天" },
                    { value: "90", label: "90 天" },
                  ]}
                />
              </Field>
              <Field label="可用人数">
                <Select
                  value={String(uses)}
                  onChange={(v) => setUses(Number(v))}
                  options={[
                    { value: "1", label: "1 人" },
                    { value: "5", label: "5 人" },
                    { value: "20", label: "20 人" },
                  ]}
                />
              </Field>
            </div>
            <Field label="备注" hint="可选，例如团队名，只用于你自己对账">
              <TextInput value={email} onChange={setEmail} placeholder="如：算法组" />
            </Field>
            <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.7 }}>
              平台不对公网开放自由注册。邀请码带角色和有效期，用一次少一次；注册后仍需管理员激活。
            </div>
          </>
        )}

        {mode !== "internal" && (
          <>
            <div className="g-half" style={{ gap: 12 }}>
              <Field label="姓名" required>
                <TextInput value={name} onChange={setName} placeholder="真实姓名" />
              </Field>
              <Field label="手机号 / 邮箱">
                <TextInput value={email} onChange={setEmail} placeholder="可选" />
              </Field>
            </div>
            <Field label="所属供应商" required>
              <Select
                value={supplierId}
                onChange={setSupplierId}
                options={s.suppliers.map((x) => ({ value: x.id, label: `${x.name}（${x.tier} 级）` }))}
              />
            </Field>
            <div className="g-half" style={{ gap: 12 }}>
              <Field label="角色">
                <Select
                  value={role}
                  onChange={setRole}
                  options={[
                    { value: "annotator", label: "标注员" },
                    { value: "supplier_lead", label: "组长" },
                    { value: "supplier_admin", label: "供应商管理员" },
                  ]}
                />
              </Field>
              <Field label="等级">
                <Select
                  value={level}
                  onChange={setLevel}
                  options={[
                    { value: "junior", label: "初级" },
                    { value: "mid", label: "中级" },
                    { value: "senior", label: "资深" },
                  ]}
                />
              </Field>
            </div>
            <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.7 }}>
              新增成员默认仅可访问已分配给其本人的任务。供应商管理员可为成员分配任务，但不可跨项目访问。
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
