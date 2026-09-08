"""Локальный xray-core: превращает vless:// в локальный SOCKS5-порт.

При первом использовании скачивает бинарник с GitHub releases, генерирует
конфиг из vless:// URI и поднимает SOCKS5 на 127.0.0.1:<случайный порт> —
его уже используют aiohttp/aiohttp_socks (checker.py, pool.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import socket
import urllib.request
import zipfile
from pathlib import Path

from .proxy_types import parse_vless_uri

logger = logging.getLogger("gatecheck_bot.transport")

STARTUP_WAIT_SECONDS = 10.0


class XrayManager:
    """Жизненный цикл одного xray-процесса (vless → локальный socks5)."""

    def __init__(self, bin_dir: str | Path = "data/xray") -> None:
        self.bin_dir = Path(bin_dir)
        self.binary = self.bin_dir / ("xray.exe" if os.name == "nt" else "xray")
        self.process: asyncio.subprocess.Process | None = None
        self.config_path: Path | None = None
        self.local_port: int | None = None

    def _download_url(self) -> str:
        if os.name == "nt":
            return "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-windows-64.zip"
        system = platform.system().lower()
        if system == "darwin":
            return "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-macos-64.zip"
        if platform.machine().lower() in ("aarch64", "arm64"):
            return "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-arm64-v8a.zip"
        return "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"

    async def ensure_binary(self) -> bool:
        if self.binary.exists():
            return True
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        url = self._download_url()
        logger.info("Скачиваю xray-core: %s", url)
        loop = asyncio.get_running_loop()

        def _download() -> bool:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = resp.read()
            zip_path = self.bin_dir / "xray.zip"
            zip_path.write_bytes(data)
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if os.path.basename(name) == self.binary.name:
                        self.binary.write_bytes(zf.read(name))
                        break
            zip_path.unlink(missing_ok=True)
            if os.name != "nt":
                self.binary.chmod(0o755)
            return self.binary.exists()

        try:
            return await loop.run_in_executor(None, _download)
        except Exception as exc:
            logger.warning("Не удалось скачать xray-core: %s", exc)
            return False

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _build_config(parsed: dict, local_port: int) -> dict:
        """Конфиг xray из разобранного vless:// (tcp/ws/grpc, tls/reality)."""
        params = parsed["params"]
        network = params.get("type", "tcp")
        security = params.get("security", "none")
        stream: dict = {"network": network, "security": security}
        if security == "tls":
            stream["tlsSettings"] = {"serverName": params.get("sni", ""), "allowInsecure": True}
        if security == "reality":
            stream["realitySettings"] = {
                "serverName": params.get("sni", ""),
                "fingerprint": params.get("fp", "chrome"),
                "publicKey": params.get("pbk", ""),
                "shortId": params.get("sid", ""),
            }
        if network == "ws":
            stream["wsSettings"] = {"path": params.get("path", "/")}
        if network == "grpc":
            stream["grpcSettings"] = {"serviceName": params.get("serviceName", "")}
        return {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "port": local_port,
                    "listen": "127.0.0.1",
                    "protocol": "socks",
                    "settings": {"udp": True},
                }
            ],
            "outbounds": [
                {
                    "protocol": "vless",
                    "settings": {
                        "vnext": [
                            {
                                "address": parsed["host"],
                                "port": parsed["port"],
                                "users": [
                                    {
                                        "id": parsed["uuid"],
                                        "encryption": "none",
                                        "flow": params.get("flow", ""),
                                    }
                                ],
                            }
                        ]
                    },
                    "streamSettings": stream,
                }
            ],
        }

    async def start(self, vless_uri: str) -> int | None:
        """Запустить xray с конфигом из vless://; вернуть локальный порт или None."""
        parsed = parse_vless_uri(vless_uri)
        if parsed is None:
            return None
        if not await self.ensure_binary():
            return None
        await self.stop()
        self.local_port = self._find_free_port()
        config = self._build_config(parsed, self.local_port)
        self.config_path = self.bin_dir / f"xray_config_{self.local_port}.json"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.binary),
                "run",
                "-config",
                str(self.config_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning("Не удалось запустить xray: %s", exc)
            await self.stop()
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_WAIT_SECONDS
        while loop.time() < deadline:
            if self._port_open("127.0.0.1", self.local_port):
                return self.local_port
            await asyncio.sleep(0.2)
        logger.warning("xray не поднял локальный порт вовремя.")
        await self.stop()
        return None

    async def stop(self) -> None:
        """Остановить xray и убрать временный конфиг (безопасно звать многократно)."""
        process, self.process = self.process, None
        if process is not None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        if self.config_path is not None:
            self.config_path.unlink(missing_ok=True)
            self.config_path = None
        self.local_port = None

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False