# -*- coding: utf-8 -*-
"""核对部署包：确认交付物里只有「甲方进 LS 原生设计器」这一条路。

为什么要单独写：打包脚本用的是目录白名单递归复制，源码里只要多留一个文件
就会被静默打进去。自建设计器下线后，最怕的事故是「服务器装完还有两套编辑器」，
而它不会报错、不会白屏，只是慢慢腐烂。所以这里逐项核对包内文件与内容。

用法：python tools/check_package.py [包路径]
"""
from __future__ import annotations

import sys
import tarfile
from pathlib import Path

P = Path(__file__).resolve().parents[1]


def default_package() -> Path:
    """默认核对 dist 里**最新**的那个包，而不是写死某个日期。

    踩过的坑：打包脚本按当天日期命名，写死日期后第二天重打包，核对脚本
    还在验昨天的旧包并打印「全部通过」—— 最危险的那种假通过。
    """
    cands = sorted((P / "deploy" / "dist").glob("forge-deploy-*.tar.gz"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else P / "deploy" / "dist" / "forge-deploy.tar.gz"


DEFAULT = default_package()

FORBIDDEN_NAMES = (
    "FlowDesigner.tsx", "flow.py", "014_label_flow.sql",
    "verify_flow_", "legacy/flow-designer",
)
FORBIDDEN_CONTENT = {
    "src/orchestrator/app/main.py": ("label-step-templates", "/flow/apply", "class FlowStep"),
    "src/orchestrator/app/repo.py": ("project_flows", "save_flow", "get_flow"),
    "src/web/src/lib/types.ts": ("FlowMaterials", "ProjectFlow", "LabelStep"),
    "src/web/src/lib/http.ts": ("export function put<",),
    "src/web/src/pages/Projects.tsx": ("FlowPanel",),
}
REQUIRED = (
    "src/web/src/design/LsDesignEntry.tsx",
    "src/web/src/lib/assignment.ts",
    "src/web/src/lib/workbench.ts",
    "src/orchestrator/app/assignment.py",
    "src/orchestrator/app/clients.py",
    "src/orchestrator/app/ls_bridge.py",
    "src/orchestrator/app/main.py",
    "src/orchestrator/app/repo.py",
    "src/labelstudio/brand/forge_sso.py",
    "src/labelstudio/brand/apply_brand.py",
    "README.md", "smoke.sh", "docker-compose.yml",
    "migrations/013_ux_enhance.sql",
    # 恢复脚本必须进包：备份里有 .env.bak，但恢复流程要不要用它、
    # 怎么提示密钥不配套，都得靠这个脚本，缺了就只能靠人猜。
    "restore.sh", "backup.sh",
    # Windows 两条路径都要在包里：服务器是 Windows，包解压后没有这两个目录
    # 就等于把客户晾在「文档说有、包里没有」的境地。
    "windows/install.ps1",
    "windows-docker/check-host.ps1",
    "windows-docker/install-docker-wsl.ps1",
    "windows-docker/forge-stack.ps1",
    "windows-docker/register-autostart.ps1",
    "windows-docker/sync-portproxy.ps1",
    "windows-docker/README.md",
    # Windows 客户端（EXE 安装程序 + 监控台）的源码与打包脚本必须进包：
    # 交付物是可以重新生成的 exe，但"怎么生成"只能在包里，否则没人能复现。
    "windows-client/README.md",
    "windows-client/build_client.py",
    "windows-client/make_payload.py",
    "windows-client/make_icon.py",
    "windows-client/verify_payload.py",
    "windows-client/src/zhitu_common.py",
    "windows-client/src/zhitu_setup.py",
    "windows-client/src/zhitu_monitor.py",
    "windows-client/src/app.ico",
    # Windows 原生路线（不碰 Docker / WSL）。这是"服务器就是一台 Windows"时
    # 的主推路径，包里缺一个脚本，客户就只能拿到一个打不开的黑盒。
    "windows-native/README.md",
    "windows-native/build_native.py",
    "windows-native/make_payload.py",
    "windows-native/verify_payload.py",
    "windows-native/make_release.py",
    # 纯源码安装入口（clone 下来一条命令装完，不需要 exe / 离线包）。
    # 少了它，客户只能拿到一堆零散脚本，没人知道从哪一步开始。
    "windows-native/install_from_source.py",
    "windows-native/src/zn_common.py",
    "windows-native/src/zn_setup.py",
    "windows-native/src/zn_monitor.py",
    "windows-native/tools/probe_pg.py",
    "windows-native/tools/probe_pg_fallback.py",
)
CONTENT_CHECKS = {
    "src/web/src/design/LsDesignEntry.tsx": ("/ls-binding", "design-ticket"),
    "src/orchestrator/app/ls_bridge.py": ("/design-ticket", '"mode": "design"',
                                          "_guard_designer", "ls_task_id", "&task=",
                                          "labeled", "/data?task="),
    # stale_secret / headers_updated：webhook 复用时既要补齐验签头、也要在
    # 平台撤掉 secret 后清掉残留头。少了这段，老部署升级或回退配置时会留下
    # 一个和当前配置不再对应的密钥头（功能不炸，但等于把旧口令挂在那儿）。
    "src/orchestrator/app/clients.py": ("with_rows", "async def get_task",
                                        "stale_secret", "headers_updated"),
    "src/orchestrator/app/main.py": ("/ls-binding", "LsBindingBody",
                                     "assignment.router", "assignment.build_assignment",
                                     "assignment.items_snapshot", "build_items",
                                     # 回调先验 project 归属再打 LS：否则未配 secret 时
                                     # 任意 project id 都能让平台替调用方压 LS。
                                     "unknown_project", "ignored"),
    "src/orchestrator/app/assignment.py": ("labelstudio-live", "build_assignment",
                                           "build_items", "data_sha256", "fingerprint",
                                           "not_connected", "共用", "/data?task="),
    "src/orchestrator/app/autosync.py": ("assignment.record", "build_assignment",
                                         "record_items", "with_rows=True"),
    "src/labelstudio/brand/forge_sso.py": ("_guard_designer", "MODE_DESIGN", "DESIGN_ONLY_FIELD"),
    "src/web/src/lib/assignment.ts": ("fetchAssignment", "fetchAssignmentItems",
                                      "AssignmentItemsResp", "dataPreview",
                                      "contract_items", "actual_items"),
    "src/web/src/lib/workbench.ts": ("ls_task_id", "workbenchUrl"),
    "src/web/src/pages/Tasks.tsx": ("fetchAssignmentItems", "MyDataPanel",
                                    "AssignmentNotice", "launchItem", "不计入可标任务",
                                    "查看这一条"),
    "src/web/src/design/LsSyncPanel.tsx": ("AssignmentLine", "ItemsLine",
                                           "items?: AssignmentItemsSummary"),
    # 自助注册链路：后端一直是完整的（邀请码+事务占位+待激活状态机），
    # 但前端「邀请内部成员」曾经是个假按钮（点了一直报错），管理员根本
    # 拿不到邀请码 → 等于没有注册。这里把三处钉死，别再退回去。
    "src/web/src/pages/People.tsx": ("createInvite", "setStaffStatus",
                                     "生成邀请码", "邀请码", "激活"),
    "src/web/src/lib/store.tsx": ("createInvite", "setStaffStatus",
                                  "/api/v1/admin/invites", "/status"),
    "src/orchestrator/app/auth.py": ("/register", "invite_code", "尚未激活",
                                     "/invites", "/users/{user_id}/status"),
    # 生产构建必须走同源相对路径：把 127.0.0.1:8300 烧进 JS，换台机器
    # 打开就请求到访问者自己的本机，页面全空态。
    "src/web/src/lib/http.ts": ("SAME_ORIGIN", "API_BASE_LABEL"),
    # 演示数据的双重保险：迁移只建企业主体；旧品牌名自愈；老库演示号点名。
    "migrations/003_bootstrap.sql": ("无演示数据", "石研博航", "update forge.enterprises"),
    "README.md": ("在标注引擎里设计", "不新增表", "_guard_designer",
                  "读时实时投影", "标注员任务一致", "唯一正本",
                  "assignment_items", "verify_annotator_data_parity",
                  # 服务器是 Windows 时，文档第一屏必须给出「不能跑 install.sh」
                  # 和两条替代路径，否则运维会照 Linux 文档硬来。
                  "Docker Desktop 不支持 Windows Server",
                  "windows-docker/", "check-host.ps1",
                  # 原生路线现在是 Windows 服务器的默认路径，文档第一屏
                  # 必须能把它和容器路线区分开，否则运维还是只会找 WSL。
                  "windows-native/"),
    "smoke.sh": ("design-ticket", "ls-binding", "自建设计器接口已下线",
                 "_guard_designer", "me/assignment",
                 "me/assignment/items", "assignment_items"),
    "docker-compose.yml": ("013_ux_enhance; do",
                           # 三个 build 型服务必须有固定镜像名，否则离线交付
                           # 时 docker save 无从引用，装到第 05 步才发现缺镜像。
                           "zhitu/web:", "zhitu/orchestrator:", "zhitu/labelstudio:"),
    # 跨机恢复时 .env.bak 必须能被用到，且不一致的键要逐项报出来，
    # 否则会出现「库恢复了、服务起来了，但平台调不动 LS / SSO 全 401」。
    "restore.sh": ("--env=backup", ".env.bak", "LS_REFRESH_TOKEN",
                   "LS_SSO_SECRET", ".env.pre-restore-"),
    "backup.sh": (".env.bak", "chmod 600"),
    # Windows 侧的三个硬约束：先判定能不能跑容器、自启要按用户账户跑、
    # WSL 的 IP 每次重启都变所以 portproxy 必须每次重写。
    "windows-docker/check-host.ps1": ("INSTALLABLE", "BLOCKED", "HypervisorPresent",
                                      "Windows Server 2019"),
    "windows-docker/forge-stack.ps1": ("docker compose up -d", "sync-portproxy",
                                       "service docker start"),
    "windows-docker/install-docker-wsl.ps1": ("set-default-version", "get.docker.com",
                                              "daemon.json", "wsl.conf"),
    "windows-docker/register-autostart.ps1": ("LogonType Password", "AtStartup",
                                              "MultipleInstances IgnoreNew"),
    "windows-docker/sync-portproxy.ps1": ("portproxy add v4tov4", "hostname -I"),
    "windows-docker/README.md": ("Docker Desktop 不支持 Windows Server",
                                 "--env=backup", "portproxy"),
    # Windows 客户端：这几条都是踩过坑才加上的不变量，删掉任何一条都会
    # 在最难查的地方翻车（编码、DPI、线程、离线 build、caddy 挂载、LS 凭证）。
    "windows-client/src/zhitu_common.py": ("setup_dpi", "fix_console", "cli_sink",
                                           "9008", "9998", "ProxyHandler({})",
                                           "caddy/Caddyfile", "build_src"),
    "windows-client/src/zhitu_setup.py": ("--no-build", "elevate_self",
                                          "check_env_file", "gen_env",
                                          "step_token", "LS_REFRESH_TOKEN"),
    "windows-client/src/zhitu_monitor.py": ("--no-build", "portproxy",
                                            "collect_status", "main thread is not",
                                            "images_ready", "fetch_ls_refresh"),
    "windows-client/build_client.py": ("--onefile", "--noconsole", "tkinter"),
    "windows-client/make_payload.py": ("--build-images", "docker save",
                                       "manifest.json", "copy_build_sources",
                                       "Caddyfile.ip"),
    "windows-client/verify_payload.py": ("build context", "caddy/Caddyfile",
                                         "dockerfile"),
    "windows-client/README.md": ("Docker Desktop 不支持 Windows Server",
                                 "payload", "--no-build", "已知限制",
                                 "联网构建", "补凭证"),
    # Windows 原生路线：这几条是"能不能装成"的分水岭。
    # run_daemon（pg_ctl start 不能用 capture_output，否则 postgres 继承的管道
    # 会把 subprocess 卡死）、两个 PG 源、无 psql 时的 SQL 执行器、步骤表同源。
    "windows-native/src/zn_common.py": ("run_daemon", "pg_fetch", "PG_SOURCES",
                                        "sql_split", "pg_apply", "_PG_RUNNER",
                                        "RESULT %d", "-EncodedCommand",
                                        "ProgressPreference", "WinError 5",
                                        # 每日备份必须有独立的定时任务注册器：
                                        # 服务型任务是「开机+每分钟重复」，备份套用
                                        # 那个触发器会一天跑 1440 次
                                       "task_register_daily", "ZhituBackupDaily",
                                       # 计划任务改走 schtasks /Create /XML：
                                       # Register-ScheduledTask 真机报 CimException，
                                       # 错误还被 CLIXML 转义成 _x000D__x000A_ 乱码
                                       "_task_create_xml", "_task_xml_dir",
                                       "_trigger_daily", "_TASK_XML",
                                       "unwrap_clixml", "_task_register_ps",
                                       # 服务器常开 → 不注册服务/任务，直接起进程 +
                                       # 一个守护进程负责保活与每日备份
                                       "pg_start_local", "pg_stop_local",
                                       "remove_service", "spawn_detached",
                                       "pid_alive", "kill_port",
                                        # initdb 在非空目录上必然 chmod 失败，
                                        # 必须先判「有 PG_VERSION 才叫装过」
                                        "pg_data_state", "leftover",
                                        "force_rmtree", "PG_VERSION",
                                        # 自己的库注册成 SCM 服务，schtasks 查不到
                                        "service_exists", "stage_runtime", "RUNTIME_MAP",
                                        # 载荷里的 payload/app 从来没人铺到安装目录，
                                        # 所以第 04 步必然报「缺编排层源码」。源码树
                                        # 与运行时都必须由安装程序自己铺开。
                                        "stage_sources", "sources_dir", "_merge_tree",
                                        "_runtime_ready", "离线包不完整",
                                        # 口令与库归属：5432 上可能是用户自己装的库，
                                        # 改口令前必须先确认 data 目录属于自己
                                        "pg_port_owner", "pg_owns_port",
                                        "pg_verify_super", "pg_reset_super_password",
                                        "HBA_TRUST", "pg_restart",
                                        # 残留锁自愈 + 日志解读（11-10 真机第 03 步踩到）
                                        "pg_clear_stale_lock", "pg_log_tail",
                                        "pg_cluster_version", "pg_binary_version",
                                        "pg_classify",
                                        # 载荷目录要能被环境变量覆盖：纯源码安装时
                                        # 载荷在 windows-native/payload，不在 src/ 下
                                        "ZHITU_PAYLOAD_DIR",
                                        # GUI 也要落盘日志，否则出问题只能靠截图
                                        "tee_sink", "install-%s-%s.log"),
    "windows-native/src/zn_setup.py": ("STEP_SPEC", "step_list", "--no-download",
                                       "PG_SUPER_PASSWORD", "fetch_ls_refresh",
                                       "elevate_self", "tools.backup_db", "--keep 7",
                                       # 老库里遗留的演示账号要在收尾报告里点名
                                       "遗留演示账号", "C.stage_sources", "run_steps",
                                       # 第 03 步起不来时必须自己读日志说清原因，
                                       # 不能只丢一句"看 log 里的最后几行"让用户去翻
                                       "C.pg_log_tail", "C.pg_classify", "pg_cluster_version",
                                       # 判「5432 是不是自己占的」靠 pg_owns_port；
                                       # 老版本遗留的服务要先删掉，否则抢 data 目录
                                       "C.remove_service(PG_SERVICE",
                                       "C.pg_start_local",
                                       "C.stage_sources", "C.stage_runtime",
                                       # 失败不中断：一次跑完，把问题一次全拿到
                                       "run_steps", "NEEDS", "format_summary",
                                       "write_report", "build_report",
                                       "_start_watchdog",
                                       # 口令要跨运行复用，否则重跑必犯「认证失败」
                                       "pg_super", "C.pg_verify_super",
                                       "C.pg_reset_super_password",
                                       'tee_sink("gui"', "安装日志：",
                                       # 纯源码安装要把安装目录传进来，不能再写死
                                       # C:\Zhitu；--plan 用来「只确认装到哪」
                                       '"--app-dir"', '"--data-dir"'),
    # 纯源码安装入口：clone 下来一条命令装完。这四条是它的存在理由，
    # 少任何一条就退化成「必须打 exe」。
    "windows-native/install_from_source.py": (
        "ZHITU_PAYLOAD_DIR",          # 载荷位置用环境变量指，不污染 src/
        "ZHITU_REEXEC",               # Python 版本不对要换解释器重跑
        "make_payload.py",            # 现场组装载荷，不要求预置 payload
        "_stub_tkinter",              # 服务器 Python 缺 tkinter 也要能装
        "--plan"),
    "windows-native/src/zn_monitor.py": ("collect_status", "proc_state",
                                         "watchdog_state", "stop_watchdog",
                                         "main thread is not",
                                         # 备份是唯一"失败了没人发现"的环节，
                                         # 监控台必须把最近一次备份摊出来
                                         "last_backup", "每日备份"),
    "windows-native/make_payload.py": ("frontend", "migrations",
                                       "Caddyfile.tpl", "manifest.json"),
    "windows-native/verify_payload.py": ("逐字节一致", "占位符", "步骤表"),
    # 单文件交付是这一版的核心形态：载荷必须内嵌，且 exe 旁的 cache 要认。
    "windows-native/build_native.py": ("--add-data", "payload", "--no-embed"),
    "windows-native/make_release.py": ("安装说明.txt", "ZIP_DEFLATED",
                                       "智途-Windows原生"),
    "windows-native/README.md": ("不需要 Docker", "备用源", "13 步",
                                 "已知限制", "verify_payload"),
}
FORBIDDEN_ANYWHERE = ("014_label_flow",)

fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)


SKIP_DIRS = {"node_modules", "dist", "build", "payload", "__pycache__",
             ".git", "release", ".tmp", "_shots", "venv", "shots"}

# 源码树 → 包内路径 的映射。必须和 deploy/make_package.py 的 COPIES 一致：
# 包会把 platform/web 装成 src/web、platform/services/orchestrator 装成
# src/orchestrator。少了这层映射，--source 模式会拿错路径去比，全部假失败。
SRC_MAP = (
    ("deploy/windows-native/", "windows-native/"),
    ("deploy/", ""),
    ("web/", "src/web/"),
    ("services/orchestrator/", "src/orchestrator/"),
    ("labelstudio/", "src/labelstudio/"),
)


def src_to_pkg(rel: str) -> str:
    for a, b in SRC_MAP:
        if rel.startswith(a):
            return b + rel[len(a):]
    return rel


def is_migration(name: str) -> bool:
    """两种模式的迁移路径写法不一样，别只认一种。

    tar 包里路径带前缀（forge-deploy/platform/deploy/migrations/013_...），
    去掉前缀后是 migrations/013_...；源码模式本来就已是 migrations/013_...
    以前只写 "/migrations/" in n，源码模式下变成 0 条，然后
    「013 仍在迁移链」这个断言会假失败。
    """
    return "/migrations/" in name or name.startswith("migrations/")


def collect_source() -> tuple:
    """直接从源码树构造 names / blob / present（--source 模式）。

    为什么需要这个模式：交付只出 Windows 包（单 exe，不带 Linux tar.gz）时，
    默认模式会因为找不到/拿到了旧的 Linux 包而误报「内容缺」。这里绕开包，
    直接核对仓库里的源码 —— 内容断言的意义（"代码在不在"）完全保留。
    """
    names: list[str] = []
    blob: dict[str, str] = {}
    present: set[str] = set()
    ps1_raw: dict[str, bytes] = {}
    for root_rel in ("deploy", "web", "services/orchestrator", "labelstudio"):
        root = P / root_rel
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if set(p.relative_to(root).parts[:-1]) & SKIP_DIRS:
                continue
            rel = src_to_pkg(p.relative_to(P).as_posix())
            names.append(rel)
            present.add(rel)
            if p.suffix == ".ps1":
                ps1_raw[rel] = p.read_bytes()
            if p.suffix in (".py", ".ts", ".tsx", ".md", ".sh", ".yml", ".sql", ".ps1"):
                blob[rel] = p.read_text(encoding="utf-8", errors="replace")
    return names, blob, present, ps1_raw


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    src_mode = "--source" in sys.argv
    path = Path(argv[0]) if argv else DEFAULT
    if src_mode:
        print(f"核对源码树 {P}（--source，不看部署包）")
        names, blob, present, ps1_raw = collect_source()
        print(f"  文件数 {len(names)}，迁移 {len([n for n in names if is_migration(n)])} 条")
    else:
        if not path.is_file():
            print(f"[X] 找不到部署包：{path}")
            print("    只交付 Windows 时用：python platform/tools/check_package.py --source")
            return 1
        print(f"核对 {path.name}  ({path.stat().st_size:,} bytes)")
        with tarfile.open(path, "r:gz") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            names = [m.name for m in members]
            blob = {}
            ps1_raw: dict[str, bytes] = {}
            for m in members:
                rel = m.name.removeprefix("forge-deploy/")
                if m.name.endswith(".ps1"):
                    fh = tf.extractfile(m)
                    ps1_raw[rel] = fh.read() if fh else b""
                if m.name.endswith((".py", ".ts", ".tsx", ".md", ".sh", ".yml", ".sql", ".ps1")):
                    f = tf.extractfile(m)
                    blob[rel] = f.read().decode("utf-8", "replace") if f else ""
        # 二进制资产（图标等）不参与文本断言，但"在不在包里"仍然要能查。
        # 之前只拿 blob 判断存在性，导致 app.ico 这种非文本文件永远判"缺失"。
        present = {m.name.removeprefix("forge-deploy/") for m in members}
        print(f"  文件数 {len(names)}，迁移 {len([n for n in names if is_migration(n)])} 条")

    print("\n=== 1. 不该出现的自建设计器资产 ===")
    for bad in FORBIDDEN_NAMES:
        hits = [n for n in names if bad.lower() in n.lower()]
        check(not hits, f"包内无 {bad}", str(hits[:3]))
    for rel, words in FORBIDDEN_CONTENT.items():
        text = blob.get(rel, "")
        hit = [w for w in words if w in text]
        check(bool(text) and not hit, f"{rel} 已无设计器代码", str(hit))
    for w in FORBIDDEN_ANYWHERE:
        # README 里必须写明「014 已废弃、升级时不要跑」，否则老环境的人会
        # 照旧文档去跑那张表。所以文档里的提及是刻意保留，只查配置与脚本。
        hits = [n for n, t in blob.items()
                if w in t and not n.endswith(".md")]
        check(not hits, f"配置与脚本不再引用 {w}（README 废弃说明除外）", str(hits[:3]))

    print("\n=== 2. 新链路必须齐备 ===")
    for rel in REQUIRED:
        check(rel in blob or rel in present, f"{rel} 在包里")
    for rel, words in CONTENT_CHECKS.items():
        text = blob.get(rel, "")
        miss = [w for w in words if w not in text]
        check(not miss, f"{rel} 内容正确", ("缺 " + str(miss)) if miss else "")

    print("\n=== 3. 迁移链 ===")
    migs = sorted(n.rsplit("/", 1)[-1] for n in names if is_migration(n))
    check(bool(migs), "找到迁移目录（路径判定对两种模式都成立）", f"已识别 {len(migs)} 条")
    check(not any("014" in n for n in migs), "迁移链不含 014_label_flow", str(migs))
    check("013_ux_enhance.sql" in migs, "013_ux_enhance 仍在迁移链")

    # PowerShell 5.1 读「无 BOM 的 .ps1」时按系统 ANSI 代码页（中文机器是 GBK）
    # 解码，UTF-8 中文会变成乱码并直接导致语法解析失败 —— 报错看起来像命令写错，
    # 排查方向完全跑偏。所以规则是：要么纯 ASCII，要么带 UTF-8 BOM。
    print("\n=== 4. Windows 脚本编码 ===")
    for rel, raw in sorted(ps1_raw.items()):
        if not raw:
            continue
        bom = raw[:3] == b"\xef\xbb\xbf"
        bad = sum(1 for b in raw if b > 127)
        check(bom or bad == 0, f"{rel} 可被 PowerShell 5.1 安全解析",
              "带 BOM" if bom else f"非 ASCII 字节 {bad} 处")

    print()
    scope = "源码树" if src_mode else "部署包"
    print(f"{scope}核对全部通过" if not fails else f"{scope}未通过 {len(fails)} 项：{fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
