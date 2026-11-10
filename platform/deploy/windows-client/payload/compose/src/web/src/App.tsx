import { useEffect, useMemo } from "react";
import {
  HashRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { SessionCtx, ToastHost, type Session } from "./design/ui";
import { AuthProvider, deriveMode, useAuth } from "./lib/auth";
import { BRAND_NAME, BRAND_SHORT } from "./lib/brand";
import { StoreProvider } from "./lib/store";
import { AppShell } from "./shell/AppShell";
import { FLAT_NAV } from "./shell/nav";
import Annotate from "./pages/Annotate";
import ChangePassword from "./pages/ChangePassword";
import DataPage from "./pages/DataPage";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import People from "./pages/People";
import Performance from "./pages/Performance";
import Portal from "./pages/Portal";
import Projects from "./pages/Projects";
import Review from "./pages/Review";
import SettlementPage from "./pages/Settlement";
import Settings from "./pages/Settings";
import SsoCallback from "./pages/SsoCallback";
import Suppliers from "./pages/Suppliers";
import Tasks from "./pages/Tasks";

function Boot() {
  // 首屏在等 /api/v1/auth/me 返回。给一个安静的整页态，
  // 不要闪一下登录页再跳回工作台 —— 那会让用户以为自己被登出了。
  return (
    <div className="login" style={{ display: "grid", placeItems: "center" }}>
      <div style={{ color: "var(--fk-ink-3)", fontSize: 13, letterSpacing: ".08em" }}>
        正在校验登录状态…
      </div>
    </div>
  );
}

/** 已登录外壳：会话由 AuthProvider 提供，这里只负责拼 SessionCtx */
function AuthedApp() {
  const auth = useAuth();
  const me = auth.me!;
  const mode = useMemo(() => deriveMode(me), [me]);

  const session = useMemo<Session>(
    () => ({
      enterprise: {
        name: BRAND_NAME,
        shortName: BRAND_SHORT,
        plan: "internal",
        seats: 0,
      },
      user: {
        name: me.name,
        title: me.title,
        email: me.email,
        phone: auth.phone,
        roles: me.roles,
      },
      me,
      mode,
      logout: auth.logout,
    }),
    [me, mode, auth.phone, auth.logout]
  );

  return (
    <SessionCtx.Provider value={session}>
      <StoreProvider>
        <Routes>
          <Route element={<ShellLayout />}>
            <Route path="/" element={<Overview />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/suppliers" element={<Suppliers />} />
            <Route path="/portal" element={<Portal />} />
            <Route path="/people" element={<People />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/data" element={<DataPage />} />
            <Route path="/annotate" element={<Annotate />} />
            <Route path="/review" element={<Review />} />
            <Route path="/performance" element={<Performance />} />
            <Route path="/settlement" element={<SettlementPage />} />
            <Route path="/settings" element={<Settings />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        <ToastHost />
      </StoreProvider>
    </SessionCtx.Provider>
  );
}

function ShellLayout() {
  const loc = useLocation();
  useEffect(() => {
    const item = FLAT_NAV.find((i) =>
      i.path === "/" ? loc.pathname === "/" : loc.pathname.startsWith(i.path)
    );
    document.title = `${item?.label ?? "工作台"} · 智途`;
  }, [loc.pathname]);
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}

/** 路由守卫：按会话状态决定去哪，而不是靠每个页面自己判断 */
function Gate() {
  const auth = useAuth();
  const loc = useLocation();

  if (auth.status === "loading") return <Boot />;

  if (auth.status === "anon") {
    // 记住来路，登录后回原页（避免从深链接进来总被丢回工作台）
    return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  }

  if (auth.status === "must_change") {
    // 不嵌套 <Routes>：父路由已经是 "*"，嵌套的绝对路径匹配不到，会白屏。
    if (loc.pathname === "/change-password") return <ChangePassword />;
    return <Navigate to="/change-password" replace />;
  }

  // 已改密却停在改密页：回工作台，别让用户困惑
  if (loc.pathname === "/change-password") return <Navigate to="/" replace />;

  return <AuthedApp />;
}

function LoginRoute() {
  const auth = useAuth();
  if (auth.status === "loading") return <Boot />;
  if (auth.status === "must_change") return <Navigate to="/change-password" replace />;
  if (auth.status === "authed") return <Navigate to="/" replace />;
  return <Login />;
}

export default function App() {
  return (
    <AuthProvider>
      <HashRouter>
        <Routes>
          <Route path="/login" element={<LoginRoute />} />
          {/* 回调页必须排在 Gate 之前：此刻用户还没登录，走 Gate 会被
              "未登录 → 跳登录页" 的规则吃掉，URL 上的 code/state 就丢了。 */}
          <Route path="/sso/callback" element={<SsoCallback />} />
          <Route path="*" element={<Gate />} />
        </Routes>
      </HashRouter>
    </AuthProvider>
  );
}
