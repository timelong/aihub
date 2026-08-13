"""Google Gemini：generateContent 流式对话、Imagen/Gemini 出图、Veo 出视频。"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import base64

from ..context import build_system_prompt
from .base import BaseProvider, ProviderError, is_data_url, ref_images, split_data_url


def _inline_part(data_url: str) -> dict:
    mime, raw = split_data_url(data_url)
    return {"inline_data": {"mime_type": mime,
                            "data": base64.b64encode(raw).decode()}}


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    contents: list[dict] = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        parts: list[dict] = []
        if m.get("content"):
            parts.append({"text": m["content"]})
        for u in m.get("images") or []:
            if is_data_url(u):
                parts.append(_inline_part(u))
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


class GeminiProvider(BaseProvider):
    kind = "gemini"

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    async def chat_stream(self, model: str, messages: list[dict],
                          params: dict) -> AsyncIterator[dict]:
        self.require_key()
        body: dict[str, Any] = {"contents": _to_gemini_contents(messages)}
        gen: dict[str, Any] = {}
        if params.get("temperature") is not None:
            gen["temperature"] = params["temperature"]
        if params.get("top_p") is not None:
            gen["topP"] = params["top_p"]
        if params.get("max_tokens"):
            gen["maxOutputTokens"] = int(params["max_tokens"])
        if gen:
            body["generationConfig"] = gen
        sys_prompt = build_system_prompt(params)
        if sys_prompt:
            body["systemInstruction"] = {"parts": [{"text": sys_prompt}]}
        # Gemini 用自带的 Google 搜索接地，不走我们的 function calling
        want_search = "web_search" in (params.get("tools") or [])
        if want_search:
            body["tools"] = [{"google_search": {}}]

        url = f"{self.base_url}/models/{model}:streamGenerateContent?alt=sse"
        announced = False
        async with self.client() as cli:
            async with cli.stream("POST", url, headers=self._headers(), json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "ignore")
                    raise ProviderError(f"HTTP {resp.status_code}: {detail[:600]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    for cand in chunk.get("candidates", []):
                        gm = cand.get("groundingMetadata") or {}
                        if want_search and not announced and gm:
                            announced = True
                            queries = gm.get("webSearchQueries") or []
                            yield {"type": "tool_call", "name": "google_search",
                                   "args": {"query": "；".join(queries)}}
                            sources = [
                                (c.get("web") or {}).get("title", "")
                                for c in (gm.get("groundingChunks") or [])
                            ]
                            yield {"type": "tool_result", "name": "google_search",
                                   "summary": f"Gemini 内置搜索 → {len(sources)} 个来源 "
                                              + "；".join(s for s in sources[:3] if s)}
                        for p in (cand.get("content") or {}).get("parts", []):
                            if p.get("thought") and p.get("text"):
                                yield {"type": "reasoning", "text": p["text"]}
                            elif p.get("text"):
                                yield {"type": "delta", "text": p["text"]}
        yield {"type": "done"}

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        refs = ref_images(params)
        if model.startswith("imagen"):
            if refs:
                raise ProviderError("Imagen 只支持文生图，图生图请选 Gemini Flash Image")
            body = {"instances": [{"prompt": prompt}],
                    "parameters": {"sampleCount": int(params.get("n", 1)),
                                   "aspectRatio": params.get("aspect_ratio", "1:1")}}
            url = f"{self.base_url}/models/{model}:predict"
            async with self.client() as cli:
                data = self.check(await cli.post(url, headers=self._headers(), json=body))
            urls = ["data:image/png;base64," + p["bytesBase64Encoded"]
                    for p in data.get("predictions", []) if p.get("bytesBase64Encoded")]
        else:  # gemini-*-image：走 generateContent
            url = f"{self.base_url}/models/{model}:generateContent"
            parts: list[dict] = [{"text": prompt}]
            for u in refs:  # 图生图 / 图片编辑：参考图直接作为 inline_data 传进去
                if not is_data_url(u):
                    raise ProviderError("Gemini 参考图需为上传的本地图片")
                parts.append(_inline_part(u))
            body = {"contents": [{"role": "user", "parts": parts}]}
            async with self.client() as cli:
                data = self.check(await cli.post(url, headers=self._headers(), json=body))
            urls = []
            for cand in data.get("candidates", []):
                for p in (cand.get("content") or {}).get("parts", []):
                    inl = p.get("inlineData") or p.get("inline_data")
                    if inl:
                        urls.append(f"data:{inl.get('mimeType', 'image/png')};base64,"
                                    + inl["data"])
        if not urls:
            raise ProviderError("未返回图片")
        return urls

    async def submit_video(self, model: str, prompt: str, params: dict) -> str:
        self.require_key()
        inst: dict[str, Any] = {"prompt": prompt}
        if params.get("image_url") and is_data_url(params["image_url"]):
            import base64
            mime, raw = split_data_url(params["image_url"])
            inst["image"] = {"bytesBase64Encoded": base64.b64encode(raw).decode(),
                             "mimeType": mime}
        body = {"instances": [inst],
                "parameters": {"aspectRatio": params.get("aspect_ratio", "16:9")}}
        url = f"{self.base_url}/models/{model}:predictLongRunning"
        async with self.client() as cli:
            data = self.check(await cli.post(url, headers=self._headers(), json=body))
        name = data.get("name")
        if not name:
            raise ProviderError(f"未拿到操作名: {data}")
        return name

    async def poll_video(self, model: str, remote_id: str) -> dict:
        url = f"{self.base_url}/{remote_id}"
        async with self.client() as cli:
            data = self.check(await cli.get(url, headers=self._headers(),
                                            extensions=self.POLL))
        if not data.get("done"):
            return {"status": "running", "urls": []}
        if data.get("error"):
            return {"status": "failed", "error": str(data["error"])[:400], "urls": []}
        resp = data.get("response", {})
        samples = (resp.get("generateVideoResponse", {}).get("generatedSamples")
                   or resp.get("generatedSamples") or [])
        urls = []
        for s in samples:
            uri = (s.get("video") or {}).get("uri")
            if uri:
                urls.append(f"{uri}&key={self.api_key}" if "?" in uri
                            else f"{uri}?key={self.api_key}")
        return {"status": "succeeded", "urls": urls}
