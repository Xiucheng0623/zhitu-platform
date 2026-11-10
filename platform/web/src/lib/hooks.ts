import { useMemo } from "react";
import { useSession } from "../design/ui";
import { useStore } from "./store";

/**
 * 视角作用域（View Scope）
 * 前端只负责呈现收敛；同一规则在 orchestrator/rbac.py 中再次执行，后者为准。
 */
export function useScoped() {
  const store = useStore();
  const { me, mode } = useSession();

  return useMemo(() => {
    if (mode === "internal") {
      return { ...store, mode, me, supplierId: null as string | null };
    }
    if (mode === "supplier" && me.supplierId) {
      const sid = me.supplierId;
      return {
        ...store,
        projects: store.projects.filter((p) => p.supplierIds.includes(sid)),
        tasks: store.tasks.filter((t) => t.supplierId === sid),
        members: store.members.filter((m) => m.supplierId === sid),
        settlements: store.settlements.filter((s) => s.supplierId === sid),
        suppliers: store.suppliers.filter((s) => s.id === sid),
        performance: store.performance.filter((p) => p.supplierId === sid),
        mode,
        me,
        supplierId: sid,
      };
    }
    // annotator：我的任务 = 派给我的。比较必须走账号 id 映射：
    // assigneeUserId 是花名册成员绑定的登录账号（usr_*），与 me.id 同体系；
    // assigneeId 是花名册成员 id（mem_*），拿它直比 me.id 恒不等，
    // 会把 RLS 已收窄的「我的批次」滤成 0 条（实测踩过）。
    // 兼容旧行（assignee 直接存 usr_*）时 assigneeId === me.id 也认。
    const myTasks = store.tasks.filter(
      (t) => t.assigneeUserId === me.id || t.assigneeId === me.id
    );
    const pids = new Set(myTasks.map((t) => t.projectId));
    return {
      ...store,
      projects: store.projects.filter((p) => pids.has(p.id)),
      tasks: myTasks,
      members: [],
      settlements: [],
      suppliers: me.supplierId ? store.suppliers.filter((s) => s.id === me.supplierId) : [],
      performance: [],
      mode,
      me,
      supplierId: me.supplierId,
    };
  }, [store, me, mode]);
}
