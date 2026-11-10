"""
FORGE · 事件循环工具
================================================================
Windows 上 psycopg3 的异步连接**只支持 SelectorEventLoop**，而
`asyncio.run()` 默认在 Windows 建 ProactorEventLoop，直接报：

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode

Web 服务端由 app/serve.py 自行接管事件循环解决了；但运维脚本
（tools/*）同样是 async 入口，也必须走同一个循环类型，否则会出现
"服务能跑、脚本连不上库"这种很费时间的错觉。

用法：
    from app.looputil import run_async
    raise SystemExit(run_async(my_async_main(args)))
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """在 Windows 上用 SelectorEventLoop 跑协程，其它平台等同 asyncio.run。"""
    if sys.platform != "win32":
        return asyncio.run(coro)

    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
