"""事件存储：所有状态变更都以不可变事件落库，当前状态由重放得到。

事件溯源让"沿历史版本追溯每项原料为何在当时被允许使用"成为查询而非补丁：
每条 review 事件本身就记录了审核员、生效范围、时间和逐原料许可依据。
"""
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol


from .contracts import utcnow_naive


def _now() -> datetime:
    return utcnow_naive()


@dataclass(frozen=True)
class Event:
    seq: int
    event_type: str
    actor: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=_now)


class EventStore(Protocol):
    def append(self, event_type: str, actor: str, payload: dict[str, Any],
               request_id: str, created_at: datetime) -> Event: ...

    def save_result(self, request_id: str, result: dict[str, Any]) -> None: ...

    def replay(self, apply: Callable[[Event], None]) -> None: ...

    def idem_result(self, request_id: str) -> dict[str, Any] | None: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[Event] = list()
        self._idem: dict[str, dict[str, Any]] = {}
        self._reserved: set[str] = set()
        self._lock = threading.RLock()

    def append(self, event_type, actor, payload, request_id, created_at) -> Event:
        with self._lock:
            if request_id in self._reserved:
                raise IdempotencyConflict(request_id)
            self._reserved.add(request_id)
            event = Event(len(self._events) + 1, event_type, actor, dict(payload), created_at)
            self._events.append(event)
            return event

    def save_result(self, request_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._idem[request_id] = dict(result)

    def replay(self, apply) -> None:
        with self._lock:
            for event in self._events:
                apply(event)

    def idem_result(self, request_id):
        return self._idem.get(request_id)


class IdempotencyConflict(Exception):
    """同一 request_id 已被处理（存储层唯一约束，防止重复发布制造第二份记录）。"""


class SqliteEventStore:
    """SQLite 持久化：events 保存事实，idempotency 保存请求->结果映射。

    即使两个服务进程同时提交相同 request_id，UNIQUE 约束也会让其中一方失败，
    调用方再读取已提交的结果返回——去重不依赖单进程内的内存判断。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        seq        INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        actor      TEXT NOT NULL,
        payload    TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS idempotency (
        request_id TEXT PRIMARY KEY,
        done       INTEGER NOT NULL DEFAULT 0,
        accepted   INTEGER NOT NULL,
        state      TEXT NOT NULL,
        message    TEXT NOT NULL,
        data       TEXT NOT NULL
    );
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(self.SCHEMA)
            columns = {row["name"] for row in self._conn.execute(
                "PRAGMA table_info(idempotency)")}
            if "done" not in columns:  # 旧库迁移
                self._conn.execute(
                    "ALTER TABLE idempotency ADD COLUMN done INTEGER NOT NULL DEFAULT 1")
            self._conn.commit()

    def append(self, event_type, actor, payload, request_id, created_at) -> Event:
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO idempotency(request_id, done, accepted, state, message, data) "
                    "VALUES (?, 0, 0, '', '', '{}')",
                    (request_id,),
                )
            except sqlite3.IntegrityError:
                raise IdempotencyConflict(request_id)
            cur = self._conn.execute(
                "INSERT INTO events(event_type, actor, payload, created_at) VALUES (?, ?, ?, ?)",
                (event_type, actor, json.dumps(payload, ensure_ascii=False), created_at.isoformat()),
            )
            self._conn.commit()
            seq = int(cur.lastrowid)
            return Event(seq, event_type, actor, dict(payload), created_at)

    def save_result(self, request_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE idempotency SET done=1, accepted=?, state=?, message=?, data=? "
                "WHERE request_id=?",
                (1 if result["accepted"] else 0, result["state"], result["message"],
                 json.dumps(result.get("data", {}), ensure_ascii=False), request_id),
            )
            self._conn.commit()

    def replay(self, apply) -> None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event_type, actor, payload, created_at FROM events ORDER BY seq"
            ).fetchall()
        for row in rows:
            apply(Event(
                seq=row["seq"],
                event_type=row["event_type"],
                actor=row["actor"],
                payload=json.loads(row["payload"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            ))

    def idem_result(self, request_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT accepted, state, message, data FROM idempotency "
                "WHERE request_id=? AND done=1",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "accepted": bool(row["accepted"]),
            "state": row["state"],
            "message": row["message"],
            "data": json.loads(row["data"]),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
