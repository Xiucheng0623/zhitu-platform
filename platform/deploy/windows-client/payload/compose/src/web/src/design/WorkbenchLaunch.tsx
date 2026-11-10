/**
 * 进入标注工作台的整页启动态。
 *
 * 用固定定位盖住整个视口（包括侧栏），让「点击 → 进入」是一次完整的
 * 页面交接，而不是在侧栏里开出一块小窗口。遮罩上只显示真实批次信息，
 * 不放任何编造的进度数字。
 */
import { BrandMark } from "./icons";
import { Button } from "./ui";

export function WorkbenchLaunch({
  batchLabel,
  error,
  onRetry,
  onBack,
}: {
  batchLabel?: string;
  error?: string;
  onRetry?: () => void;
  onBack?: () => void;
}) {
  return (
    <div className="wb-launch" role="status" aria-live="polite">
      <div className="wb-launch__glow" aria-hidden="true" />
      <div className="wb-launch__card">
        <div className="wb-launch__brand">
          <BrandMark size={44} />
          <span className="wb-launch__name">智途</span>
        </div>
        <div className="wb-launch__rule" />
        {error ? (
          <>
            <div className="wb-launch__status wb-launch__status--error">无法进入标注工作台</div>
            <p className="wb-launch__hint">{error}</p>
            <div className="wb-launch__actions">
              {onRetry && <Button size="sm" variant="secondary" onClick={onRetry}>重试</Button>}
              {onBack && <Button size="sm" variant="ghost" onClick={onBack}>返回平台</Button>}
            </div>
          </>
        ) : (
          <>
            <div className="wb-launch__status">正在进入标注工作台</div>
            <p className="wb-launch__hint">
              {batchLabel ? `${batchLabel} · 平台账号直通，无需二次登录` : "正在校验平台账号并打通标注会话…"}
            </p>
            <div className="wb-launch__bar" aria-hidden="true"><i /></div>
          </>
        )}
      </div>
      <div className="wb-launch__foot">智途 · AI 数据生产平台</div>
    </div>
  );
}
