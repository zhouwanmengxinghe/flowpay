# 从有序事件重建会话视图；投影只读取历史，不重新执行模型或工具。
from __future__ import annotations

from pydantic import TypeAdapter

from flowpay.core.chat.errors import SessionNotFound
from flowpay.core.chat.types import ChatMessage, SessionView
from flowpay.core.event_log.types import Event
from flowpay.core.execution.types import (
    TurnRecord,
    TurnResultStatus,
    TurnStarted,
    TurnStatus,
    WorkflowIdentity,
)
from flowpay.core.json import require_object, require_text


def build_session_view(events: list[Event]) -> SessionView:
    """按会话序号递增的事件列表重建消息、运行和最近提交状态。

    输入应从 session.created 开始且包含完整历史；空列表表示会话不存在。
    模型片段、工具结果等调用事件留在轨迹中，不自动成为聊天历史。
    只有 run.finished 更新 state；失败回合不会把半成品状态带入下一次继续。
    """
    if not events:
        raise SessionNotFound("Session not found")
    first = events[0]
    result: SessionView = {
        "id": first["session_id"],
        "title": "新会话",
        "status": "idle",
        "created_at": first["timestamp"],
        "updated_at": events[-1]["timestamp"],
        "last_seq": events[-1]["seq"],
        "workflow": TypeAdapter(WorkflowIdentity).validate_python(first["payload"]["workflow"]),
        "state": {},
        "messages": [],
        "runs": [],
    }
    # 这是对已提交事实的顺序折叠，不根据模型文本猜测状态。
    # 未识别的扩展事件不改变会话摘要，但原始日志仍完整保存它们。
    for event in events:
        payload = event["payload"]
        kind = event["type"]
        if kind == "message.user":
            if not result["messages"]:
                result["title"] = require_text(payload["content"])[:36]
            result["messages"].append(
                ChatMessage(
                    role="user",
                    content=require_text(payload["content"]),
                    run_id=event["run_id"],
                    seq=event["seq"],
                )
            )
        elif kind == "run.started":
            result["status"] = "running"
            if event["run_id"] is None:
                raise ValueError("run.started is missing run_id")
            started = TypeAdapter(TurnStarted).validate_python(payload, strict=True)
            run: TurnRecord = {
                **started,
                "id": event["run_id"],
                "status": "running",
                "started_at": event["timestamp"],
            }
            result["runs"].append(run)
        elif kind == "message.assistant":
            result["messages"].append(
                ChatMessage(
                    role="assistant",
                    content=require_text(payload["content"]),
                    run_id=event["run_id"],
                    seq=event["seq"],
                    evidence=TypeAdapter(list[dict[str, object]]).validate_python(
                        payload.get("evidence", []), strict=True
                    ),
                )
            )
        # 检查点只在成功结束（含等待补充）时前进；错误、取消和中断只更新状态。
        # 因此显式继续会从最后一次成功检查点重跑，而不是从半条工具结果恢复。
        elif kind == "run.finished":
            result["state"] = require_object(payload["state"])
            result["status"] = TypeAdapter(TurnResultStatus).validate_python(payload["status"])
            result["runs"][-1]["status"] = result["status"]
            result["runs"][-1]["ended_at"] = event["timestamp"]
        elif kind in {"run.failed", "run.canceled", "run.interrupted"}:
            terminals: dict[str, TurnStatus] = {
                "run.failed": "failed",
                "run.canceled": "canceled",
                "run.interrupted": "interrupted",
            }
            result["status"] = terminals[kind]
            result["runs"][-1].update(
                status=result["status"],
                ended_at=event["timestamp"],
                error=payload,
            )
    return result
