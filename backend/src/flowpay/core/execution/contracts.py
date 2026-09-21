"""教学工作流与基座之间的契约。

一"轮"（turn）是一次用户提交到一次回答提交，中间可以有多次模型请求、多次工具调用，
也可以一次模型都不调。因此这里的 TurnInput / TurnResult 与 Chat Completions 的
请求/响应不是一回事：它们额外承载恢复标记、阶段检查点和引用片段，这些都不发给模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from flowpay.core.chat.types import ChatMessage
from flowpay.core.execution.stage import StageCard
from flowpay.core.execution.types import TurnResultStatus
from flowpay.core.json import JsonObject
from flowpay.core.knowledge.contracts import Passage

if TYPE_CHECKING:
    from .runtime import AgentRuntime


@dataclass
class TurnResult:
    """一轮工作流的提交结果，由 SessionManager 落库。

    管理器把回答与 run.finished 原子提交；本轮失败或取消时 saved_state 保持上一轮的值。

    Attributes:
        text: 向用户展示的最终回答。
        saved_state: 下一轮的 JSON 检查点，内容由阶段决定。
        status: completed 表示回答完成；waiting_user 表示提交后等待新的用户输入，
            本轮就此结束，不会挂起协程。
        evidence: 交给界面展开的片段，落库时由管理器序列化。
    """

    text: str
    saved_state: JsonObject
    status: TurnResultStatus = "completed"
    evidence: list[Passage] = field(default_factory=list)


@dataclass
class TurnInput:
    """管理器交给工作流的一轮输入。

    Attributes:
        user_message: 当前用户问题；继续中断的一轮时是原问题。
        chat_history: 事件投影出的聊天历史；新一轮里不含当前 user_message，
            继续中断的一轮里可能已含它，用 resumed 区分。
        saved_state: 最近一次成功提交的阶段检查点。
        resumed: 本轮是否在重跑失败或中断的输入。
    """

    user_message: str
    chat_history: list[ChatMessage]
    saved_state: JsonObject
    resumed: bool = False


class Workflow(Protocol):
    """一个课程阶段的执行契约，供 SessionManager 调用。

    id 与 version 决定哪些历史会话可以继续；版本变更后旧会话不再进入当前实现。
    """

    id: str
    version: str
    card: StageCard

    async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
        """处理本轮输入并返回待提交结果；异常和取消向上传播，由管理器记录终态。"""
        ...
