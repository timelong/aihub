"""AI Hub —— 本地多模型问答 / 图片生成 / 视频生成服务。"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import providers, storage
from .config import (MEDIA_DIR, ROOT, load_config, raw_config_text, resolve_dir,
                     save_config, set_storage_dirs, storage_dirs, storage_raw)
from .providers.base import ProviderError, is_data_url, split_data_url

app = FastAPI(title="AI Hub", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
storage.init()


# ============================ 数据模型 ============================
class ChatReq(BaseModel):
    conversation_id: str | None = None
    model: str
    message: str = ""
    images: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
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
    image_dir: str | None = None
    video_dir: str | None = None


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

    async def gen():
        yield _sse({"type": "start", "conversation_id": cid, "model": req.model})
        buf, think = [], []
        try:
            async for ev in prov.chat_stream(model_id, msgs, params):
                if ev["type"] == "delta":
                    buf.append(ev["text"])
                elif ev["type"] == "reasoning":
                    think.append(ev["text"])
                yield _sse(ev)
        except ProviderError as e:
            yield _sse({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        else:
            if req.save and cid:
                storage.add_message(cid, "assistant", "".join(buf),
                                    reasoning="".join(think), model=req.model)
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
    job = storage.create_job("image", prov.id, model_id, req.prompt, req.params)
    storage.update_job(job["id"], status="running")
    try:
        urls = await prov.generate_image(model_id, req.prompt, req.params)
    except Exception as e:  # noqa: BLE001
        storage.update_job(job["id"], status="failed", error=str(e))
        raise HTTPException(502, str(e))
    local = [await _persist(u, "png", "image") for u in urls]
    storage.update_job(job["id"], status="succeeded", result=local)
    return storage.get_job(job["id"])


# ============================ 视频生成（异步任务）============================
@app.post("/api/video")
async def api_video(req: VideoReq, bg: BackgroundTasks) -> dict:
    try:
        prov, model_id = providers.resolve(req.model)
    except Exception as e:
        raise HTTPException(400, str(e))
    job = storage.create_job("video", prov.id, model_id, req.prompt, req.params)
    try:
        remote = await prov.submit_video(model_id, req.prompt, req.params)
    except Exception as e:  # noqa: BLE001
        storage.update_job(job["id"], status="failed", error=str(e))
        raise HTTPException(502, str(e))
    storage.update_job(job["id"], status="running", remote_id=remote)
    bg.add_task(_watch_video, job["id"], prov.id, model_id, remote)
    return storage.get_job(job["id"])


async def _watch_video(jid: str, pid: str, model_id: str, remote: str) -> None:
    prov = providers.build(pid)
    deadline = time.time() + 60 * 30
    while time.time() < deadline:
        await asyncio.sleep(6)
        try:
            st = await prov.poll_video(model_id, remote)
        except Exception as e:  # noqa: BLE001
            storage.update_job(jid, status="failed", error=str(e))
            return
        if st["status"] == "succeeded":
            local = [await _persist(u, "mp4", "video") for u in st.get("urls", [])]
            storage.update_job(jid, status="succeeded", result=local)
            return
        if st["status"] == "failed":
            storage.update_job(jid, status="failed", error=st.get("error", ""))
            return
    storage.update_job(jid, status="failed", error="轮询超时（30 分钟）")


@app.get("/api/jobs")
def api_jobs(kind: str | None = None) -> dict:
    return {"items": storage.list_jobs(kind)}


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
    except Exception:  # 下载/写盘失败就直接返回原始 URL
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
    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), reload=False)


if __name__ == "__main__":
    main()
