"""Тесты транспортного слоя: парсинг, маскировка, ранжирование пула."""

from gatecheck_bot.transport.proxy_types import (
    HTTP,
    KIND_PRIORITY,
    SOCKS5,
    VLESS,
    ProxyInfo,
    mask_proxy_url,
    parse_proxy_line,
    parse_vless_uri,
)


def test_parse_socks5_url() -> None:
    info = parse_proxy_line("socks5://1.2.3.4:1080", source="s")
    assert info is not None
    assert info.kind == SOCKS5
    assert info.host == "1.2.3.4"
    assert info.port == 1080


def test_parse_hostport_defaults_to_socks5() -> None:
    info = parse_proxy_line("5.6.7.8:9999")
    assert info is not None
    assert info.kind == SOCKS5


def test_parse_http_url() -> None:
    info = parse_proxy_line("http://proxy.example:8080")
    assert info is not None
    assert info.kind == HTTP


def test_parse_vless() -> None:
    line = "vless://uuid@host:443?type=ws&security=tls&sni=a.b#c1"
    info = parse_proxy_line(line)
    assert info is not None
    assert info.kind == VLESS
    assert info.host == "host"
    assert info.port == 443
    parsed = parse_vless_uri(line)
    assert parsed is not None
    assert parsed["params"]["type"] == "ws"


def test_mtproto_and_unknown_lines_skipped() -> None:
    assert parse_proxy_line("tg://proxy?server=h&port=443&secret=ee") is None
    assert parse_proxy_line("https://t.me/proxy?server=h&port=443&secret=dd") is None
    assert parse_proxy_line("1.2.3.4:443#dd0011") is None  # MTProto host:port#secret
    assert parse_proxy_line("hysteria2://x@y:1") is None
    assert parse_proxy_line("vmess://abcdef") is None
    assert parse_proxy_line("# comment") is None
    assert parse_proxy_line("") is None


def test_mask_hides_credentials() -> None:
    masked = mask_proxy_url("socks5://user:secret@1.2.3.4:1080")
    assert "secret" not in masked
    assert masked == "socks5://1.2.3.4:1080"


def test_connector_url_vless_uses_local_xray() -> None:
    info = ProxyInfo(VLESS, "h", 443, local_port=12345, uri="vless://u@h:443")
    assert info.connector_url == "socks5://127.0.0.1:12345"
    assert info.cache_line() == "vless://u@h:443"


def test_ranking_prefers_vless_then_latency() -> None:
    pool = [
        ProxyInfo(SOCKS5, "a", 1, latency=0.1),
        ProxyInfo(VLESS, "b", 2, latency=0.5),
        ProxyInfo(HTTP, "c", 3, latency=0.05),
    ]
    pool.sort(key=lambda p: (KIND_PRIORITY.get(p.kind, 9), p.latency))
    assert [p.kind for p in pool] == [VLESS, SOCKS5, HTTP]