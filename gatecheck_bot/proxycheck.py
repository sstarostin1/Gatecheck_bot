"""Диагностика автономного пула: добыть и проверить прокси, не запуская бота.

Использование:
    .venv\\Scripts\\python -m gatecheck_bot.proxycheck
"""

from __future__ import annotations

import asyncio
import logging

from .config import ConfigError, Settings
from .transport import ProxyPool
from .transport.xray import XrayManager


async def _main() -> int:
    settings = Settings.from_env(require_token=False)
    xray = XrayManager()
    pool = ProxyPool(
        sources=settings.proxy_sources,
        manual=settings.proxy_manual,
        cache_path=settings.proxy_cache_path,
        pool_size=settings.proxy_pool_size,
        check_timeout=settings.proxy_check_timeout,
        xray=xray,
    )
    working = await pool.refresh()
    print(f"\nРабочих прокси: {working}")
    for info in pool.pool:
        latency = "—" if info.latency == float("inf") else f"{info.latency * 1000:.0f} ms"
        print(f"  [{info.kind:>6}] {info.label():<40} {latency:>9}  ({info.source})")
    await xray.stop()
    return 0 if working else 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    try:
        code = asyncio.run(_main())
    except ConfigError as exc:
        print(f"Конфигурация: {exc}")
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()