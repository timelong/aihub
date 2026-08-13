"""阿里百炼 DashScope：通义千问(chat, 走兼容模式) + 万相(图片/视频, 异步任务)。"""
from __future__ import annotations

from typing import Any

from .base import ProviderError, ref_images
from .openai_compat import OpenAICompatProvider

IMG_PATH = "/services/aigc/text2image/image-synthesis"
VID_T2V = "/services/aigc/video-generation/video-synthesis"
VID_I2V = "/services/aigc/image2video/video-synthesis"


class DashScopeProvider(OpenAICompatProvider):
    kind = "dashscope"

    def __init__(self, conf: dict[str, Any]):
        super().__init__(conf)
        self.compat_url = (conf.get("compat_url")
                           or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")

    def chat_url(self) -> str:
        return f"{self.compat_url}/chat/completions"

    def _async_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable"}

    async def _wait_task(self, task_id: str, want: str) -> dict:
        """轮询任务；want = image | video"""
        import asyncio
        url = f"{self.base_url}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with self.client() as cli:
            for _ in range(300):
                data = self.check(await cli.get(url, headers=headers))
                out = data.get("output", {})
                st = out.get("task_status")
                if st == "SUCCEEDED":
                    if want == "image":
                        return {"urls": [r["url"] for r in out.get("results", []) if r.get("url")]}
                    return {"urls": [out.get("video_url")] if out.get("video_url") else []}
                if st in ("FAILED", "CANCELED", "UNKNOWN"):
                    raise ProviderError(f"任务失败: {out.get('message') or st}")
                await asyncio.sleep(3)
        raise ProviderError("任务超时")

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        if ref_images(params):
            self.no_image_input()
        body = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {
                "n": int(params.get("n", 1)),
                "size": params.get("size", "1024*1024").replace("x", "*"),
            },
        }
        if params.get("negative_prompt"):
            body["input"]["negative_prompt"] = params["negative_prompt"]
        if params.get("seed") is not None:
            body["parameters"]["seed"] = int(params["seed"])
        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}{IMG_PATH}",
                                             headers=self._async_headers(), json=body))
        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise ProviderError(f"未拿到 task_id: {data}")
        return (await self._wait_task(task_id, "image"))["urls"]

    async def submit_video(self, model: str, prompt: str, params: dict) -> str:
        self.require_key()
        img = params.get("image_url")
        path = VID_I2V if img else VID_T2V
        inp: dict[str, Any] = {"prompt": prompt}
        if img:
            inp["img_url"] = img
        body = {
            "model": model,
            "input": inp,
            "parameters": {
                "size": params.get("video_size", "1920*1080").replace("x", "*"),
                "duration": int(params.get("duration", 5)),
                "prompt_extend": bool(params.get("prompt_extend", True)),
            },
        }
        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}{path}",
                                             headers=self._async_headers(), json=body))
        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise ProviderError(f"未拿到 task_id: {data}")
        return task_id

    async def poll_video(self, model: str, remote_id: str) -> dict:
        url = f"{self.base_url}/tasks/{remote_id}"
        async with self.client() as cli:
            data = self.check(await cli.get(
                url, headers={"Authorization": f"Bearer {self.api_key}"}))
        out = data.get("output", {})
        st = out.get("task_status")
        if st == "SUCCEEDED":
            return {"status": "succeeded",
                    "urls": [out["video_url"]] if out.get("video_url") else []}
        if st in ("FAILED", "CANCELED", "UNKNOWN"):
            return {"status": "failed", "error": out.get("message") or st, "urls": []}
        return {"status": "running", "urls": []}
