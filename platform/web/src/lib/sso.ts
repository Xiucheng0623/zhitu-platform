/**
 * 企业统一登录（OIDC Relying Party 的前端一侧）
 * ---------------------------------------------------------------------------
 * 这里只做三件事：问服务端"能不能用"、把浏览器送到 IdP、把 IdP 带回来的
 * code/state 交给服务端。**不解析 id_token，不读 claim，不判断角色** ——
 * 那些决定权在服务端，前端多解一次只是多一处可以骗自己的地方。
 *
 * 流程：
 *   浏览器 → 本服务 /sso/start 拿 authorize_url（内含 PKCE challenge 与自签 state）
 *          → 跳到对方 IdP 登录
 *          → IdP 带 code+state 回到 /#/sso/callback
 *          → 本服务 /sso/callback 用 code 换 id_token、验签、查库、签自己的 token
 */
import { get, post } from "./http";

export interface SsoStatus {
  enabled: boolean;
  provider?: string;
  /** 配置齐全但 IdP 不可达时的原因，仅用于展示，不外泄 issuer/client_id */
  error?: string;
}

export interface SsoLoginPayload {
  ok: true;
  access_token: string;
  expires_at: number;
  via: "sso";
  user: { id: string; name: string; phone: string; roles: string[] };
}

export interface SsoNeedLink {
  ok: false;
  need_link: true;
  link_token: string;
  name: string;
  phone: string;
  message: string;
}

export interface SsoNeedAdmin {
  ok: false;
  need_admin: true;
  message: string;
}

export type SsoCallbackResult = SsoLoginPayload | SsoNeedLink | SsoNeedAdmin;

/** 未启用/不可达一律返回 enabled:false，调用方据此隐藏按钮，不用 try/catch 兜 */
export async function ssoStatus(): Promise<SsoStatus> {
  try {
    return await get<SsoStatus>("/api/v1/auth/sso/status");
  } catch {
    return { enabled: false };
  }
}

/** 拿授权地址。真正的跳转由调用方做（window.location.assign） */
export async function startSso(returnTo: string): Promise<string> {
  const r = await post<{ ok: boolean; authorize_url: string }>(
    "/api/v1/auth/sso/start",
    { return_to: returnTo }
  );
  return r.authorize_url;
}

export function ssoCallback(code: string, state: string) {
  return post<SsoCallbackResult>("/api/v1/auth/sso/callback", { code, state });
}

export function ssoLink(linkToken: string, password: string) {
  return post<SsoLoginPayload>("/api/v1/auth/sso/link", {
    link_token: linkToken,
    password,
  });
}
