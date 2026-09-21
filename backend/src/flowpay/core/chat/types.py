"""前端会话列表、详情和聊天消息的数据结构。"""

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from flowpay.core.execution.types import TurnRecord, WorkflowIdentity
from flowpay.core.json import JsonObject

SessionStatus = Literal[
    "idle", "running", "completed", "waiting_user", "failed", "canceled", "interrupted"
]


class ChatMessage(TypedDict):
    """界面聊天历史的投影，额外携带 run_id、seq 和引用。

    它不是模型请求消息；发送到模型的 messages 使用 SDK 的 ChatCompletionMessageParam。
    """

    role: Literal["user", "assistant"]
    content: str
    run_id: str | None
    seq: int
    evidence: NotRequired[list[JsonObject]]


class SessionSummary(TypedDict):
    """会话目录的简要投影，供列表页显示标题、状态和更新时间。"""

    id: str
    title: str
    status: SessionStatus
    updated_at: str
    last_seq: int
    workflow: WorkflowIdentity


class SessionView(SessionSummary):
    """由事件重建的会话完整视图，包含历史消息、各轮运行和最后提交的阶段状态。"""

    created_at: str
    state: JsonObject
    messages: list[ChatMessage]
    runs: list[TurnRecord]
