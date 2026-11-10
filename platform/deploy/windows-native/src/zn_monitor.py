#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智途 · 原生 Windows 监控台（不依赖 Docker / WSL）。

三层状态摊平，哪一层断了就指哪一层：
  1. 数据库   智途自己的 Windows 服务是否运行 + 端口是否在听
  2. 进程     三个计划任务是不是在运行（编排层 / 标注引擎 / 入口）
  3. 服务     HTTP 端点是否真的回 200（任务在跑 ≠ 服务可用）

为什么用计划任务状态而不是进程名：三个服务都靠 `cmd.exe /c run-xxx.cmd` 启动，
进程名全是 python.exe / caddy.exe，靠名字根本分不清谁是谁；计划任务名是我们
自己注册的，状态也只有 Running / Ready 两种，判断没有歧义。

用法
----------------------------------------------------------------------------
    智途监控（原生）.exe             打开监控台
    智途监控（原生）.exe --status    打印一次状态后退出（0 = 全部正常）
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
import zn_common as C  # noqa: E402

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

REFRESH_MS = 6000

SERVICES = [
    ("平台入口", C.DEFAULT_HTTP_PORT, "http://127.0.0.1:%d/" % C.DEFAULT_HTTP_PORT),
    ("平台 API", C.DEFAULT_API_PORT, "http://127.0.0.1:%d/healthz" % C.DEFAULT_API_PORT),
    ("标注引擎 LS", C.DEFAULT_LS_PORT, "http://127.0.0.1:%d/health" % C.DEFAULT_LS_PORT),
]

# 早期版本用 4 个计划任务（orch / ls / caddy / backup）。现在不注册任务了：
# 三个进程直接跑，守护进程负责保活与每日备份，所以下面按「端口 + 进程」看状态。

# 现在不注册计划任务了：三个进程直接跑，由守护进程负责保活与每日备份。
# 所以「状态」改成看端口，「启停」改成起进程/杀进程。
PROCS = (
    ("orch", "编排层", C.DEFAULT_API_PORT, "run-orchestrator.cmd"),
    ("ls", "标注引擎", C.DEFAULT_LS_PORT, "run-labelstudio.cmd"),
    ("caddy", "入口", C.DEFAULT_HTTP_PORT, "run-caddy.cmd"),
)


def proc_state(port: int) -> str:
    """Running / 未运行。判据是端口在不在监听。"""
    return "Running" if C.port_open(port) else "未运行"


def cfg() -> Dict[str, object]:
    return C.load_config()


def host_ip() -> str:
    return str(cfg().get("host_ip") or C.local_ip() or "127.0.0.1")


def pg_port() -> int:
    try:
        return int(cfg().get("pg_port") or C.DEFAULT_PG_PORT)
    except (TypeError, ValueError):
        return C.DEFAULT_PG_PORT


def last_backup() -> Dict[str, object]:
    """最近一份备份的时间与大小。

    为什么要显示这个：备份是唯一「失败了也没人发现」的环节 —— 任务在、
    目录在、计划任务状态是 Ready，看着一切正常，直到真要恢复才发现最近
    七天全是空文件。所以把「最近一次成功是什么时候」摊在监控台上。
    """
    try:
        st = C.load_config()
        data = Path(str(st.get("data_dir") or C.DEFAULT_DATA_DIR))
        d = data / "backup"
        if not d.is_dir():
            return {}
        files = [f for f in d.glob("forge-*") if f.is_file()]
        if not files:
            return {}
        f = max(files, key=lambda q: q.stat().st_mtime)
        mtime = f.stat().st_mtime
        return {"name": f.name,
                "mb": f.stat().st_size / 1048576,
                "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
                "age_h": (time.time() - mtime) / 3600.0}
    except Exception:  # noqa: BLE001
        return {}


