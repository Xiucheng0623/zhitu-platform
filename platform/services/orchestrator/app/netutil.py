"""小的 HTTP 工具。放这里是为了让 main 与各 router 共用同一份实现，
避免"三个文件各抄一遍 _ip"这种必然会漂移的写法。"""
from __future__ import annotations

from typing import Optional

from fastapi import Request


def client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None
