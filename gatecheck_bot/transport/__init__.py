"""Транспортный слой: автономный прокси-пул для доступа к api.telegram.org."""

from .pool import ProxyPool
from .proxy_types import (
    DEFAULT_SOURCES,
    HTTP,
    SOCKS5,
    VLESS,
    ProxyInfo,
    mask_proxy_url,
    parse_proxy_line,
    parse_vless_uri,
)
from .xray import XrayManager

__all__ = [
    "DEFAULT_SOURCES",
    "HTTP",
    "SOCKS5",
    "VLESS",
    "ProxyInfo",
    "ProxyPool",
    "XrayManager",
    "mask_proxy_url",
    "parse_proxy_line",
    "parse_vless_uri",
]