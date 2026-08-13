"""统一日志：控制台 + 按天滚动文件。

当天日志写在 data/logs/aihub.log，跨天后自动改名成 aihub-2026-08-12.log。
启动时会清理超过保留天数的历史日志，天数在 config.yaml 里配（也能在
「🗂 日志管理」页改）：

    logging:
      retention_days: 7     # 0 或负数表示永久保留

环境变量：
  AIHUB_LOG_DIR    日志目录，默认 <工程>/data/logs
  AIHUB_LOG_LEVEL  级别，默认 INFO（DEBUG 会额外打印请求体/响应体预览）
  AIHUB_LOG_BODY   请求/响应体预览的最大字符数，默认 800
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DATA_DIR, load_config
from .logctx import ContextFilter

DEFAULT_RETENTION_DAYS = 7
# 历史日志文件名：aihub-2026-08-12.log
_HIST_RE = re.compile(r"^aihub-(\d{4}-\d{2}-\d{2})\.log$")

# 带时区偏移和 PID：多个服务实例写同一个文件时（比如你起了一个、脚本又起了一个），
# 靠 pid 就能把两条时间线分开，靠 %z 能看出它们是不是同一个时区。
FMT = "%(asctime)s%(tzoff)s %(levelname)-5s [%(name)s:%(process)d] %(message)s%(ctx)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
BODY_LIMIT = int(os.getenv("AIHUB_LOG_BODY", "800"))

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|x-goog-api-key)"
    r"(\"?\s*[:=]\s*\"?)([^\"',\s]+)"
)
_DATAURL_RE = re.compile(r"data:([\w./+-]+);base64,[A-Za-z0-9+/=]+")

_ready = False


def log_dir() -> Path:
    d = Path(os.getenv("AIHUB_LOG_DIR") or (DATA_DIR / "logs")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return log_dir() / "aihub.log"


# ---------------- 保留天数 / 历史文件 ----------------
def retention_days() -> int:
    conf = load_config().get("logging") or {}
    try:
        return int(conf.get("retention_days", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def list_files() -> list[dict]:
    """日志文件清单，当天的排最前，其余按日期倒序。"""
    d = log_dir()
    out: list[dict] = []
    cur = d / "aihub.log"
    if cur.exists():
        out.append({"name": cur.name, "date": "", "current": True,
                    "size": cur.stat().st_size})
    hist = []
    for f in d.iterdir():
        m = _HIST_RE.match(f.name)
        if m:
            hist.append({"name": f.name, "date": m.group(1), "current": False,
                         "size": f.stat().st_size})
    hist.sort(key=lambda x: x["date"], reverse=True)
    return out + hist


def resolve_file(name: str | None) -> Path:
    """把前端传来的文件名解析成真实路径，只允许我们自己的日志文件。"""
    if not name or name == "aihub.log":
        return log_path()
    if not _HIST_RE.match(name):
        raise ValueError(f"不是有效的日志文件名: {name}")
    path = log_dir() / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def cleanup(days: int | None = None) -> list[str]:
    """删除超过保留天数的历史日志，返回被删掉的文件名。当天日志永不删。"""
    keep = retention_days() if days is None else int(days)
    if keep <= 0:
        return []
    cutoff = date.today() - timedelta(days=keep)
    removed: list[str] = []
    for f in log_dir().iterdir():
        m = _HIST_RE.match(f.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                f.unlink()
                removed.append(f.name)
            except OSError as e:
                logging.getLogger("aihub").warning("删除历史日志 %s 失败: %s", f.name, e)
    return removed


def setup() -> Path:
    """配置根 logger，返回日志文件路径。可重复调用。"""
    global _ready
    path = log_path()
    if _ready:
        return path

    level = getattr(logging, os.getenv("AIHUB_LOG_LEVEL", "INFO").upper(), logging.INFO)

    class _Fmt(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            record.tzoff = time.strftime("%z")
            if not hasattr(record, "ctx"):      # 第三方库直接打的日志没走 filter
                record.ctx = ""
            return super().format(record)

    fmt = _Fmt(FMT, DATEFMT)

    # 按天滚动：当天写 aihub.log，跨天后改名 aihub-2026-08-12.log。
    # backupCount=0 表示不让 handler 自己删，历史清理由我们的 cleanup() 按天数做。
    fileh = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=0, encoding="utf-8")
    fileh.suffix = "%Y-%m-%d"
    fileh.namer = lambda name: str(
        Path(name).parent / f"aihub-{name.rsplit('.', 1)[-1]}.log")
    fileh.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addFilter(ContextFilter())          # 只对 root 自己的记录生效
    for h in (fileh, console):               # handler 级 filter 才能覆盖所有 logger
        h.addFilter(ContextFilter())
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
    lg = logging.getLogger("aihub")
    keep = retention_days()
    lg.info("日志已启用: %s (level=%s, 按天滚动, 保留 %s)", path,
            logging.getLevelName(level), f"{keep} 天" if keep > 0 else "永久")
    removed = cleanup()
    if removed:
        lg.info("已清理 %d 个过期日志: %s", len(removed), ", ".join(sorted(removed)))
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
