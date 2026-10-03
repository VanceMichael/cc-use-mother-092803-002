"""SQLite 持久化层：菜单治理服务的表结构与连接管理。

表设计要点：
- requests 以 request_id 为主键缓存响应，任何动作重放都返回首次结果；
- versions 以 (recipe_id, version_no) 唯一，重复提交不会产生第二份版本；
- approvals 以 (version_id, market) 唯一，同一版本在同一市场只有一份审核记录；
- releases 以 (recipe_id, market) 为主键，是市场当前放行指针；
- release_events 以 request_id 唯一，重复发布/回滚不会写入第二条事件。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS actors (
    actor TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    markets TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    market TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    UNIQUE (recipe_id, version_no)
);

CREATE TABLE IF NOT EXISTS approvals (
    version_id TEXT NOT NULL,
    market TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    reason TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_at TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (version_id, market)
);

CREATE TABLE IF NOT EXISTS releases (
    recipe_id TEXT NOT NULL,
    market TEXT NOT NULL,
    version_id TEXT NOT NULL,
    released_by TEXT NOT NULL,
    released_at TEXT NOT NULL,
    request_id TEXT NOT NULL,
    PRIMARY KEY (recipe_id, market)
);

CREATE TABLE IF NOT EXISTS release_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id TEXT NOT NULL,
    market TEXT NOT NULL,
    version_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    response TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并确保表结构存在。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
