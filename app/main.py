"""AI Hub —— 本地多模型问答 / 图片生成 / 视频生成服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
# 注意：pydantic 模型字段与 FastAPI 路由参数的注解会在运行时被求值，
# 所以这两处只能用 Optional[X]，不能用 3.10+ 才支持的 X | None（见 README「Python 版本」）。
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import context as ctx
from . import cos
from . import logctx
from . import providers, storage
from . import tools as toolkit
from . import logging_setup as logs
from .config import (MEDIA_DIR, ROOT, defaults_raw, find_model_meta, load_config,
                     patch_section, raw_config_text, resolve_dir, save_config,
                     set_defaults, set_storage_dirs, storage_dirs, storage_raw)
from .logging_setup import preview, setup as setup_logging
from .providers.base import ProviderError, is_data_url, ref_images, split_data_url

LOG_FILE = setup_logging()
log = logging.getLogger("aihub")

app = FastAPI(title="AI Hub", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
storage.init()
log.info("AI Hub 启动 PID=%s 时区=%s 日志文件=%s",
         os.getpid(), time.strftime("%Z %z"), LOG_FILE)
_stale = storage.sweep_stale_jobs()
if _stale:
    log.warning("发现 %d 个上次残留的「生成中」任务，已标为失败: %s", len(_stale), _stale)


@app.on_event("shutdown")
def _on_shutdown() -> None:
    # 正常退出与被 kill/pkill 都会走到这里；写清 PID，便于和同时在跑的其它实例区分。
    log.warning("服务开始关闭 PID=%s。若你没有主动停止，通常是收到了外部信号"
                "（Ctrl-C / kill / pkill / 终端关闭），不是程序崩溃。", os.getpid())


@app.middleware("http")
async def access_log(request: Request, call_next):
    """记录每个接口调用：耗时、状态码；静态资源和媒体文件不记。"""
    path = request.url.path
    quiet = path.startswith(("/media/", "/assets/")) or path in ("/", "/favicon.ico")
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("✗ %s %s 未处理异常", request.method, path)
        raise
    ms = (time.perf_counter() - t0) * 1000
    if not quiet:
        lvl = logging.WARNING if resp.status_code >= 400 else logging.INFO
        log.log(lvl, "%s %s → %s %.0fms", request.method, path,
                resp.status_code, ms)
    return resp


@app.get("/api/logs")
def api_logs(lines: int = 200, file: Optional[str] = None, q: str = "",
             level: str = "") -> dict:
    """回看 / 搜索日志。

    file  留空看当天的 aihub.log，也可传 aihub-2026-08-12.log 看历史
    q     关键词，空格分隔表示「都要包含」，不区分大小写
    level 只看某个级别及以上：ERROR / WARNING / INFO / DEBUG
    """
    n = max(1, min(int(lines or 200), 5000))
    try:
        path = logs.resolve_file(file)
    except FileNotFoundError:
        raise HTTPException(404, f"日志文件不存在: {file}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        content = ""

    all_lines = content.splitlines()
    kept = all_lines
    if level:
        wanted = LEVEL_ORDER.get(level.upper())
        if wanted is None:
            raise HTTPException(400, f"未知日志级别: {level}")
        kept = [l for l in kept if _line_level(l) >= wanted]
    for kw in q.split():
        low = kw.lower()
        kept = [l for l in kept if low in l.lower()]

    return {"path": str(path), "file": path.name, "total": len(all_lines),
            "matched": len(kept), "lines": kept[-n:]}


LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LEVEL_RE = re.compile(r"^\S+ \S+ (DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")


def _line_level(line: str) -> int:
    """从日志行里取级别；取不到（比如 traceback 续行）当成最高级别，避免被过滤掉。"""
    m = _LEVEL_RE.match(line)
    return LEVEL_ORDER[m.group(1)] if m else 50


@app.get("/api/logs/files")
def api_log_files() -> dict:
    keep = logs.retention_days()
    return {"dir": str(logs.log_dir()), "retention_days": keep,
            "items": logs.list_files()}


class LogConfReq(BaseModel):
    retention_days: int


@app.put("/api/logs/config")
def api_log_config(req: LogConfReq) -> dict:
    days = int(req.retention_days)
    if days < 0 or days > 3650:
        raise HTTPException(400, "保留天数需在 0–3650 之间（0 表示永久保留）")
    patch_section("logging", {"retention_days": str(days)})
    removed = logs.cleanup()
    log.info("日志保留天数改为 %s，顺带清理了 %d 个文件", days, len(removed))
    return {"retention_days": logs.retention_days(), "removed": removed}


@app.post("/api/logs/cleanup")
def api_log_cleanup() -> dict:
    removed = logs.cleanup()
    log.info("手动清理过期日志，删除 %d 个: %s", len(removed), removed or "无")
    return {"removed": removed, "items": logs.list_files()}


@app.delete("/api/logs/{name}")
def api_log_delete(name: str) -> dict:
    """删除某个历史日志文件（当天的不允许删，它正在被写）。"""
    if name == "aihub.log":
        raise HTTPException(400, "当天日志正在写入，不能删除")
    try:
        path = logs.resolve_file(name)
    except FileNotFoundError:
        raise HTTPException(404, "文件不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    path.unlink()
    log.info("删除历史日志 %s", name)
    return {"items": logs.list_files()}


# ============================ 数据模型 ============================
class ChatReq(BaseModel):
    conversation_id: Optional[str] = None
    model: str
    message: str = ""
    images: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    save: bool = True


class ImageReq(BaseModel):
    model: str
    prompt: str
    params: dict[str, Any] = Field(default_factory=dict)


class VideoReq(BaseModel):
    model: str
    prompt: str
    params: dict[str, Any] = Field(default_factory=dict)


class ConvReq(BaseModel):
    model: str = ""
    system_prompt: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class ConfigReq(BaseModel):
    content: str


class StorageReq(BaseModel):
    image_dir: Optional[str] = None
    video_dir: Optional[str] = None


class DefaultsReq(BaseModel):
    chat: Optional[str] = None
    image: Optional[str] = None
    video: Optional[str] = None


# ============================ 模型 / 配置 ============================
@app.get("/api/models")
def api_models() -> dict:
    cfg = load_config(force=True)
    out = []
    for p in cfg.get("providers", []):
        models = p.get("models") or {}
        out.append({
            "id": p["id"],
            "name": p.get("name", p["id"]),
            "kind": p.get("kind", "openai"),
            "base_url": p.get("base_url", ""),
            "has_key": bool(p.get("api_key")),
            "chat": models.get("chat") or [],
            "image": models.get("image") or [],
            "video": models.get("video") or [],
        })
    return {"providers": out, "defaults": cfg.get("defaults", {})}


@app.get("/api/config")
def api_get_config() -> dict:
    return {"content": raw_config_text()}


@app.put("/api/config")
def api_put_config(req: ConfigReq) -> dict:
    try:
        save_config(req.content)
    except Exception as e:  # yaml 语法错误等
        raise HTTPException(400, f"配置无效: {e}")
    return {"ok": True}


# ============================ 运行时上下文 ============================
@app.get("/api/context")
def api_context() -> dict:
    """当前会注入给模型的时间上下文，前端展示 + 让用户确认时区对不对。"""
    conf = ctx.chat_conf()
    t = ctx.now()
    return {
        "enabled": bool(conf.get("inject_datetime", True)),
        "timezone": conf.get("timezone") or str(t.tzinfo),
        "now": f"{t:%Y-%m-%d %H:%M} {ctx.WEEKDAYS[t.weekday()]}",
        "note": ctx.datetime_note(),
    }


# ============================ 腾讯云 COS（临时图床）============================
@app.get("/api/cos")
def api_cos() -> dict:
    return cos.status()


@app.post("/api/cos/test")
async def api_cos_test() -> dict:
    """上传 → 预签名 → 下载 → 删除，验证 secretId/secretKey/region/bucket 是否可用。"""
    try:
        return await cos.self_test()
    except cos.CosError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("COS 自检失败")
        raise HTTPException(502, f"{type(e).__name__}: {e}")


# ============================ 工具 ============================
@app.get("/api/tools")
def api_tools() -> dict:
    return {"items": toolkit.catalog()}


# ============================ 默认模型 ============================
@app.get("/api/defaults")
def api_get_defaults() -> dict:
    return defaults_raw()


@app.put("/api/defaults")
def api_put_defaults(req: DefaultsReq) -> dict:
    try:
        return set_defaults(chat=req.chat, image=req.image, video=req.video)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"默认模型无效: {e}")


# ============================ 保存目录 ============================
@app.get("/api/storage")
def api_get_storage() -> dict:
    raw = storage_raw()
    real = storage_dirs()
    return {
        "image_dir": raw["image_dir"],
        "video_dir": raw["video_dir"],
        "image_abs": str(real["image"]),
        "video_abs": str(real["video"]),
        "home": str(Path.home()),
        "project": str(ROOT),
    }


@app.put("/api/storage")
def api_put_storage(req: StorageReq) -> dict:
    try:
        set_storage_dirs(req.image_dir, req.video_dir)
    except PermissionError:
        raise HTTPException(400, "目录不可写，请检查权限")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"目录无效: {e}")
    return api_get_storage()


@app.get("/api/fs")
def api_fs(path: str = "") -> dict:
    """浏览服务器端目录，供前端目录选择器使用（只返回目录）。"""
    target = resolve_dir(path) if path else Path.home()
    try:
        target = target.resolve()
        if not target.is_dir():
            raise HTTPException(400, "不是有效目录")
        dirs = sorted(
            (d.name for d in target.iterdir() if d.is_dir() and not d.name.startswith(".")),
            key=str.lower,
        )
    except PermissionError:
        raise HTTPException(403, "无权访问该目录")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "dirs": dirs[:500],
        "writable": os.access(target, os.W_OK),
        "shortcuts": [
            {"name": "工程目录", "path": str(ROOT)},
            {"name": "用户主目录", "path": str(Path.home())},
        ],
    }


class MkdirReq(BaseModel):
    path: str
    name: str


@app.post("/api/fs/mkdir")
def api_mkdir(req: MkdirReq) -> dict:
    name = req.name.strip().strip("/\\")
    if not name or name in (".", ".."):
        raise HTTPException(400, "文件夹名无效")
    target = resolve_dir(req.path) / name
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"创建失败: {e}")
    return {"path": str(target)}


# ============================ 会话 ============================
@app.get("/api/conversations")
def api_list_conv() -> dict:
    return {"items": storage.list_conversations()}


@app.post("/api/conversations")
def api_new_conv(req: ConvReq) -> dict:
    return storage.create_conversation(req.model, req.system_prompt, req.params)


@app.get("/api/conversations/{cid}")
def api_get_conv(cid: str) -> dict:
    conv = storage.get_conversation(cid)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return conv


@app.delete("/api/conversations/{cid}")
def api_del_conv(cid: str) -> dict:
    storage.delete_conversation(cid)
    return {"ok": True}


# ============================ 对话（SSE 流式）============================
def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def api_chat(req: ChatReq) -> StreamingResponse:
    try:
        prov, model_id = providers.resolve(req.model)
    except Exception as e:
        raise HTTPException(400, str(e))

    cid = req.conversation_id
    if req.save:
        if not cid or not storage.get_conversation(cid):
            cid = storage.create_conversation(req.model, req.system_prompt, req.params)["id"]
        storage.add_message(cid, "user", req.message, req.images)
        history = storage.get_conversation(cid)["messages"]
    else:
        history = [{"role": "user", "content": req.message, "images": req.images}]

    msgs = [{"role": m["role"], "content": m["content"], "images": m.get("images") or []}
            for m in history]
    params = dict(req.params)
    params["system_prompt"] = req.system_prompt
    params["tools"] = [t for t in req.tools if t in toolkit.SPECS]

    meta = find_model_meta(prov.id, model_id, "chat")
    with logctx.bind(conv=cid, model=req.model):
        log.info("对话开始 模型=%s(%s) 历史=%d条 图片=%d张 工具=%s 系统提示词=%d字 参数=%s",
                 meta.get("name") or model_id, model_id, len(msgs), len(req.images),
                 params["tools"] or "无", len(req.system_prompt or ""),
                 preview({k: v for k, v in req.params.items() if k != "tools"}))

    async def gen():
        logctx.set_ctx(conv=cid, model=req.model)
        yield _sse({"type": "start", "conversation_id": cid, "model": req.model})
        buf, think, trace = [], [], []
        t0 = time.perf_counter()
        try:
            async for ev in prov.chat_stream(model_id, msgs, params):
                if ev["type"] == "delta":
                    buf.append(ev["text"])
                elif ev["type"] == "reasoning":
                    think.append(ev["text"])
                elif ev["type"] == "tool_call":
                    log.info("调用工具 %s args=%s", ev["name"], preview(ev.get("args")))
                elif ev["type"] == "tool_result":
                    log.info("工具结果 %s", preview(ev.get("summary")))
                    trace.append(f"{ev['name']}: {ev.get('summary', '')}")
                yield _sse(ev)
        except ProviderError as e:
            log.error("对话失败 用时%.1fs: %s", time.perf_counter() - t0, e)
            yield _sse({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("对话异常 用时%.1fs", time.perf_counter() - t0)
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        else:
            text = "".join(buf)
            log.info("对话完成 输出%d字 思考%d字 工具%d次 用时%.1fs",
                     len(text), len("".join(think)), len(trace),
                     time.perf_counter() - t0)
            if req.save and cid:
                storage.add_message(cid, "assistant", text, reasoning="".join(think),
                                    model=req.model, tools=trace)
        yield _sse({"type": "end", "conversation_id": cid})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ============================ 图片生成 ============================
@app.post("/api/image")
async def api_image(req: ImageReq) -> dict:
    try:
        prov, model_id = providers.resolve(req.model)
    except Exception as e:
        raise HTTPException(400, str(e))
    refs = ref_images(req.params)
    meta = find_model_meta(prov.id, model_id, "image")
    allowed = [str(s) for s in (meta.get("sizes") or [])]
    size = str(req.params.get("size") or "")
    job = storage.create_job("image", prov.id, model_id, req.prompt, req.params)
    storage.update_job(job["id"], status="running")

    with logctx.bind(job=job["id"], model=req.model):
        if allowed and size and size not in allowed:
            log.warning("出图尺寸 %s 不在该模型支持列表 %s 内，仍按请求发出", size, allowed)
        log.info("出图开始 模型=%s(%s) 尺寸=%s 数量=%s 参考图%d张 prompt=%s 其它参数=%s",
                 meta.get("name") or model_id, model_id, size or "默认",
                 req.params.get("n", 1), len(refs), preview(req.prompt, 300),
                 preview({k: v for k, v in req.params.items()
                          if k not in ("images", "image_url", "size", "n")}))
        t0 = time.perf_counter()
        try:
            # 只认公网 URL 的服务商：参考图先经 COS 上传成临时预签名 URL，
            # 退出这个 with 时（无论成功失败）临时对象立刻删除。
            async with cos.public_refs(req.params, prov.public_url_refs) as p:
                urls = await prov.generate_image(model_id, req.prompt, p)
        except Exception as e:  # noqa: BLE001
            log.error("出图失败 用时%.1fs: %s", time.perf_counter() - t0, e)
            storage.update_job(job["id"], status="failed", error=str(e))
            raise HTTPException(502, str(e))
        log.info("上游返回 %d 张，开始落盘", len(urls))
        local = [await _persist(u, "png", "image") for u in urls]
        log.info("出图成功 用时%.1fs 共%d张 %s", time.perf_counter() - t0, len(local),
                 "; ".join(_describe(u, "image") for u in local))
        storage.update_job(job["id"], status="succeeded", result=local)
    return storage.get_job(job["id"])


def _describe(url: str, kind: str) -> str:
    """把 /media/image/x.png 描述成 '绝对路径 (123.4 KB)'，方便直接去文件夹里找。"""
    if not url.startswith("/media/"):
        return url
    path = storage_dirs()[kind] / url.rsplit("/", 1)[-1]
    try:
        return f"{path} ({path.stat().st_size / 1024:.1f} KB)"
    except OSError:
        return str(path)


# ============================ 视频生成（异步任务）============================
@app.post("/api/video")
async def api_video(req: VideoReq, bg: BackgroundTasks) -> dict:
    try:
        prov, model_id = providers.resolve(req.model)
    except Exception as e:
        raise HTTPException(400, str(e))
    meta = find_model_meta(prov.id, model_id, "video")
    job = storage.create_job("video", prov.id, model_id, req.prompt, req.params)
    with logctx.bind(job=job["id"], model=req.model):
        log.info("视频提交 模型=%s(%s) 首帧图=%s prompt=%s 参数=%s",
                 meta.get("name") or model_id, model_id,
                 "有" if req.params.get("image_url") else "无", preview(req.prompt, 300),
                 preview({k: v for k, v in req.params.items() if k != "image_url"}))
        # 视频是异步任务，上游可能过一会儿才来取图，所以临时对象不能马上删，
        # 要留到轮询结束（成功/失败/超时）后由 _watch_video 删。
        cos_keys: list[str] = []
        params = req.params
        try:
            if prov.public_url_refs and any(
                    is_data_url(u) for u in ref_images(req.params)):
                params, cos_keys = await cos.upload_refs(req.params)
            remote = await prov.submit_video(model_id, req.prompt, params)
        except Exception as e:  # noqa: BLE001
            log.error("视频提交失败: %s", e)
            await cos.delete(cos_keys)
            storage.update_job(job["id"], status="failed", error=str(e))
            raise HTTPException(502, str(e))
        log.info("视频任务已受理 remote=%s，转后台轮询", remote)
        storage.update_job(job["id"], status="running", remote_id=remote)
    bg.add_task(_watch_video, job["id"], prov.id, model_id, remote, req.model, cos_keys)
    return storage.get_job(job["id"])


async def _watch_video(jid: str, pid: str, model_id: str, remote: str,
                       ref: str = "", cos_keys: list[str] | None = None) -> None:
    logctx.set_ctx(job=jid, model=ref or f"{pid}/{model_id}")
    try:
        await _watch_video_inner(jid, pid, model_id, remote)
    finally:
        await cos.delete(cos_keys or [])   # 任务结束就删掉临时参考图


async def _watch_video_inner(jid: str, pid: str, model_id: str, remote: str) -> None:
    prov = providers.build(pid)
    t0 = time.time()
    deadline = t0 + 60 * 30
    attempt = 0
    while time.time() < deadline:
        await asyncio.sleep(6)
        attempt += 1
        try:
            st = await prov.poll_video(model_id, remote)
        except Exception as e:  # noqa: BLE001
            log.error("视频轮询异常（第 %d 次，已等 %.0fs）: %s", attempt, time.time() - t0, e)
            storage.update_job(jid, status="failed", error=str(e))
            return
        prov.poll_progress(attempt, st["status"], time.time() - t0, remote)
        if st["status"] == "succeeded":
            local = [await _persist(u, "mp4", "video") for u in st.get("urls", [])]
            log.info("视频完成 用时%.0fs 共%d个 %s", time.time() - t0, len(local),
                     "; ".join(_describe(u, "video") for u in local))
            storage.update_job(jid, status="succeeded", result=local)
            return
        if st["status"] == "failed":
            log.error("视频失败 用时%.0fs: %s", time.time() - t0, st.get("error", ""))
            storage.update_job(jid, status="failed", error=st.get("error", ""))
            return
    log.error("视频轮询超时（30 分钟，共轮询 %d 次）", attempt)
    storage.update_job(jid, status="failed", error="轮询超时（30 分钟）")


@app.get("/api/jobs")
def api_jobs(kind: Optional[str] = None, include_hidden: bool = False) -> dict:
    return {"items": storage.list_jobs(kind, include_hidden=include_hidden)}


@app.delete("/api/jobs/{jid}")
def api_job_delete(jid: str) -> dict:
    """从列表移除一条记录。

    有结果文件的（生成成功的）只标记隐藏——记录保留、磁盘上的图片/视频一律不删；
    没有结果的（失败、卡住的）才真正删掉这条记录。
    """
    j = storage.get_job(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    if j["status"] == "succeeded" or j.get("result"):
        storage.hide_job(jid, True)
        log.info("任务 %s 已从页面隐藏（记录与 %d 个结果文件保留）", jid, len(j["result"]))
        return {"action": "hidden", "id": jid, "files_kept": j["result"]}
    storage.delete_job(jid)
    log.info("任务 %s 记录已删除（状态=%s，无结果文件）", jid, j["status"])
    return {"action": "deleted", "id": jid}


@app.post("/api/jobs/{jid}/restore")
def api_job_restore(jid: str) -> dict:
    if not storage.get_job(jid):
        raise HTTPException(404, "任务不存在")
    storage.hide_job(jid, False)
    log.info("任务 %s 恢复显示", jid)
    return {"action": "restored", "id": jid}


@app.post("/api/jobs/clear")
def api_jobs_clear(kind: Optional[str] = None) -> dict:
    """清空列表：成功的隐藏（记录和文件都留着），失败/未完成的删记录。"""
    r = storage.clear_jobs(kind)
    log.info("清空 %s 列表：隐藏 %d 条（保留文件），删除 %d 条无结果记录",
             kind or "全部", len(r["hidden"]), len(r["deleted"]))
    return r


@app.get("/api/jobs/{jid}")
def api_job(jid: str) -> dict:
    j = storage.get_job(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


# ============================ 媒体落盘 ============================
async def _persist(url: str, ext: str, kind: str = "image") -> str:
    """把远端/base64 结果保存到用户配置的目录，返回 /media/{kind}/{文件名}。"""
    base = storage_dirs()[kind]
    name = f"{storage.new_id()}.{ext}"
    try:
        if is_data_url(url):
            mime, raw = split_data_url(url)
            name = f"{storage.new_id()}{mimetypes.guess_extension(mime) or '.' + ext}"
            (base / name).write_bytes(raw)
        else:
            async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as cli:
                r = await cli.get(url)
                r.raise_for_status()
                (base / name).write_bytes(r.content)
    except Exception as e:  # noqa: BLE001  # 下载/写盘失败就直接返回原始 URL
        log.warning("结果落盘失败（回退为远端 URL）: %s | %s", e, preview(url, 120))
        return url
    return f"/media/{kind}/{name}"


def _serve(base: Path, name: str) -> FileResponse:
    path = (base / name).resolve()
    if not path.is_file() or base.resolve() not in path.parents:
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@app.get("/media/{kind}/{name}")
def api_media_kind(kind: str, name: str) -> FileResponse:
    if kind not in ("image", "video"):
        raise HTTPException(404, "文件不存在")
    return _serve(storage_dirs()[kind], name)


@app.get("/media/{name}")
def api_media(name: str) -> FileResponse:
    """兼容旧记录（早期结果直接存在 data/media 下）。"""
    for base in (MEDIA_DIR, *storage_dirs().values()):
        try:
            return _serve(base, name)
        except HTTPException:
            continue
    raise HTTPException(404, "文件不存在")


# ============================ 静态前端 ============================
WEB_DIR = ROOT / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn
    import os
    # log_config=None：不让 uvicorn 覆盖我们的 handler，日志统一进 data/logs/aihub.log
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), reload=False,
                log_config=None, access_log=False)


if __name__ == "__main__":
    main()
