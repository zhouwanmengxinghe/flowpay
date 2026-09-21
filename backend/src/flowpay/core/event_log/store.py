# SQLite 事件存储用于离线演示和验收；按会话追加事件，事务内分配连续序号。
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override
from uuid import uuid4

from pydantic import TypeAdapter

from flowpay.core.chat.types import SessionStatus, SessionSummary
from flowpay.core.event_log.types import Event, require_event
from flowpay.core.execution.types import WorkflowIdentity
from flowpay.core.json import parse_json, require_int, require_text

from .contracts import AsyncEventWriter, EventItems


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class EventStore(AsyncEventWriter):
    """SQLite 会话事件存储；执行过程只追加，另提供事务性的整会话删除。"""

    def __init__(self, path: Path):
        """创建事件表和不可改写约束；仅专用连接允许删除整会话，启动不删除历史。"""
        self.path: Path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq)
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update
                    BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'append-only event log'); END;
                DROP TRIGGER IF EXISTS events_no_delete;
                CREATE TRIGGER events_no_delete
                    BEFORE DELETE ON events WHEN allow_session_delete() = 0 BEGIN
                    SELECT RAISE(ABORT, 'append-only event log'); END;
            """)

    @contextmanager
    def connect(self):
        """提供一次短生命周期连接，自动提交或回滚并确保连接关闭。

        WAL 模式配合 FULL 同步用于本地持久化；它不保证磁盘永不损坏，
        也不把 SQLite 当作多主机共享服务。
        """
        connection = sqlite3.connect(self.path, timeout=10)
        connection.create_function("allow_session_delete", 0, lambda: 0)
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # 批量事件作为一次原子提交；序号用于有序回放，而不是全局事件计数。
    @override
    def append(
        self,
        session_id: str,
        items: EventItems,
        run_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> list[Event]:
        """在一个 SQLite 写事务内分配序号、序列化并插入整批事件。

        BEGIN IMMEDIATE 在读取最大序号之前取得写锁，避免并发请求分配同一序号。
        任一载荷不能序列化或插入失败都回滚整批；返回的事件已经提交，才可用于 SSE。
        allow_nan=False 防止向跨语言 JSON 协议写入 NaN 或 Infinity。
        """
        events: list[Event] = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = cast(
                tuple[object],
                db.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,)
                ).fetchone(),
            )
            seq = require_int(row[0])
            for kind, payload in items:
                seq += 1
                event: Event = {
                    "schema_version": 1,
                    "event_id": new_id("evt"),
                    "session_id": session_id,
                    "seq": seq,
                    "run_id": run_id,
                    "call_id": call_id,
                    "parent_call_id": parent_call_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": kind,
                    "payload": payload,
                }
                encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
                db.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        seq,
                        event["event_id"],
                        run_id,
                        kind,
                        encoded,
                    ),
                )
                events.append(event)
        return events

    def read(self, session_id: str, after: int = 0) -> list[Event]:
        """读取指定会话在 after 之后的事件，并按序号升序解码为事件对象。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT data FROM events WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, after),
            ).fetchall()
        return [
            require_event(parse_json(require_text(row[0])))
            for row in cast(list[tuple[object]], rows)
        ]

    def session_ids(self) -> list[str]:
        """列出有持久化事件的会话 ID；不依赖当前进程的内存任务。"""
        with self.connect() as db:
            return [
                row[0]
                for row in cast(
                    list[tuple[str]],
                    db.execute(
                        "SELECT session_id FROM events WHERE type = 'session.created' "
                        + "ORDER BY rowid DESC"
                    ).fetchall(),
                )
            ]

    def session_summaries(self) -> list[SessionSummary]:
        """在数据库内选取摘要所需事件，不把模型原始请求和响应读回 Python。"""
        with self.connect() as db:
            rows = db.execute("""
                SELECT c.session_id, json_extract(c.data, '$.payload.workflow'),
                  COALESCE((SELECT substr(json_extract(u.data,'$.payload.content'),1,36)
                    FROM events u WHERE u.session_id=c.session_id AND u.type='message.user'
                    ORDER BY u.seq LIMIT 1), '新会话'),
                  json_extract(last.data,'$.timestamp'), last.seq,
                  COALESCE((SELECT CASE r.type
                    WHEN 'run.started' THEN 'running'
                    WHEN 'run.finished' THEN json_extract(r.data,'$.payload.status')
                    WHEN 'run.failed' THEN 'failed' WHEN 'run.canceled' THEN 'canceled'
                    ELSE 'interrupted' END FROM events r
                    WHERE r.session_id=c.session_id AND r.type IN
                    ('run.started','run.finished','run.failed','run.canceled','run.interrupted')
                    ORDER BY r.seq DESC LIMIT 1), 'idle')
                FROM events c JOIN events last ON last.session_id=c.session_id
                  AND last.seq=(SELECT max(e.seq) FROM events e WHERE e.session_id=c.session_id)
                WHERE c.type='session.created' ORDER BY 4 DESC, c.session_id
            """).fetchall()
        return [
            SessionSummary(
                id=sid,
                workflow=TypeAdapter(WorkflowIdentity).validate_json(workflow),
                title=title,
                updated_at=updated,
                last_seq=seq,
                status=status,
            )
            for sid, workflow, title, updated, seq, status in TypeAdapter(
                list[tuple[str, str, str, str, int, SessionStatus]]
            ).validate_python(rows, strict=True)
        ]

    def delete_sessions(self, session_ids: list[str]) -> list[str]:
        """仅在本连接授权整会话删除，事务结束后关闭连接，不开放一般事件改写。"""
        deleted: list[str] = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.create_function("allow_session_delete", 0, lambda: 1)
            for sid in sorted(set(session_ids)):
                if db.execute("DELETE FROM events WHERE session_id=?", (sid,)).rowcount:
                    deleted.append(sid)
        return deleted
