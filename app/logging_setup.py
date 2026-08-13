"""统一日志：控制台 + 滚动文件。

日志文件默认写到 data/logs/aihub.log（10MB x 5 份滚动），可用环境变量调整：
  AIHUB_LOG_DIR    日志目录，默认 <工程>/data/logs
  AIHUB_LOG_LEVEL  级别，默认 INFO（DEBUG 会额外打印请求体/响应体预览）
  AIHUB_LOG_BODY   请求/响应体预览的最大字符数，默认 800
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Any

from .config import DATA_DIR

FMT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
BODY_LIMIT = int(os.getenv("AIHUB_LOG_BODY", "800"))

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|x-goog-api-key)"
    r"(\"?\s*[:=]\s*\"?)([^\"',\s]+)"
)
_DATAURL_RE = re.compile(r"data:([\w./+-]+);base64,[A-Za-z0-9+/=]+")

_ready = False


def log_path() -> Path:
    d = Path(os.getenv("AIHUB_LOG_DIR") or (DATA_DIR / "logs")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d / "aihub.log"


def setup() -> Path:
    """配置根 logger，返回日志文件路径。可重复调用。"""
    global _ready
    path = log_path()
    if _ready:
        return path

    level = getattr(logging, os.getenv("AIHUB_LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = logging.Formatter(FMT, DATEFMT)

    fileh = logging.handlers.RotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(fileh)
    root.addHandler(console)

    # uvicorn 自带 logger 默认不 propagate，这里接到根 handler 上
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 我们自己打更详细的
    # 访问日志我们在中间件里自己打（带耗时），uvicorn 那份就不要了，免得每行重复
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _ready = True
    logging.getLogger("aihub").info("日志已启用: %s (level=%s)", path,
                                    logging.getLevelName(level))
    return path


def scrub(text: str) -> str:
    """抹掉密钥、压掉 base64 图片，避免日志爆炸和泄漏。"""
    text = _DATAURL_RE.sub(lambda m: f"<data:{m.group(1)} base64 {len(m.group(0))}B>", text)
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def preview(data: Any, limit: int = BODY_LIMIT) -> str:
    """把任意对象转成可入日志的短字符串。"""
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return f"<{len(data)} bytes>"
    if not isinstance(data, str):
        import json
        try:
            data = json.dumps(data, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            data = str(data)
    data = scrub(data)
    return data if len(data) <= limit else data[:limit] + f"…(+{len(data) - limit})"
