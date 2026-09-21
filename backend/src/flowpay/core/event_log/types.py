"""持久化事件的记录结构与读取校验。"""

from typing import cast

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from flowpay.core.json import JsonObject


class Event(TypedDict):
    """只追加会话事件的完整记录；seq 用于重放，call_id 与 parent_call_id 关联执行过程。"""

    schema_version: int
    event_id: str
    session_id: str
    seq: int
    run_id: str | None
    call_id: str | None
    parent_call_id: str | None
    timestamp: str
    type: str
    payload: JsonObject


_EVENT_ADAPTER = TypeAdapter(Event)


def require_event(value: object) -> Event:
    """验证事件协议并保留原对象，包括历史记录中的额外顶层字段。"""
    _EVENT_ADAPTER.validate_python(value, strict=True)
    return cast(Event, value)
