import { useMemo, useState } from "react";
import { Icon } from "../design/icons";
import {
  Badge,
  Button,
  DataTable,
  DList,
  PageHead,
  Panel,
  toast,
  useSession,
} from "../design/ui";
import { dateTime, modalityLabel, num, timeAgo } from "../lib/format";
import { useScoped } from "../lib/hooks";
import { API_BASE, isBackendConfigured, probeBackend, type ProbeRow } from "../lib/http";

/**
 * 系统设置：只展示真实状态。
 *
 * 删除的东西（2026-11-06）：
 *   · 6 条写死的「标注模板」（名字 / 时间 / LS ID 全是编的）
 *   · Keycloak / ls.internal / s3://forge-prod-data 等虚构集成地址
 *   · 「已用 128 席」「创建时间 2026-03-18」「数据区域 华东 1」
 *   · 「本地确定性种子」「重置演示数据 / 初始种子」
 *   · 点了只改本地 state 的「强制 SSO」「严格隔离」开关
 *   · 未探测就显示「已登记」的假状态
 */
const SECTIONS = [
  { key: "tenant", label: "租户与组织", icon: "suppliers" as const },
  { key: "roles", label: "角色与权限", icon: "lock" as const },
  { key: "configs", label: "标注模板", icon: "annotate" as const },
  { key: "integration", label: "系统集成", icon: "sync" as const },
  { key: "isolation", label: "数据隔离", icon: "layers" as const },
  { key: "audit", label: "审计日志", icon: "file-text" as const },
  { key: "danger", label: "环境与刷新", icon: "alert" as const },
];

const PERM_COLS = ["项目", "供应商", "任务分配", "审核裁定", "结算", "系统设置"];
/** 内置角色策略定义（静态规则，不是业务记录） */
const PERM_MATRIX: Array<[string, string, number[]]> = [
  ["平台管理员", "全平台", [1, 1, 1, 1, 1, 1]],
  ["运营总监", "运营中心", [1, 1, 1, 1, 1, 0]],
  ["项目经理", "交付部", [1, 0, 1, 1, 0, 0]],
  ["审核员", "质量中心", [0, 0, 0, 1, 0, 0]],
  ["数据工程师", "平台组", [0, 0, 0, 0, 0, 0]],
  ["供应商管理员", "供应商侧", [0, 0, 1, 0, 0, 0]],
  ["供应商标注员", "供应商侧", [0, 0, 0, 0, 0, 0]],
];

/** 只列真实存在、可被 /api/v1/integrations/health 探测到的系统 */
const INTEGRATIONS = [
  { key: "database", n: "业务数据库", v: "PostgreSQL · forge", ep: "PostgreSQL · RLS 强制", lic: "PostgreSQL" },
  { key: "labelstudio", n: "标注引擎", v: "Label Studio CE 1.23.2", ep: "http://127.0.0.1:8080 · SSO 票据整页进入", lic: "Apache-2.0" },
  { key: "storage", n: "对象存储", v: "S3 兼容", ep: "未配置 MINIO_ROOT_PASSWORD", lic: "—" },
  { key: "payments", n: "付款执行", v: "线下银行", ep: "平台只登记流水与回单号", lic: "—" },
];

