"""SQLite 持久化：会话、消息、生成任务。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from .config import DATA_DIR

DB_PATH = DATA_DIR / "aihub.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '新会话',
    model TEXT,
    system_prompt TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    images TEXT DEFAULT '[]',
    reasoning TEXT DEFAULT '',
    tools TEXT DEFAULT '[]',
    model TEXT,
    created_at REAL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,          -- image | video
    provider TEXT,
    model TEXT,
    prompt TEXT,
    params TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending', -- pending | running | succeeded | failed
    hidden INTEGER DEFAULT 0,      -- 1 = 页面上不显示（记录和磁盘文件都保留）
    remote_id TEXT,
    result TEXT DEFAULT '[]',      -- 结果 URL 列表
    error TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        if "tools" not in cols:  # 老库补上工具调用轨迹字段
            c.execute("ALTER TABLE messages ADD COLUMN tools TEXT DEFAULT '[]'")
        jcols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        if "hidden" not in jcols:  # 从页面隐藏（记录与磁盘文件都保留）
            c.execute("ALTER TABLE jobs ADD COLUMN hidden INTEGER DEFAULT 0")


def _now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------- 会话 ----------------
def create_conversation(model: str = "", system_prompt: str = "", params: dict | None = None) -> dict:
    cid = new_id()
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO conversations(id,title,model,system_prompt,params,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (cid, "新会话", model, system_prompt, json.dumps(params or {}), now, now),
        )
    return get_conversation(cid)


def list_conversations() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id,title,model,updated_at FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(cid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        conv = dict(row)
        conv["params"] = json.loads(conv.get("params") or "{}")
        msgs = c.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (cid,)
        ).fetchall()
    conv["messages"] = [
        {**dict(m), "images": json.loads(m["images"] or "[]"),
         "tools": json.loads((dict(m).get("tools") or "[]"))} for m in msgs
    ]
    return conv


def update_conversation(cid: str, **fields: Any) -> None:
    if not fields:
        return
    if "params" in fields and isinstance(fields["params"], dict):
        fields["params"] = json.dumps(fields["params"])
    fields["updated_at"] = _now()
    sets = ",".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE conversations SET {sets} WHERE id=?", (*fields.values(), cid))


def delete_conversation(cid: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM conversations WHERE id=?", (cid,))


def add_message(cid: str, role: str, content: str, images: list | None = None,
                reasoning: str = "", model: str = "", tools: list | None = None) -> dict:
    mid = new_id()
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO messages(id,conversation_id,role,content,images,reasoning,"
            "model,tools,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, cid, role, content, json.dumps(images or []), reasoning, model,
             json.dumps(tools or []), now),
        )
        c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))
        # 首条用户消息作为标题
        row = c.execute(
            "SELECT title FROM conversations WHERE id=?", (cid,)
        ).fetchone()
        if row and row["title"] == "新会话" and role == "user" and content.strip():
            c.execute("UPDATE conversations SET title=? WHERE id=?", (content.strip()[:30], cid))
    return {"id": mid, "role": role, "content": content, "images": images or [],
            "reasoning": reasoning, "model": model, "tools": tools or [],
            "created_at": now}


# ---------------- 生成任务 ----------------
def create_job(kind: str, provider: str, model: str, prompt: str, params: dict) -> dict:
    jid = new_id()
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs(id,kind,provider,model,prompt,params,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (jid, kind, provider, model, prompt, json.dumps(params), "pending", now, now),
        )
    return get_job(jid)


def update_job(jid: str, **fields: Any) -> None:
    if "result" in fields and not isinstance(fields["result"], str):
        fields["result"] = json.dumps(fields["result"])
    fields["updated_at"] = _now()
    sets = ",".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), jid))


def get_job(jid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        return None
    j = dict(row)
    j["result"] = json.loads(j.get("result") or "[]")
    j["params"] = json.loads(j.get("params") or "{}")
    return j


def sweep_stale_jobs(max_age_seconds: float = 45 * 60) -> list[str]:
    """把上次进程留下的「生成中」任务标成失败。

    服务被杀掉后，进行中的任务没人再更新它，界面上会永远转圈。这里只处理
    明显过期的（默认 45 分钟，比视频轮询上限 30 分钟还宽），避免误伤另一个
    正在同时运行的实例。
    """
    cutoff = _now() - max_age_seconds
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM jobs WHERE status IN ('running','pending') AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.execute(
                f"UPDATE jobs SET status='failed', error=?, updated_at=? "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                ("状态未知：服务在任务进行中被重启或终止", _now(), *ids),
            )
    return ids


def hide_job(jid: str, hidden: bool = True) -> None:
    """只改显示状态，绝不动记录和磁盘文件。"""
    with _conn() as c:
        c.execute("UPDATE jobs SET hidden=? WHERE id=?", (1 if hidden else 0, jid))


def delete_job(jid: str) -> None:
    """删除任务记录本身（调用方要先确认这条没有已保存的结果文件）。"""
    with _conn() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))


def clear_jobs(kind: str | None = None) -> dict[str, list[str]]:
    """清空列表：成功的只隐藏（保留记录与图片），其余直接删记录。"""
    hidden: list[str] = []
    deleted: list[str] = []
    with _conn() as c:
        q = "SELECT id,status,result FROM jobs WHERE hidden=0"
        args: tuple = ()
        if kind:
            q += " AND kind=?"
            args = (kind,)
        for r in c.execute(q, args).fetchall():
            has_files = bool(json.loads(r["result"] or "[]"))
            if r["status"] == "succeeded" or has_files:
                c.execute("UPDATE jobs SET hidden=1 WHERE id=?", (r["id"],))
                hidden.append(r["id"])
            else:
                c.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                deleted.append(r["id"])
    return {"hidden": hidden, "deleted": deleted}


def list_jobs(kind: str | None = None, limit: int = 50,
              include_hidden: bool = False) -> list[dict]:
    q = "SELECT * FROM jobs"
    conds: list[str] = []
    args: list = []
    if kind:
        conds.append("kind=?")
        args.append(kind)
    if not include_hidden:
        conds.append("COALESCE(hidden,0)=0")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at DESC LIMIT ?"
    args = tuple(args)
    with _conn() as c:
        rows = c.execute(q, (*args, limit)).fetchall()
    out = []
    for r in rows:
        j = dict(r)
        j["result"] = json.loads(j.get("result") or "[]")
        j["params"] = json.loads(j.get("params") or "{}")
        out.append(j)
    return out
