import { useState } from "react";
import { BrandMark, Icon } from "../design/icons";
import { Button, Field, TextInput, toast } from "../design/ui";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/http";

/**
 * 强制改密页
 * ---------------------------------------------------------------------------
 * 触发场景只有两个：首次登录（含邀请码注册激活后）、管理员重置密码后。
 * 服务端对所有其它接口返回 403 password_change_required，前端在 App.tsx
 * 的 Gate 里把路由钉死在这一页，所以这里不需要判断「我该不该在这」。
 *
 * 改密成功后服务端回一个新 token（token_version 已 +1，旧 token 全部作废），
 * auth.changePassword 会把它写进 localStorage，因此当前标签页不会被踢。
 */
export default function ChangePassword() {
  const auth = useAuth();
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!oldPwd) return setErr("请输入当前密码");
    if (newPwd.length < 8) return setErr("新密码至少 8 位");
    if (newPwd !== confirm) return setErr("两次输入的新密码不一致");
    setBusy(true);
    setErr("");
    try {
      const msg = await auth.changePassword(oldPwd, newPwd);
      toast(msg);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "网络异常，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const strength = scorePassword(newPwd);

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
            先改密码，
            <br />
            再进<em>生产链路</em>。
          </h1>
          <p className="login__desc">
            当前密码来自初始开通或管理员重置，属于可预测凭证。
            修改后系统会让其它设备上的登录立即失效，包括旧的浏览器标签页。
          </p>
        </div>
      </div>

      <div className="login__right">
        <div className="login__box">

          <h2 className="login__form-title">设置新密码</h2>
          <p className="login__form-sub">改密后即可进入工作台</p>

          <div className="stack" style={{ gap: 14 }}>
            <Field label="当前密码" required>
              <TextInput
                value={oldPwd}
                onChange={(v) => {
                  setOldPwd(v);
                  setErr("");
                }}
                type="password"
                autoComplete="current-password"
                autoFocus
                placeholder="管理员提供的初始密码"
              />
            </Field>

            <Field
              label="新密码"
              required
              hint="至少 8 位；建议由两个以上不相关词组成，不必刻意加符号"
            >
              <TextInput
                value={newPwd}
                onChange={(v) => {
                  setNewPwd(v);
                  setErr("");
                }}
                type="password"
                autoComplete="new-password"
                placeholder="••••••••••"
              />
            </Field>

            {newPwd && (
              <div style={{ display: "flex", gap: 4 }}>
                {[0, 1, 2, 3].map((i) => (
                  <span
                    key={i}
                    style={{
                      height: 3,
                      flex: 1,
                      borderRadius: 999,
                      background:
                        i < strength
                          ? strength <= 2
                            ? "var(--fk-accent-500)"
                            : "var(--fk-success-600)"
                          : "var(--fk-line)",
                    }}
                  />
                ))}
              </div>
            )}

            <Field label="确认新密码" required error={err || undefined}>
              <TextInput
                value={confirm}
                onChange={(v) => {
                  setConfirm(v);
                  setErr("");
                }}
                type="password"
                autoComplete="new-password"
                placeholder="再输入一次"
                onKeyDown={(e) => e.key === "Enter" && submit()}
              />
            </Field>

            <Button variant="primary" size="lg" block onClick={submit} disabled={busy}>
              {busy ? "提交中…" : "修改密码并进入工作台"}
            </Button>

            <Button variant="ghost" block onClick={auth.logout}>
              换一个账号登录
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 粗略强度：长度 + 字符类别数。不追求精确，只给人一个即时反馈 */
function scorePassword(p: string): number {
  if (!p) return 0;
  let s = 0;
  if (p.length >= 8) s++;
  if (p.length >= 12) s++;
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^a-zA-Z0-9]/].filter((r) => r.test(p)).length;
  if (classes >= 2) s++;
  if (classes >= 3) s++;
  return Math.min(4, s);
}