export default function Settings() {
  const s = useScoped();
  const { enterprise, user } = useSession();
  const [sec, setSec] = useState("tenant");
  const [auditQ, setAuditQ] = useState("");
  const [probe, setProbe] = useState<Record<string, ProbeRow>>({});
  const [probing, setProbing] = useState(false);
  const [probedAt, setProbedAt] = useState<number | null>(null);

  const runProbe = async () => {
    setProbing(true);
    try {
      const r = await probeBackend();
      if (!r.configured) {
        toast("未配置 VITE_API_BASE，无法探测真实后端");
        return;
      }
      const map: Record<string, ProbeRow> = {};
      for (const row of r.rows) map[row.key] = row;
      setProbe(map);
      setProbedAt(r.at);
      const bad = r.rows.filter((x) => x.state === "fail").length;
      toast(bad === 0 ? `探测完成 · orchestrator ${r.version ?? ""}` : `${bad} 项探测失败`);
    } catch (e) {
      toast(`探测失败：${(e as Error).message}`);
    } finally {
      setProbing(false);
    }
  };

  const auditRows = s.audit.filter((a) => !auditQ || a.actorName.includes(auditQ) || a.detail.includes(auditQ));

  /** 标注模板来自真实项目记录，不再是一张写死的表 */
  const configRows = useMemo(
    () =>
      s.projects.map((p) => ({
        id: p.id,
        name: p.labelingConfigName || "未指定模板",
        modality: modalityLabel[p.modality],
        project: p.name,
        lsId: s.tasks.find((t) => t.projectId === p.id)?.lsProjectId ?? null,
        status: p.status,
      })),
    [s.projects, s.tasks]
  );

  return (
    <>
      <PageHead
        title="系统设置"
        desc="租户、权限、标注模板、外部系统连接与数据隔离策略。状态与记录均来自服务端，未接入的能力会明确标出。"
        actions={
          <Button icon="sync" variant="secondary" onClick={() => void s.reload()}>
            重新加载
          </Button>
        }
      />

      <div className="g-sidebar">
        <div className="fk-panel" style={{ padding: 6 }}>
          {SECTIONS.map((x) => (
            <button
              key={x.key}
              onClick={() => setSec(x.key)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 9,
                width: "100%",
                height: 33,
                padding: "0 10px",
                border: 0,
                borderRadius: 4,
                background: sec === x.key ? "var(--fk-accent-50)" : "transparent",
                color: sec === x.key ? "var(--fk-accent-700)" : "var(--fk-ink-2)",
                fontSize: 12.5,
                fontWeight: sec === x.key ? 500 : 400,
                cursor: "pointer",
                textAlign: "left",
              }}
            >
              <Icon name={x.icon} size={14} />
              {x.label}
            </button>
          ))}
        </div>

        <div className="stack">
          {sec === "tenant" && (
            <>
              <Panel title="企业信息">
                <DList
                  items={[
                    ["企业名称", enterprise.name],
                    ["租户标识", <span className="fk-mono">{enterprise.shortName} · ent_forge</span>],
                    ["部署形态", enterprise.plan === "internal" ? "单租户内部部署" : "SaaS 多租户"],
                    ["内部账号", `${s.employees.length} 个（来自 forge.users）`],
                    ["供应商成员", `${s.members.length} 个（来自 forge.supplier_members）`],
                    ["当前访问地址", <span className="fk-mono">{window.location.host}</span>],
                  ]}
                />
              </Panel>

              <Panel title="统一身份认证">
                <DList
                  items={[
                    ["平台登录", "手机号 + 密码 · 服务端 JWT（forge.users）"],
                    ["标注引擎登录", "平台 JWT 换 90 秒一次性 HMAC 票据，整页进入，无需二次登录"],
                    ["角色来源", "forge.users.roles · 服务端 RBAC 校验为准"],
                    ["OIDC / 外部 IdP", "未接入（当前使用平台自有认证）"],
                  ]}
                />
              </Panel>
            </>
          )}

          {sec === "roles" && (
            <Panel title="角色权限矩阵" sub="内置策略定义 · 7 个角色 · 6 个权限域" flush>
              <div style={{ padding: 16 }}>
                <div className="fk-tablewrap">
                  <table className="fk-table">
                    <thead>
                      <tr>
                        <th>角色</th>
                        <th style={{ width: 96 }}>归属</th>
                        {PERM_COLS.map((c) => (
                          <th key={c} style={{ textAlign: "center", width: 76 }}>{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {PERM_MATRIX.map(([role, scope, perms], i) => (
                        <tr key={i}>
                          <td className="fk-cell-strong">{role}</td>
                          <td style={{ fontSize: 11.5, color: "var(--fk-ink-3)" }}>{scope}</td>
                          {perms.map((p, j) => (
                            <td key={j} style={{ textAlign: "center" }}>
                              {p ? (
                                <span style={{ color: "var(--fk-success-600)", display: "inline-grid", placeContent: "center" }}>
                                  <Icon name="check" size={13} strokeWidth={2.4} />
                                </span>
                              ) : (
                                <span style={{ color: "var(--fk-line-strong)" }}>—</span>
                              )}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div style={{ marginTop: 12, fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.7 }}>
                  上表是策略定义，不代表已逐条通过自动化用例覆盖；实际权限以服务端 RBAC 为准。
                </div>
              </div>
            </Panel>
          )}

          {sec === "configs" && (
            <Panel
              title="标注模板"
              sub={configRows.length ? `${configRows.length} 个项目 · 来自 forge.projects` : "暂无项目"}
              flush
            >
              {configRows.length === 0 ? (
                <div style={{ padding: 30, textAlign: "center", fontSize: 12, color: "var(--fk-ink-3)" }}>
                  尚无项目。创建项目并同步标注配置后，这里会列出真实模板。
                </div>
              ) : (
                <DataTable
                  rows={configRows}
                  columns={[
                    { key: "n", header: "模板名称", render: (c) => <span className="fk-mono" style={{ fontSize: 11.5, color: "var(--fk-ink)" }}>{c.name}</span> },
                    { key: "m", header: "模态", width: 76, render: (c) => <Badge tone="neutral">{c.modality}</Badge> },
                    { key: "p", header: "所属项目", render: (c) => <span style={{ fontSize: 11.5 }}>{c.project}</span> },
                    { key: "l", header: "LS Project", width: 92, align: "right", render: (c) => <span className="fk-mono">{c.lsId != null ? `#${c.lsId}` : "未关联"}</span> },
                    { key: "u", header: "项目状态", width: 90, align: "right", render: (c) => <span style={{ fontSize: 11.5 }}>{c.status}</span> },
                  ]}
                />
              )}
            </Panel>
          )}

          {sec === "integration" && (
            <>
              <Panel
                title="外部系统连接"
                sub={
                  isBackendConfigured()
                    ? probedAt
                      ? `真实探测 · ${timeAgo(new Date(probedAt).toISOString())}`
                      : `编排层 ${API_BASE} · 点击右侧实际探测`
                    : "当前未配置后端地址（VITE_API_BASE）"
                }
                actions={
                  <Button
                    size="sm"
                    icon="sync"
                    onClick={runProbe}
                    disabled={probing || !isBackendConfigured()}
                  >
                    {probing ? "探测中…" : "检测连通性"}
                  </Button>
                }
              >
                <div className="stack" style={{ gap: 10 }}>
                  {INTEGRATIONS.map((c) => {
                    const live = probe[c.key];
                    const state = live ? live.state : "unknown";
                    const label = !live
                      ? "未探测"
                      : state === "ok"
                        ? "已连接"
                        : state === "fail"
                          ? "不可达"
                          : "未验证";
                    const tone =
                      state === "ok" && live ? "success" : state === "fail" && live ? "danger" : "neutral";
                    const dot =
                      live && state === "ok"
                        ? "var(--fk-success-600)"
                        : live && state === "fail"
                          ? "var(--fk-danger-600)"
                          : "var(--fk-ink-4)";
                    return (
                      <div
                        key={c.key}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 12,
                          padding: "11px 13px",
                          border: "1px solid var(--fk-line)",
                          borderRadius: 4,
                          background: "var(--fk-surface)",
                        }}
                      >
                        <span
                          style={{
                            width: 7,
                            height: 7,
                            borderRadius: 999,
                            flex: "none",
                            background: dot,
                            boxShadow: live && state === "ok" ? "0 0 0 3px var(--fk-success-50)" : undefined,
                          }}
                        />
                        <div style={{ minWidth: 150 }}>
                          <div style={{ fontSize: 12.5, fontWeight: 500 }}>{c.n}</div>
                          <div style={{ fontSize: 10.5, color: "var(--fk-ink-3)" }} className="fk-mono">{c.v}</div>
                        </div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className="fk-mono" style={{ fontSize: 11, color: "var(--fk-ink-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {c.ep}
                          </div>
                          <div style={{ fontSize: 10.5, color: "var(--fk-ink-3)", marginTop: 2 }}>
                            {live ? live.detail : "点击「检测连通性」获取实际状态"}
                          </div>
                        </div>
                        <Badge tone="outline">{c.lic}</Badge>
                        <Badge tone={tone} dot>{label}</Badge>
                      </div>
                    );
                  })}
                </div>
              </Panel>

              <Panel title="同步策略">
                <DList
                  items={[
                    ["认证方式", "平台 JWT → LS 一次性票据（服务端签发，前端不可见）"],
                    ["标注结果", "Label Studio 为唯一作业面，平台通过任务关联读取状态"],
                    ["幂等键", "project_id + batch_no + ls_project_id"],
                    ["对象存储", "尚未配置凭据，原始文件暂不提供签名 URL"],
                  ]}
                />
              </Panel>
            </>
          )}

          {sec === "isolation" && (
            <Panel title="数据隔离策略" sub="当前实现状态">
              <div className="stack" style={{ gap: 12 }}>
                {[
                  ["租户边界", "forge 业务表带 enterprise_id，PostgreSQL RLS 按会话上下文过滤。", "已配置"],
                  ["供应商边界", "供应商身份只能读取 supplier_id 命中的记录；服务端 RBAC 为准。", "已配置"],
                  ["任务边界", "标注员仅可读写 assignee_id 为本人且状态允许的任务。", "待用例覆盖"],
                  ["文件边界", "对象存储未配置，签名 URL 能力暂不可用；此项尚未生效。", "未启用"],
                ].map(([k, v, st], i) => (
                  <div key={i} className="tile" style={{ padding: "12px 14px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                      <span style={{ fontSize: 12.5, fontWeight: 500 }}>{k}</span>
                      <Badge tone={st === "未启用" ? "danger" : st === "已配置" ? "success" : "outline"}>{st}</Badge>
                    </div>
                    <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.65 }}>{v}</div>
                  </div>
                ))}
              </div>
              <div className="hr" />
              <div style={{ fontSize: 11.5, color: "var(--fk-ink-3)", lineHeight: 1.7 }}>
                以上为当前实现状态，不宣称已通过完整的「正向可见 / 反向不可见」自动化用例覆盖。
              </div>
            </Panel>
          )}

          {sec === "audit" && (
            <Panel
              title="审计日志"
              sub={`${auditRows.length} 条 · 服务端真实记录`}
              actions={
                <div className="fk-search" style={{ width: 200 }}>
                  <Icon name="search" size={13} />
                  <input className="fk-input" value={auditQ} placeholder="搜索操作人 / 内容" onChange={(e) => setAuditQ(e.target.value)} />
                </div>
              }
              flush
            >
              {auditRows.length === 0 ? (
                <div style={{ padding: 30, textAlign: "center", fontSize: 12, color: "var(--fk-ink-3)" }}>
                  尚无审计记录。产生写操作后由服务端写入。
                </div>
              ) : (
                <DataTable
                  rows={auditRows.slice(0, 20)}
                  columns={[
                    { key: "t", header: "时间", width: 130, render: (a) => <span className="fk-num" style={{ fontSize: 11.5 }}>{dateTime(a.at)}</span> },
                    { key: "u", header: "操作人", width: 88, render: (a) => <span style={{ fontSize: 12 }}>{a.actorName || "—"}</span> },
                    { key: "a", header: "动作", width: 128, render: (a) => <span className="fk-mono" style={{ fontSize: 11, color: "var(--fk-ink-2)" }}>{a.action}</span> },
                    { key: "d", header: "内容", render: (a) => <span style={{ fontSize: 11.5, color: "var(--fk-ink-2)" }}>{a.detail}</span> },
                    { key: "ip", header: "来源 IP", width: 110, align: "right", render: (a) => <span className="fk-mono" style={{ fontSize: 10.5, color: "var(--fk-ink-3)" }}>{a.ip || "—"}</span> },
                    { key: "ago", header: "距今", width: 82, align: "right", render: (a) => <span style={{ fontSize: 11, color: "var(--fk-ink-3)" }}>{timeAgo(a.at)}</span> },
                  ]}
                />
              )}
            </Panel>
          )}

          {sec === "danger" && (
            <>
              <Panel title="环境信息">
                <DList
                  items={[
                    ["当前用户", `${user.name} · ${user.title}`],
                    ["角色", user.roles.join(" / ")],
                    ["前端版本", <span className="fk-mono">forge-web 0.1.0</span>],
                    ["编排层地址", <span className="fk-mono">{API_BASE || "未配置"}</span>],
                    ["数据来源", s.dataSource === "api" ? "PostgreSQL · 服务端真实数据" : "后端未连接 · 页面显示空态"],
                    ["项目数 / 任务数", `${num(s.projects.length)} / ${num(s.tasks.length)}`],
                  ]}
                />
                {s.partialErrors.length > 0 && (
                  <div style={{ marginTop: 12, fontSize: 11.5, color: "var(--fk-danger-600)", lineHeight: 1.7 }}>
                    以下模块未能加载：{s.partialErrors.join("；")}
                  </div>
                )}
              </Panel>

              <Panel title="重新拉取服务端数据">
                <div style={{ fontSize: 12, color: "var(--fk-ink-3)", lineHeight: 1.75, marginBottom: 12 }}>
                  平台不在浏览器本地保存任何业务数据。此操作会重新请求编排层的全部列表接口，
                  用于排查数据过期或部分模块加载失败；不会清空、重置或伪造任何记录。
                </div>
                <Button
                  variant="secondary"
                  icon="refresh"
                  onClick={() => {
                    void s.reload().then(() => toast("已重新拉取服务端数据"));
                  }}
                >
                  重新加载
                </Button>
              </Panel>
            </>
          )}
        </div>
      </div>
    </>
  );
}
