"""契约验收剧本：追问后挂起、补充后作答，以及模型与工具的失败路径。

它不模拟业务诊断，只保证 HTTP/SSE 冒烟能复现地走到这些分支。
"""

from pathlib import Path

import pytest
from scripts.runtime_demo.workflow import DemoWorkflow

from flowpay.core.config import Settings
from flowpay.core.event_log import EventStore
from flowpay.core.execution.manager import SessionManager
from flowpay.core.json import require_list, require_object, require_text


async def play(manager: SessionManager, session: str, request: str, text: str) -> str:
    """提交一轮并等它结束，返回会话状态。"""
    await manager.submit_message(session, request, text)
    await manager.tasks[session]
    return require_text((await manager.get_session(session))["status"])


@pytest.fixture
def manager(tmp_path: Path) -> SessionManager:
    """隔离事件库与零延迟剧本；测试不依赖外部服务。"""
    settings = Settings(tmp_path, tmp_path, mock_delay_ms=0)
    return SessionManager(EventStore(tmp_path / "events.sqlite"), DemoWorkflow(settings), settings)


async def test_first_turn_pauses_and_second_turn_answers(manager: SessionManager) -> None:
    """剧本固定两轮：先追问挂起，补充之后作答。检查点记录已经问过。"""
    session = require_text((await manager.create_session())["id"])
    assert await play(manager, session, "one", "没有收到支付通知") == "waiting_user"
    assert await play(manager, session, "two", "补充信息") == "completed"
    view = await manager.get_session(session)
    assert len(view["messages"]) == 4
    assert require_object(view["state"])["resolved"] is True


async def test_tool_calls_carry_the_model_call_as_parent(manager: SessionManager) -> None:
    """工具事件必须挂在发起它的模型调用下，界面与评测据此还原调用树。"""
    session = require_text((await manager.create_session())["id"])
    await play(manager, session, "one", "没有收到支付通知")
    started = [e for e in manager.store.read(session) if e["type"] == "tool.started"]
    assert started and all(e["parent_call_id"] for e in started)
    assert [require_object(e["payload"])["name"] for e in started] == ["probe_rules"]


@pytest.mark.parametrize(
    "trigger, failing",
    [("/model-error", "model.error"), ("/tool-error", "tool.error")],
)
async def test_failure_triggers_are_recorded_and_can_be_continued(
    manager: SessionManager, trigger: str, failing: str
) -> None:
    """两个触发器让失败路径可复现；继续同一轮时不再失败，运行随之完成。"""
    session = require_text((await manager.create_session())["id"])
    assert await play(manager, session, "one", trigger) == "failed"
    assert any(e["type"] == failing for e in manager.store.read(session))
    await manager.resume_run(session, "retry")
    await manager.tasks[session]
    assert require_text((await manager.get_session(session))["status"]) == "waiting_user"
    assert require_list((await manager.get_session(session))["messages"])
