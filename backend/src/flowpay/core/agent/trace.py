"""把一轮运行里的调用写进事件日志。"""

from flowpay.core.event_log.contracts import EventStoreProtocol
from flowpay.core.event_log.types import Event
from flowpay.core.json import JsonObject


class Tracer:
    """绑定会话与运行的事件出口，是能力客户端记录调用轨迹的唯一入口。

    调用方只给事件类型和载荷，session_id、run_id 由本对象补齐。call_id 关联同一次
    调用的开始、片段与结果；parent_call_id 指出工具由哪次模型响应发起。
    """

    def __init__(self, store: EventStoreProtocol, session_id: str, run_id: str) -> None:
        """绑定事件存储、会话与运行；每轮新建一个，不跨轮复用。"""
        self.store: EventStoreProtocol = store
        self.session_id: str = session_id
        self.run_id: str = run_id

    async def emit(
        self,
        kind: str,
        payload: JsonObject,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> Event:
        """追加一条事件，持久化成功后返回该记录。"""
        events = await self.store.aappend(
            self.session_id,
            [(kind, payload)],
            self.run_id,
            call_id=call_id,
            parent_call_id=parent_call_id,
        )
        return events[0]
