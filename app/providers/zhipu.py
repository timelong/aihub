"""智谱 GLM：chat/图片走 OpenAI 兼容，CogVideoX 走异步任务。"""
from __future__ import annotations

from typing import Any

from .base import ProviderError
from .openai_compat import OpenAICompatProvider


class ZhipuProvider(OpenAICompatProvider):
    kind = "zhipu"

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        body: dict[str, Any] = {"model": model, "prompt": prompt,
                                "size": params.get("size", "1024x1024")}
        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}/images/generations",
                                             headers=self.headers(), json=body))
        urls = [d["url"] for d in data.get("data", []) if d.get("url")]
        if not urls:
            raise ProviderError(f"未返回图片: {data}")
        return urls

    async def submit_video(self, model: str, prompt: str, params: dict) -> str:
        self.require_key()
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "with_audio": bool(params.get("with_audio", True)),
            "quality": params.get("quality", "quality"),
        }
        if params.get("image_url"):
            body["image_url"] = params["image_url"]
        if params.get("video_size"):
            body["size"] = params["video_size"].replace("*", "x")
        if params.get("duration"):
            body["duration"] = int(params["duration"])
        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}/videos/generations",
                                             headers=self.headers(), json=body))
        tid = data.get("id")
        if not tid:
            raise ProviderError(f"未拿到任务 id: {data}")
        return tid

    async def poll_video(self, model: str, remote_id: str) -> dict:
        async with self.client() as cli:
            data = self.check(await cli.get(f"{self.base_url}/async-result/{remote_id}",
                                            headers=self.headers()))
        st = (data.get("task_status") or "").upper()
        if st == "SUCCESS":
            urls = [v.get("url") for v in data.get("video_result", []) if v.get("url")]
            return {"status": "succeeded", "urls": urls}
        if st == "FAIL":
            return {"status": "failed", "error": str(data)[:400], "urls": []}
        return {"status": "running", "urls": []}
