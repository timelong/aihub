"""对话可选工具：联网搜索、网页抓取。

搜索后端按以下顺序选择（谁配了 key 用谁）：
  1. Tavily      —— .env 里 TAVILY_API_KEY，效果最好，专为 LLM 设计
  2. Bocha 博查   —— .env 里 BOCHA_API_KEY，国内可直连
  3. Bing        —— 无需 key 的兜底，抓 cn.bing.com 搜索结果页（可能被限流）

工具以 OpenAI function calling 的形式交给模型，由服务端执行后把结果回灌，
所以任意支持 function calling 的 OpenAI 兼容模型都能用。
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger("aihub.tools")

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=10.0)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                             headers={"User-Agent": UA,
                                      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})


def strip_html(html: str, sep: str = " ") -> str:
    """去标签取文本；sep="" 用于标题/摘要这类行内片段，避免把词拆开。"""
    text = _TAG_RE.sub(" ", html)
    text = _HTML_RE.sub(sep, text)
    text = html_lib.unescape(text).replace(" ", " ").replace(" ", " ")
    lines = [ln.strip() for ln in text.splitlines()]
    return _WS_RE.sub("\n\n", "\n".join(ln for ln in lines if ln))


# ============================ 搜索后端 ============================
def search_backend() -> str:
    if os.getenv("TAVILY_API_KEY"):
        return "tavily"
    if os.getenv("BOCHA_API_KEY"):
        return "bocha"
    return "bing"


async def _tavily(query: str, n: int) -> list[dict]:
    async with _client() as cli:
        r = await cli.post("https://api.tavily.com/search", json={
            "api_key": os.environ["TAVILY_API_KEY"], "query": query,
            "max_results": n, "search_depth": "basic"})
        r.raise_for_status()
        data = r.json()
    return [{"title": it.get("title", ""), "url": it.get("url", ""),
             "snippet": (it.get("content") or "")[:500]}
            for it in data.get("results", [])]


async def _bocha(query: str, n: int) -> list[dict]:
    async with _client() as cli:
        r = await cli.post("https://api.bochaai.com/v1/web-search",
                           headers={"Authorization": f"Bearer {os.environ['BOCHA_API_KEY']}"},
                           json={"query": query, "count": n, "summary": True})
        r.raise_for_status()
        data = r.json()
    pages = (((data.get("data") or {}).get("webPages") or {}).get("value")) or []
    return [{"title": p.get("name", ""), "url": p.get("url", ""),
             "snippet": (p.get("summary") or p.get("snippet") or "")[:500]}
            for p in pages[:n]]


_BING_H2 = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_P = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


async def _bing(query: str, n: int) -> list[dict]:
    """抓 cn.bing.com 的结果页。免 key，但依赖页面结构，失效时会返回 0 条。"""
    async with _client() as cli:
        r = await cli.get("https://cn.bing.com/search",
                          params={"q": query, "setlang": "zh-CN"})
        r.raise_for_status()
        html = r.text
    out: list[dict] = []
    for chunk in html.split('<li class="b_algo"')[1:]:
        m = _BING_H2.search(chunk)
        if not m:
            continue
        p = _BING_P.search(chunk)
        out.append({"title": strip_html(m.group(2), ""),
                    "url": m.group(1),
                    "snippet": strip_html(p.group(1), "")[:500] if p else ""})
        if len(out) >= n:
            break
    if not out:
        raise RuntimeError("Bing 未返回可解析的结果（可能被限流，建议配置 TAVILY_API_KEY）")
    return out


async def web_search(query: str, max_results: int = 5) -> dict:
    n = max(1, min(int(max_results or 5), 10))
    backend = search_backend()
    fn = {"tavily": _tavily, "bocha": _bocha, "bing": _bing}[backend]
    log.info("web_search[%s] q=%r n=%d", backend, query, n)
    try:
        results = await fn(query, n)
    except Exception as e:  # noqa: BLE001
        log.warning("web_search[%s] 失败: %s", backend, e)
        if backend != "bing":  # 有 key 的后端挂了就退回免 key 的
            try:
                results = await _bing(query, n)
                backend += "→bing"
            except Exception as e2:  # noqa: BLE001
                return {"error": f"搜索失败: {e} / 兜底也失败: {e2}", "results": []}
        else:
            return {"error": f"搜索失败: {e}", "results": []}
    log.info("web_search[%s] 命中 %d 条", backend, len(results))
    return {"backend": backend, "query": query, "results": results}


async def fetch_url(url: str, max_chars: int = 6000) -> dict:
    if not re.match(r"^https?://", url):
        return {"error": "只支持 http/https 链接"}
    log.info("fetch_url %s", url)
    try:
        async with _client() as cli:
            r = await cli.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            text = r.text if "html" in ctype or "text" in ctype or "json" in ctype else ""
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_url 失败 %s: %s", url, e)
        return {"error": f"抓取失败: {e}"}
    if not text:
        return {"error": f"不是文本内容（{ctype}）"}
    body = strip_html(text) if "html" in ctype else text
    limit = max(500, min(int(max_chars or 6000), 20000))
    return {"url": url, "truncated": len(body) > limit, "content": body[:limit]}


# ============================ 注册表 ============================
SPECS: dict[str, dict[str, Any]] = {
    "web_search": {
        "label": "联网搜索",
        "desc": "让模型自己上网搜最新信息",
        "run": web_search,
        "schema": {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "联网搜索实时信息。当问题涉及最新新闻、当前时间之后的事件、"
                               "具体数据或你不确定的事实时，必须调用它，不要凭记忆回答。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "max_results": {"type": "integer",
                                        "description": "返回条数，1-10，默认 5"},
                    },
                    "required": ["query"],
                },
            },
        },
    },
    "fetch_url": {
        "label": "网页抓取",
        "desc": "按 URL 打开网页并读取正文",
        "run": fetch_url,
        "schema": {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "抓取指定网页并返回正文文本，用于查看搜索结果里的具体页面。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "http/https 链接"},
                        "max_chars": {"type": "integer", "description": "最多返回多少字符"},
                    },
                    "required": ["url"],
                },
            },
        },
    },
}


def catalog() -> list[dict]:
    """给前端用的工具清单。"""
    backend = search_backend()
    out = []
    for name, spec in SPECS.items():
        note = ""
        if name == "web_search":
            note = {"tavily": "Tavily", "bocha": "博查",
                    "bing": "Bing（未配 key 的兜底，可能被限流）"}[backend]
        out.append({"name": name, "label": spec["label"], "desc": spec["desc"],
                    "backend": note})
    return out


def schemas(names: list[str]) -> list[dict]:
    return [SPECS[n]["schema"] for n in names if n in SPECS]


async def run(name: str, args: dict) -> Any:
    spec = SPECS.get(name)
    if not spec:
        return {"error": f"未知工具: {name}"}
    try:
        return await asyncio.wait_for(spec["run"](**args), timeout=60)
    except asyncio.TimeoutError:
        return {"error": f"{name} 执行超时"}
    except TypeError as e:
        return {"error": f"参数不对: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("工具 %s 执行异常", name)
        return {"error": f"{type(e).__name__}: {e}"}


def summarize(name: str, args: dict, result: Any) -> str:
    """给前端展示的一行摘要。"""
    if isinstance(result, dict) and result.get("error"):
        return f"{name} 失败：{result['error']}"
    if name == "web_search":
        items = (result or {}).get("results", [])
        head = "；".join(i["title"][:40] for i in items[:3])
        return f"搜索「{args.get('query', '')}」→ {len(items)} 条结果 {head}"
    if name == "fetch_url":
        return f"读取 {args.get('url', '')} → {len(((result or {}).get('content') or ''))} 字"
    return json.dumps(result, ensure_ascii=False)[:200]