def watchdog_state() -> str:
    """守护进程在不在（读 watchdog.pid 再核进程是否活着）。"""
    try:
        pid = int((C.DEFAULT_DATA_DIR / "watchdog.pid")
                  .read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "未运行"
    return "Running" if C.pid_alive(pid) else "未运行"


def stop_watchdog() -> bool:
    """停守护。不停它的话，点「全部停止」5 秒后进程又被拉回来。"""
    try:
        pid = int((C.DEFAULT_DATA_DIR / "watchdog.pid")
                  .read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    rc, _ = C.run(["taskkill.exe", "/PID", str(pid), "/F", "/T"], timeout=60)
    return rc == 0


def pg_service_state() -> tuple:
    """PostgreSQL 的状态。返回 (中文状态, 是否运行中)。

    不注册服务了，所以判据是「端口在监听」+「占用它的确实是我们的 data 目录」。
    只判端口会把别人的 PostgreSQL 当成自己的。
    """
    port = pg_port()
    if not C.port_open(port):
        return "未运行", False
    mine, why = C.pg_owns_port(C.DEFAULT_DATA_DIR / "pgdata", port)
    if mine:
        return "运行中", True
    return "被其它程序占用", False


def collect_status() -> Dict[str, object]:
    st: Dict[str, object] = {"at": time.strftime("%H:%M:%S"), "services": [],
                             "tasks": [], "db": {}, "error": ""}
    try:
        db_port = pg_port()
        # 只看端口不够：这台机器上可能装着别人的 PostgreSQL，5432 一样在监听，
        # 界面就会报"在监听"而智途的库其实没起来。先问我们自己的服务状态。
        svc, running = pg_service_state()
        listening = C.port_open(db_port)
        st["db"] = {"port": db_port, "service": svc, "listening": listening,
                    "ok": running and listening}
        st["services"] = [{"name": label, "port": port,
                           **C.probe_http(url, timeout=4)}
                          for label, port, url in SERVICES]
        st["tasks"] = (
            [{"key": k, "label": lab, "name": "端口 %d" % port,
              "state": proc_state(port)} for k, lab, port, _c in PROCS]
            + [{"key": "watchdog", "label": "守护进程", "name": "watchdog.pid",
                "state": watchdog_state()}])
        st["backup"] = last_backup()
    except Exception as exc:  # noqa: BLE001
        st["error"] = str(exc)[:300]
    return st


def summarize(st: Dict[str, object]) -> str:
    db = st.get("db") or {}
    if not db.get("ok"):
        return "数据库未连接"
    svc = st.get("services") or []
    bad = [s for s in svc if not s["ok"]]
    if not bad:
        return "全部正常"
    return "%d 个服务不可达" % len(bad)


class MonitorApp:
    def __init__(self) -> None:
        self.s = C.setup_dpi()
        self.root = tk.Tk()
        self.root.title("智途 · 监控台（Windows 原生）")
        self.root.configure(bg=BG)
        self.root.geometry("%dx%d" % (int(1020 * self.s), int(700 * self.s)))

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self._auto = tk.BooleanVar(value=True)

        self._style()
        self._ui()
        self._fit(1020, 700)
        self.root.after(200, self._drain)
        self.refresh()

    # -- 布局 -------------------------------------------------------------
    def _fit(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("%dx%d" % (
            min(max(int(w * self.s), self.root.winfo_reqwidth()), sw - 24),
            min(max(int(h * self.s), self.root.winfo_reqheight()), sh - 80)))

    def f(self, size: int, bold: bool = False, mono: bool = False) -> tuple:
        fam = "Consolas" if mono else "Microsoft YaHei UI"
        return (fam, max(8, int(round(size * self.s))), "bold" if bold else "normal")

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
        st.configure("Treeview", background=BG2, fieldbackground=BG2, foreground=FG,
                     rowheight=self.px(30), borderwidth=0)
        st.configure("Treeview.Heading", background=BG3, foreground=FG_DIM,
                     relief="flat", font=self.f(9))
        st.map("Treeview", background=[("selected", "#2E3440")])

    def _ui(self) -> None:
        head = tk.Frame(self.root, bg=BG2)
        head.pack(fill="x")
        tk.Label(head, text="智途", bg=BG2, fg=FG,
                 font=self.f(15, bold=True)).pack(side="left",
                                                  padx=(self.px(20), self.px(6)))
        tk.Label(head, text="· 监控台（Windows 原生）", bg=BG2, fg=FG_DIM,
                 font=self.f(10)).pack(side="left", pady=self.px(14))
        self.lbl_overall = tk.Label(head, text="正在检查…", bg=BG2, fg=FG_DIM,
                                    font=self.f(10, bold=True))
        self.lbl_overall.pack(side="right", padx=20, pady=self.px(14))

        # 三张服务卡
        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x", padx=self.px(16), pady=(self.px(12), self.px(6)))
        self.cards: Dict[str, Dict[str, tk.Label]] = {}
        for label, port, _ in SERVICES:
            box = tk.Frame(cards, bg=BG2, highlightbackground=LINE, highlightthickness=1)
            box.pack(side="left", fill="both", expand=True, padx=(0, 10))
            tk.Label(box, text=label, bg=BG2, fg=FG_DIM,
                     font=self.f(9)).pack(anchor="w", padx=self.px(14),
                                          pady=(self.px(10), 0))
            val = tk.Label(box, text="—", bg=BG2, fg=FG_DIM, font=self.f(16, bold=True))
            val.pack(anchor="w", padx=self.px(14), pady=(self.px(2), 0))
            sub = tk.Label(box, text=":%d" % port, bg=BG2, fg=FG_DIM,
                           font=self.f(8, mono=True))
            sub.pack(anchor="w", padx=self.px(14), pady=(0, self.px(10)))
            self.cards[label] = {"val": val, "sub": sub}

        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill="both", expand=True, padx=self.px(16), pady=(self.px(6), 0))
        tk.Label(mid, text="计划任务与数据库", bg=BG, fg=FG_DIM,
                 font=self.f(9)).pack(anchor="w", pady=(0, self.px(6)))
        self.tbl = ttk.Treeview(mid, columns=("a", "b", "c"), show="headings", height=5)
        for c, t, w in [("a", "组件", 260), ("b", "状态", 180), ("c", "说明", 520)]:
            self.tbl.heading(c, text=t)
            self.tbl.column(c, width=self.px(w), anchor="w")
        self.tbl.pack(fill="both", expand=True)

        low = tk.Frame(self.root, bg=BG)
        low.pack(fill="both", expand=True, padx=self.px(16), pady=(self.px(10), 0))
        bar = tk.Frame(low, bg=BG)
        bar.pack(fill="x")
        tk.Label(bar, text="最近日志", bg=BG, fg=FG_DIM, font=self.f(9)).pack(side="left")
        self.lbl_meta = tk.Label(bar, text="", bg=BG, fg=FG_DIM, font=self.f(8, mono=True))
        self.lbl_meta.pack(side="right")
        wrap = tk.Frame(low, bg=LINE)
        wrap.pack(fill="both", expand=True, pady=(self.px(6), 0))
        self.log = tk.Text(wrap, bg="#0F1113", fg="#C9CFD6", bd=0, wrap="none",
                           width=1, height=8, font=self.f(9, mono=True),
                           padx=self.px(10), pady=self.px(8))
        sb = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        foot = tk.Frame(self.root, bg=BG2)
        # 底栏先占位：pack 按顺序分配空间，后 pack 的会被压成 0 宽
        foot.pack(side="bottom", fill="x", pady=(self.px(10), 0), before=cards)
        pad = self.px(8)
        row1 = tk.Frame(foot, bg=BG2)
        row1.pack(fill="x", padx=self.px(14), pady=(self.px(8), self.px(4)))
        ttk.Button(row1, text="全部启动", style="Accent.TButton",
                   command=lambda: self._act("start")).pack(side="left")
        ttk.Button(row1, text="全部停止",
                   command=lambda: self._act("stop")).pack(side="left", padx=(pad, 0))
        ttk.Button(row1, text="重启编排层",
                   command=lambda: self._act("restart_orch")).pack(side="left",
                                                                   padx=(pad, 0))
        ttk.Button(row1, text="打开平台",
                   command=lambda: self._open("http://%s/" % host_ip())).pack(side="right")
        ttk.Button(row1, text="标注引擎",
                   command=lambda: self._open("http://%s:%d/"
                                              % (host_ip(), C.DEFAULT_LS_PORT))
                   ).pack(side="right", padx=(0, pad))

        row2 = tk.Frame(foot, bg=BG2)
        row2.pack(fill="x", padx=self.px(14), pady=(0, self.px(8)))
        self.btn_auto = ttk.Button(row2, text="", command=self._toggle_auto)
        self.btn_auto.pack(side="left")
        self._paint_auto()
        ttk.Button(row2, text="立即刷新",
                   command=self.refresh).pack(side="left", padx=(pad, 0))
        ttk.Button(row2, text="打开日志目录",
                   command=self._open_logs).pack(side="left", padx=(pad, 0))
        ttk.Button(row2, text="补凭证",
                   command=lambda: self._act("token")).pack(side="left", padx=(pad, 0))

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
            self.refresh()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self._paint(payload)
                    self.busy = False
                    # 自动刷新只能在主线程续期：子线程调 root.after 会抛
                    # RuntimeError: main thread is not in main loop（实测）
                    if self._auto.get():
                        self.root.after(REFRESH_MS, self.refresh)
                elif kind == "log":
                    self._append(str(payload))
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
            try:
                st = collect_status()
            except Exception as exc:  # noqa: BLE001
                st = {"at": time.strftime("%H:%M:%S"), "services": [], "tasks": [],
                      "db": {}, "error": str(exc)[:300]}
            self.q.put(("status", st))

        threading.Thread(target=work, daemon=True).start()

    def _paint(self, st: Dict[str, object]) -> None:
        overall = summarize(st)
        ok = overall == "全部正常"
        db = st.get("db") or {}
        self.lbl_overall.config(
            text=overall, fg=OK_C if ok else (WARN_C if db.get("ok") else ERR_C))

        for s in st.get("services") or []:
            box = self.cards.get(s["name"])
            if not box:
                continue
            if s["ok"]:
                box["val"].config(text="正常", fg=OK_C)
                box["sub"].config(text="%d ms · :%d" % (s["ms"], s["port"]))
            else:
                box["val"].config(text="不可达", fg=ERR_C)
                box["sub"].config(text="code=%s · :%d" % (s["code"], s["port"]))

        self.tbl.delete(*self.tbl.get_children())
        db = st.get("db") or {}
        if db.get("ok"):
            db_note = "127.0.0.1:%s 在监听" % db.get("port")
        elif db.get("service") in ("未安装", "无法查询"):
            db_note = "PostgreSQL 没在跑（点「全部启动」或重跑安装程序）"
        elif db.get("listening"):
            db_note = ("127.0.0.1:%s 有程序在监听，但不是智途的数据目录，"
                       "可能你自己另装了 PostgreSQL"
                       % db.get("port"))
        else:
            db_note = "%s，端口 %s 没监听" % (db.get("service"), db.get("port"))
        self.tbl.insert("", "end", values=(
            "PostgreSQL", db.get("service") or "未知", db_note))
        for t in st.get("tasks") or []:
            state = t["state"]
            if t.get("key") == "backup":
                # 每日任务是「按点触发」，平时状态就是 Ready，不是故障。
                # 用 Running 那套判词会让人以为备份坏了。
                if state in ("不存在", "查询失败", "MISSING"):
                    note = "没见到这个任务 —— 重跑安装程序补上"
                else:
                    b = st.get("backup") or {}
                    if not b:
                        note = "还没有备份过（首次将在 03:00 自动跑）"
                    elif float(b.get("age_h") or 0) > 48:
                        note = ("最近一次是 %s（%.1f MB），已超过 48 小时 —— "
                                "看 logs\\backup.log" % (b.get("at"), b.get("mb") or 0))
                    else:
                        note = ("最近一次 %s（%.1f MB）· 每天 03:00，保留 7 份"
                                % (b.get("at"), b.get("mb") or 0))
                self.tbl.insert("", "end", values=(
                    "%s（%s）" % (t["label"], t["name"]), state, note))
                continue
            good = state == "Running"
            self.tbl.insert("", "end", values=(
                "%s（%s）" % (t["label"], t["name"]),
                state, "开机自启，崩溃后 1 分钟内自动拉起"
                if good else "没有在运行 —— 任务管理器里看 run-*.cmd 的输出"))

        meta = "刷新于 %s · 主机 %s" % (st.get("at"), host_ip())
        self.lbl_meta.config(text=meta)
        if st.get("error"):
            self._append("[状态采集] " + str(st["error"]))

    # -- 操作 -------------------------------------------------------------
    def _open(self, url: str) -> None:
        os.startfile(url)  # noqa: S606

    def _open_logs(self) -> None:
        d = Path(str(cfg().get("data_dir") or C.DEFAULT_DATA_DIR)) / "logs"
        if d.is_dir():
            os.startfile(str(d))  # noqa: S606
        else:
            messagebox.showinfo("提示", "日志目录还没有生成：%s" % d)

    def _act(self, action: str) -> None:
        if self.busy:
            return
        self.busy = True
        self._append("")
        self._append("$ %s  (%s)" % (action, time.strftime("%H:%M:%S")))

        def work() -> None:
            try:
                if action == "start":
                    for _k, lab, port, cmdfile in PROCS:
                        if C.port_open(port):
                            self.q.put(("log", "%s 已在 %d 上，跳过" % (lab, port)))
                            continue
                        pid = C.spawn_detached(
                            ["cmd.exe", "/c", str(C.DEFAULT_DATA_DIR / cmdfile)],
                            C.DEFAULT_APP_DIR)
                        self.q.put(("log", "启动 %s → PID %s" % (lab, pid or "失败")))
                    self.q.put(("msg", "已尝试启动三个进程。等 10-60 秒再刷新"
                                       "（标注引擎要跑迁移，最慢）。"))
                    return
                if action == "stop":
                    # 先停守护，否则 5 秒后它又把进程拉回来
                    self.q.put(("log", "停止守护进程：%s"
                                % ("成功" if stop_watchdog() else "本来就没跑")))
                    for _k, lab, port, _c in PROCS:
                        ok = C.kill_port(port, lambda m: self.q.put(("log", m)))
                        self.q.put(("log", "%s：%s" % (lab, "已停止" if ok else "没停掉")))
                    self.q.put(("msg", "已停止三个进程（含守护）。"
                                       "重新启动请点「全部启动」。"))
                    return
                if action == "restart_orch":
                    C.kill_port(C.DEFAULT_API_PORT,
                                lambda m: self.q.put(("log", m)))
                    time.sleep(1)
                    pid = C.spawn_detached(
                        ["cmd.exe", "/c",
                         str(C.DEFAULT_DATA_DIR / "run-orchestrator.cmd")],
                        C.DEFAULT_APP_DIR)
                    self.q.put(("log", "编排层 → PID %s" % (pid or "失败")))
                    self.q.put(("msg", "编排层已重启。"))
                    return
                if action == "token":
                    self.q.put(("err", "请在安装程序里重跑第 12 步「LS 长期凭证」。\n"
                                       "它需要 LS 管理员口令，监控台不保存口令。"))
                    return
                self.q.put(("err", "未知操作：%s" % action))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("err", "异常：%s" % exc))

        threading.Thread(target=work, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()


def status_once() -> int:
    out = C.cli_sink("status")
    st = collect_status()
    out("时间      : %s" % st["at"])
    out("总体      : %s" % summarize(st))
    out("平台入口  : http://%s/" % host_ip())
    out("标注引擎  : http://%s:%d/" % (host_ip(), C.DEFAULT_LS_PORT))
    db = st["db"]
    out("PostgreSQL: :%s 服务=%s 监听=%s %s"
        % (db.get("port"), db.get("service") or "?",
           "是" if db.get("listening") else "否",
           "OK" if db.get("ok") else "不可用"))
    for s in st["services"]:
        out("  %-12s :%-5d %s (%dms)" % (s["name"], s["port"],
                                         "OK" if s["ok"] else "不可达 code=%s" % s["code"],
                                         s["ms"]))
    for t in st["tasks"]:
        out("  任务 %-24s %s" % (t["name"], t["state"]))
    b = st.get("backup") or {}
    if b:
        out("  最近备份     %s（%.1f MB，%.0f 小时前）"
            % (b.get("at"), b.get("mb") or 0, b.get("age_h") or 0))
    else:
        out("  最近备份     还没有（每天 03:00 自动跑第一次）")
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
