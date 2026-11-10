import { useCallback, useEffect, useState } from "react";
import { Badge, Button, toast } from "./ui";
import { get } from "../lib/http";

/**
 * 项目详情里的「标注流程」入口 —— 把甲方送进 Label Studio 原生编辑器。
 *
 * 为什么这里只有一个按钮，没有一个表单设计器
 * ---------------------------------------------------------------------------
 * Label Studio 自带的「浏览模板 / 代码 / 可视化」编辑器覆盖全部控件、
 * 条件分支、配置数据和实时预览，而且随 LS 升级演进。曾经评估过的平台自建
 * 方案只能覆盖 8 种控件 + 一级条件，长尾还得退回手写 XML —— 等于长期维护
 * 一份永远追不上上游的半成品，已整体下线。这里只负责：
 *   1. 判断这个项目有没有绑定 LS；
 *   2. 向平台要一张被严格收窄的一次性门票；
 *   3. 把人送进 LS 的 /settings/labeling。
 *
 * 安全边界不在前端：LS CE 版没有 RBAC，一个原生账号能看所有项目、
 * 建项目、导数据、导出标注。票据里带 mode=design / can_import=false /
 * 单项目白名单，LS 侧 forge_sso.py 的 _guard_designer 在服务端把写操作
 * 默认全拒，只留 `PATCH /api/projects/<id>/` 且请求体只有 label_config
 * 一条例外。前端这里藏不藏按钮都不影响实际权限。
 */

interface LsBinding {
  bound: boolean;
  ls_project_id: number | null;
  ls_config_name?: string | null;
}

interface Ticket {
  ok: boolean;
  url: string;
  ttl_seconds: number;
}

export function LsDesignEntry({
  projectId,
  writable,
}: {
  projectId: string;
  /** 有 labeling:write 才能签设计票据 */
  writable: boolean;
}) {
  const [brief, setBrief] = useState<LsBinding | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    setErr("");
    get<LsBinding>(`/api/v1/projects/${projectId}/ls-binding`)
      .then((d) => {
        if (alive) setBrief(d);
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

  async function enter() {
    setBusy(true);
    try {
      const t = await get<Ticket>(
        `/api/v1/ls/design-ticket?project_id=${encodeURIComponent(projectId)}`
      );
      if (!t.ok || !t.url) {
        throw new Error("票据签发失败");
      }
      // 票据 90 秒一次性，直接导航而不是塞进 iframe：设置页是全屏 SPA，
      // iframe 里会被 LS 的 X-Frame-Options / SameSite 折腾，而且甲方要
      // 能自己看地址、刷新页面。另开标签页，平台这边的抽屉不丢。
      window.open(t.url, "_blank", "noopener");
    } catch (e) {
      toast((e as Error).message || "进不去标注引擎");
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return <div style={{ padding: 16, fontSize: 12.5, color: "var(--fk-ink-3)" }}>正在读取项目绑定…</div>;
  }
  if (err) {
    return (
      <div style={{ padding: 16, fontSize: 12.5, color: "var(--fk-ink-3)", lineHeight: 1.9 }}>
        读取失败：{err}
      </div>
    );
  }

  const lsId = brief?.ls_project_id;
  const bound = !!lsId;

  return (
    <div style={{ padding: 16 }}>
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Badge tone={bound ? "success" : "neutral"} dot>
          {bound ? "已绑定标注引擎" : "尚未绑定"}
        </Badge>
        {bound && (
          <span className="fk-mono" style={{ fontSize: 11.5, color: "var(--fk-ink-3)" }}>
            project #{String(lsId)}
          </span>
        )}
      </div>

      <div style={{ marginTop: 10, fontSize: 12.5, lineHeight: 1.9, color: "var(--fk-ink-2)" }}>
        {bound ? (
          <>
            标注员在页面上按什么顺序、用什么控件、哪些必填、哪一步要按条件出现，
            都在这里定。改动保存后，标注员刷新标注页就是新表单。
          </>
        ) : (
          <>这个项目还没绑定标注引擎。先在平台创建/绑定 LS 项目，才能进编辑器。</>
        )}
      </div>

      <div className="row" style={{ gap: 8, marginTop: 12, alignItems: "center" }}>
        <Button
          variant="primary"
          icon="external"
          onClick={enter}
          disabled={!writable || !bound || busy}
          title={
            !writable
              ? "当前角色没有标注标准编辑权限"
              : !bound
                ? "项目还没绑定标注引擎"
                : "打开 Label Studio 原生标注界面编辑器"
          }
        >
          {busy ? "正在打开…" : "在标注引擎里设计"}
        </Button>
        <span style={{ fontSize: 11.5, color: "var(--fk-ink-3)" }}>
          用的是 LS 原生「可视化 / 代码 / 模板」，不是平台自建编辑器
        </span>
      </div>

      <div
        style={{
          marginTop: 12,
          paddingTop: 10,
          borderTop: "1px solid var(--fk-line)",
          fontSize: 11.5,
          color: "var(--fk-ink-3)",
          lineHeight: 1.85,
        }}
      >
        账号权限由平台签发的一次性票据收窄：只能编辑这一个项目的标注界面，
        不能建项目、导数据、导出标注或查看成员。
        <br />
        注意：改表单结构可能让已有标注显示不出来。项目里已有标注时，
        请先在 LS 里确认影响范围再保存。
        {!writable && <><br />你的角色只能查看，改动需要项目负责人或客户方账号。</>}
      </div>
    </div>
  );
}
