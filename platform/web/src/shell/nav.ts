import type { IconName } from "../design/icons";

export interface NavItem {
  key: string;
  path: string;
  label: string;
  icon: IconName;
  /** 仅这些视角可见 */
  scope?: Array<"internal" | "supplier" | "annotator">;
  hint?: string;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "生产",
    items: [
      { key: "overview", path: "/", label: "工作台", icon: "overview", scope: ["internal", "supplier", "annotator"] },
      { key: "projects", path: "/projects", label: "项目管理", icon: "projects", scope: ["internal", "supplier"] },
      // 同一个路由、两个入口名：管理者看的是全公司批次（任务管理），
      // 标注员看的只有分配给自己的（我的任务），页内按视角裁剪内容。
      { key: "tasks", path: "/tasks", label: "任务管理", icon: "tasks", scope: ["internal", "supplier"] },
      { key: "my-tasks", path: "/tasks", label: "我的任务", icon: "tasks", scope: ["annotator"] },
      { key: "data", path: "/data", label: "数据管理", icon: "data", scope: ["internal", "supplier"] },
      { key: "annotate", path: "/annotate", label: "标注工作台", icon: "annotate", scope: ["internal", "annotator"] },
      { key: "review", path: "/review", label: "审核中心", icon: "review", scope: ["internal"] },
    ],
  },
  {
    label: "协作",
    items: [
      { key: "suppliers", path: "/suppliers", label: "供应商管理", icon: "suppliers", scope: ["internal"] },
      { key: "portal", path: "/portal", label: "供应商门户", icon: "portal", scope: ["internal", "supplier"] },
      { key: "people", path: "/people", label: "人员管理", icon: "people", scope: ["internal", "supplier"] },
    ],
  },
  {
    label: "经营",
    items: [
      { key: "performance", path: "/performance", label: "绩效中心", icon: "perf", scope: ["internal", "supplier"] },
      { key: "settlement", path: "/settlement", label: "结算中心", icon: "settle", scope: ["internal", "supplier"] },
    ],
  },
  {
    label: "系统",
    items: [
      { key: "settings", path: "/settings", label: "系统设置", icon: "settings", scope: ["internal"] },
    ],
  },
];

export const FLAT_NAV = NAV_GROUPS.flatMap((g) => g.items);
