import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { BrandMark, Icon } from "../design/icons";
import { Button, Field, TextInput } from "../design/ui";
import { useAuth } from "../lib/auth";
import { ApiError, setToken } from "../lib/http";
import { ssoCallback, ssoLink } from "../lib/sso";

/**
 * 企业统一登录回调页
 * ---------------------------------------------------------------------------
 * 这个页面唯一的输入是 URL 上的 code 与 state，唯一的作用是把它们交给服务端。
 * **前端不做任何身份判断**：谁是管理员、该给什么角色，全部由服务端在
 * /sso/callback 里查库决定。前端只负责三件事：
 *   ok        → 存 token、重新拉 /me、进工作台
 *   need_link → 让本人输一次本地密码（证明这个本地账号本来就是他的）
 *   need_admin→ 如实说明"要管理员确认"，不给绕过入口
 *
 * StrictMode 下 effect 会跑两次，而 authorization code 是一次性的 ——
 * 第二次必然 401。所以用 ref 锁住，避免开发态自己把自己踢出去。
 */
type Phase = "verifying" | "need_link" | "need_admin" | "error";

export default function SsoCallback() {
  const auth = useAuth();
  const nav = useNavigate();
  const [params] = useSearchParams();

  const [phase, setPhase] = useState<Phase>("verifying");
  const [msg, setMsg] = useState("");
  const [linkToken, setLinkToken] = useState("");
  const [who, setWho] = useState("");
  const [pwd, setPwd] = useState("");
  const [busy, setBusy] = useState(false);
  const started = useRef(false);

  const finish = useCallback(
    async (token: string) => {
      setToken(token);
      // 统一走 /me 拿权威身份（角色随时可能被管理员调整过）
      await auth.reload();
      nav("/", { replace: true });
    },
    [auth, nav]
  );

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const idpError = params.get("error");
    if (idpError) {
      setPhase("error");
      setMsg(params.get("error_description") || idpError);
      return;
    }
    const code = params.get("code") || "";
    const state = params.get("state") || "";
    if (!code || !state) {
      setPhase("error");
      setMsg("回调缺少 code 或 state，请重新发起企业账号登录。");
      return;
    }

    void (async () => {
      try {
        const r = await ssoCallback(code, state);
        if ("access_token" in r) {
          await finish(r.access_token);
          return;
        }
        if ("link_token" in r) {
          setLinkToken(r.link_token);
          setWho([r.name, r.phone].filter(Boolean).join(" · "));
          setPhase("need_link");
          return;
        }
        setPhase("need_admin");
        setMsg(r.message);
      } catch (e) {
        setPhase("error");
        setMsg(e instanceof ApiError ? e.message : "网络异常，请稍后重试");
      }
    })();
  }, [params, finish]);

  const confirmLink = async () => {
    if (!pwd) return setMsg("请输入该账号的密码");
    setBusy(true);
    setMsg("");
    try {
      const r = await ssoLink(linkToken, pwd);
      await finish(r.access_token);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="login__left">
        <div className="login__grid" />
        <div className="login__glow" />
        <div className="login__brand">
          <span style={{ color: "#FAF9F7", display: "grid", placeContent: "center" }}>
            <BrandMark size={36} />
          </span>
          <div>
            <div className="login__brand-name">智途</div>
            <div className="login__brand-sub">AI 数据生产平台</div>
          </div>
        </div>

        <div className="login__hero">
          <h1 className="login__title">
            认证交给集团，
            <br />
            授权留在<em>本平台</em>。
          </h1>
          <p className="login__desc">
            身份由集团账号体系确认，但你能看到哪些项目、能审批哪笔款项，
            仍然由本平台的管理员授予。集团身份里的角色声明不会带进来。
          </p>
        </div>
      </div>

      <div className="login__right">
        <div className="login__box">

          {phase === "verifying" && (
            <>
              <h2 className="login__form-title">正在校验企业身份…</h2>
              <p className="login__form-sub">
                正在向集团身份服务核对你带回来的凭证，请不要关闭本页。
              </p>
            </>
          )}

          {phase === "need_link" && (
            <>
              <h2 className="login__form-title">确认这个账号是你的</h2>
              <p className="login__form-sub">
                {who ? `${who} ` : "该手机号"}
                在本平台已有一个本地账号。请输入那个账号的密码完成首次关联；
                我们不会仅凭集团侧的手机号就把账号并过去。
              </p>
              <div className="stack" style={{ gap: 14 }}>
                <Field
                  label="本地账号密码"
                  required
                  error={msg || undefined}
                  hint="关联成功后，以后可直接用企业账号登录"
                >
                  <TextInput
                    value={pwd}
                    onChange={(v) => {
                      setPwd(v);
                      setMsg("");
                    }}
                    type="password"
                    autoComplete="current-password"
                    autoFocus
                    placeholder="••••••••"
                    onKeyDown={(e) => e.key === "Enter" && confirmLink()}
                  />
                </Field>
                <Button
                  variant="primary"
                  size="lg"
                  block
                  onClick={confirmLink}
                  disabled={busy}
                >
                  {busy ? "关联中…" : "确认关联并登录"}
                </Button>
                <Button variant="ghost" block onClick={() => nav("/login", { replace: true })}>
                  返回普通登录
                </Button>
              </div>
            </>
          )}

          {phase === "need_admin" && (
            <>
              <h2 className="login__form-title">需要管理员确认</h2>
              <p className="login__form-sub">{msg}</p>
              <div className="stack" style={{ gap: 14 }}>
                <Button variant="primary" size="lg" block onClick={() => nav("/login", { replace: true })}>
                  返回登录页
                </Button>
              </div>
            </>
          )}

          {phase === "error" && (
            <>
              <h2 className="login__form-title">企业账号登录未完成</h2>
              <p className="login__form-sub" style={{ color: "var(--fk-danger-600, #C0392B)" }}>
                {msg}
              </p>
              <div className="stack" style={{ gap: 14 }}>
                <Button variant="primary" size="lg" block onClick={() => nav("/login", { replace: true })}>
                  返回登录页重试
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
