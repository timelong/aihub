"""重试策略：所有上游请求统一走这里，撞到限流/网关抖动会自动退避重试。

放在 transport 层而不是每个 provider 里手写，是因为 provider 有十几处
`cli.post(...)`，逐个包一遍容易漏；client() 是统一入口，换掉它的 transport
就能覆盖对话、出图、视频提交和轮询。

**幂等性**：只有服务端明确告诉我们"现在别打了"（429 / 5xx）才重试提交类请求；
连接失败、读超时这类"不知道对方到底收到没有"的错误，只对 GET（轮询）重试——
否则可能重复提交一次出图/视频任务，白花钱。
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import httpx

log = logging.getLogger("aihub.provider")

DEFAULTS = {
    "attempts": 3,           # 总共尝试几次（1 = 不重试）
    "backoff": 2.0,          # 首次等待秒数，之后翻倍
    "max_backoff": 30.0,     # 单次等待上限
    "statuses": [429, 500, 502, 503, 504],
}
RETRY_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
             httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError)


def settings(conf: Optional[dict] = None) -> dict:
    """全局 retry 段 + provider 级覆盖，缺项用默认值。"""
    from ..config import load_config

    out = dict(DEFAULTS)
    for src in (load_config().get("retry") or {}, (conf or {}).get("retry") or {}):
        if isinstance(src, dict):
            out.update({k: v for k, v in src.items() if v is not None})
    out["attempts"] = max(1, int(out["attempts"]))
    out["backoff"] = max(0.1, float(out["backoff"]))
    out["max_backoff"] = max(out["backoff"], float(out["max_backoff"]))
    out["statuses"] = {int(x) for x in (out["statuses"] or [])}
    return out


def retry_after(resp: httpx.Response, fallback: float) -> float:
    """服务端给了 Retry-After 就听它的，别自己瞎猜。"""
    v = resp.headers.get("retry-after", "")
    try:
        return max(0.5, float(v))
    except (TypeError, ValueError):
        return fallback


class RetryTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, conf: dict, pid: str = ""):
        self.inner = inner
        self.cfg = settings(conf)
        self.pid = pid

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        cfg = self.cfg
        idempotent = request.method in ("GET", "HEAD")
        delay = cfg["backoff"]
        last_exc = None
        for attempt in range(1, cfg["attempts"] + 1):
            final = attempt >= cfg["attempts"]
            try:
                resp = await self.inner.handle_async_request(request)
            except RETRY_EXC as e:
                # 不知道对方收到没有：只有幂等请求（轮询）才敢重试
                if final or not idempotent:
                    raise
                last_exc = e
                wait = min(delay, cfg["max_backoff"])
                log.warning("[%s] %s %s 连接异常(%s)，%.1fs 后重试（第 %d/%d 次）",
                            self.pid, request.method, request.url.path,
                            type(e).__name__, wait, attempt + 1, cfg["attempts"])
                await asyncio.sleep(wait + random.uniform(0, 0.3))
                delay *= 2
                continue
            if resp.status_code not in cfg["statuses"] or final:
                if resp.status_code in cfg["statuses"] and final and cfg["attempts"] > 1:
                    log.error("[%s] %s %s 仍然 %d，已重试 %d 次，放弃",
                              self.pid, request.method, request.url.path,
                              resp.status_code, cfg["attempts"] - 1)
                return resp
            # 服务端明确拒绝（429/5xx）：这次肯定没被处理，重试是安全的
            await resp.aread()
            await resp.aclose()
            wait = min(retry_after(resp, delay), cfg["max_backoff"])
            log.warning("[%s] %s %s 返回 %d%s，%.1fs 后重试（第 %d/%d 次）",
                        self.pid, request.method, request.url.path, resp.status_code,
                        "（限流）" if resp.status_code == 429 else "", wait,
                        attempt + 1, cfg["attempts"])
            await asyncio.sleep(wait + random.uniform(0, 0.3))
            delay *= 2
        raise last_exc if last_exc else RuntimeError("unreachable")

    async def aclose(self) -> None:
        await self.inner.aclose()
