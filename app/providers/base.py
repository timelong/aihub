"""Provider 抽象基类与通用工具。"""
from __future__ import annotations

import base64
from typing import Any, AsyncIterator

import httpx

TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)


class ProviderError(RuntimeError):
    pass


class BaseProvider:
    kind = "base"

    def __init__(self, conf: dict[str, Any]):
        self.conf = conf
        self.id = conf["id"]
        self.name = conf.get("name", conf["id"])
        self.base_url = (conf.get("base_url") or "").rstrip("/")
        self.api_key = conf.get("api_key") or ""

    # --- 能力接口，子类按需覆写 ---
    async def chat_stream(self, model: str, messages: list[dict],
                          params: dict) -> AsyncIterator[dict]:
        raise ProviderError(f"{self.name} 不支持对话")
        yield  # pragma: no cover

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        raise ProviderError(f"{self.name} 不支持图片生成")

    async def submit_video(self, model: str, prompt: str, params: dict) -> str:
        raise ProviderError(f"{self.name} 不支持视频生成")

    async def poll_video(self, model: str, remote_id: str) -> dict:
        raise ProviderError(f"{self.name} 不支持视频生成")

    # --- 工具 ---
    def client(self, **kw) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, **kw)

    def require_key(self) -> None:
        if not self.api_key:
            raise ProviderError(
                f"{self.name} 未配置 API Key，请在 .env 或 config.yaml 中填写。"
            )

    @staticmethod
    def check(resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:600]}")
        return resp.json()


def split_data_url(data_url: str) -> tuple[str, bytes]:
    """'data:image/png;base64,xxx' -> ('image/png', bytes)"""
    header, b64 = data_url.split(",", 1)
    mime = header.split(";")[0].removeprefix("data:") or "image/png"
    return mime, base64.b64decode(b64)


def is_data_url(s: str) -> bool:
    return s.startswith("data:")
