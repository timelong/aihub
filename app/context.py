"""给模型注入运行时上下文（当前时间等）。

模型的知识有截止日期，也不知道"今天"是哪天，所以每次请求都在 system prompt
前面加一段当前时间说明。可在 config.yaml 里关掉或改时区：

    chat:
      inject_datetime: true       # 关掉就写 false
      timezone: Asia/Shanghai     # 留空用系统时区
      extra: ""                   # 想追加的固定说明，会拼在时间后面

也可以单次请求用 params.inject_datetime = false 临时关掉。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import load_config

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def chat_conf() -> dict:
    return load_config().get("chat") or {}


def now() -> datetime:
    tz_name = (chat_conf().get("timezone") or "").strip()
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now(timezone.utc).astimezone()  # 系统本地时区


def datetime_note() -> str:
    t = now()
    return (
        f"当前时间：{t:%Y-%m-%d %H:%M} {WEEKDAYS[t.weekday()]}"
        f"（时区 {t.tzname()}，UTC{t:%z}）。\n"
        "回答任何与时间相关的问题（今天/现在/最近/最新、日期计算、年龄、期限等）"
        "都以这个时间为基准，不要用你训练数据里的日期。"
        "涉及此时间之后才发生的事实，如果你不确定就说明不确定，"
        "有联网搜索工具时应先搜索再回答。"
    )


def build_system_prompt(params: dict) -> str:
    """把运行时上下文拼到用户自己的 system prompt 前面。"""
    user_part = (params.get("system_prompt") or "").strip()
    conf = chat_conf()
    enabled = params.get("inject_datetime")
    if enabled is None:
        enabled = conf.get("inject_datetime", True)
    if not enabled:
        return user_part

    blocks = [datetime_note()]
    if (conf.get("extra") or "").strip():
        blocks.append(conf["extra"].strip())
    if user_part:
        blocks.append(user_part)
    return "\n\n".join(blocks)
