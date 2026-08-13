"""Provider 抽象基类与通用工具。"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ..logging_setup import preview

TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
log = logging.getLogger("aihub.provider")


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
        """带日志钩子的 httpx client：每个上游请求/响应都会记到日志文件。"""
        hooks = {"request": [self._on_request], "response": [self._on_response]}
        kw.setdefault("event_hooks", hooks)
        return httpx.AsyncClient(timeout=TIMEOUT, **kw)

    async def _on_request(self, request: httpx.Request) -> None:
        request.extensions["aihub_t0"] = time.perf_counter()
        body = ""
        if log.isEnabledFor(logging.DEBUG):
            try:
                body = " body=" + preview(request.content)
            except Exception:  # noqa: BLE001  # 流式/multipart 请求读不到内容
                body = ""
        # 轮询会重复几十次，只在 DEBUG 下打原始行；进度由 poll_progress() 汇总
        lvl = logging.DEBUG if request.extensions.get("aihub_poll") else logging.INFO
        log.log(lvl, "→ [%s] %s %s%s", self.id, request.method, request.url, body)

    async def _on_response(self, response: httpx.Response) -> None:
        t0 = response.request.extensions.get("aihub_t0")
        ms = f"{(time.perf_counter() - t0) * 1000:.0f}ms" if t0 else "-"
        stream = response.headers.get("content-type", "").startswith("text/event-stream")
        detail = ""
        if response.status_code >= 400 or (log.isEnabledFor(logging.DEBUG) and not stream):
            try:
                await response.aread()
                detail = " " + preview(response.text)
            except Exception:  # noqa: BLE001
                detail = ""
        if response.status_code >= 400:
            lvl = logging.ERROR
        elif response.request.extensions.get("aihub_poll"):
            lvl = logging.DEBUG   # 轮询太密，进度用 poll_progress() 汇总成一行
        else:
            lvl = logging.INFO
        log.log(lvl, "← [%s] %s %s %s%s", self.id, response.status_code,
                response.request.url.path, ms, detail)

    POLL = {"aihub_poll": True}   # 传给 httpx 的 extensions，标记这是轮询请求

    def poll_progress(self, attempt: int, status: str, waited: float,
                      task_id: str = "") -> None:
        """轮询进度：每次一行，能看出等了多久、上游是什么状态。"""
        log.info("[%s] 轮询 #%d 状态=%s 已等 %.0fs%s", self.id, attempt,
                 status or "?", waited, f" task={task_id}" if task_id else "")

    def require_key(self) -> None:
        if not self.api_key:
            raise ProviderError(
                f"{self.name} 未配置 API Key，请在 .env 或 config.yaml 中填写。"
            )

    def no_image_input(self) -> None:
        """不支持「图生图」的服务商统一在这里报错，提示可用的替代。"""
        raise ProviderError(
            f"{self.name} 的出图接口不支持上传参考图（图生图）。"
            "本地上传的图片是 base64，目前支持图生图的是：OpenAI 兼容接口、"
            "火山方舟即梦、Google Gemini Flash Image、魔搭。"
            "阿里百炼/智谱的图生图需要公网可访问的图片 URL。"
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


def ref_images(params: dict) -> list[str]:
    """出图时的参考图（图生图）：支持 images 列表或单个 image_url。"""
    imgs = [u for u in (params.get("images") or []) if u]
    if not imgs and params.get("image_url"):
        imgs = [params["image_url"]]
    return imgs
