"""OpenAI 兼容接口：OpenAI / DeepSeek / Kimi / 硅基流动 / Ollama / vLLM ...

同时被 zhipu、ark 等复用（它们的 chat 接口都是 OpenAI 兼容的）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from .. import tools as toolkit
from ..context import build_system_prompt
from .base import BaseProvider, ProviderError, is_data_url, ref_images, split_data_url

CHAT_PARAM_KEYS = ("temperature", "top_p", "max_tokens", "presence_penalty",
                   "frequency_penalty", "stop", "seed")
MAX_TOOL_ROUNDS = 5
log = logging.getLogger("aihub.provider")


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
        oai_msgs = to_openai_messages(messages, build_system_prompt(params))
        wanted = [t for t in (params.get("tools") or []) if t in toolkit.SPECS]
        schemas = toolkit.schemas(wanted)

        for rnd in range(MAX_TOOL_ROUNDS):
            calls: list[dict] = []
            async for ev in self._stream_once(model, oai_msgs, params, schemas):
                if ev["type"] == "_tool_calls":
                    calls = ev["calls"]
                else:
                    yield ev
            if not calls:
                break
            if rnd == MAX_TOOL_ROUNDS - 1:
                yield {"type": "delta", "text": f"\n\n（工具调用超过 {MAX_TOOL_ROUNDS} 轮，已停止）"}
                break

            oai_msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
            for call in calls:
                fn = call["function"]
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                yield {"type": "tool_call", "name": name, "args": args}
                result = await toolkit.run(name, args)
                yield {"type": "tool_result", "name": name,
                       "summary": toolkit.summarize(name, args, result)}
                oai_msgs.append({
                    "role": "tool", "tool_call_id": call.get("id") or name,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False)[:12000],
                })
        yield {"type": "done"}

    async def _stream_once(self, model: str, oai_msgs: list[dict], params: dict,
                           schemas: list[dict]) -> AsyncIterator[dict]:
        """跑一次流式请求；如果模型要调工具，最后 yield 一个 _tool_calls 事件。"""
        body: dict[str, Any] = {
            "model": model,
            "messages": oai_msgs,
            "stream": True,
            **pick_params(params),
        }
        if schemas:
            body["tools"] = schemas
            body["tool_choice"] = "auto"

        acc: dict[int, dict] = {}
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
                    for tc in delta.get("tool_calls") or []:
                        self._merge_tool_call(acc, tc)
        if acc:
            calls = [acc[i] for i in sorted(acc)]
            log.info("[%s] 模型请求调用工具: %s", self.id,
                     [c["function"].get("name") for c in calls])
            yield {"type": "_tool_calls", "calls": calls}

    @staticmethod
    def _merge_tool_call(acc: dict[int, dict], tc: dict) -> None:
        """流式 tool_calls 是按 index 分片下发的，这里拼起来。"""
        idx = tc.get("index", 0)
        cur = acc.setdefault(idx, {"id": "", "type": "function",
                                   "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            cur["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            cur["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            cur["function"]["arguments"] += fn["arguments"]

    async def generate_image(self, model: str, prompt: str, params: dict) -> list[str]:
        self.require_key()
        refs = ref_images(params)
        if refs:
            return await self._edit_image(model, prompt, params, refs)
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
        return self._extract_images(data)

    async def _edit_image(self, model: str, prompt: str, params: dict,
                          refs: list[str]) -> list[str]:
        """图生图 / 图片编辑。

        默认走 OpenAI 官方的 /images/edits（multipart 上传参考图）；
        有些兼容服务（如硅基流动）是在 /images/generations 里传 image 字段，
        可在 config.yaml 该 provider 下写 image_edit_mode: json 切换。
        """
        mode = (self.conf.get("image_edit_mode") or "multipart").lower()
        if mode == "json":
            body: dict[str, Any] = {"model": model, "prompt": prompt,
                                    "image": refs[0] if len(refs) == 1 else refs}
            if params.get("size"):
                body["size"] = params["size"]
            if params.get("image_size"):
                body["image_size"] = params["image_size"]
            if params.get("n"):
                body["n"] = int(params["n"])
            async with self.client() as cli:
                data = self.check(await cli.post(f"{self.base_url}/images/generations",
                                                 headers=self.headers(), json=body))
            return self._extract_images(data)

        files = []
        for i, u in enumerate(refs):
            if is_data_url(u):
                mime, raw = split_data_url(u)
            else:
                async with self.client() as cli:
                    r = await cli.get(u)
                    if r.status_code >= 400:
                        raise ProviderError(f"参考图下载失败 HTTP {r.status_code}")
                    mime, raw = r.headers.get("content-type", "image/png"), r.content
            ext = (mime.split("/")[-1] or "png").split(";")[0]
            key = "image[]" if len(refs) > 1 else "image"
            files.append((key, (f"ref{i}.{ext}", raw, mime)))

        form: dict[str, str] = {"model": model, "prompt": prompt,
                                "n": str(int(params.get("n", 1)))}
        if params.get("size"):
            form["size"] = str(params["size"])
        headers = {"Authorization": f"Bearer {self.api_key}"}  # multipart 不能带 Content-Type
        async with self.client() as cli:
            data = self.check(await cli.post(f"{self.base_url}/images/edits",
                                             headers=headers, data=form, files=files))
        return self._extract_images(data)

    @staticmethod
    def _extract_images(data: dict) -> list[str]:
        urls: list[str] = []
        for item in data.get("data", []):
            if item.get("url"):
                urls.append(item["url"])
            elif item.get("b64_json"):
                urls.append("data:image/png;base64," + item["b64_json"])
        if not urls:
            raise ProviderError(f"未返回图片: {json.dumps(data)[:400]}")
        return urls
