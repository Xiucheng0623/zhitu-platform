/**
 * 会话层（真实身份）
 * ---------------------------------------------------------------------------
 * 替换掉 Phase 2/3 的「前端按视角伪造 Principal」：App.tsx 里的
 * buildPrincipal("supplier") 之类会让任何人在浏览器里点一下就看到别人家的
 * 数据。真实环境里主体只有一个来源 —— 服务端签发的 token。
 *
 * 三条约束：
 * 1. **身份从 /api/v1/auth/me 取，不从 token 里解。** token 只当凭证用；
 *    roles / status 每次请求由服务端从库里读（停用、改角色立即生效）。
 * 2. **视角（ViewMode）是身份的推论，不是开关。** 有 supplier_id 就是供应商侧，
 *    只有 annotator 角色就是标注员视角 —— 由 deriveMode() 算出来，
 *    界面上不再提供「切换视角」按钮。
 * 3. **未改初始密码时只放行 /change-password。** 服务端也会拦（403
 *    password_change_required），前端在这里先跳，避免用户看到一堆报错。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { ViewMode } from "../design/ui";
import type { Principal, RoleCode } from "./types";
import {
  ApiError,
  clearToken,
  get,
  getToken,
  isBackendConfigured,
  post,
  setToken,
  setUnauthorizedHandler,
} from "./http";

/* ----------------------------- 服务端返回形状 ----------------------------- */

interface MeResponse {
  id: string;
  name: string;
  email: string;
  phone: string;
  roles: string[];
  tenant_id: string;
  supplier_id: string | null;
  must_change_password: boolean;
  last_login_at: string | null;
}

interface LoginResponse {
  ok: boolean;
  access_token: string;
  token_type: string;
  expires_at: number;
  must_change_password: boolean;
  user: { id: string; name: string; phone: string; roles: string[] };
}

interface ChangePasswordResponse {
  ok: boolean;
  access_token: string;
  expires_at: number;
  message: string;
}

/* ------------------------------- 角色 → 文案 ------------------------------ */

const ROLE_LABEL: Record<string, string> = {
  platform_admin: "平台管理员",
  ops_director: "运营总监",
  finance: "财务",
  project_manager: "项目经理",
  data_engineer: "数据工程师",
  reviewer: "审核员",
  supplier_admin: "供应商管理员",
  supplier_lead: "供应商组长",
  annotator: "标注员",
  client_viewer: "客户观察员",
};
const ROLE_ORDER = Object.keys(ROLE_LABEL);

/** 取「最能代表这个人的」一个角色作为职务文案 */
export function primaryRoleTitle(roles: string[]): string {
  const sorted = [...roles].sort(
    (a, b) => ROLE_ORDER.indexOf(a) - ROLE_ORDER.indexOf(b)
  );
  const first = sorted.find((r) => ROLE_LABEL[r]) ?? roles[0] ?? "";
  return ROLE_LABEL[first] ?? first;
}

/**
 * 视角由身份推导：
 *   只有标注类角色            → annotator（只看自己的任务，有没有绑定供应商都一样）
 *   有 supplier_id            → supplier（只看本供应商）
 *   否则                       → internal
 * ★ 纯标注员判定必须在 supplierId 判定**之前**：否则一个还没绑定
 *   供应商账号的标注员会被判成 internal，界面上又冒出分配视图 ——
 *   数据面有 RLS 兜底不泄露，但"标注员看不到分配关系"的需求就破了。
 */
export function deriveMode(me: Principal): ViewMode {
  if (me.roles.length === 1 && me.roles[0] === "annotator") return "annotator";
  if (!me.supplierId) return "internal";
  return "supplier";
}

