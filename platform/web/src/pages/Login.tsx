import { useEffect, useState } from "react";
import { BrandMark, Icon } from "../design/icons";
import { Badge, Button, Field, TextInput, toast } from "../design/ui";
import { useAuth } from "../lib/auth";
import { ApiError, post } from "../lib/http";
import { ssoStatus, startSso, type SsoStatus } from "../lib/sso";

type Tab = "login" | "register";

/**
 * 登录 / 注册
 * ---------------------------------------------------------------------------
 * 登录名是**手机号**，不是邮箱 —— 与后端 forge.users.phone（E.164）一致。
 * 输入侧不做严格校验：13800138000 / 138 0013 8000 / +86 138… 都收，
 * 规范化是服务端 normalize_phone() 的职责，前端只挡明显为空的情况。
 * 前端也校验一遍格式的话，两边规则一旦漂移就会出现「前端说可以、后端说不行」。
 *
 * 注册走邀请码 + 管理员激活：内部系统不该对公网开放自由注册。
 */
export default function Login() {
  const auth = useAuth();
  const [tab, setTab] = useState<Tab>("login");

  const [phone, setPhone] = useState("");
  const [pwd, setPwd] = useState("");
  const [name, setName] = useState("");
  const [invite, setInvite] = useState("");

  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  // null = 还没问完，先不显示按钮，避免"闪一下又消失"
  const [sso, setSso] = useState<SsoStatus | null>(null);

  useEffect(() => {
    let alive = true;
    void ssoStatus().then((s) => {
      if (alive) setSso(s);
    });
    return () => {
      alive = false;
    };
  }, []);

  const reset = () => {
    setErr("");
  };

  /**
   * 跳去集团 IdP。return_to 用**相对路径**：它由浏览器按当前源解析，
   * 换域名（本地 127.0.0.1 / 生产 https://domain）都不用改代码；
   * 服务端也只接受同源地址，绝不放行任意外部 URL。
   */
  const doSso = async () => {
    setBusy(true);
    reset();
    try {
      const url = await startSso("/#/sso/callback");
      window.location.assign(url);
    } catch (e) {
      setErr(
        e instanceof ApiError
          ? e.message
          : "无法连接企业身份服务，请使用手机号登录或联系管理员"
      );
      setBusy(false);
    }
  };

  const doLogin = async () => {
    if (!phone.trim()) return setErr("请输入手机号");
    if (!pwd) return setErr("请输入密码");
    setBusy(true);
    reset();
    try {
      await auth.login(phone, pwd);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const doRegister = async () => {
    if (!phone.trim()) return setErr("请输入手机号");
    if (pwd.length < 8) return setErr("密码至少 8 位");
    if (!invite.trim()) return setErr("请输入邀请码");
    setBusy(true);
    reset();
    try {
      await post("/api/v1/auth/register", {
        phone: phone.trim(),
        password: pwd,
        name: name.trim(),
        invite_code: invite.trim(),
      });
      toast("注册已提交，等待管理员激活后即可登录");
      setTab("login");
      setPwd("");
      setInvite("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = () => (tab === "login" ? doLogin() : doRegister());

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
            把原始数据，
            <br />
            做成<em>可交付的训练资产</em>。
          </h1>
          <p className="login__desc">
            项目立项、供应商协同、多模态标注、多级质检、交付验收与结算对账，
            在同一条生产链路上完成。每一批数据的归属、经手人与状态变更均留痕可追溯。
          </p>
        </div>
      </div>

      <div className="login__right">
        <div className="login__box">
          <h2 className="login__form-title">{tab === "login" ? "登录工作台" : "凭邀请码注册"}</h2>
          <p className="login__form-sub">
            {tab === "login"
              ? "使用手机号登录。首次登录需修改初始密码。"
              : "注册后需管理员激活，激活前无法登录。"}
          </p>

          <div className="stack" style={{ gap: 14 }}>
            <Field label="手机号" required>
              <TextInput
                value={phone}
                onChange={(v) => {
                  setPhone(v);
                  reset();
                }}
                placeholder="13800138000"
                inputMode="tel"
                autoComplete="username"
                name="phone"
                maxLength={20}
                autoFocus
              />
            </Field>

            {tab === "register" && (
              <Field label="姓名" hint="用于审核记录与绩效归属">
                <TextInput
                  value={name}
                  onChange={(v) => {
                    setName(v);
                    reset();
                  }}
                  placeholder="张伟"
                  maxLength={32}
                />
              </Field>
            )}

            <Field
              label="密码"
              required
              error={err || undefined}
              hint={tab === "login" ? "连续 5 次失败将锁定账号 15 分钟" : "至少 8 位，不能是纯数字"}
            >
              <TextInput
                value={pwd}
                onChange={(v) => {
                  setPwd(v);
                  reset();
                }}
                type="password"
                placeholder="••••••••"
                autoComplete={tab === "login" ? "current-password" : "new-password"}
                name="password"
                onKeyDown={(e) => e.key === "Enter" && onSubmit()}
              />
            </Field>

            {tab === "register" && (
              <Field label="邀请码" required hint="向平台管理员索取">
                <TextInput
                  value={invite}
                  onChange={(v) => {
                    setInvite(v.toUpperCase());
                    reset();
                  }}
                  placeholder="10 位字母数字"
                  maxLength={24}
                  onKeyDown={(e) => e.key === "Enter" && onSubmit()}
                />
              </Field>
            )}

            <Button variant="primary" size="lg" block onClick={onSubmit} disabled={busy}>
              {busy ? "处理中…" : tab === "login" ? "登录" : "提交注册"}
            </Button>

            <div className="row" style={{ justifyContent: "center", gap: 6 }}>
              <span style={{ fontSize: 12.5, color: "var(--fk-ink-3)" }}>
                {tab === "login" ? "还没有账号？" : "已有账号？"}
              </span>
              <button
                className="fk-btn fk-btn--link"
                style={{ fontSize: 12.5 }}
                onClick={() => {
                  setTab(tab === "login" ? "register" : "login");
                  reset();
                }}
              >
                {tab === "login" ? "凭邀请码注册" : "返回登录"}
              </button>
            </div>
          </div>

          <div className="login__divider">{sso?.enabled ? "或使用企业账号" : "忘记密码"}</div>

          <div className="login__sso">
            {sso?.enabled && (
              <button onClick={doSso} disabled={busy} title="跳转到集团身份服务完成登录">
                <Icon name="lock" size={14} />
                用企业账号登录
                <Badge tone="outline">{sso.provider ?? "SSO"}</Badge>
              </button>
            )}
            {sso?.enabled && sso.error && (
              <div style={{ fontSize: 11, color: "var(--fk-danger-600, #C0392B)" }}>
                企业身份服务当前不可达：{sso.error}
              </div>
            )}
            <button
              onClick={() =>
                toast("请携工号联系平台管理员重置密码——重置后首次登录仍需改密")
              }
            >
              <Icon name="lock" size={14} />
              联系管理员重置
              <Badge tone="outline">无需邮箱</Badge>
            </button>
          </div>

          <p style={{ fontSize: 11, color: "var(--fk-ink-4)", marginTop: 22, lineHeight: 1.7 }}>
            登录即表示同意《数据处理协议》与《保密承诺》。供应商账号由本企业管理员开通，
            仅可访问授权项目范围内的数据。
          </p>
        </div>
      </div>
    </div>
  );
}
