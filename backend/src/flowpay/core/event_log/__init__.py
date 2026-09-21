# 统一导出事件存储接口，供运行器与验收脚本使用。
from .contracts import EventStoreProtocol
from .store import EventStore

__all__ = ["EventStore", "EventStoreProtocol"]
