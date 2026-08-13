"""日志上下文：让底层 provider 的日志也能带上"哪个任务、哪个模型"。

用法：

    with bind(job=job_id, model="modelscope/Qwen-Image"):
        await prov.generate_image(...)

之后这段调用里所有日志行都会自动带上 `{job=... model=...}`，包括 provider 里
那些轮询的 HTTP 行——否则一堆 `GET /v1/tasks/xxx` 根本看不出是谁在跑。

contextvars 在 async 任务间会自动继承，所以后台轮询任务里也有效。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any, Iterator

_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("aihub_ctx",
                                                                     default={})


@contextlib.contextmanager
def bind(**kv: Any) -> Iterator[None]:
    """在这个 with 块内给日志附加字段（可嵌套，内层与外层合并）。"""
    merged = {**_ctx.get(), **{k: v for k, v in kv.items() if v not in (None, "")}}
    token = _ctx.set(merged)
    try:
        yield
    finally:
        _ctx.reset(token)


def set_ctx(**kv: Any) -> None:
    """不带 with 的版本，用于后台任务这类不好包 with 的地方。"""
    _ctx.set({**_ctx.get(), **{k: v for k, v in kv.items() if v not in (None, "")}})


def current() -> dict[str, Any]:
    return dict(_ctx.get())


def rendered() -> str:
    c = _ctx.get()
    return " {" + " ".join(f"{k}={v}" for k, v in c.items()) + "}" if c else ""


class ContextFilter(logging.Filter):
    """给每条日志补上 ctx 字段，供 formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = rendered()
        return True
