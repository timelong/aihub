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


def list_jobs(kind: str | None = None, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM jobs"
    args: tuple = ()
    if kind:
        q += " WHERE kind=?"
        args = (kind,)
    q += " ORDER BY created_at DESC LIMIT ?"
    with _conn() as c:
        rows = c.execute(q, (*args, limit)).fetchall()
    out = []
    for r in rows:
        j = dict(r)
        j["result"] = json.loads(j.get("result") or "[]")
        j["params"] = json.loads(j.get("params") or "{}")
        out.append(j)
    return out
