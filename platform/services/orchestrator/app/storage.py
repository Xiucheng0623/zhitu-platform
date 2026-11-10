"""
FORGE Orchestrator · 对象存储（MinIO / S3 兼容）
================================================================
Key 规则（多租户隔离的第一道物理防线）：

    {enterprise_id}/{project_id}/{batch_no}/{filename}

不在 key 里带租户前缀的话，将来任何一次「按前缀列对象」的运维脚本或
误配的 bucket policy 都会跨租户泄露数据。所以前缀由服务端拼，
**不接受客户端传入的完整 key**。

上传走预签名 URL：文件不经过中间层，避免大文件把 FastAPI 的 worker 堵死。
预签名有效期故意压到 15 分钟——够一次上传，不足以长期外泄。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import quote

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .config import settings
from .security import Principal

log = logging.getLogger("forge.storage")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

_client = None


def client():
    global _client
    if _client is None:
        # 与 clients.py 同一原则：默认不让系统代理插手内网对象存储请求。
        # boto3 通过 urllib3 读环境变量，这里显式清空即可。
        cfg: Dict[str, Any] = {
            "signature_version": "s3v4",
            "s3": {"addressing_style": "path"},
            # 关键：boto3 默认 connect_timeout=60 且重试 3 次，对象存储不可达时
            # 一次 head_bucket 能卡 20 秒以上。健康检查端点因此会拖垮整个探活，
            # 监控看到的是"平台挂了"，实际只是 MinIO 没起来。
            "connect_timeout": 2,
            "read_timeout": 5,
            "retries": {"max_attempts": 1},
        }
        if not settings.http_trust_env:
            cfg["proxies"] = {}
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=BotoConfig(**cfg),
        )
    return _client


def safe_name(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    cleaned = _SAFE.sub("-", base).strip("-.")
    return cleaned[:120] or "file"


def build_key(p: Principal, project_id: str, batch_no: str, filename: str) -> str:
    return "/".join([
        p.tenant_id,
        _SAFE.sub("-", project_id),
        _SAFE.sub("-", batch_no),
        safe_name(filename),
    ])


async def ensure_bucket() -> Dict[str, Any]:
    """启动时确保 bucket 存在；失败不致命（存储可后置到 Phase 4）。"""
    def _do() -> Dict[str, Any]:
        c = client()
        try:
            c.head_bucket(Bucket=settings.s3_bucket)
            return {"ok": True, "bucket": settings.s3_bucket, "created": False}
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket", "NotFound"):
                c.create_bucket(Bucket=settings.s3_bucket)
                return {"ok": True, "bucket": settings.s3_bucket, "created": True}
            raise
    try:
        return await asyncio.to_thread(_do)
    except (BotoCoreError, ClientError, Exception) as exc:  # noqa: BLE001
        log.warning("对象存储不可用（不阻塞启动）: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def presign_put(p: Principal, project_id: str, batch_no: str, filename: str,
                      content_type: str = "application/octet-stream",
                      expires_in: int = 900) -> Dict[str, Any]:
    key = build_key(p, project_id, batch_no, filename)

    def _do() -> str:
        return client().generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.s3_bucket, "Key": key,
                    "ContentType": content_type},
            ExpiresIn=expires_in,
        )
    url = await asyncio.to_thread(_do)
    return {"key": key, "url": url, "expires_in": expires_in,
            "bucket": settings.s3_bucket, "content_type": content_type}


async def presign_get(p: Principal, key: str, expires_in: int = 900) -> Dict[str, Any]:
    # 越权保护：只允许签发本租户前缀内的 key
    if not key.startswith(f"{p.tenant_id}/"):
        raise PermissionError("key 不属于当前租户")
    if p.supplier_id:
        # 供应商侧不做细粒度对象鉴权（目录粒度在资产表里控制），此处直接拒
        raise PermissionError("供应商主体无对象直读权限")
    def _do() -> str:
        return client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    url = await asyncio.to_thread(_do)
    return {"key": key, "url": url, "expires_in": expires_in}


async def health() -> Dict[str, Any]:
    """健康探测必须有硬上限。存储不可达时要立刻说"不可用"，
    而不是让调用方等着 —— 探活接口超时会被上游判定为服务整体故障。"""
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(lambda: client().head_bucket(Bucket=settings.s3_bucket)),
            timeout=3.0,
        )
        return {"ok": True, "bucket": settings.s3_bucket,
                "region": info.get("BucketRegion") or settings.s3_region}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "TimeoutError: 对象存储 3s 内无响应"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def public_hint(key: str) -> Optional[str]:
    """给前端展示用的对象路径（不含签名）。"""
    return f"{settings.s3_endpoint.rstrip('/')}/{settings.s3_bucket}/{quote(key)}"
