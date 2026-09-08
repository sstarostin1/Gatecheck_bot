"""Типы, парсинг и сериализация прокси-кандидатов.

Пул несёт только HTTPS-совместимые типы: socks5, http и vless (через локальный
xray-core). MTProto-прокси (tg://proxy, host:port#secret) и прочие схемы
(hysteria2, vmess, trojan, ss) намеренно отбрасываются: MTProto-прокси туннелируют
только MTProto-трафик и для Bot API непригодны, остальные — вне скоупа v1.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

SOCKS5 = "socks5"
HTTP = "http"
VLESS = "vless"

# Порядок предпочтения при равной пригодности: vless устойчивее к DPI,
# затем socks5, затем http.
KIND_PRIORITY = {VLESS: 0, SOCKS5: 1, HTTP: 2}

# Проверенные открытые списки (обычный HTTPS, доступен даже при блокировке TG).
DEFAULT_SOURCES = (
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/Argh94/Proxy-List/main/SOCKS5.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    "https://raw.githubusercontent.com/Argh94/Proxy-List/main/All_Config.txt",
)


@dataclass
class ProxyInfo:
    """Кандидат/рабочий прокси (kind: socks5 | http | vless)."""

    kind: str
    host: str
    port: int
    uri: str = ""  # исходная строка vless:// (нужна xray)
    source: str = ""  # откуда взяли (manual/cache/URL источника)
    latency: float = float("inf")  # сек; время пробы до api.telegram.org
    local_port: int | None = None  # локальный socks5-порт xray (для vless)

    @property
    def connector_url(self) -> str:
        """URL для aiohttp_socks.ProxyConnector (для vless — локальный xray)."""
        if self.kind == VLESS and self.local_port:
            return f"socks5://127.0.0.1:{self.local_port}"
        return f"{self.kind}://{self.host}:{self.port}"

    def label(self) -> str:
        return f"{self.kind}://{self.host}:{self.port}"

    def cache_line(self) -> str:
        """Строка для кэша/восстановления (vless сохраняется целиком)."""
        return self.uri if (self.kind == VLESS and self.uri) else self.label()


def mask_proxy_url(proxy_url: str) -> str:
    """Скрыть логин/пароль в URL прокси — для безопасного вывода в лог."""
    parts = urllib.parse.urlsplit(proxy_url)
    host = parts.hostname or "?"
    if ":" in host:
        host = f"[{host}]"  # IPv6-литерал
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


_VLESS_RE = re.compile(r"^vless://([^@\s]+)@([^:\s/]+):(\d+)(?:\?([^#]*))?(?:#.*)?$")
_PROXY_URL_RE = re.compile(r"^(socks5|http)://(?:[^@\s/]+@)?([^:\s/]+):(\d+)/?$", re.IGNORECASE)
_HOSTPORT_RE = re.compile(r"^([^:\s#]+):(\d+)$")


def parse_vless_uri(uri: str) -> dict | None:
    """Разобрать vless://uuid@host:port?params#name в словарь (или None)."""
    match = _VLESS_RE.match(uri.strip())
    if not match:
        return None
    uuid, host, port, params = match.groups()
    params_dict: dict[str, str] = {}
    for kv in (params or "").split("&"):
        if "=" in kv:
            key, value = kv.split("=", 1)
            params_dict[key] = urllib.parse.unquote(value)
    return {"uuid": uuid, "host": host, "port": int(port), "params": params_dict}


def parse_proxy_line(line: str, source: str = "") -> ProxyInfo | None:
    """Одна строка списка → ProxyInfo, либо None (формат не несёт Bot API)."""
    line = line.strip()
    if not line or line.startswith(("#", "//")):
        return None
    if line.lower().startswith("vless://"):
        parsed = parse_vless_uri(line)
        if parsed:
            return ProxyInfo(VLESS, parsed["host"], parsed["port"], uri=line, source=source)
        return None
    match = _PROXY_URL_RE.match(line)
    if match:
        return ProxyInfo(match.group(1).lower(), match.group(2), int(match.group(3)), source=source)
    match = _HOSTPORT_RE.match(line)
    if match:  # host:port без секрета — традиционно это SOCKS5-список
        return ProxyInfo(SOCKS5, match.group(1), int(match.group(2)), source=source)
    return None