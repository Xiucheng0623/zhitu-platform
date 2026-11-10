import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { BrandMark, Icon } from "../design/icons";
import { Kbd, SessionCtx, useSession, type ViewMode } from "../design/ui";
import { WorkbenchLaunch } from "../design/WorkbenchLaunch";
import { OFFLINE_TOOLTIP } from "../lib/dataMode";
import { useStore } from "../lib/store";
import { enterWorkbench, pickWorkbenchTask } from "../lib/workbench";
import { FLAT_NAV, NAV_GROUPS } from "./nav";

/**
 * 顶栏那个「视角」标签只是**说明当前身份能看到什么**，不是开关。
 * 早期版本它可以点开切换，那是配合种子数据的演示设施；接真后端后
 * 保留切换入口等于把越权入口摆在界面上。种子数据已于 2026-11-06 全部删除。
 */
const SCOPE_LABEL: Record<ViewMode, string> = {
  internal: "内部视角",
  supplier: "供应商视角",
  annotator: "标注员视角",
};

const SCOPE_HINT: Record<ViewMode, string> = {
  internal: "运营方全量数据：可见全部项目、供应商、结算与绩效",
  supplier: "仅可见授权给本供应商的项目、成员与结算单",
  annotator: "仅可见分配给本人的标注任务",
};