function toPrincipal(m: MeResponse): Principal {
  const roles = m.roles.filter((r): r is RoleCode => ROLE_ORDER.includes(r));
  return {
    id: m.id,
    tenantId: m.tenant_id,
    name: m.name || m.phone || "未命名",
    // PrincipalKind 只有 employee / supplier_member / annotator / reviewer / client，没有 "internal"：内部员工就是 employee。
    // 纯标注员直接给 annotator kind，与 deriveMode 同一口径（别再依赖 supplier_id）。
    kind:
      roles.length === 1 && roles[0] === "annotator"
        ? "annotator"
        : m.supplier_id
          ? "supplier_member"
          : "employee",
    roles: roles.length ? roles : ["annotator"],
    supplierId: m.supplier_id,
    // email 只用于展示，真实登录名是手机号
    email: m.email || m.phone,
    title: primaryRoleTitle(m.roles),
    initials: (m.name || "?").slice(0, 1),
  };
}

/* --------------------------------- Context -------------------------------- */

export type AuthStatus = "loading" | "anon" | "authed" | "must_change";

export interface AuthState {
  status: AuthStatus;
  me: Principal | null;
  /** 手机号掩码，如 138****8000 */
  phone: string;
  mustChangePassword: boolean;
  /** 服务端是否可达；false 时登录页给出明确的配置提示而不是「密码错误」 */
  backendReady: boolean;
  login: (phone: string, password: string) => Promise<void>;
  logout: () => void;
  changePassword: (oldPwd: string, newPwd: string) => Promise<string>;
  reload: () => Promise<void>;
}

const AuthCtx = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const v = useContext(AuthCtx);
  if (!v) throw new Error("AuthCtx missing");
  return v;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [me, setMe] = useState<Principal | null>(null);
  const [phone, setPhone] = useState("");
  const [mustChange, setMustChange] = useState(false);

  const applyMe = useCallback((m: MeResponse) => {
    setMe(toPrincipal(m));
    setPhone(m.phone);
    setMustChange(m.must_change_password);
    setStatus(m.must_change_password ? "must_change" : "authed");
  }, []);

  const reset = useCallback(() => {
    setMe(null);
    setPhone("");
    setMustChange(false);
    setStatus("anon");
  }, []);

  const load = useCallback(async () => {
    if (!getToken()) {
      reset();
      return;
    }
    try {
      applyMe(await get<MeResponse>("/api/v1/auth/me"));
    } catch (e) {
      // 401 = token 失效（改密/停用/过期）；其它错误（后端挂掉）不该悄悄登出，
      // 否则运维看到的现象是「所有人都被踢了」，而真实原因是 502。
      if (e instanceof ApiError && e.status === 401) {
        clearToken();
        reset();
      } else {
        reset();
      }
    }
  }, [applyMe, reset]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      clearToken();
      reset();
    });
    void load();
    return () => setUnauthorizedHandler(null);
  }, [load, reset]);

  const login = useCallback(
    async (rawPhone: string, password: string) => {
      const r = await post<LoginResponse>("/api/v1/auth/login", {
        phone: rawPhone,
        password,
      });
      setToken(r.access_token);
      // 不回填登录响应里的 user：那只是登录瞬间的快照（无角色过滤、无职务），
      // 统一走 /me 拿权威身份，少一条「两处字段不一致」的隐患。
      await load();
    },
    [load]
  );

  const logout = useCallback(() => {
    clearToken();
    reset();
  }, [reset]);

  const changePassword = useCallback(
    async (oldPwd: string, newPwd: string) => {
      const r = await post<ChangePasswordResponse>("/api/v1/auth/change-password", {
        old_password: oldPwd,
        new_password: newPwd,
      });
      // 改密会 bump token_version，旧 token 立刻失效 —— 必须换用服务端回的新 token，
      // 否则当前标签页下一次请求就被登出了。
      setToken(r.access_token);
      await load();
      return r.message;
    },
    [load]
  );

  const value = useMemo<AuthState>(
    () => ({
      status,
      me,
      phone,
      mustChangePassword: mustChange,
      backendReady: isBackendConfigured(),
      login,
      logout,
      changePassword,
      reload: load,
    }),
    [status, me, phone, mustChange, login, logout, changePassword, load]
  );

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
