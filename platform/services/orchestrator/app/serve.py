"""
FORGE Orchestrator · 启动入口
================================================================
为什么需要这个文件（这不是可有可无的包装）：

uvicorn 0.54 的 `uvicorn.loops.asyncio.asyncio_loop_factory` 在 Windows 上
**硬编码返回 asyncio.ProactorEventLoop**，完全不看 asyncio 的事件循环策略：

    def asyncio_loop_factory(use_subprocess: bool = False):
        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop      # ← 就这一行
        return asyncio.SelectorEventLoop

而 psycopg3 的异步连接只支持 SelectorEventLoop，于是只要直接
`uvicorn app.main:app` 启动，数据库连接池就会无限重连失败
（日志：Psycopg cannot use the 'ProactorEventLoop'）。

结论：Windows 下必须自己建事件循环并把 server.serve() 跑在 Selector 上，
不能走 uvicorn 的 loop 选择逻辑。Linux / 容器内行为不变。

用法：
  python -m app.serve                  # 读 FORGE_HOST / FORGE_PORT / FORGE_RELOAD
  python -m app.serve --port 8300 --reload
"""
from __future__ import annotations

import asyncio
import os
import sys

IS_WIN = sys.platform == "win32"


def _resolve(host: str, port: int, reload: bool) -> tuple[str, int, bool]:
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a in ("--host",) and i + 1 < len(argv):
            host = argv[i + 1]; i += 2; continue
        if a == "--reload":
            reload = True; i += 1; continue
        i += 1
    return host, port, reload


def main() -> None:
    import uvicorn

    host = os.environ.get("FORGE_HOST", "127.0.0.1")
    port = int(os.environ.get("FORGE_PORT", "8300"))
    reload = os.environ.get("FORGE_RELOAD", "").lower() in ("1", "true", "yes")
    log_level = os.environ.get("FORGE_LOG_LEVEL", "info")
    host, port, reload = _resolve(host, port, reload)

    if IS_WIN and not reload:
        # 显式接管事件循环：绕开 uvicorn 在 Windows 上强制 Proactor 的行为
        config = uvicorn.Config("app.main:app", host=host, port=port,
                                log_level=log_level, loop="asyncio")
        server = uvicorn.Server(config)
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        except KeyboardInterrupt:
            pass
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        return

    if IS_WIN and reload:
        print("[forge] 警告：Windows 下 --reload 会把子进程交回 uvicorn 的 Proactor 工厂，"
              "数据库连接会失败。联调请用非 reload 启动：python -m app.serve",
              file=sys.stderr)

    uvicorn.run("app.main:app", host=host, port=port, reload=reload, log_level=log_level)


if __name__ == "__main__":
    main()
