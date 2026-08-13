"""魔搭社区 ModelScope（API-Inference）。

- 对话：完全 OpenAI 兼容，直接复用 OpenAICompatProvider。
- 图片：/v1/images/generations，支持同步与异步两种模式。
  异步模式需带 `X-ModelScope-Async-Mode: true`，返回 task_id，
  再轮询 /v1/tasks/{task_id}（带 `X-ModelScope-Task-Type: image_generation`）。
  这里默认走异步（大图/LoRA 模型同步容易超时）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .base import ProviderError, ref_images
from .openai_compat import OpenAICompatProvider


class ModelScopeProvider(OpenAICompatProvider):
    kind = "modelscope"

    def __init__(self, conf: dict[str, Any]):
        super().__init__(conf)
        # 是否使用异步出图模式，可在 config.yaml 中用 async_image: false 关闭
        self.async_image = conf.get("async_image", True)

    # ---------- 图片 ----------
    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        if params.get("negative_prompt"):
            body["negative_prompt"] = params["negative_prompt"]
        if params.get("size"):
            # 魔搭接受 "1024x1024"
            body["size"] = str(params["size"]).replace("*", "x")
        if params.get("steps"):
            body["steps"] = int(params["steps"])
        if params.get("guidance") is not None:
            body["guidance"] = float(params["guidance"])
        if params.get("seed") is not None:
            body["seed"] = int(params["seed"])
        refs = ref_images(params)
        if refs:  # 图生图
            body["image_url"] = refs[0]

        headers = self.headers()
        if self.async_image:
            headers = {**headers, "X-ModelScope-Async-Mode": "true"}

        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}/images/generations",
                                             headers=headers, json=body))

        if not self.async_image:
            return self._extract(data)

        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            # 有些模型即使带了异步头也直接返回结果
            urls = self._extract(data, strict=False)
            if urls:
                return urls
            raise ProviderError(f"未拿到 task_id: {data}")
        return await self._wait(task_id)

    async def _wait(self, task_id: str) -> list[str]:
        url = f"{self.base_url}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "X-ModelScope-Task-Type": "image_generation"}
        t0 = time.monotonic()
        async with self.client() as cli:
            for i in range(1, 201):
                data = self.check(await cli.get(url, headers=headers,
                                                extensions=self.POLL))
                st = (data.get("task_status") or data.get("status") or "").upper()
                self.poll_progress(i, st, time.monotonic() - t0, task_id)
                if st in ("SUCCEED", "SUCCEEDED", "SUCCESS"):
                    return self._extract(data)
                if st in ("FAILED", "FAIL", "CANCELED"):
                    raise ProviderError(f"任务失败: {data.get('message') or data}")
                await asyncio.sleep(3)
        raise ProviderError(f"出图任务超时（等了 {time.monotonic() - t0:.0f}s）")

    @staticmethod
    def _extract(data: dict, strict: bool = True) -> list[str]:
        urls: list[str] = []
        for key in ("output_images", "images", "data"):
            for item in data.get(key) or []:
                if isinstance(item, str):
                    urls.append(item)
                elif isinstance(item, dict):
                    if item.get("url"):
                        urls.append(item["url"])
                    elif item.get("b64_json"):
                        urls.append("data:image/png;base64," + item["b64_json"])
            if urls:
                break
        if not urls and strict:
            raise ProviderError(f"未返回图片: {str(data)[:400]}")
        return urls
