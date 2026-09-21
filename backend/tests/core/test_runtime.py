# 回归测试：运行生命周期与恢复。测试中的固定输入用于复现边界，不作为业务答案来源。
import asyncio
from pathlib import Path

import pytest

from flowpay.core.config import Settings
from flowpay.core.event_log import EventStore
from flowpay.core.event_log.contracts import EventItems
from flowpay.core.event_log.types import Event
from flowpay.core.execution.contracts import TurnInput, TurnResult, Workflow
from flowpay.core.execution.errors import Conflict
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard
from tests.core.fixtures import FixtureWorkflow


def manager(tmp_path: Path, delay: int = 0, workflow: Workflow | None = None) -> SessionManager:
    settings = Settings(tmp_path, tmp_path / "dataset", mock_delay_ms=delay)
    return SessionManager(
        EventStore(tmp_path / "sessions.sqlite"), workflow or FixtureWorkflow(settings), settings
    )


async def settle(runtime: SessionManager, sid: str):
    task = runtime.tasks.get(sid)
    if task:
        await task
    return await runtime.get_session(sid)


async def test_idempotency_and_conflicting_input(tmp_path: Path) -> None:
    runtime = manager(tmp_path, delay=50)
    sid = (await runtime.create_session())["id"]
    run = await runtime.submit_message(sid, "one", "question")
    duplicate = await runtime.submit_message(sid, "one", "question")
    assert run["id"] == duplicate["id"]
    with pytest.raises(Conflict, match="different content"):
        await runtime.submit_message(sid, "one", "different")
    with pytest.raises(Conflict, match="active run"):
        await runtime.submit_message(sid, "two", "question")
    await runtime.cancel_run(sid)
    assert (await runtime.get_session(sid))["status"] == "canceled"
    assert len([e for e in runtime.store.read(sid) if e["type"] == "message.user"]) == 1


async def test_cancel_before_task_starts_then_resume(tmp_path: Path) -> None:
    runtime = manager(tmp_path)
    sid = (await runtime.create_session())["id"]
    await runtime.submit_message(sid, "one", "acct_demo production req_demo_503")
    await runtime.cancel_run(sid)
    assert (await runtime.get_session(sid))["status"] == "canceled"
    await runtime.resume_run(sid, "resume")
    final = await settle(runtime, sid)
    assert final["status"] == "completed"
    assert len([m for m in final["messages"] if m["role"] == "user"]) == 1
    assert final["runs"][-1].get("resumed_from") == final["runs"][0]["id"]


async def test_restart_marks_orphan_run_and_preserves_history(tmp_path: Path) -> None:
    runtime = manager(tmp_path)
    sid = (await runtime.create_session())["id"]
    runtime.store.append(
        sid,
        [
            ("message.user", {"content": "question"}),
            (
                "run.started",
                {
                    "request_id": "one",
                    "input": "question",
                    "resumed": False,
                    "resumed_from": None,
                    "config": {},
                    "workflow": runtime.identity,
                },
            ),
            ("tool.started", {"name": "read_only", "arguments": {}}),
        ],
        "orphan",
    )
    reopened = manager(tmp_path)
    await reopened.recover()
    assert (await reopened.get_session(sid))["status"] == "interrupted"
    await reopened.resume_run(sid, "two")
    final = await settle(reopened, sid)
    assert final["status"] == "waiting_user"
    assert final["runs"][-1].get("resumed_from") == "orphan"


@pytest.mark.parametrize(
    "flag,event_type", [("/model-error", "model.error"), ("/tool-error", "tool.error")]
)
async def test_errors_recorded_and_explicit_retry(
    tmp_path: Path, flag: str, event_type: str
) -> None:
    runtime = manager(tmp_path)
    sid = (await runtime.create_session())["id"]
    await runtime.submit_message(sid, "one", f"acct_demo production req_demo_503 {flag}")
    assert (await settle(runtime, sid))["status"] == "failed"
    assert any(e["type"] == event_type for e in runtime.store.read(sid))
    await runtime.resume_run(sid, "retry")
    assert (await settle(runtime, sid))["status"] == "completed"


async def test_version_mismatch_rejected(tmp_path: Path) -> None:
    runtime = manager(tmp_path)
    sid = (await runtime.create_session())["id"]
    runtime.workflow.version = "2"
    with pytest.raises(Conflict, match="version"):
        await runtime.submit_message(sid, "one", "question")


async def test_storage_failure_disables_new_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = manager(tmp_path)
    sid = (await runtime.create_session())["id"]
    original = runtime.store.append

    def fail_on_calls(
        session_id: str,
        items: EventItems,
        run_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> list[Event]:
        if any(kind.startswith("model.") or kind == "run.failed" for kind, _ in items):
            raise OSError("disk unavailable")
        return original(session_id, items, run_id, call_id, parent_call_id)

    monkeypatch.setattr(runtime.store, "append", fail_on_calls)
    await runtime.submit_message(sid, "one", "question")
    await settle(runtime, sid)
    assert runtime.storage_error
    with pytest.raises(Conflict, match="unavailable"):
        await runtime.submit_message(sid, "two", "question")


async def test_shutdown_preserves_interrupted_status(tmp_path: Path) -> None:
    runtime = manager(tmp_path, delay=100)
    sid = (await runtime.create_session())["id"]
    await runtime.submit_message(sid, "one", "question")
    await asyncio.sleep(0.02)
    await runtime.shutdown()
    assert (await runtime.get_session(sid))["status"] == "interrupted"
    assert not runtime.tasks


async def test_generic_workflow_does_not_need_demo_tools(tmp_path: Path) -> None:
    class Echo:
        id: str = "echo"
        version: str = "1"
        card: StageCard = StageCard(label="Echo", description="contract test")

        async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
            await runtime.tracer.emit("custom.event", {"value": turn.user_message})
            return TurnResult("echo", {"opaque": ["arbitrary", 1]})

    runtime = manager(tmp_path, workflow=Echo())
    sid = (await runtime.create_session())["id"]
    await runtime.submit_message(sid, "one", "hello")
    final = await settle(runtime, sid)
    assert final["state"] == {"opaque": ["arbitrary", 1]}
    assert any(e["type"] == "custom.event" for e in runtime.store.read(sid))


async def test_submission_canceled_after_commit_remains_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    runtime = manager(tmp_path)
    sid = (await runtime.create_session())["id"]
    committed = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    append = runtime.store.append

    def delayed_append(
        session_id: str,
        items: EventItems,
        run_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> list[Event]:
        result = append(session_id, items, run_id, call_id, parent_call_id)
        if any(kind == "run.started" for kind, _ in items):
            loop.call_soon_threadsafe(committed.set)
            if not release.wait(5):
                raise TimeoutError("Test writer not released")
        return result

    monkeypatch.setattr(runtime.store, "append", delayed_append)
    submit = asyncio.create_task(runtime.submit_message(sid, "one", "question"))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        submit.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await submit
    assert (await runtime.get_session(sid))["status"] == "interrupted"
    assert not runtime.tasks
    await runtime.resume_run(sid, "continue")
    assert (await settle(runtime, sid))["status"] == "waiting_user"
