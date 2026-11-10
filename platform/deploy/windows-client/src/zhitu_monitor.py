#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智途 · Windows 本地监控台。

它解决什么
----------------------------------------------------------------------------
容器跑在 WSL 2 里，Windows 这边看不到进程，出问题时只有两个线索：
"网页打不开"和"不知道该怎么办"。这个客户端把三层状态摊平到一张界面上：
  1. 宿主机：WSL / 发行版 / Docker Engine 是否在；
  2. 容器层：每个服务是 running 还是 exited、健康检查过没过；
  3. 服务层：HTTP 健康端点是否真的回 200（容器 running ≠ 服务可用）。
三层任何一层断了，页面都会指出是哪一层 —— 省掉"重启试试"这种瞎猜。

刻意不做的事
----------------------------------------------------------------------------
* 不做托盘常驻守护：Windows 上没有可靠的"后台进程自愈"机制，做半个反而
  让人以为它在保护你。真要看住它就用计划任务（installer 里可选）。
* 不内置 docker.exe：容器引擎在 WSL 里，客户端只调用 wsl.exe 转发。
* 不缓存"上次还好"的状态：每次刷新都重新探，界面上的绿灯只能是这一次真的通。

用法
----------------------------------------------------------------------------
    智途监控.exe                 # 打开监控台
    智途监控.exe --status        # 打印一次状态后退出（脚本/排障用）
    智途监控.exe --selftest      # 自检
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zhitu_common as C  # noqa: E402

BG = "#14161A"
BG2 = "#1A1D22"
BG3 = "#22262C"
FG = "#E8E6E3"
FG_DIM = "#9BA1AA"
ACCENT = "#9B82E8"
OK_C = "#5FBF8B"
WARN_C = "#E0A340"
ERR_C = "#E0685C"
LINE = "#333941"

REFRESH_MS = 5000


def project_dir() -> Path:
    cfg = C.load_config()
    return Path(cfg.get("install_dir") or C.DEFAULT_INSTALL_DIR) / "deploy"


def distro() -> str:
    return str(C.load_config().get("distro") or C.DEFAULT_DISTRO)


def host_ip() -> str:
    """员工访问用的服务器地址。安装时写进 config.json，换网段要重跑安装第 06 步。"""
    return str(C.load_config().get("host_ip") or C.local_ip() or "127.0.0.1")


def images_ready(d: str) -> bool:
    """三个自建镜像是否都在本地。

    决定「启动」按钮要不要带 --no-build：本地有镜像时带它，启动是秒级且
    **完全不碰外网**；没有镜像（首装还没 build 过）时不能带，否则 compose
    会直接报 "image not found"，看起来像栈坏了。
    """
    rc, out = C.run(["wsl.exe", "-d", d, "-u", "root", "--", "docker", "images",
                     "--format", "{{.Repository}}:{{.Tag}}"], timeout=60)
    if rc != 0:
        return False
    have = {ln.strip() for ln in (out or "").splitlines() if ln.strip()}
    need = ("zhitu/web", "zhitu/orchestrator", "zhitu/labelstudio")
    return all(any(h.split(":")[0] == n for h in have) for n in need)


def collect_status() -> Dict[str, object]:
    """一次性收齐三层状态。所有探测都在这里做，界面只负责画。"""
    st: Dict[str, object] = {
        "at": time.strftime("%H:%M:%S"),
        "distro": distro(),
        "project": str(project_dir()),
        "wsl_ok": False, "docker_ok": False, "docker_ver": "",
        "containers": [], "services": [], "error": "",
    }
    d = distro()
    st["wsl_ok"] = C.wsl_distro_ready(d)
    if st["wsl_ok"]:
        ok, ver = C.docker_in_wsl(d)
        st["docker_ok"], st["docker_ver"] = ok, ver
    if st["docker_ok"]:
        try:
            st["containers"] = C.compose_ps(d, str(project_dir()))
        except Exception as exc:  # noqa: BLE001
            st["error"] = str(exc)[:200]
    st["services"] = C.stack_health()
    return st