export function AppShell({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const loc = useLocation();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [cmdkOpen, setCmdkOpen] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState("");
  const [launchBatch, setLaunchBatch] = useState("");

  // 数据来源以运行时实际状态为准，不再看构建期环境变量：
  // 同一份前端包，后端连上了就该显示真实数字，连不上就必须挂警示。
  const { dataSource, partialErrors, tasks } = useStore();
  const showOfflineBadge = dataSource === "offline";

  /** 点击「标注工作台」= 整页进入标注器，不渲染任何嵌套壳。 */
  const launchWorkbench = () => {
    const task = pickWorkbenchTask(tasks);
    if (!task) {
      // 列表还没拉回来或确实没有批次：走中转页，由它给出真实原因。
      nav("/annotate");
      return;
    }
    setLaunchBatch(`${task.batchNo} · ${task.projectName}`);
    setLaunchError("");
    setLaunching(true);
    void enterWorkbench(task.id).catch((e: unknown) => {
      setLaunchError(e instanceof Error ? e.message : String(e));
    });
  };

  const go = (path: string) => {
    if (path === "/annotate") {
      launchWorkbench();
    } else {
      nav(path);
    }
    setCmdkOpen(false);
    setOpen(false);
  };

  const pendingReview = tasks.filter((t) => t.status === "submitted" || t.status === "reviewing").length;

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdkOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  useEffect(() => {
    setOpen(false);
  }, [loc.pathname]);

  const visibleNav = useMemo(
    () =>
      NAV_GROUPS.map((g) => ({
        ...g,
        items: g.items.filter((i) => !i.scope || i.scope.includes(session.mode)),
      })).filter((g) => g.items.length > 0),
    [session.mode]
  );

  const activeItem = FLAT_NAV.find((i) =>
    i.path === "/" ? loc.pathname === "/" : loc.pathname.startsWith(i.path)
  );

  return (
    <div className="shell">
      {open && <div className="shell__scrim" onClick={() => setOpen(false)} />}
      <aside className="shell__side" data-open={open}>
        <div className="shell__brand">
          <span className="shell__brand-mark">
            <BrandMark size={26} />
          </span>
          <div>
            <div className="shell__brand-name">智途</div>
            <div className="shell__brand-sub">AI 数据生产平台</div>
          </div>
        </div>

        <nav className="shell__nav">
          {visibleNav.map((g) => (
            <div className="shell__group" key={g.label}>
              <div className="shell__group-label">{g.label}</div>
              {g.items.map((it) => {
                const active = it.path === "/" ? loc.pathname === "/" : loc.pathname.startsWith(it.path);
                return (
                  <a
                    key={it.key}
                    className="shell__link"
                    data-active={active}
                    href={`#${it.path}`}
                    onClick={(e) => {
                      e.preventDefault();
                      go(it.path);
                    }}
                  >
                    <Icon name={it.icon} size={15} />
                    {it.label}
                    {it.key === "review" && pendingReview > 0 && (
                      <span className="shell__link-badge">{pendingReview}</span>
                    )}
                  </a>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="shell__side-foot">
          <div className="shell__tenant">
            <span className="shell__tenant-org">{session.enterprise.shortName.slice(0, 1)}</span>
            <div style={{ minWidth: 0 }}>
              <div className="shell__tenant-name">{session.enterprise.name}</div>
              <div className="shell__tenant-plan">
                租户 ent_forge · {session.enterprise.plan === "internal" ? "内部部署" : "SaaS"}
              </div>
            </div>
          </div>
          <div className="shell__user" onClick={() => nav("/settings")}>
            <span className="shell__user-avatar">{session.user.name.slice(0, 1)}</span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="shell__user-name">{session.user.name}</div>
              <div className="shell__user-role">
                {session.user.title}
                {session.user.phone ? ` · ${session.user.phone}` : ""}
              </div>
            </div>
            <Icon name="chevron-right" size={13} />
          </div>
        </div>
      </aside>

      <div className="shell__main">
        <header className="shell__top">
          <button className="shell__iconbtn shell__mobile-btn" onClick={() => setOpen(true)} aria-label="打开导航">
            <Icon name="menu" size={17} />
          </button>

          <div className="shell__crumb">
            <span>{session.enterprise.shortName}</span>
            <Icon name="chevron-right" size={12} />
            <strong>{activeItem?.label ?? "工作台"}</strong>
          </div>

          <div style={{ flex: 1 }} />

          <button className="shell__omni" onClick={() => setCmdkOpen(true)}>
            <Icon name="search" size={14} />
            <span>搜索项目 / 供应商 / 任务</span>
            <kbd>
              <Kbd>⌘</Kbd>
              <Kbd>K</Kbd>
            </kbd>
          </button>

          {showOfflineBadge ? (
            <span
              title={OFFLINE_TOOLTIP}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                height: 24,
                padding: "0 9px",
                borderRadius: 3,
                fontSize: 11.5,
                fontWeight: 600,
                letterSpacing: ".03em",
                color: "var(--fk-danger-600, #B42318)",
                background: "var(--fk-danger-50, #FEF3F2)",
                border: "1px solid var(--fk-danger-200, #FECDCA)",
                cursor: "help",
              }}
            >
              后端未连接
            </span>
          ) : partialErrors.length > 0 ? (
            // 后端连上了，但部分模块没权限/没加载出来。
            // 这种情况必须说出来 —— 否则用户会以为"绩效中心是空的，说明没人做绩效"。
            <span
              title={`以下模块未能加载：${partialErrors.join("；")}`}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                height: 24,
                padding: "0 9px",
                borderRadius: 3,
                fontSize: 11.5,
                fontWeight: 600,
                color: "var(--fk-danger-600, #B42318)",
                background: "var(--fk-danger-50, #FEF3F2)",
                border: "1px solid var(--fk-danger-200, #FECDCA)",
                cursor: "help",
              }}
            >
              部分模块未加载（{partialErrors.length}）
            </span>
          ) : null}

          <span className="shell__mode" title={SCOPE_HINT[session.mode]}>
            <Icon name="eye" size={12} />
            <span>{SCOPE_LABEL[session.mode]}</span>
          </span>

          <button className="shell__iconbtn" title="退出登录" onClick={session.logout}>
            <Icon name="lock" size={16} />
          </button>

          <button className="shell__iconbtn" title="系统设置" onClick={() => nav("/settings")}>
            <Icon name="help" size={16} />
          </button>
        </header>

        <main className="shell__content">
          <div className="shell__inner">{children}</div>
        </main>
      </div>

      {cmdkOpen && (
        <CommandPalette
          onClose={() => setCmdkOpen(false)}
          mode={session.mode}
          onGo={go}
        />
      )}

      {launching && (
        <WorkbenchLaunch
          batchLabel={launchBatch}
          error={launchError}
          onRetry={launchError ? () => { setLaunchError(""); launchWorkbench(); } : undefined}
          onBack={() => { setLaunching(false); setLaunchError(""); }}
        />
      )}
    </div>
  );
}

function CommandPalette({
  onClose,
  mode,
  onGo,
}: {
  onClose: () => void;
  mode: ViewMode;
  onGo: (path: string) => void;
}) {
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const items = FLAT_NAV.filter((i) => !i.scope || i.scope.includes(mode)).filter(
    (i) => !q || i.label.includes(q) || i.key.includes(q.toLowerCase())
  );

  const go = (path: string) => onGo(path);

  return (
    <div className="cmdk" onClick={onClose}>
      <div className="cmdk__box" onClick={(e) => e.stopPropagation()}>
        <input
          className="cmdk__input"
          autoFocus
          value={q}
          placeholder="跳转到…"
          onChange={(e) => {
            setQ(e.target.value);
            setIdx(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setIdx((i) => Math.min(items.length - 1, i + 1));
            }
            if (e.key === "ArrowUp") {
              e.preventDefault();
              setIdx((i) => Math.max(0, i - 1));
            }
            if (e.key === "Enter" && items[idx]) go(items[idx].path);
          }}
        />
        <div className="cmdk__list">
          {items.map((it, i) => (
            <div
              key={it.key}
              className="cmdk__item"
              data-active={i === idx}
              onMouseEnter={() => setIdx(i)}
              onClick={() => go(it.path)}
            >
              <Icon name={it.icon} size={14} />
              {it.label}
              <span className="cmdk__item-hint">{it.path}</span>
            </div>
          ))}
          {!items.length && (
            <div style={{ padding: "18px 12px", fontSize: 12.5, color: "var(--fk-ink-3)", textAlign: "center" }}>
              当前视角下无匹配页面
            </div>
          )}
        </div>
        <div className="cmdk__foot">
          <span>↑↓ 选择</span>
          <span>↵ 打开</span>
          <span>esc 关闭</span>
        </div>
      </div>
    </div>
  );
}

export { SessionCtx };
