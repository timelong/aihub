"""配置加载：读取 config.yaml + .env，做环境变量插值。"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("AIHUB_CONFIG", ROOT / "config.yaml"))
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _interp(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_interp(v) for v in value]
    if isinstance(value, dict):
        return {k: _interp(v) for k, v in value.items()}
    return value


def load_config(force: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None or force:
            load_dotenv()
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            _cache = _interp(raw)
        return _cache


def save_config(raw_text: str) -> dict[str, Any]:
    """保存原始 yaml 文本（不做插值），并重新加载。"""
    yaml.safe_load(raw_text)  # 校验语法
    CONFIG_PATH.write_text(raw_text, encoding="utf-8")
    return load_config(force=True)


def raw_config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


# ---------------- 保存目录 ----------------
DEFAULT_DIRS = {"image_dir": "data/media/images", "video_dir": "data/media/videos"}


def resolve_dir(p: str) -> Path:
    """相对路径按工程根目录解析；支持 ~ 展开。"""
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def storage_dirs() -> dict[str, Path]:
    """返回 {'image': Path, 'video': Path}，并确保目录存在。"""
    conf = (load_config().get("storage") or {})
    out: dict[str, Path] = {}
    for kind, key in (("image", "image_dir"), ("video", "video_dir")):
        raw = conf.get(key) or DEFAULT_DIRS[key]
        d = resolve_dir(str(raw))
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            d = resolve_dir(DEFAULT_DIRS[key])
            d.mkdir(parents=True, exist_ok=True)
        out[kind] = d
    return out


def storage_raw() -> dict[str, str]:
    conf = (load_config().get("storage") or {})
    return {k: str(conf.get(k) or DEFAULT_DIRS[k]) for k in DEFAULT_DIRS}


def set_storage_dirs(image_dir: str | None = None,
                     video_dir: str | None = None) -> dict[str, str]:
    """就地修改 config.yaml 的 storage 段（保留注释与排版）。"""
    updates = {k: v for k, v in
               (("image_dir", image_dir), ("video_dir", video_dir)) if v}
    if not updates:
        return storage_raw()

    for v in updates.values():
        d = resolve_dir(v)
        d.mkdir(parents=True, exist_ok=True)  # 提前校验可写

    lines = raw_config_text().splitlines()
    out: list[str] = []
    in_storage = False
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if re.match(r"^storage\s*:", line):
            in_storage = True
            out.append(line)
            continue
        if in_storage:
            # 顶格的新键说明 storage 段结束
            if stripped and not line.startswith((" ", "\t")) and not stripped.startswith("#"):
                for k, v in updates.items():
                    if k not in seen:
                        out.append(f"  {k}: {v}")
                        seen.add(k)
                in_storage = False
            else:
                m = re.match(r"^(\s*)(image_dir|video_dir)\s*:", line)
                if m and m.group(2) in updates:
                    out.append(f"{m.group(1)}{m.group(2)}: {updates[m.group(2)]}")
                    seen.add(m.group(2))
                    continue
        out.append(line)

    missing = {k: v for k, v in updates.items() if k not in seen}
    if missing:
        if not in_storage and not any(re.match(r"^storage\s*:", l) for l in lines):
            out.append("")
            out.append("storage:")
        for k, v in missing.items():
            out.append(f"  {k}: {v}")

    save_config("\n".join(out) + "\n")
    return storage_raw()


def get_provider(pid: str) -> dict[str, Any]:
    for p in load_config().get("providers", []):
        if p["id"] == pid:
            return p
    raise KeyError(f"未知的 provider: {pid}")


def parse_ref(ref: str) -> tuple[dict[str, Any], str]:
    """'provider_id/model_id' -> (provider_conf, model_id)"""
    if "/" not in ref:
        raise ValueError(f"模型标识需为 provider/model 格式，收到: {ref}")
    pid, model = ref.split("/", 1)
    return get_provider(pid), model


def find_model_meta(pid: str, model_id: str, capability: str) -> dict[str, Any]:
    prov = get_provider(pid)
    for m in prov.get("models", {}).get(capability) or []:
        if m["id"] == model_id:
            return m
    return {"id": model_id, "name": model_id}
