"""
FORGE Orchestrator · 外部系统客户端（Label Studio）
================================================================
本文件只保留一个外部业务系统：Label Studio。
ERPNext 已从架构中移除（2026-11 决策，见 docs/02 §9）：供应商、采购、结算、
开票全部由本平台自有数据库承载，不再跨系统对账。

两条硬约束（都是踩出来的，别改回去）：

1. **不走系统代理**。本机/机房常有 HTTP_PROXY，httpx 默认 trust_env=True 会把
   发往 127.0.0.1 或容器内网域名的请求交给代理，表现为莫名其妙的 404 / 502 /
   "不知道这样的主机"。默认 trust_env=False，要出公网再显式打开。

2. **Label Studio 1.23 只认 JWT**。legacy `Token <key>` 已被移除（返回
   401 legacy token authentication has been removed）。access token 只有 5 分钟，
   必须用 refresh token 自动续期，否则集成会在运行 5 分钟后集体 401。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

log = logging.getLogger("forge.integration")


class IntegrationError(RuntimeError):
    def __init__(self, system: str, detail: str, status: Optional[int] = None):
        self.system = system
        self.detail = detail
        self.status = status
        super().__init__(f"[{system}] {detail}")


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.outbound_timeout_s,
        trust_env=settings.http_trust_env,   # 默认 False：绝不把内网请求交给系统代理
    )


def jwt_exp(token: str) -> float:
    """不验签地读出 exp（仅用于本地缓存判断，安全性不依赖它）。"""
    try:
        import base64
        import json as _json
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(_json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Label Studio 1.23 CE
# ---------------------------------------------------------------------------
class LabelStudioClient:
    """标注引擎的权威数据在 LS。平台只创建项目、导入条目、读统计、收回调。

    绝不复制 LS 的标注结果到自有库（只存 ls_project_id + annotation 引用），
    否则 LS 升级或标注修订会立刻造成双写不一致。
    """

    def __init__(self) -> None:
        self.base = settings.ls_base_url.rstrip("/")
        self._access: str = settings.ls_token
        self._refresh: str = settings.ls_refresh_token
        self._exp: float = jwt_exp(settings.ls_token) if settings.ls_token else 0.0
        self._lock = asyncio.Lock()
        self._ping_cache: Optional[tuple] = None

    # -- token 管理 ---------------------------------------------------------
    async def access_token(self, force: bool = False) -> str:
        if not self._refresh:
            return self._access
        if not force and self._access and time.time() < self._exp - 30:
            return self._access
        async with self._lock:
            if not force and self._access and time.time() < self._exp - 30:
                return self._access
            async with _http() as c:
                r = await c.post(f"{self.base}/api/token/refresh/",
                                 json={"refresh": self._refresh})
            if r.status_code != 200:
                raise IntegrationError("labelstudio",
                                       f"刷新 access token 失败 {r.status_code}: {r.text[:200]}",
                                       r.status_code)
            self._access = r.json()["access"]
            self._exp = jwt_exp(self._access)
            log.info("LS access token 已刷新，有效期至 %s",
                     time.strftime("%H:%M:%S", time.localtime(self._exp)))
            return self._access

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """带 401 自动续期重试一次。"""
        last: Optional[httpx.Response] = None
        for attempt in (0, 1):
            tok = await self.access_token(force=attempt == 1)
            async with _http() as c:
                last = await c.request(
                    method, f"{self.base}{path}",
                    headers={"Authorization": f"Bearer {tok}",
                             "Content-Type": "application/json"},
                    **kw,
                )
            if last.status_code == 401 and attempt == 0 and self._refresh:
                continue
            break
        assert last is not None
        return last

    # -- 探测 ---------------------------------------------------------------
    # 健康探测和业务请求的取舍完全不同：
    #   业务请求可以等 6 秒（宁可慢也要成功）；
    #   健康探测不行 —— 它被监控系统每秒调，一旦外部系统挂掉，
    #   每次探测都跑满超时，健康接口本身就成了故障源。
    # 实测：LS 未启动时 /healthz?deep=1 要 2.1 秒（连接失败 × 2 次往返）。
    # 这里给硬预算 + 短期缓存，把最坏情况钉死在 1 秒、且 15 秒才真探一次。
    # 局域网内一个活着的服务响应 < 50 ms，1 秒足够判断"是死是活"。
    _PING_BUDGET_S = 1.0
    _PING_CACHE_S = 15.0

    async def ping(self) -> Dict[str, Any]:
        now = time.time()
        if self._ping_cache and now - self._ping_cache[0] < self._PING_CACHE_S:
            return self._ping_cache[1]
        try:
            result = await asyncio.wait_for(self._ping_inner(),
                                            timeout=self._PING_BUDGET_S)
        except asyncio.TimeoutError:
            result = {"ok": False,
                      "error": f"探测超时（>{self._PING_BUDGET_S}s），外部系统可能不可达"}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._ping_cache = (time.time(), result)
        return result

    async def _ping_inner(self) -> Dict[str, Any]:
        if not (settings.ls_token or settings.ls_refresh_token):
            return {"ok": False, "error": "未配置 LABELSTUDIO_TOKEN / FORGE_LS_REFRESH_TOKEN"}
        try:
            r = await self._req("GET", "/api/current-user/whoami")
            if r.status_code != 200:
                return {"ok": False, "error": f"whoami HTTP {r.status_code}: {r.text[:120]}"}
            d = r.json()
            return {"ok": True,
                    "user": d.get("email") or d.get("username") or f"uid={d.get('id')}",
                    "auth": "jwt-refresh" if self._refresh else "jwt-access"}
        except IntegrationError as exc:
            return {"ok": False, "error": exc.detail}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- 项目 ---------------------------------------------------------------
    async def list_projects(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        path = "/api/projects/?page_size=100"
        while path:
            r = await self._req("GET", path)
            if r.status_code != 200:
                raise IntegrationError("labelstudio", f"列项目失败 {r.status_code}", r.status_code)
            data = r.json()
            out.extend(data.get("results", []))
            nxt = data.get("next")
            path = nxt.replace(self.base, "") if nxt else ""
        return out

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        """读单个 LS 项目，主要用来核实「要绑定的项目号真的存在」。

        绑定接口在写库之前先调它：绑到不存在的 LS 项目，甲方点「在标注引擎
        里设计」只会进 404，而且界面上还显示「已绑定」—— 属于骗人。
        顺带把 label_config 带回来，供对账/诊断读现状。
        """
        r = await self._req("GET", f"/api/projects/{project_id}/")
        if r.status_code != 200:
            raise IntegrationError("labelstudio", f"读项目失败 {r.status_code}",
                                   r.status_code)
        d = r.json()
        return {"id": d.get("id"), "title": d.get("title"),
                "label_config": d.get("label_config") or ""}

    async def create_project(self, title: str, label_config: str,
                             description: str = "") -> Dict[str, Any]:
        r = await self._req("POST", "/api/projects/", json={
            "title": title, "label_config": label_config, "description": description})
        if r.status_code not in (200, 201):
            raise IntegrationError("labelstudio", f"建项目失败 {r.status_code}: {r.text[:300]}",
                                   r.status_code)
        d = r.json()
        return {"id": d["id"], "title": d.get("title"), "task_number": d.get("task_number", 0)}

    async def import_tasks(self, project_id: int, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """tasks 为 LS 原生格式 [{"data": {"image": "https://..."}}]。"""
        r = await self._req("POST", f"/api/projects/{project_id}/import", json=tasks)
        if r.status_code not in (200, 201):
            raise IntegrationError("labelstudio", f"导入失败 {r.status_code}: {r.text[:300]}",
                                   r.status_code)
        return r.json()

    async def update_project(self, project_id: int, **fields: Any) -> Dict[str, Any]:
        """改 LS 项目属性（目前只用来把标题对齐到平台的「批次号 · 名称」）。

        平台是命名的**主源**：同一批数据在两边叫两个名字，一线最容易搞混，
        所以同步时以平台标题为准回写。
        """
        r = await self._req("PATCH", f"/api/projects/{project_id}/", json=fields)
        if r.status_code not in (200, 201):
            raise IntegrationError("labelstudio",
                                   f"改项目失败 {r.status_code}: {r.text[:200]}",
                                   r.status_code)
        d = r.json()
        return {"id": d.get("id"), "title": d.get("title")}

    async def project_stats(self, project_id: int) -> Dict[str, Any]:
        """逐条任务聚合出「条目数 / 已标注数 / 标注记录数」。

        **为什么不用 `/api/projects/{id}/stats`**（踩过的坑）：
        LS 1.23 CE 根本没有这个端点，请求一律 404。曾经照旧版文档写了，
        结果对账接口整个瘫痪 —— errors 里全是 `[labelstudio] stats 404`，
        看起来像对账逻辑错了，实际是数据源压根不存在。
        现在改为拉 `/api/tasks?project=N` 逐行累加，字段都是列表接口自带的：
          total_annotations —— 这一题上的标注记录数（一题多人标过就会 >1）
          is_labeled        —— 这一题是否已达最大标注数
        """
        tasks = await self.list_tasks(project_id)
        return {
            "total": sum(int(t.get("total_annotations") or 0) for t in tasks),
            "completed": sum(1 for t in tasks if t.get("is_labeled")),
            "tasks": len(tasks),
        }

    async def list_tasks(self, project_id: int, page_size: int = 100) -> List[Dict[str, Any]]:
        """把项目下的任务全部翻出来（对账只关心计数）。

        响应结构和本项目其它列表接口**不一样**（踩过的坑）：
          `/api/projects/`、`/api/webhooks/` 用 DRF 的 {"results": [...]} 分页，
          而 `/api/tasks` 返回的是 {"tasks": [...], "total": N}。
        照 DRF 习惯取 results 会静默拿到空数组 —— 于是「已标注数」永远是 0，
        同步看起来"执行成功"但一点没变，排查会误以为是写库失败。

        `fields` 参数在这个版本被忽略（传了也照样返回全字段），所以不传；
        代价是每行带 data / annotations_results，页太大会很重，page_size 保持 100。
        """
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            r = await self._req(
                "GET", f"/api/tasks?project={project_id}"
                       f"&page={page}&page_size={page_size}")
            if r.status_code != 200:
                raise IntegrationError("labelstudio",
                                       f"列任务失败 {r.status_code}: {r.text[:160]}",
                                       r.status_code)
            data = r.json()
            if isinstance(data, list):          # 有的版本不分页，直接给数组
                out.extend(data)
                break
            batch = data.get("tasks") or data.get("results") or []
            out.extend(batch)
            total = int(data.get("total") or 0)
            # 三种收口：拿空 / 拿够 / 到页数上限。上限是防御 —— 
            # 万一后端忽略分页参数，别把进程转死。
            if not batch or len(out) >= total or page >= 100:
                break
            page += 1
        return out

    async def project_summary(self, project_id: int,
                              with_rows: bool = False) -> Dict[str, Any]:
        """一次拿到对账要的全部数字：标题、条目总数、已标注条目、标注记录数。

        total_annotations 是「标注记录条数」（一题可有多人/多次标注），
        completed 是「已标注的条目数」—— 两者不同，done_count 要用后者，
        否则同一题被两个人标过就会算成 2 条进度。

        `with_rows=True` 会把本轮**同一份** LS 任务行原样带出来，供
        「标注员数据行一致性」自检复用。为什么要这么做：平台页面要展示的
        每一条数据都必须与 LS 当下同源；如果自检另外再拉一次、或把行复制
        进平台库，就又会出现两份可能不同的数据。这里只拉一次，计数与
        数据行从同一个 list_tasks 结果派生，天然不可能互相矛盾。
        """
        r = await self._req("GET", f"/api/projects/{project_id}/")
        if r.status_code != 200:
            raise IntegrationError("labelstudio",
                                   f"项目 {project_id} 读取失败 {r.status_code}",
                                   r.status_code)
        p = r.json()
        rows = await self.list_tasks(project_id)
        stats = {
            "total": sum(int(t.get("total_annotations") or 0) for t in rows),
            "completed": sum(1 for t in rows if t.get("is_labeled")),
            "tasks": len(rows),
        }
        out: Dict[str, Any] = {
            "id": project_id, "title": p.get("title", ""),
            # 以任务列表实际条数为准：project.task_number 是缓存字段，
            # 导入失败/删过任务时会和真实条数对不上，用它做对账容易误判。
            "tasks": int(stats["tasks"] or p.get("task_number") or 0),
            "annotations": int(stats["total"]),
            "completed": int(stats["completed"]),
        }
        if with_rows:
            out["rows"] = rows
        return out

    async def get_task(self, task_id: int) -> Dict[str, Any]:
        """读一条 LS 任务，用于「逐条进工作台」时核实归属。

        平台不能凭前端传来的 ls_task_id 就把人送进任意一条数据：先查 LS，
        确认 `project` 等于该账号 RLS 可见的目标项目，否则 404。
        CE 版没有项目成员隔离，这一步是唯一能防止跨项目串数据的地方。
        """
        r = await self._req("GET", f"/api/tasks/{task_id}/")
        if r.status_code != 200:
            raise IntegrationError("labelstudio",
                                   f"任务 {task_id} 读取失败 {r.status_code}",
                                   r.status_code)
        return r.json()

    # LS webhooks/models.py 里的动作常量（逐字抄，别按英文习惯改）：
    #   新增单条 = ANNOTATION_CREATED，新增批量 = ANNOTATIONS_CREATED，
    #   更新     = ANNOTATION_UPDATED，
    #   删除     = **ANNOTATIONS_DELETED（复数！）** —— 没有 ANNOTATION_DELETED。
    # 曾经按单数写，LS 那边照建不误、但删除事件永远不推，
    # 表现是「标注一条平台 +1，删掉平台不动」，进度只涨不跌。
    LS_WEBHOOK_ACTIONS = [
        "ANNOTATION_CREATED", "ANNOTATIONS_CREATED",
        "ANNOTATION_UPDATED", "ANNOTATIONS_DELETED",
    ]

    async def ensure_webhook(self, project_id: int, url: str) -> Dict[str, Any]:
        """确保项目挂着一个「标注变动就回调平台」的 webhook（幂等）。

        两个字段名都以 LS 1.23 的序列化结果为准（踩过的坑）：
          · 事件列表叫 **actions**，不是 event_actions。写成 event_actions 时
            PATCH 照样返回 200，但字段被静默忽略 —— 于是"校准成功"是假的，
            事件列表实际没变，问题被掩盖。
          · 删除事件是复数 ANNOTATIONS_DELETED。
        """
        want = list(self.LS_WEBHOOK_ACTIONS)
        r = await self._req("GET", f"/api/webhooks/?project={project_id}")
        existing = r.json() if r.status_code == 200 else []
        if isinstance(existing, dict):
            existing = existing.get("results", [])
        want_headers = ({"X-Forge-Secret": settings.ls_webhook_secret}
                        if settings.ls_webhook_secret else {})
        for w in existing:
            if w.get("url") != url:
                continue
            have = list(w.get("actions") or [])
            have_headers = dict(w.get("headers") or {})
            # send_for_all_actions=True 时 actions 为空但什么都推，算作已满足。
            actions_ok = (bool(w.get("send_for_all_actions"))
                          or sorted(have) == sorted(want))
            # 复用时必须核对鉴权头：平台配了 secret、而这条 webhook 是更早
            # （secret 为空时）建的 → 回调会被 handler 按 404 丢掉，这里却
            # 报 "reused ok"。症状和 SSRF 拦截完全一样：数字不动，但所有
            # 自检都说同步正常。老部署升级到带 secret 的版本时会踩。
            headers_ok = all(have_headers.get(k) == v
                             for k, v in want_headers.items())
            # 反向也要收敛：平台把 secret 撤掉（或将配置回退成空）后，LS 侧的
            # X-Forge-Secret 不会自动消失，会留下一个和当前配置不再对应的密钥头。
            # 功能上不致命（secret 为空时 handler 不校验），但等于把旧口令
            # 明文挂在 LS 的 webhook 配置里，任何能看该项目的账号都能读到。
            stale_secret = (not settings.ls_webhook_secret) and (
                "X-Forge-Secret" in have_headers)
            if stale_secret:
                headers_ok = False
            if actions_ok and headers_ok:
                return {"ok": True, "id": w.get("id"), "reused": True}
            patch: Dict[str, Any] = {}
            if not actions_ok:
                patch["actions"] = want
            if not headers_ok:
                merged = {**have_headers, **want_headers}
                if not settings.ls_webhook_secret:
                    merged.pop("X-Forge-Secret", None)
                patch["headers"] = merged
            pr = await self._req("PATCH", f"/api/webhooks/{w['id']}/", json=patch)
            if pr.status_code not in (200, 201):
                return {"ok": False, "id": w.get("id"), "reused": True,
                        "reason": f"校准 webhook 失败 {pr.status_code}: {pr.text[:160]}"}
            got = list((pr.json() or {}).get("actions") or [])
            return {"ok": True, "id": w.get("id"), "reused": True,
                    "events_updated": not actions_ok,
                    "headers_updated": not headers_ok, "actions": got}
        payload: Dict[str, Any] = {
            "project": project_id, "url": url,
            "actions": want,
            "is_active": True, "send_payload": True,
        }
        if settings.ls_webhook_secret:
            payload["headers"] = {"X-Forge-Secret": settings.ls_webhook_secret}
        r = await self._req("POST", "/api/webhooks/", json=payload)
        if r.status_code not in (200, 201):
            raise IntegrationError("labelstudio", f"注册 webhook 失败 {r.status_code}: {r.text[:200]}",
                                   r.status_code)
        return {"ok": True, "id": r.json().get("id"), "reused": False}

    @staticmethod
    def embed_path(project_id: int) -> str:
        """前端 iframe 用相对路径。

        路径必须用 /data：LS 1.23 的 React 路由是 /projects/{id}/data（标注流），
        旧的 /projects/{id}/label 已不存在，会命中前端 NotFound 组件显示 404。
        """
        return f"/projects/{project_id}/data"


ls = LabelStudioClient()


async def integrations_health() -> Dict[str, Any]:
    return {"labelstudio": await ls.ping()}
