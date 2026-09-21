"""一轮运行的状态、记录与工作流身份。"""

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from flowpay.core.json import JsonObject

TurnResultStatus = Literal["completed", "waiting_user"]
TurnStatus = Literal["running", "completed", "waiting_user", "failed", "canceled", "interrupted"]


class WorkflowIdentity(TypedDict):
    """保存在会话中的工作流 ID 和版本，用于判断历史状态能否由当前代码继续解释。"""

    id: str
    version: str


class TurnStarted(TypedDict):
    """新运行事件的固定载荷；不把事件字段藏在可扩展 JSON 中。"""

    request_id: str
    input: str
    resumed: bool
    resumed_from: str | None
    config: JsonObject
    workflow: WorkflowIdentity


class TurnRecord(TypedDict):
    """一次已受理运行的状态记录，包含输入、请求幂等 ID、开始时间与可选终态信息。"""

    id: str
    request_id: str
    input: str
    resumed: bool
    status: TurnStatus
    started_at: str
    resumed_from: str | None
    config: JsonObject
    workflow: WorkflowIdentity
    ended_at: NotRequired[str]
    error: NotRequired[JsonObject]
