"""火山方舟 Ark：豆包(chat, OpenAI 兼容) + 即梦 Seedream(图片) / Seedance(视频)。"""
from __future__ import annotations

from typing import Any

from .base import ProviderError, ref_images
from .openai_compat import OpenAICompatProvider


class ArkProvider(OpenAICompatProvider):
    kind = "ark"

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": params.get("size", "2K"),
            "response_format": "url",
            "watermark": bool(params.get("watermark", False)),
        }
        refs = ref_images(params)          # 即梦支持 URL 或 base64，多图组图传数组
        if refs:
            body["image"] = refs[0] if len(refs) == 1 else refs
        if params.get("seed") is not None:
            body["seed"] = int(params["seed"])
        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}/images/generations",
                                             headers=self.headers(), json=body))
        urls = [d["url"] for d in data.get("data", []) if d.get("url")]
        if not urls:
            raise ProviderError(f"未返回图片: {data}")
        return urls

    async def submit_video(self, model: str, prompt: str, params: dict) -> str:
        self.require_key()
        text = prompt
        # Ark 用 --ratio/--dur 这类后缀参数控制
        if params.get("ratio"):
            text += f" --ratio {params['ratio']}"
        if params.get("duration"):
            text += f" --dur {int(params['duration'])}"
        if params.get("resolution"):
            text += f" --rs {params['resolution']}"
        content: list[dict] = [{"type": "text", "text": text}]
        if params.get("image_url"):
            content.append({"type": "image_url",
                            "image_url": {"url": params["image_url"]}})
        async with self.client() as cli:
            data = self.check(await cli.post(
                f"{self.base_url}/contents/generations/tasks",
                headers=self.headers(), json={"model": model, "content": content}))
        tid = data.get("id")
        if not tid:
            raise ProviderError(f"未拿到任务 id: {data}")
        return tid

    async def poll_video(self, model: str, remote_id: str) -> dict:
        async with self.client() as cli:
            data = self.check(await cli.get(
                f"{self.base_url}/contents/generations/tasks/{remote_id}",
                headers=self.headers(), extensions=self.POLL))
        st = (data.get("status") or "").lower()
        if st == "succeeded":
            url = (data.get("content") or {}).get("video_url")
            return {"status": "succeeded", "urls": [url] if url else []}
        if st in ("failed", "cancelled", "canceled"):
            return {"status": "failed",
                    "error": (data.get("error") or {}).get("message", st), "urls": []}
        return {"status": "running", "urls": []}
