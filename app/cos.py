"""腾讯云 COS：把本地上传的参考图临时放到公网，供只接受 URL 的服务商拉取。

有些服务商（阿里百炼图生视频、智谱 CogVideoX、魔搭图生图）只认公网可访问的图片
URL，不接受 base64。这里的做法是：

    1. 上传对象   put_object            → data:image/png;base64,... 转成 COS 上的临时对象
    2. 获取预签名 get_presigned_url     → 给服务商一个有时效的下载链接
    3. 删除对象   delete_object         → 任务结束（成功/失败/超时）后立刻删掉

配置写在 config.yaml 的 cos 段，密钥建议放 .env 用 ${} 引用：

    cos:
      secret_id: ${COS_SECRET_ID}
      secret_key: ${COS_SECRET_KEY}
      region: ap-guangzhou
      bucket: my-bucket-1250000000
      prefix: aihub/tmp/        # key 前缀，最终 key = 前缀/时间戳/文件名
      timestamp_format: "%Y%m%d%H%M%S"   # 时间戳目录格式，写 epoch 用 Unix 秒
      expire_seconds: 1800      # 预签名有效期，要覆盖生成耗时（视频最长轮询 30 分钟）
      scheme: https
      enabled: true             # 留空/不写 = 配全了就自动启用

文档: https://cloud.tencent.com/document/product/436/12269
      https://cloud.tencent.com/document/product/436/35153
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Iterable

from .config import load_config
from .providers.base import is_data_url, split_data_url

log = logging.getLogger("aihub.cos")

DEFAULT_PREFIX = "aihub/tmp/"
DEFAULT_TS_FORMAT = "%Y%m%d%H%M%S"   # key 里的时间戳目录，写 epoch 则用 Unix 秒
DEFAULT_EXPIRE = 1800          # 30 分钟，和视频轮询上限一致
REQUIRED = ("secret_id", "secret_key", "region", "bucket")

_client: Any = None
_client_key: tuple | None = None


class CosError(RuntimeError):
    pass


# ============================ 配置 ============================
def conf() -> dict:
    return load_config().get("cos") or {}


def _ident() -> tuple:
    c = conf()
    return tuple(str(c.get(k) or "") for k in REQUIRED) + (str(c.get("scheme") or "https"),)


def missing_fields() -> list[str]:
    c = conf()
    return [k for k in REQUIRED if not str(c.get(k) or "").strip()]


def is_configured() -> bool:
    c = conf()
    if c.get("enabled") is False:      # 显式关掉
        return False
    return not missing_fields()


def expire_seconds() -> int:
    try:
        return max(60, int(conf().get("expire_seconds") or DEFAULT_EXPIRE))
    except (TypeError, ValueError):
        return DEFAULT_EXPIRE


def status() -> dict:
    """给界面看的状态，不含密钥。"""
    c = conf()
    sid = str(c.get("secret_id") or "")
    return {
        "configured": is_configured(),
        "enabled": c.get("enabled") is not False,
        "missing": missing_fields(),
        "region": c.get("region") or "",
        "bucket": c.get("bucket") or "",
        "prefix": c.get("prefix") or DEFAULT_PREFIX,
        "key_sample": build_key("image/png"),
        "scheme": c.get("scheme") or "https",
        "expire_seconds": expire_seconds(),
        # 只回显前 4 位，确认填的是哪把 key 就够了
        "secret_id_masked": (sid[:4] + "***" + sid[-2:]) if len(sid) > 8 else ("***" if sid else ""),
        "sdk_installed": _sdk_available(),
    }


def _sdk_available() -> bool:
    try:
        import qcloud_cos  # noqa: F401
        return True
    except ImportError:
        return False


# ============================ 客户端 ============================
def client() -> Any:
    """惰性创建 CosS3Client；配置变了会自动重建。"""
    global _client, _client_key
    if not is_configured():
        raise CosError("腾讯云 COS 未配置完整，缺少: " + ", ".join(missing_fields() or ["-"]))
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as e:
        raise CosError("未安装 COS SDK，请执行: pip install cos-python-sdk-v5") from e

    ident = _ident()
    if _client is None or _client_key != ident:
        c = conf()
        cfg = CosConfig(Region=c["region"], SecretId=c["secret_id"],
                        SecretKey=c["secret_key"], Token=c.get("token") or None,
                        Scheme=c.get("scheme") or "https")
        _client = CosS3Client(cfg)
        _client_key = ident
        log.info("COS 客户端已创建 region=%s bucket=%s", c["region"], c["bucket"])
    return _client


# ============================ 上传 / 预签名 / 删除 ============================
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
        "image/gif": "gif", "image/bmp": "bmp"}


def _put_sync(key: str, body: bytes, mime: str) -> None:
    cli, c = client(), conf()
    cli.put_object(Bucket=c["bucket"], Body=body, Key=key,
                   ContentType=mime, StorageClass="STANDARD", EnableMD5=False)


def _sign_sync(key: str) -> str:
    cli, c = client(), conf()
    return cli.get_presigned_url(Bucket=c["bucket"], Key=key, Method="GET",
                                 Expired=expire_seconds())


def _delete_sync(key: str) -> None:
    cli, c = client(), conf()
    cli.delete_object(Bucket=c["bucket"], Key=key)


def build_key(mime: str = "image/png") -> str:
    """对象 key = 配置的前缀 / 时间戳 / 文件名。

    例：aihub/tmp/20260813192450/9f2c….png
    时间戳格式可用 cos.timestamp_format 改，写 epoch 则用 Unix 秒。
    """
    fmt = str(conf().get("timestamp_format") or DEFAULT_TS_FORMAT)
    now = datetime.now()
    ts = str(int(now.timestamp())) if fmt.lower() == "epoch" else now.strftime(fmt)
    prefix = (conf().get("prefix") or DEFAULT_PREFIX).strip().strip("/")
    name = f"{uuid.uuid4().hex}.{_EXT.get(mime, 'png')}"
    return "/".join(p for p in (prefix, ts, name) if p)


async def upload_data_url(data_url: str) -> tuple[str, str]:
    """上传一张 base64 图片，返回 (对象 key, 预签名下载 URL)。"""
    mime, raw = split_data_url(data_url)
    key = build_key(mime)
    await asyncio.to_thread(_put_sync, key, raw, mime)
    url = await asyncio.to_thread(_sign_sync, key)
    log.info("COS 已上传 key=%s 大小=%.1fKB 预签名有效期=%ds",
             key, len(raw) / 1024, expire_seconds())
    return key, url


async def delete(keys: Iterable[str]) -> None:
    """删除临时对象；失败只记日志，不影响主流程。"""
    for key in keys:
        try:
            await asyncio.to_thread(_delete_sync, key)
            log.info("COS 已删除临时对象 key=%s", key)
        except Exception as e:  # noqa: BLE001
            log.warning("COS 删除临时对象失败 key=%s: %s（请到控制台确认，或配置生命周期规则）",
                        key, e)


async def upload_refs(params: dict) -> tuple[dict, list[str]]:
    """把参考图换成预签名 URL，返回 (新 params, 需要善后删除的 key 列表)。

    删除由调用方负责——视频这类异步任务要等轮询结束才能删，不能用
    public_refs 的 with 语义（那会在提交完就删掉，上游还没来取图）。
    """
    from .providers.base import ref_images

    refs = ref_images(params)
    if not is_configured():
        raise CosError(
            "该服务商只接受公网图片 URL，需要先配置腾讯云 COS 作为临时图床："
            "在 config.yaml 的 cos 段填 secret_id / secret_key / region / bucket"
            f"（当前缺少: {', '.join(missing_fields())}）"
        )
    keys: list[str] = []
    out: list[str] = []
    try:
        for u in refs:
            if is_data_url(u):
                key, url = await upload_data_url(u)
                keys.append(key)
                out.append(url)
            else:
                out.append(u)
    except Exception:
        await delete(keys)          # 上传中途失败，先把已传的清掉
        raise
    patched = dict(params)
    patched["images"] = out
    patched["image_url"] = out[0] if out else None
    return patched, keys


@contextlib.asynccontextmanager
async def public_refs(params: dict, needed: bool = True) -> AsyncIterator[dict]:
    """把 params 里的 base64 参考图换成 COS 预签名 URL，退出时删除临时对象。

    needed=False（服务商本身支持 base64）时原样返回，不碰 COS。
    """
    from .providers.base import ref_images        # 延迟导入避免循环

    refs = ref_images(params)
    if not needed or not any(is_data_url(u) for u in refs):
        yield params
        return
    patched, keys = await upload_refs(params)
    try:
        yield patched
    finally:
        await delete(keys)


async def self_test() -> dict:
    """上传 → 预签名 → 下载校验 → 删除，用于界面上的「测试连接」。"""
    import httpx
    png = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AARAAA//8DAAT/AhWJ"
           "8QAAAABJRU5ErkJggg==")
    key, url = await upload_data_url(png)
    try:
        async with httpx.AsyncClient(timeout=20.0) as cli:
            r = await cli.get(url)
        readable = r.status_code == 200
        detail = "" if readable else f"HTTP {r.status_code}: {r.text[:200]}"
    finally:
        await delete([key])
    log.info("COS 自检完成 可下载=%s", readable)
    return {"ok": readable, "key": key, "url_sample": url.split("?")[0],
            "detail": detail, "expire_seconds": expire_seconds()}
