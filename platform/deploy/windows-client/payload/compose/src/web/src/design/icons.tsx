import type { ReactNode } from "react";

/** 图标集 · 统一 24 视框、stroke 1.7，几何线条风格（不用面性图标，避免廉价感） */
const paths: Record<string, ReactNode> = {
  overview: (
    <>
      <rect x="3" y="3" width="7.5" height="7.5" rx="1" />
      <rect x="13.5" y="3" width="7.5" height="4.5" rx="1" />
      <rect x="3" y="13.5" width="7.5" height="7.5" rx="1" />
      <rect x="13.5" y="10.5" width="7.5" height="10.5" rx="1" />
    </>
  ),
  projects: (
    <>
      <path d="M3 7.5A2 2 0 0 1 5 5.5h3.6l1.8 2.2H19a2 2 0 0 1 2 2v8.8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      <path d="M3 11.5h18" />
    </>
  ),
  suppliers: (
    <>
      <path d="M3 21V9.5l6-3.5 6 3.5V21" />
      <path d="M15 12h3.2l2.8 3v6" />
      <path d="M7 21v-4.5h4V21" />
      <path d="M7 11.5h.01M11 11.5h.01" />
    </>
  ),
  portal: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="1.5" />
      <path d="M3 9h18M8 4v5" />
      <path d="M7.5 13.5h5v3.5h-5z" />
      <path d="M15.5 13.5H18M15.5 17H18" />
    </>
  ),
  people: (
    <>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M3 20v-1.6A4.4 4.4 0 0 1 7.4 14h3.2a4.4 4.4 0 0 1 4.4 4.4V20" />
      <path d="M16.2 5.2a3.2 3.2 0 0 1 0 5.9" />
      <path d="M17.6 14.2A4.4 4.4 0 0 1 21 18.4V20" />
    </>
  ),
  tasks: (
    <>
      <rect x="3.5" y="4.5" width="17" height="16" rx="1.5" />
      <path d="M8 3v3M16 3v3M3.5 9.5h17" />
      <path d="m8 14 1.8 1.8L13.5 12" />
      <path d="M15.5 14.2h3M15.5 17.4h3" />
    </>
  ),
  data: (
    <>
      <ellipse cx="12" cy="5.5" rx="8.5" ry="3" />
      <path d="M3.5 5.5v6c0 1.66 3.8 3 8.5 3s8.5-1.34 8.5-3v-6" />
      <path d="M3.5 11.5v6c0 1.66 3.8 3 8.5 3s8.5-1.34 8.5-3v-6" />
    </>
  ),
  annotate: (
    <>
      <path d="M12 20h9" />
      <path d="M16.4 3.6a2.1 2.1 0 0 1 3 3L7.5 18.5 3 20l1.5-4.5z" />
    </>
  ),
  review: (
    <>
      <path d="M12 21s8-3.6 8-9.6V5.4L12 2.5 4 5.4v6c0 6 8 9.6 8 9.6z" />
      <path d="m8.8 11.8 2.2 2.2 4.4-4.6" />
    </>
  ),
  perf: (
    <>
      <path d="M4 20V10.5M10 20V4M16 20v-6.5M22 20v-11" />
      <path d="M2 20h20" />
    </>
  ),
  settle: (
    <>
      <rect x="2.5" y="5" width="19" height="14" rx="1.8" />
      <path d="M2.5 10h19" />
      <path d="M6.5 14.5h4M16.5 14.5h1.5" />
    </>
  ),
  settings: (
    <>
      <path d="M4 6.5h5M13 6.5h7M4 12h9M17 12h3M4 17.5h3M11 17.5h9" />
      <circle cx="11" cy="6.5" r="2" />
      <circle cx="15" cy="12" r="2" />
      <circle cx="9" cy="17.5" r="2" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20.5 20.5-4.2-4.2" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  check: <path d="m20 6.5-10.5 10.5L4 11.5" />,
  x: <path d="M18 6 6 18M6 6l12 12" />,
  "chevron-down": <path d="m6 9.5 6 6 6-6" />,
  "chevron-right": <path d="m9.5 18 6-6-6-6" />,
  "chevron-left": <path d="m14.5 18-6-6 6-6" />,
  "arrow-up": <path d="M12 19V5m0 0-6.5 6.5M12 5l6.5 6.5" />,
  "arrow-down": <path d="M12 5v14m0 0 6.5-6.5M12 19l-6.5-6.5" />,
  "arrow-right": <path d="M5 12h14m0 0-6.5-6.5M19 12l-6.5 6.5" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3.2 2" />
    </>
  ),
  alert: (
    <>
      <path d="M10.3 3.9 2.6 17.1A1.9 1.9 0 0 0 4.3 20h15.4a1.9 1.9 0 0 0 1.7-2.9L13.7 3.9a1.9 1.9 0 0 0-3.4 0z" />
      <path d="M12 9v4M12 16.5h.01" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5M12 7.8h.01" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1.2" />
      <circle cx="12" cy="12" r="1.2" />
      <circle cx="19" cy="12" r="1.2" />
    </>
  ),
  filter: <path d="M3.5 5h17l-6.6 7.6V20l-3.8-2.2v-5.2z" />,
  download: (
    <>
      <path d="M20.5 15v3.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2V15" />
      <path d="M7.5 10.5 12 15l4.5-4.5M12 15V3.5" />
    </>
  ),
  upload: (
    <>
      <path d="M20.5 15v3.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2V15" />
      <path d="M7.5 7.5 12 3l4.5 4.5M12 3v11.5" />
    </>
  ),
  sync: (
    <>
      <path d="M20.5 12a8.5 8.5 0 0 1-14.6 5.9" />
      <path d="M3.5 12a8.5 8.5 0 0 1 14.6-5.9" />
      <path d="M18.5 2.5v4h-4M5.5 21.5v-4h4" />
    </>
  ),
  external: (
    <>
      <path d="M18 13.5V19a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2V8.5a2 2 0 0 1 2-2H11" />
      <path d="M15 3h6v6M10.5 13.5 21 3" />
    </>
  ),
  "log-out": (
    <>
      <path d="M9.5 21H5.5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 16.5 20.5 12 16 7.5M20.5 12H9" />
    </>
  ),
  bell: (
    <>
      <path d="M18 8.5a6 6 0 0 0-12 0c0 6-2.5 7.5-2.5 7.5h17S18 14.5 18 8.5" />
      <path d="M13.7 20a2 2 0 0 1-3.4 0" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 1.9-2.8 2.4-2.8 4" />
      <path d="M12 17.4h.01" />
    </>
  ),
  lock: (
    <>
      <rect x="4" y="10.5" width="16" height="10" rx="1.8" />
      <path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5" />
    </>
  ),
  menu: <path d="M3.5 7h17M3.5 12h17M3.5 17h17" />,
  calendar: (
    <>
      <rect x="3.5" y="5" width="17" height="15.5" rx="1.5" />
      <path d="M8 3v4M16 3v4M3.5 10h17" />
    </>
  ),
  "file-text": (
    <>
      <path d="M13.5 2.5H6.5a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V8.5z" />
      <path d="M13.5 2.5v6h6M8.5 13h7M8.5 17h4.5" />
    </>
  ),
  image: (
    <>
      <rect x="3" y="3.5" width="18" height="17" rx="1.8" />
      <circle cx="8.6" cy="9.2" r="1.6" />
      <path d="m20.5 16.5-5-5L5.5 20.5" />
    </>
  ),
  video: (
    <>
      <rect x="2.5" y="5.5" width="13.5" height="13" rx="1.8" />
      <path d="m16 11 5.5-3.2v8.4L16 13z" />
    </>
  ),
  audio: (
    <>
      <rect x="9" y="2.5" width="6" height="11.5" rx="3" />
      <path d="M18.5 11.5v1a6.5 6.5 0 0 1-13 0v-1M12 19v2.5" />
    </>
  ),
  layers: (
    <>
      <path d="m12 2.8 9 4.6-9 4.6-9-4.6z" />
      <path d="m3 12.4 9 4.6 9-4.6M3 16.9l9 4.6 9-4.6" />
    </>
  ),
  activity: <path d="M22 12h-4l-2.6 7L9 5l-2.6 7H2" />,
  "trending-up": (
    <>
      <path d="m22 6.5-8.5 8.5-5-5L2 16.5" />
      <path d="M16.5 6.5H22v5.5" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="4.5" />
      <circle cx="12" cy="12" r="0.8" />
    </>
  ),
  eye: (
    <>
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z" />
      <circle cx="12" cy="12" r="2.8" />
    </>
  ),
  edit: (
    <>
      <path d="M11 4.5H5.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2V13" />
      <path d="M17.8 2.9a2.05 2.05 0 0 1 2.9 2.9L12 14.5l-4 1 1-4z" />
    </>
  ),
  play: <path d="m6.5 3.5 13 8.5-13 8.5z" />,
  pause: <path d="M8.5 4v16M15.5 4v16" />,
  "help-circle": (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.5 9.5a2.6 2.6 0 0 1 5 .9c0 1.7-2.5 2.1-2.5 3.6M12 17h.01" />
    </>
  ),
  command: (
    <>
      <path d="M9 9h6v6H9z" />
      <path d="M9 9V6.5A2.5 2.5 0 1 0 6.5 9H9zM15 9V6.5A2.5 2.5 0 1 1 17.5 9H15zM9 15v2.5A2.5 2.5 0 1 1 6.5 15H9zM15 15v2.5a2.5 2.5 0 1 0 2.5-2.5H15z" />
    </>
  ),
  flag: (
    <>
      <path d="M4.5 21V3.5" />
      <path d="M4.5 4.5h10.2l-1.2 3.4 1.2 3.4H4.5" />
    </>
  ),
  box: (
    <>
      <path d="m12 2.8 8.5 4.4v9.6L12 21.2 3.5 16.8V7.2z" />
      <path d="M3.5 7.2 12 11.7l8.5-4.5M12 11.7V21.2" />
    </>
  ),
  wallet: (
    <>
      <path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h13a2 2 0 0 1 2 2v1" />
      <rect x="3.5" y="8.5" width="17" height="11" rx="1.8" />
      <path d="M16.5 14h2" />
    </>
  ),
};

