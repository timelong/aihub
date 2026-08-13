"""Google Gemini：generateContent 流式对话、Imagen/Gemini 出图、Veo 出视频。"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .base import BaseProvider, ProviderError, is_data_url, split_data_url


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    contents: list[dict] = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        parts: list[dict] = []
        if m.get("content"):
            parts.append({"text": m["content"]})
        for u in m.get("images") or []:
            if is_data_url(u):
                mime, raw = split_data_url(u)
                import base64
                parts.append({"inline_data": {"mime_type": mime,
                                              "data": base64.b64encode(raw).decode()}})
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
        if params.get("system_prompt"):
            body["systemInstruction"] = {"parts": [{"text": params["system_prompt"]}]}

        url = f"{self.base_url}/models/{model}:streamGenerateContent?alt=sse"
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
                        for p in (cand.get("content") or {}).get("parts", []):
                            if p.get("thought") and p.get("text"):
                                yield {"type": "reasoning", "text": p["text"]}
                            elif p.get("text"):
                                yield {"type": "delta", "text": p["text"]}
        yield {"type": "done"}

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        if model.startswith("imagen"):
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
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
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
            data = self.check(await cli.get(url, headers=self._headers()))
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