def summarize(st: Dict[str, object]) -> str:
    if not st.get("wsl_ok"):
        return "WSL 未就绪"
    if not st.get("docker_ok"):
        return "Docker 引擎未运行"
    svc = st.get("services") or []
    bad = [s for s in svc if not s["ok"]]
    if not bad:
        return "全部正常"
    return "%d 个服务不可达" % len(bad)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class MonitorApp:
    def __init__(self) -> None:
        self.s = C.setup_dpi()
        self.root = tk.Tk()
        self.root.title("智途 · 监控台")
        self.root.configure(bg=BG)
        self.root.geometry("%dx%d" % (int(1020 * self.s), int(700 * self.s)))
        self.root.minsize(int(940 * self.s), int(640 * self.s))

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.last: Dict[str, object] = {}
        self._auto = tk.BooleanVar(value=True)

        self._style()
        self._ui()
        self._fit_window(1020, 700)
        self.root.after(200, self._drain)
        self.refresh()

    def _fit_window(self, w: int, h: int) -> None:
        """按内容的实际需求尺寸兜底放大窗口，但**绝不超过屏幕**。

        只写死 geometry 会出事：字号按 DPI 放大后内容变高，而窗口高度还是
        按逻辑像素给的，底栏按钮就被裁在窗口外（本次实测：一整排操作键
        看不见，等于功能缺失）。这里取「期望尺寸」与「内容需求」的较大者，
        再夹到屏幕工作区内 —— 150% 缩放的 1920x1080 上，内容需求会到 1233px，
        不夹的话窗口比屏幕还高，底栏永远看不到。
        """
        self.root.update_idletasks()
        need_w = self.root.winfo_reqwidth()
        need_h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry("%dx%d" % (min(max(int(w * self.s), need_w), sw - self.px(24)),
                                      min(max(int(h * self.s), need_h),
                                          sh - self.px(80))))
        self.root.minsize(min(int(880 * self.s), sw - self.px(24)),
                          min(int(600 * self.s), sh - self.px(80)))

    # -- 布局 -------------------------------------------------------------
    # 字号/像素都按 DPI 缩放，否则 150% 的笔记本上界面像缩略图
    def f(self, size: int, bold: bool = False, mono: bool = False) -> tuple:
        fam = "Consolas" if mono else "Microsoft YaHei UI"
        px = max(8, int(round(size * self.s)))
        return (fam, px, "bold" if bold else "normal")

    def px(self, n: int) -> int:
        return max(1, int(round(n * self.s)))

    def _style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                     bordercolor=LINE, font=self.f(9))
        st.configure("TButton", background=BG3, foreground=FG, padding=(12, 6))
        st.map("TButton", background=[("active", "#2C3138"), ("disabled", BG2)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#14161A",
                     font=self.f(9, bold=True))
        st.map("Accent.TButton", background=[("active", "#AD97EE"),
                                             ("disabled", "#4A4560")])
        st.configure("Treeview", background=BG2, fieldbackground=BG2,
                     foreground=FG, rowheight=self.px(30), borderwidth=0)
        st.configure("Treeview.Heading", background=BG3, foreground=FG_DIM,
                     relief="flat", font=self.f(9))
        st.configure("TCheckbutton", background=BG2, foreground=FG_DIM)

    def _ui(self) -> None:
        head = tk.Frame(self.root, bg=BG2, height=self.px(62))
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="智途", bg=BG2, fg=FG,
                 font=self.f(15, bold=True)).pack(side="left", padx=(self.px(20), self.px(6)))
        tk.Label(head, text="· 监控台", bg=BG2, fg=FG_DIM,
                 font=self.f(10)).pack(side="left")
        self.lbl_overall = tk.Label(head, text="正在检查…", bg=BG2, fg=FG_DIM,
                                    font=self.f(10, bold=True))
        self.lbl_overall.pack(side="right", padx=20)

        # 三张服务卡
        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x", padx=self.px(16), pady=(self.px(14), self.px(6)))
        self.cards: Dict[str, Dict[str, tk.Label]] = {}
        for name, port, _ in C.STACK_PORTS:
            box = tk.Frame(cards, bg=BG2, highlightbackground=LINE,
                           highlightthickness=1)
            box.pack(side="left", fill="both", expand=True, padx=(0, 10))
            tk.Label(box, text=name, bg=BG2, fg=FG_DIM,
                     font=self.f(9)).pack(anchor="w", padx=self.px(14),
                                          pady=(self.px(10), 0))
            val = tk.Label(box, text="—", bg=BG2, fg=FG_DIM,
                           font=self.f(17, bold=True))
            val.pack(anchor="w", padx=self.px(14), pady=(self.px(2), 0))
            sub = tk.Label(box, text="127.0.0.1:%d" % port, bg=BG2, fg=FG_DIM,
                           font=self.f(8, mono=True))
            sub.pack(anchor="w", padx=self.px(14), pady=(0, self.px(10)))
            self.cards[name] = {"val": val, "sub": sub}

        # 容器表
        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill="both", expand=True, padx=self.px(16), pady=(self.px(6), 0))
        tk.Label(mid, text="容器", bg=BG, fg=FG_DIM,
                 font=self.f(9)).pack(anchor="w", pady=(0, self.px(6)))
        cols = ("service", "state", "health", "image")
        self.tbl = ttk.Treeview(mid, columns=cols, show="headings", height=6)
        for c, t, w in [("service", "服务", 190), ("state", "状态", 110),
                        ("health", "健康", 100), ("image", "镜像", 480)]:
            self.tbl.heading(c, text=t)
            self.tbl.column(c, width=self.px(w), anchor="w")
        self.tbl.pack(fill="both", expand=True)

        # 日志
        low = tk.Frame(self.root, bg=BG)
        low.pack(fill="both", expand=True, padx=self.px(16), pady=(self.px(12), 0))
        bar = tk.Frame(low, bg=BG)
        bar.pack(fill="x")
        tk.Label(bar, text="最近日志", bg=BG, fg=FG_DIM,
                 font=self.f(9)).pack(side="left")
        self.lbl_meta = tk.Label(bar, text="", bg=BG, fg=FG_DIM,
                                 font=self.f(8, mono=True))
        self.lbl_meta.pack(side="right")
        wrap = tk.Frame(low, bg=LINE)
        wrap.pack(fill="both", expand=True, pady=(self.px(6), 0))
        self.log = tk.Text(wrap, bg="#0F1113", fg="#C9CFD6", bd=0, wrap="none",
                           width=1, font=self.f(9, mono=True), padx=self.px(10),
                           pady=self.px(8), height=9)
        sb = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        # 底栏：**两行**。
        # 单行塞不下（启动/停止/重启/备份/平台/标注 + 自动刷新 + 修复网络），
        # 而 tkinter 的 pack 在空间不足时会把最后 pack 的控件压成 0 宽 ——
        # 表现是按钮"根本没做"，不是显示不全。分两行后每行都有余量，
        # 且 1024 宽的旧显示器上也不会被挤掉。
        foot = tk.Frame(self.root, bg=BG2)
        # side="bottom" + before=cards：pack 是按**打包顺序**分配空间的，
        # 谁后 pack 谁先被压成 0。底栏必须比可伸缩的表格/日志先拿到空间，
        # 否则窗口一矮，最先消失的就是操作按钮。
        foot.pack(side="bottom", fill="x", pady=(self.px(10), 0), before=cards)
        pad = self.px(8)

        row1 = tk.Frame(foot, bg=BG2)
        row1.pack(fill="x", padx=self.px(14), pady=(self.px(8), self.px(4)))
        ttk.Button(row1, text="启动", style="Accent.TButton",
                   command=lambda: self._act("up")).pack(side="left")
        ttk.Button(row1, text="停止",
                   command=lambda: self._act("down")).pack(side="left", padx=(pad, 0))
        ttk.Button(row1, text="重启",
                   command=lambda: self._act("restart")).pack(side="left", padx=(pad, 0))
        ttk.Button(row1, text="打开平台",
                   command=lambda: self._open(C.platform_url(host_ip()))).pack(side="right")
        ttk.Button(row1, text="标注引擎",
                   command=lambda: self._open(C.ls_url(host_ip()))).pack(
                       side="right", padx=(0, pad))

        row2 = tk.Frame(foot, bg=BG2)
        row2.pack(fill="x", padx=self.px(14), pady=(0, self.px(8)))
        # 用**按钮**而不是 Checkbutton：clam 的复选框指示器在这里被画成一个
        # 10px 的小叉（150% 缩放下几乎看不清是勾还是空），而"自动刷新开没开"
        # 是运维判断界面数字新不新的依据，必须文字化。
        self.btn_auto = ttk.Button(row2, text="", command=self._toggle_auto)
        self.btn_auto.pack(side="left")
        self._paint_auto()
        ttk.Button(row2, text="立即刷新",
                   command=self.refresh).pack(side="left", padx=(pad, 0))
        ttk.Button(row2, text="修复网络",
                   command=lambda: self._act("net")).pack(side="left", padx=(pad, 0))
        # 「补凭证」放第二行左侧：它是装完才会用到的补救键，但一旦需要就是
        # 唯一能让平台数字重新跟着标注走的东西，不能藏在菜单里。
        ttk.Button(row2, text="补凭证",
                   command=lambda: self._act("token")).pack(side="left", padx=(pad, 0))
        ttk.Button(row2, text="备份",
                   command=lambda: self._act("backup")).pack(side="right")

        self.root.bind("<F5>", lambda _e: self.refresh())

    # -- 数据流 -----------------------------------------------------------
    def _auto_text(self) -> str:
        return "自动刷新：%s（%ds）" % ("开" if self._auto.get() else "关",
                                       REFRESH_MS // 1000)

    def _paint_auto(self) -> None:
        self.btn_auto.configure(text=self._auto_text())

    def _toggle_auto(self) -> None:
        self._auto.set(not self._auto.get())
        self._paint_auto()
        if self._auto.get():
            self.refresh()   # 刚打开就立刻取一次，不用等下一个周期

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self._paint(payload)
                    self.busy = False
                    # 自动刷新在**主线程**续期：子线程里 root.after 会抛
                    # "main thread is not in main loop"，而且失败是静默的。
                    if self._auto.get():
                        self.root.after(REFRESH_MS, self.refresh)
                elif kind == "log":
                    self._append(payload)
                elif kind == "msg":
                    messagebox.showinfo("提示", payload)
                    self.busy = False
                elif kind == "err":
                    messagebox.showerror("操作失败", payload)
                    self.busy = False
        except queue.Empty:
            pass
        self.root.after(150, self._drain)

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True

        def work() -> None:
            # 只做探测，只往队列写。**不要在这里读 self._auto 或调 root.after**：
            # tkinter 变量与 after() 都只能在主线程碰，从子线程读会抛
            # RuntimeError: main thread is not in main loop（实测踩到），
            # 而且它不一定当场炸，可能只是让自动刷新静默失效。
            try:
                st = collect_status()
            except Exception as exc:  # noqa: BLE001
                st = {"at": time.strftime("%H:%M:%S"), "wsl_ok": False,
                      "docker_ok": False, "containers": [], "services": [],
                      "error": str(exc)[:300]}
            self.q.put(("status", st))

        threading.Thread(target=work, daemon=True).start()

    def _paint(self, st: Dict[str, object]) -> None:
        self.last = st
        overall = summarize(st)
        ok = overall == "全部正常"
        self.lbl_overall.config(text=overall, fg=OK_C if ok else
                                (WARN_C if st.get("wsl_ok") else ERR_C))

        for s in st.get("services") or []:
            box = self.cards.get(s["name"])
            if not box:
                continue
            if s["ok"]:
                box["val"].config(text="正常", fg=OK_C)
                box["sub"].config(text="%d ms · 127.0.0.1:%d" % (s["ms"], s["port"]))
            else:
                box["val"].config(text="不可达", fg=ERR_C)
                box["sub"].config(text="code=%s · 127.0.0.1:%d" % (s["code"], s["port"]))

        self.tbl.delete(*self.tbl.get_children())
        for c in st.get("containers") or []:
            health = c.get("health") or "—"
            self.tbl.insert("", "end", values=(
                c.get("service") or c.get("name"), c.get("state"),
                health if health else "—", c.get("image")))

        d = st.get("distro") or distro()
        meta = "%s · 刷新于 %s · WSL %s · Docker %s" % (
            d, st.get("at"), "OK" if st.get("wsl_ok") else "×",
            (st.get("docker_ver") or ("OK" if st.get("docker_ok") else "×")))
        self.lbl_meta.config(text=meta)

        if st.get("error"):
            self._append("[状态采集] " + str(st["error"]))

    # -- 操作 -------------------------------------------------------------
    def _open(self, url: str) -> None:
        os.startfile(url)  # noqa: S606

    def _act(self, action: str) -> None:
        if self.busy:
            return
        self.busy = True
        d = distro()
        pdir = str(project_dir())
        self._append("")
        self._append("$ %s  (%s)" % (action, time.strftime("%H:%M:%S")))

        def work() -> None:
            try:
                if action == "net":
                    from zhitu_setup import _wsl_ip  # 复用同一套取 IP 逻辑
                    ok, ip = _wsl_ip(d)
                    if not ok:
                        self.q.put(("err", "拿不到 WSL 的 IP，无法修复端口映射。"))
                        return
                    self.q.put(("log", "WSL IP = %s" % ip))
                    n = 0
                    # 只映射**发布到宿主**的端口（80 平台入口 / 8100 标注引擎）。
                    # 编排层 8300、web 3000、minio 都是 compose 里的 expose，
                    # 宿主机根本没有它们，映射了也只会得到一条"连不上"的规则。
                    for port in C.HOST_PORTS:
                        rc, out = C.run(["netsh", "interface", "portproxy", "add",
                                         "v4tov4", "listenport=%d" % port,
                                         "listenaddress=0.0.0.0",
                                         "connectport=%d" % port,
                                         "connectaddress=%s" % ip], timeout=30)
                        if rc != 0:
                            self.q.put(("log", "端口 %d 失败：%s" % (port, out[:160])))
                            continue
                        C.run(["netsh", "advfirewall", "firewall", "add", "rule",
                               "name=Zhitu Port %d" % port, "dir=in", "action=allow",
                               "protocol=TCP", "localport=%d" % port], timeout=30)
                        n += 1
                    self.q.put(("msg", "已重建 %d 条端口映射（80 平台 / 8100 标注引擎）。\n"
                                       "WSL 的 IP 每次重启都会变，所以开机后要再点一次。"
                                % n))
                    return

                if action == "token":
                    from zhitu_setup import REMOTE_DIR, _sync_to_wsl
                    env = project_dir() / ".env"
                    cur = C.read_env(env)
                    email = cur.get("LS_ADMIN_EMAIL") or "admin@zhitu.local"
                    pw = cur.get("LS_ADMIN_PASSWORD") or ""
                    if not pw:
                        self.q.put(("err", ".env 里没有 LS_ADMIN_PASSWORD，无法自动登录。\n"
                                           "请用管理员账号登录标注引擎生成 token，"
                                           "填进 deploy\\.env 的 LS_REFRESH_TOKEN。"))
                        return
                    self.q.put(("log", "登录 LS 取 refresh token（%s）…" % email))
                    ok, res = C.fetch_ls_refresh(
                        "http://127.0.0.1:%d" % C.DEFAULT_LS_PORT, email, pw)
                    if not ok:
                        self.q.put(("err", "拿不到 LS 凭证：%s\n\n"
                                           "常见原因：LS 还没起、管理员密码被改过、"
                                           "或 .env 里的 LS_ADMIN_EMAIL 不对。" % res))
                        return
                    C.write_env_key(env, "LS_REFRESH_TOKEN", res)
                    self.q.put(("log", "已写入 .env（%s…）" % res[:8]))
                    rc, out = _sync_to_wsl(env.parent, REMOTE_DIR, d)
                    if rc != 0:
                        self.q.put(("err", "部署目录同步失败：%s" % (out or "")[:300]))
                        return
                    rc, out = C.compose(d, pdir, ["up", "-d", "orchestrator"], timeout=300)
                    if rc != 0:
                        self.q.put(("err", "编排层重启失败：%s" % (out or "")[:400]))
                        return
                    self.q.put(("msg", "LS 凭证已更新，编排层已重启。\n"
                                       "现在在标注引擎里提交的标注，1-2 秒内就会反映到平台数字。"))
                    return

                if action == "backup":
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    outdir = project_dir().parent / ("backup-" + stamp)
                    rc, out = C.run(["wsl.exe", "-d", d, "-u", "root", "--", "sh", "-c",
                                     "cd %s && tar -cf - .env 2>/dev/null; "
                                     "docker compose -f %s/docker-compose.yml ps -q"
                                     % (pdir, pdir)], timeout=120)
                    self.q.put(("log", (out or "").strip()[:400]))
                    # 备份走 compose exec 里的 pg 工具最稳（不依赖宿主机 psql）
                    rc2, out2 = C.run(["wsl.exe", "-d", d, "-u", "root", "--", "sh", "-c",
                                       "mkdir -p /opt/zhitu/backup && "
                                       "docker exec $(docker ps -qf name=orchdb) "
                                       "pg_dump -U forge -d forge | gzip > "
                                       "/opt/zhitu/backup/forge-%s.sql.gz" % stamp],
                                      timeout=600)
                    self.q.put(("log", (out2 or "").strip()[:400]))
                    if rc2 == 0:
                        self.q.put(("msg", "数据库已备份到 WSL:/opt/zhitu/backup/forge-%s.sql.gz\n"
                                           "（完整备份请用 deploy/backup.sh，含镜像与配置）" % stamp))
                    else:
                        self.q.put(("err", "备份失败：%s\n容器名可能不叫 orchdb，"
                                           "请用 deploy/backup.sh 做完整备份。" % (out2 or "")[:300]))
                    return

                if action == "up":
                    # 本地已有三个自建镜像 → 带 --no-build，秒级起栈且不碰外网；
                    # 没有（首装还没构建过）→ 不能带，否则 compose 报 image not
                    # found，看起来像栈坏了。
                    args = ["up", "-d", "--remove-orphans"]
                    if images_ready(d):
                        args.append("--no-build")
                    else:
                        self.q.put(("log", "本地没有 zhitu/* 镜像，将现场构建（需外网，较慢）…"))
                else:
                    args = {"down": ["stop"],
                            "restart": ["restart"]}.get(action)
                if args is None:
                    self.q.put(("err", "未知操作：%s" % action))
                    return
                rc, out = C.compose(d, pdir, args, timeout=2400)
                tail = "\n".join((out or "").strip().splitlines()[-12:])
                self.q.put(("log", tail or "(无输出)"))
                if rc != 0:
                    self.q.put(("err", "操作失败（rc=%s）。\n%s" % (rc, tail[:600])))
                    return
                self.q.put(("msg", "操作完成。"))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("err", "异常：%s" % exc))

        threading.Thread(target=work, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()


def status_once() -> int:
    out = C.cli_sink("status")
    st = collect_status()
    out("时间      : %s" % st["at"])
    out("发行版    : %s" % st["distro"])
    out("WSL       : %s" % ("OK" if st["wsl_ok"] else "不可用"))
    out("Docker    : %s" % (st["docker_ver"] or ("OK" if st["docker_ok"] else "不可用")))
    out("总体      : %s" % summarize(st))
    out("平台入口  : %s" % C.platform_url(host_ip()))
    out("标注引擎  : %s" % C.ls_url(host_ip()))
    for s in st["services"]:
        out("  %-12s :%-5d %s (%dms)" % (s["name"], s["port"],
                                         "OK" if s["ok"] else "不可达", s["ms"]))
    for c in st["containers"]:
        out("  容器 %-16s %-10s %s" % (c["service"], c["state"], c["health"]))
    out("日志      : %s" % out.path)
    return 0 if summarize(st) == "全部正常" else 1


def main() -> int:
    C.fix_console()
    if "--selftest" in sys.argv:
        return C.selftest()
    if "--status" in sys.argv:
        return status_once()
    MonitorApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