export type IconName = keyof typeof paths;

export function Icon({
  name,
  size = 16,
  strokeWidth = 1.7,
  className,
}: {
  name: IconName | string;
  size?: number;
  strokeWidth?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {paths[name] ?? paths.info}
    </svg>
  );
}

/**
 * 品牌标识 · 智途
 * ---------------------------------------------------------------------------
 * 图形标是 AI 生成的位图（晶格棱柱 + 轨道弧），不是 SVG 手绘。
 * 源文件：brand/ai-logo/v2/…16-13-06.png
 * 导出流程见 brand/ai-logo/ 的处理脚本说明；产物在 public/brand/。
 *
 * 为什么分深/浅两个文件：
 *   位图不能像 SVG 那样继承 currentColor 反白。浅底用主紫 #7A5CD6，
 *   深底必须用更亮的 #9B82E8，否则压在石墨底上会发灰。
 *   全站目前品牌位都在深色底（登录左栏、改密页、侧栏），所以默认 dark。
 *
 * framed 参数保留是为了兼容旧调用点，但位图本身自带圆角底板形态，
 * 这里不再额外画框 —— favicon / apple-touch 直接用 public/brand/ 里的成品。
 */
export function BrandMark({
  size = 22,
  tone = "dark",
  framed: _framed = false,
}: {
  size?: number;
  tone?: "dark" | "light";
  /** @deprecated 位图版本不再需要额外画框 */
  framed?: boolean;
}) {
  const src = tone === "dark" ? "/brand/sy-mark-dark.png" : "/brand/sy-mark-light.png";
  return (
    <img
      src={src}
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      draggable={false}
      style={{ display: "block", objectFit: "contain" }}
    />
  );
}
