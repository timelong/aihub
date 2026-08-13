#!/usr/bin/env python3
"""AI Hub 终端客户端 —— 直接调用 provider 层，无需启动 Web 服务。

用法:
    python cli.py                      # 进入交互式对话
    python cli.py -m openai/gpt-4o     # 指定模型
    python cli.py --list               # 列出全部可用模型
    python cli.py --image "提示词"      # 直接出图
    python cli.py --video "提示词"      # 直接出视频

交互命令:
    /model            切换对话模型
    /image <提示词>    生成图片
    /video <提示词>    生成视频
    /sys <提示词>      设置 system prompt
    /set temp 0.3     调整参数 (temp / top_p / max_tokens)
    /new              清空上下文
    /save             保存当前会话到数据库
    /help  /exit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.prompt import Prompt  # noqa: E402
from rich.table import Table  # noqa: E402

from app import providers, storage  # noqa: E402
from app.config import load_config  # noqa: E402
from app.config import storage_dirs
from app.main import _persist  # noqa: E402

con = Console()


def all_models(cap: str) -> list[tuple[str, str, str]]:
    out = []
    for p in load_config().get("providers", []):
        for m in (p.get("models") or {}).get(cap) or []:
            out.append((f"{p['id']}/{m['id']}", m.get("name", m["id"]), p.get("name", p["id"])))
    return out


def show_models() -> None:
    for cap, title in (("chat", "💬 对话"), ("image", "🎨 图片"), ("video", "🎬 视频")):
        t = Table(title=title, show_lines=False, title_justify="left")
        t.add_column("标识", style="cyan"); t.add_column("名称"); t.add_column("服务商", style="dim")
        for ref, name, prov in all_models(cap):
            t.add_row(ref, name, prov)
        con.print(t)


async def do_chat(ref: str, msgs: list[dict], params: dict) -> str:
    prov, model = providers.resolve(ref)
    con.print("[bold green]助手[/] ", end="")
    acc, in_think = [], False
    async for ev in prov.chat_stream(model, msgs, params):
        if ev["type"] == "reasoning":
            if not in_think:
                con.print("\n[dim]💭 ", end=""); in_think = True
            con.print(f"[dim]{ev['text']}[/]", end="")
        elif ev["type"] == "delta":
            if in_think:
                con.print("[/]\n"); in_think = False
            con.print(ev["text"], end="", highlight=False)
            acc.append(ev["text"])
    con.print("\n")
    return "".join(acc)


async def do_image(ref: str, prompt: str, params: dict) -> None:
    prov, model = providers.resolve(ref)
    with con.status("[yellow]出图中…"):
        urls = await prov.generate_image(model, prompt, params)
        local = [await _persist(u, "png", "image") for u in urls]
    job = storage.create_job("image", prov.id, model, prompt, params)
    storage.update_job(job["id"], status="succeeded", result=local)
    for u in local:
        p = storage_dirs()["image"] / Path(u).name
        con.print(f"[green]✅ 图片已保存：[/] {p if p.exists() else u}")


async def do_video(ref: str, prompt: str, params: dict) -> None:
    prov, model = providers.resolve(ref)
    job = storage.create_job("video", prov.id, model, prompt, params)
    remote = await prov.submit_video(model, prompt, params)
    storage.update_job(job["id"], status="running", remote_id=remote)
    con.print(f"[dim]任务已提交: {remote}[/]")
    with con.status("[yellow]视频生成中（通常 1–5 分钟）…"):
        for _ in range(300):
            await asyncio.sleep(6)
            st = await prov.poll_video(model, remote)
            if st["status"] == "succeeded":
                local = [await _persist(u, "mp4", "video") for u in st["urls"]]
                storage.update_job(job["id"], status="succeeded", result=local)
                for u in local:
                    p = storage_dirs()["video"] / Path(u).name
                    con.print(f"[green]✅ 视频已保存：[/] {p if p.exists() else u}")
                return
            if st["status"] == "failed":
                storage.update_job(job["id"], status="failed", error=st.get("error", ""))
                con.print(f"[red]❌ 失败: {st.get('error')}[/]")
                return
    con.print("[red]❌ 超时[/]")


def pick(cap: str, cur: str) -> str:
    items = all_models(cap)
    if not items:
        con.print(f"[red]没有可用的 {cap} 模型[/]"); return cur
    for i, (ref, name, prov) in enumerate(items, 1):
        mark = "▶" if ref == cur else " "
        con.print(f" {mark} [cyan]{i:>2}[/]. {name} [dim]({prov} · {ref})[/]")
    idx = Prompt.ask("选择编号", default="1")
    try:
        return items[int(idx) - 1][0]
    except Exception:
        return cur


async def repl(model: str) -> None:
    storage.init()
    cfg = load_config()
    model = model or cfg.get("defaults", {}).get("chat") or (all_models("chat") or [("", "", "")])[0][0]
    img_model = cfg.get("defaults", {}).get("image", "")
    vid_model = cfg.get("defaults", {}).get("video", "")
    params: dict = {"temperature": 0.7}
    system = ""
    msgs: list[dict] = []
    cid = None

    con.print(Panel.fit(
        "[bold]AI Hub 终端客户端[/]\n输入消息开始对话，[cyan]/help[/] 查看命令，[cyan]/exit[/] 退出",
        border_style="blue"))
    con.print(f"[dim]当前模型: {model}[/]\n")

    while True:
        try:
            line = Prompt.ask("[bold cyan]你[/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        if line in ("/exit", "/quit", "/q"):
            break
        if line == "/help":
            con.print(__doc__); continue
        if line == "/new":
            msgs, cid = [], None; con.print("[dim]上下文已清空[/]"); continue
        if line == "/models":
            show_models(); continue
        if line == "/model":
            model = pick("chat", model); con.print(f"[green]已切换: {model}[/]"); continue
        if line.startswith("/sys "):
            system = line[5:].strip(); con.print("[dim]system prompt 已设置[/]"); continue
        if line.startswith("/set "):
            try:
                _, k, v = line.split(maxsplit=2)
                key = {"temp": "temperature", "top_p": "top_p", "max_tokens": "max_tokens"}.get(k, k)
                params[key] = int(v) if key == "max_tokens" else float(v)
                con.print(f"[dim]{key} = {params[key]}[/]")
            except Exception:
                con.print("[red]用法: /set temp 0.3[/]")
            continue
        if line.startswith("/image"):
            prompt = line[6:].strip() or Prompt.ask("提示词")
            if not img_model:
                img_model = pick("image", "")
            try:
                await do_image(img_model, prompt, {"size": "1024x1024", "n": 1})
            except Exception as e:
                con.print(f"[red]❌ {e}[/]")
            continue
        if line.startswith("/video"):
            prompt = line[6:].strip() or Prompt.ask("提示词")
            if not vid_model:
                vid_model = pick("video", "")
            try:
                await do_video(vid_model, prompt, {"duration": 5, "ratio": "16:9",
                                                   "aspect_ratio": "16:9"})
            except Exception as e:
                con.print(f"[red]❌ {e}[/]")
            continue
        if line == "/save":
            if cid is None:
                cid = storage.create_conversation(model, system, params)["id"]
                for m in msgs:
                    storage.add_message(cid, m["role"], m["content"])
            con.print(f"[green]已保存会话 {cid}[/]"); continue

        msgs.append({"role": "user", "content": line, "images": []})
        try:
            reply = await do_chat(model, msgs, {**params, "system_prompt": system})
            msgs.append({"role": "assistant", "content": reply, "images": []})
        except Exception as e:
            msgs.pop()
            con.print(f"[red]❌ {e}[/]\n")

    con.print("[dim]再见 👋[/]")


def main() -> None:
    ap = argparse.ArgumentParser(description="AI Hub 终端客户端")
    ap.add_argument("-m", "--model", default="", help="provider/model")
    ap.add_argument("--list", action="store_true", help="列出可用模型")
    ap.add_argument("--image", help="直接生成图片")
    ap.add_argument("--video", help="直接生成视频")
    ap.add_argument("-p", "--prompt", help="单轮提问后退出")
    a = ap.parse_args()

    storage.init()
    if a.list:
        show_models(); return
    if a.image:
        m = a.model or load_config().get("defaults", {}).get("image", "")
        asyncio.run(do_image(m, a.image, {"size": "1024x1024", "n": 1})); return
    if a.video:
        m = a.model or load_config().get("defaults", {}).get("video", "")
        asyncio.run(do_video(m, a.video, {"duration": 5, "aspect_ratio": "16:9",
                                          "ratio": "16:9"})); return
    if a.prompt:
        m = a.model or load_config().get("defaults", {}).get("chat", "")
        asyncio.run(do_chat(m, [{"role": "user", "content": a.prompt, "images": []}],
                            {"temperature": 0.7})); return
    asyncio.run(repl(a.model))


if __name__ == "__main__":
    main()
