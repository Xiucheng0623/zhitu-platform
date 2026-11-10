#!/usr/bin/env python3
"""
Label Studio 项目 Provisioning
================================================================
把 labelstudio/configs/*.xml 变成 LS 里真实存在的项目，并挂上回调。

幂等策略：按项目 title 精确匹配。已存在则只更新 label_config（不动已有标注），
不存在才创建。因此可以反复执行，不会产生重复项目。

LS 会在 create/update 时校验 label_config 的合法性，返回 400 + 具体节点错误 ——
这就是我们的 config 语法校验器，不需要自己写 XML schema。

用法：
  python labelstudio/provision.py --ls-url http://localhost:8080 --token <TOKEN>
  python labelstudio/provision.py --dry-run          # 只校验本地文件与计划动作
  python labelstudio/provision.py --webhook-url http://localhost:8300/api/v1/webhooks/labelstudio

依赖：httpx（orchestrator 已装）。不依赖 label-studio SDK。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    sys.exit("需要 httpx：python -m pip install httpx -i https://mirrors.aliyun.com/pypi/simple/")

CONFIG_DIR = Path(__file__).parent / "configs"

# 文件名 → (LS 项目名, 模态, 说明)。项目名是幂等键，改名前请先确认 LS 里没有同名项目。
MANIFEST: List[Dict[str, str]] = [
    {"file": "01-bbox-sku.xml", "title": "FORGE · 商品图 BBox + SKU 分类",
     "modality": "image", "desc": "电商商品图主体框选与类目分类"},
    {"file": "02-polygon-dicom.xml", "title": "FORGE · 医学影像多边形分割",
     "modality": "image", "desc": "DICOM 预览图的病灶轮廓分割"},
    {"file": "03-video-temporal.xml", "title": "FORGE · 视频时序边界与安全标签",
     "modality": "video", "desc": "行车/作业视频的片段切分与安全等级"},
    {"file": "04-audio-asr.xml", "title": "FORGE · 语音转写与说话人分离",
     "modality": "audio", "desc": "客服/车载语音逐句转写与意图标注"},
    {"file": "05-text-ner.xml", "title": "FORGE · 文本 NER 与意图分类",
     "modality": "text", "desc": "工单文本实体抽取与意图判定"},
]


def load_config(entry: Dict[str, str]) -> str:
    """本地先做能做的校验：XML 良构 + 至少一个标注控件 + toName 指向存在的对象。

    LS 自己的校验更严格（会检查控件/对象组合是否合法），但那个必须等 LS 起来。
    这里先把「一眼就能查出来」的问题挡在提交之前，省一轮来回。
    """
    path = CONFIG_DIR / entry["file"]
    if not path.is_file():
        raise FileNotFoundError(f"缺少模板文件：{path}")
    text = path.read_text(encoding="utf-8").strip()
    if "<View>" not in text:
        raise ValueError(f"{path.name} 缺少 <View> 根节点，LS 会拒绝")

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"{path.name} XML 不合法：{exc}")

    # 对象标签：有 name + value 的（Image/Text/Audio/Video/Paragraphs…）
    objects = {el.get("name") for el in root if el.get("value") and el.get("name")}
    # 标注控件：带 toName 的
    controls = [el for el in root if el.get("toName")]
    if not controls:
        raise ValueError(f"{path.name} 没有任何标注控件（toName 缺失）")
    for c in controls:
        target = c.get("toName", "").split(",")[0].strip()
        if target and target not in objects:
            raise ValueError(
                f"{path.name} 控件 <{c.tag} name={c.get('name')}> 的 toName={target} "
                f"找不到对应对象（现有对象：{sorted(o for o in objects if o)}）")
    # 标注控件必须至少有一个可选标签，否则标注员无从下手
    for c in controls:
        if c.tag in ("RectangleLabels", "PolygonLabels", "KeyPointLabels",
                     "Labels", "Choices", "TimelineLabels", "Taxonomy"):
            if not len(c):
                raise ValueError(f"{path.name} 控件 <{c.tag} name={c.get('name')}> 没有任何选项")
    return text


class LS:
    def __init__(self, base: str, token: str, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base,
            # 1.23 起只能用 Bearer；legacy `Token` 认证已被移除
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=False,   # 内网地址绝不走系统代理（否则会被代理改写成 404/502）
        )

    def whoami(self) -> Dict[str, Any]:
        r = self.client.get("/api/current-user/whoami")
        r.raise_for_status()
        return r.json()

    def list_projects(self) -> List[Dict[str, Any]]:
        """LS 的分页上限是 100；传更大值会返回 404（不是 400，容易误判成路径写错）。
        因此这里固定 100 并逐页跟进 next。"""
        out: List[Dict[str, Any]] = []
        url: Optional[str] = "/api/projects/"
        params: Dict[str, Any] = {"page_size": 100}
        while url:
            r = self.client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                out.extend(data)
                break
            out.extend(data.get("results", []))
            nxt = data.get("next")
            if not nxt:
                break
            # next 是绝对 URL，拆成 path+query 交给 base_url 处理
            url = nxt.replace(self.base, "")
            params = {}
        return out

    def create_project(self, title: str, label_config: str, description: str) -> Dict[str, Any]:
        r = self.client.post("/api/projects", json={
            "title": title, "label_config": label_config, "description": description,
        })
        if r.status_code not in (200, 201):
            raise RuntimeError(f"建项目失败 {r.status_code}: {r.text[:400]}")
        return r.json()

    def update_config(self, pid: int, label_config: str) -> Dict[str, Any]:
        r = self.client.patch(f"/api/projects/{pid}", json={"label_config": label_config})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"更新 config 失败 {r.status_code}: {r.text[:400]}")
        return r.json()

    def ensure_webhook(self, pid: int, url: str, secret: str = "") -> Dict[str, Any]:
        r = self.client.get("/api/webhooks", params={"project": pid})
        existing = r.json() if r.status_code == 200 else []
        if isinstance(existing, dict):
            existing = existing.get("results", [])
        for w in existing:
            if w.get("url") == url:
                return {"ok": True, "id": w.get("id"), "reused": True}
        payload = {
            "project": pid, "url": url,
            "event_actions": ["ANNOTATION_CREATED", "ANNOTATION_UPDATED"],
            "is_active": True, "send_payload": True,
        }
        if secret:
            payload["headers"] = {"X-Forge-Secret": secret}
        r = self.client.post("/api/webhooks", json=payload)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"注册 webhook 失败 {r.status_code}: {r.text[:300]}")
        return {"ok": True, "id": r.json().get("id"), "reused": False}


def main() -> int:
    ap = argparse.ArgumentParser(description="FORGE × Label Studio 项目 provisioning")
    ap.add_argument("--ls-url", default="http://localhost:8080")
    ap.add_argument("--token", default="", help="LS access token（5 分钟有效）")
    ap.add_argument("--refresh", default="",
                    help="LS refresh token；给出则自动换 access，更适合脚本化调用")
    ap.add_argument("--webhook-url", default="", help="orchestrator 回调地址")
    ap.add_argument("--webhook-secret", default="", help="对应 FORGE_LS_WEBHOOK_SECRET")
    ap.add_argument("--dry-run", action="store_true", help="只检查本地模板，不调 API")
    ap.add_argument("--output", default="", help="把映射结果写成 JSON 文件")
    args = ap.parse_args()

    # 1) 本地模板自检（不依赖 LS 也能跑）
    configs: Dict[str, str] = {}
    problems = 0
    for entry in MANIFEST:
        try:
            configs[entry["title"]] = load_config(entry)
            print(f"  OK    {entry['file']}  ({len(configs[entry['title']])} 字节)")
        except Exception as exc:  # noqa: BLE001
            problems += 1
            print(f"  FAIL  {entry['file']}  {exc}")
    if problems:
        return 1
    if args.dry_run:
        print(f"\n本地模板全部就绪（{len(MANIFEST)} 个）。--dry-run 未调用 API。")
        return 0

    if not (args.token or args.refresh):
        return print("缺少 --token 或 --refresh"
                     "（LS：Account & Settings → Access Token，或取 /api/token/ 返回的 refresh）") or 2

    token = args.token
    if args.refresh:
        try:
            with httpx.Client(timeout=15, trust_env=False) as c:
                r = c.post(f"{args.ls_url.rstrip('/')}/api/token/refresh/",
                           json={"refresh": args.refresh})
            if r.status_code != 200:
                return print(f"refresh 换 access 失败 {r.status_code}: {r.text[:200]}") or 2
            token = r.json()["access"]
        except httpx.HTTPError as exc:
            return print(f"连接 LS 失败：{type(exc).__name__}: {exc}") or 2

    ls = LS(args.ls_url, token)
    try:
        me = ls.whoami()
        print(f"\n已连接 Label Studio：{me.get('username')} @ {args.ls_url}")
    except httpx.HTTPError as exc:
        return print(f"连接 LS 失败：{type(exc).__name__}: {exc}") or 2

    existing = {p["title"]: p for p in ls.list_projects()}
    result: List[Dict[str, Any]] = []

    for entry in MANIFEST:
        title = entry["title"]
        cfg = configs[title]
        hit = existing.get(title)
        if hit:
            try:
                ls.update_config(hit["id"], cfg)
                action = "updated"
            except RuntimeError as exc:
                print(f"  FAIL  {title}: {exc}")
                return 1
            pid = hit["id"]
        else:
            try:
                created = ls.create_project(title, cfg, entry["desc"])
            except RuntimeError as exc:
                print(f"  FAIL  {title}: {exc}")
                return 1
            pid = created["id"]
            action = "created"

        hook = None
        if args.webhook_url:
            try:
                hook = ls.ensure_webhook(pid, args.webhook_url, args.webhook_secret)
            except RuntimeError as exc:
                hook = {"ok": False, "error": str(exc)}

        result.append({**entry, "ls_project_id": pid, "action": action, "webhook": hook})
        print(f"  {action:8s} #{pid:<6d} {title}")

    print("\n把下面这些 ls_project_id 写进 forge.projects.labeling_config_id / tasks.ls_project_id：")
    print(json.dumps([{ "title": r["title"], "ls_project_id": r["ls_project_id"]} for r in result],
                     ensure_ascii=False, indent=2))

    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n映射已写入 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
