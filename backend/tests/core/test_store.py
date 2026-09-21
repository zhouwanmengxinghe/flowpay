# 回归测试：事件事务与回放。测试中的固定输入用于复现边界，不作为业务答案来源。
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import override

import pytest

from flowpay.core.event_log import EventStore
from flowpay.core.event_log.contracts import EventItems
from flowpay.core.event_log.types import Event
from flowpay.core.json import parse_json


def test_append_only_and_reopen(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    store.append("s", [("example", {"unicode": "支付", "nested": {"a": [1, 2]}})])
    with store.connect() as db:
        for sql in ("DELETE FROM events", "UPDATE events SET type = 'changed'"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(sql)
    reopened = EventStore(store.path)
    assert reopened.read("s") == store.read("s")
    assert reopened.read("s")[0]["payload"]["unicode"] == "支付"


def test_concurrent_batches_are_atomic_and_ordered(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")

    def append_pair(n: int) -> list[Event]:
        return store.append("s", [("a", {"n": n}), ("b", {"n": n})])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append_pair, range(30)))
    events = store.read("s")
    assert [e["seq"] for e in events] == list(range(1, 61))
    for i in range(0, len(events), 2):
        assert events[i]["payload"] == events[i + 1]["payload"]
        assert [events[i]["type"], events[i + 1]["type"]] == ["a", "b"]
    assert len(store.read("s", after=57)) == 3


def test_bad_payload_rolls_back_entire_batch(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    with pytest.raises(ValueError):
        store.append("s", [("good", {}), ("bad", {"amount": float("nan")})])
    assert store.read("s") == []
    event = store.append("s", [("ok", {})])[0]
    assert event["seq"] == 1
    assert parse_json(json.dumps(event)) == event


async def test_async_writer_waits_for_commit_before_propagating_cancellation() -> None:
    """取消发生在线程写入期间时，终态处理必须等该批写入完成。"""
    import asyncio
    import threading

    from flowpay.core.event_log.contracts import AsyncEventWriter

    entered = asyncio.Event()
    release = threading.Event()
    committed: list[tuple[str, EventItems, str | None, str | None, str | None]] = []
    loop = asyncio.get_running_loop()

    class BlockingWriter(AsyncEventWriter):
        @override
        def append(
            self,
            session_id: str,
            items: EventItems,
            run_id: str | None = None,
            call_id: str | None = None,
            parent_call_id: str | None = None,
        ) -> list[Event]:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise TimeoutError("Test did not release writer")
            committed.append((session_id, items, run_id, call_id, parent_call_id))
            return []

    task = asyncio.create_task(BlockingWriter().aappend("s", [("event", {})], "r", "c", "p"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        # 给取消处理机会运行，但底层提交还被阻塞，此时不能结束外层任务。
        await asyncio.sleep(0)
        assert not task.done()
        assert not committed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert committed == [("s", [("event", {})], "r", "c", "p")]


def test_summary_matches_replay_without_loading_raw_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flowpay.core.chat.session_view import build_session_view

    store = EventStore(tmp_path / "events.sqlite")
    store.append("s", [("session.created", {"workflow": {"id": "test", "version": "1"}})])
    items: EventItems = [
        (
            "run.started",
            {
                "request_id": "req",
                "input": "问题",
                "resumed": False,
                "resumed_from": None,
                "config": {},
                "workflow": {"id": "test", "version": "1"},
            },
        ),
        ("message.user", {"content": "问题"}),
        ("model.chunk", {"data": "x" * 10000}),
        ("run.finished", {"status": "waiting_user", "state": {}}),
    ]
    for kind, payload in items:
        store.append("s", [(kind, payload)], "r")
        expected = build_session_view(store.read("s"))
        summary = store.session_summaries()[0]
        assert summary == {key: dict(expected)[key] for key in summary}

    def fail_read(_session_id: str, _after: int = 0) -> list[Event]:
        pytest.fail("listing must not read raw history")

    monkeypatch.setattr(store, "read", fail_read)
    assert store.session_summaries()[0]["title"] == "问题"


def test_run_view_requires_current_start_metadata(tmp_path: Path) -> None:
    """缺少当前固定字段的早期运行记录不再被默认为空配置。"""
    from pydantic import ValidationError

    from flowpay.core.chat.session_view import build_session_view

    store = EventStore(tmp_path / "events.sqlite")
    store.append("s", [("session.created", {"workflow": {"id": "test", "version": "1"}})])
    store.append(
        "s", [("run.started", {"request_id": "r", "input": "问题", "resumed": False})], "r"
    )
    with pytest.raises(ValidationError):
        build_session_view(store.read("s"))
