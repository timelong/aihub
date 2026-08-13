"""OpenAI 兼容接口：OpenAI / DeepSeek / Kimi / 硅基流动 / Ollama / vLLM ...

同时被 zhipu、ark 等复用（它们的 chat 接口都是 OpenAI 兼容的）。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .base import BaseProvider, ProviderError

CHAT_PARAM_KEYS = ("temperature", "top_p", "max_tokens", "presence_penalty",
                   "frequency_penalty", "stop", "seed")


def to_openai_messages(messages: list[dict], system_prompt: str = "") -> list[dict]:
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for m in messages:
        imgs = m.get("images") or []
        if imgs and m["role"] == "user":
            parts: list[dict] = []
            if m.get("content"):
                parts.append({"type": "text", "text": m["content"]})
            for u in imgs:
                parts.append({"type": "image_url", "image_url": {"url": u}})
            out.append({"role": "user", "content": parts})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def pick_params(params: dict) -> dict:
    return {k: v for k, v in params.items()
            if k in CHAT_PARAM_KEYS and v not in (None, "", [])}


class OpenAICompatProvider(BaseProvider):
    kind = "openai"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    async def chat_stream(self, model: str, messages: list[dict],
                          params: dict) -> AsyncIterator[dict]:
        self.require_key()
        body: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(messages, params.get("system_prompt", "")),
            "stream": True,
            **pick_params(params),
        }
        async with self.client() as cli:
            async with cli.stream("POST", self.chat_url(), headers=self.headers(),
                                  json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "ignore")
                    raise ProviderError(f"HTTP {resp.status_code}: {detail[:600]}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        yield {"type": "reasoning", "text": rc}
                    txt = delta.get("content")
                    if txt:
                        yield {"type": "delta", "text": txt}
        yield {"type": "done"}

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        body: dict[str, Any] = {"model": model, "prompt": prompt,
                                "n": int(params.get("n", 1))}
        if params.get("size"):
            body["size"] = params["size"]
        if params.get("quality"):
            body["quality"] = params["quality"]
        if params.get("image_size"):  # 硅基流动
            body["image_size"] = params["image_size"]
        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}/images/generations",
                                             headers=self.headers(), json=body))
        urls: list[str] = []
        for item in data.get("data", []):
            if item.get("url"):
                urls.append(item["url"])
            elif item.get("b64_json"):
                urls.append("data:image/png;base64," + item["b64_json"])
        if not urls:
            raise ProviderError(f"未返回图片: {json.dumps(data)[:400]}")
        return urls
