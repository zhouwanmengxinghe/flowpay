"""会话删除回归：全批拒绝运行中会话、永久清除、幂等重试和 HTTP 校验。"""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flowpay.core.chat.errors import SessionNotFound
from flowpay.core.config import Settings
from flowpay.core.event_log import EventStore
from flowpay.core.execution.contracts import TurnInput, TurnResult
from flowpay.core.execution.errors import Conflict
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard
from flowpay.core.http import create_app
from flowpay.core.json import parse_json, require_object, require_text
from tests.core.fixtures import FixtureWorkflow


class WaitingWorkflow:
    """保持运行中状态，供测试验证删除与取消的边界。"""

    id: str = "waiting"
    version: str = "1"
    card: StageCard = StageCard(label="test", description="test")

    async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
        """等待测试显式取消，不产生模型或工具副作用。"""
        _ = turn, runtime
        await asyncio.Event().wait()
        return TurnResult("unreachable", {})


async def test_batch_delete_running_guard_and_no_revival(tmp_path: Path) -> None:
    """批量含运行项时一个也不删；停止后清除，旧请求不能重新创建会话。"""
    settings = Settings(tmp_path, tmp_path)
    store = EventStore(tmp_path / "sessions.sqlite")
    manager = SessionManager(store, WaitingWorkflow(), settings)
    try:
        idle = (await manager.create_session())["id"]
        busy = (await manager.create_session())["id"]
        keep = (await manager.create_session())["id"]
        await manager.submit_message(busy, "request", "hello")
        with pytest.raises(Conflict, match="Stop running"):
            await manager.delete_sessions([idle, busy])
        assert set(store.session_ids()) == {idle, busy, keep}
        await manager.cancel_run(busy)
        assert await manager.delete_sessions([busy, idle, idle, "missing"]) == sorted([busy, idle])
        assert store.read(busy) == [] and store.read(idle) == []
        assert store.session_ids() == [keep]
        assert await manager.delete_sessions([busy]) == []
        with pytest.raises(SessionNotFound):
            await manager.submit_message(busy, "retry", "cannot revive")
        reopened = EventStore(tmp_path / "sessions.sqlite")
        assert reopened.session_ids() == [keep]
    finally:
        await manager.shutdown()


def test_delete_http_contract(tmp_path: Path) -> None:
    """单删和批删均返回实际 ID；读、导出、续跑已删除会话均不可访问。"""
    settings = Settings(tmp_path, tmp_path)
    with TestClient(create_app(settings, FixtureWorkflow(settings))) as client:
        ids = [client.post("/api/sessions").json()["id"] for _ in range(3)]
        first = client.delete(f"/api/sessions/{ids[0]}")
        assert first.status_code == 200
        assert first.json() == {"deleted_ids": [ids[0]]}
        assert client.delete(f"/api/sessions/{ids[0]}").json() == {"deleted_ids": []}
        for suffix in ("", "/events", "/export", "/stream"):
            assert client.get(f"/api/sessions/{ids[0]}{suffix}").status_code == 404
        assert (
            client.post(f"/api/sessions/{ids[0]}/continue", json={"request_id": "x"}).status_code
            == 404
        )
        batch = client.post("/api/sessions/batch-delete", json={"session_ids": ids[1:]})
        assert batch.json() == {"deleted_ids": sorted(ids[1:])}
        assert client.get("/api/sessions").json() == []
        for bad in ([], [" "], ["x"] * 101):
            assert (
                client.post("/api/sessions/batch-delete", json={"session_ids": bad}).status_code
                == 422
            )


async def test_cancel_delete_waits_for_storage_before_unlock(tmp_path: Path) -> None:
    """删除请求取消时仍等待数据库完成，排队的提交只能看到删除后的 404。"""
    from threading import Event
    from typing import override

    entered, release = Event(), Event()

    class SlowStore(EventStore):
        """在实际删除前设置屏障，观察管理器是否过早释放会话锁。"""

        @override
        def delete_sessions(self, session_ids: list[str]) -> list[str]:
            """等待测试放行后执行原有删除事务。"""
            entered.set()
            assert release.wait(3)
            return super().delete_sessions(session_ids)

    settings = Settings(tmp_path, tmp_path)
    manager = SessionManager(SlowStore(tmp_path / "slow.sqlite"), WaitingWorkflow(), settings)
    try:
        sid = (await manager.create_session())["id"]
        deletion = asyncio.create_task(manager.delete_sessions([sid]))
        assert await asyncio.to_thread(entered.wait, 3)
        deletion.cancel()
        submission = asyncio.create_task(manager.submit_message(sid, "late", "hello"))
        await asyncio.sleep(0)
        assert not submission.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        with pytest.raises(SessionNotFound):
            await submission
    finally:
        release.set()
        await manager.shutdown()


def test_active_stream_closes_when_session_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已建立的 SSE 收到删除通知后结束，而不是无限重连已删除会话。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from typing import cast

    from flowpay.core.event_log.types import Event as TraceEvent

    settings = Settings(tmp_path, tmp_path)
    app = create_app(settings, FixtureWorkflow(settings))
    with TestClient(app) as client:
        sid = require_text(require_object(parse_json(client.post("/api/sessions").content))["id"])
        runtime = cast(SessionManager, app.state.manager)
        read = runtime.store.read
        streaming = Event()

        def observed(session_id: str, after: int = 0) -> list[TraceEvent]:
            """让测试只在流开始轮询后发起删除。"""
            if after == 1:
                streaming.set()
            return read(session_id, after)

        monkeypatch.setattr(runtime.store, "read", observed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(client.get, f"/api/sessions/{sid}/stream?after=1")
            assert streaming.wait(3)
            assert client.delete(f"/api/sessions/{sid}").status_code == 200
            assert "event: session-deleted" in response.result(timeout=3).text
