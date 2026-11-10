/**
 * 标注工作台 · 整页入口
 * ============================================================================
 * 这里**不再有 iframe、不再有画布外壳**。平台只做两件事：选中批次、换一张
 * 一次性 SSO 票据，然后整页导航到品牌化的 Label Studio 工作台，标注器占满
 * 整个浏览器窗口。
 *
 * 为什么删掉了原来的左右面板：那 12 个 .jpg 条目、vehicle.car 标签体系、
 * SVG 画出来的道路场景和 BBox 全是本地生成的示例，与真库没有任何关系。
 * 把它们摆在一个真实标注器旁边，等于用假数据冒充作业现场。真实条目、
 * 标签、标注结果一律以标注器内 LS 的数据为准，平台侧不复制、不伪造。
 *
 * 本页同时是深链接入口：#/annotate?task=<批次id> 可直接进指定批次；
 * 不带参数时自动选当前可标注批次。侧栏点击由 AppShell 直接拦截并启动，
 * 连这个中转页都不会停留。
 */
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { WorkbenchLaunch } from "../design/WorkbenchLaunch";
import { useStore } from "../lib/store";
import { enterWorkbench, pickWorkbenchTask } from "../lib/workbench";

export default function Annotate() {
  const loc = useLocation();
  const nav = useNavigate();
  const { tasks, dataSource } = useStore();
  const [error, setError] = useState("");
  const [round, setRound] = useState(0);
  const started = useRef(false);

  const explicit = new URLSearchParams(loc.search).get("task");

  useEffect(() => {
    // StrictMode 下 effect 会跑两次；票据是一次性的，必须只发一次。
    if (started.current) return;

    // 深链接已经给了批次 id，不必等列表——票据接口自己会按 RLS 校验权限。
    if (explicit) {
      started.current = true;
      void enterWorkbench(explicit).catch((e: unknown) => {
        started.current = false;
        setError(e instanceof Error ? e.message : String(e));
      });
      return;
    }

    // 没给 id：等真库列表拉完再挑，绝不在等待期间用示例数据凑一个批次。
    if (dataSource === "loading") return;
    const task = pickWorkbenchTask(tasks);
    if (!task) {
      setError("当前账号下没有已关联标注项目的批次。请先在任务管理里创建批次并同步标注项目。");
      return;
    }
    started.current = true;
    void enterWorkbench(task.id).catch((e: unknown) => {
      started.current = false;
      setError(e instanceof Error ? e.message : String(e));
    });
  }, [explicit, tasks, dataSource, round]);

  return (
    <WorkbenchLaunch
      batchLabel={tasks.find((t) => t.id === (explicit ?? ""))?.name}
      error={error}
      onRetry={error ? () => { setError(""); setRound((r) => r + 1); } : undefined}
      onBack={error ? () => nav("/tasks") : undefined}
    />
  );
}
