"""事件存储的结构化协议，以及同步存储共用的异步写入实现。

调用方只依赖 EventStoreProtocol，因此 SQLite 与 PostgreSQL 可以互换：
建连、运行锁和关闭由装配层负责，不进入协议。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from flowpay.core.chat.types import SessionSummary
from flowpay.core.event_log.types import Event
from flowpay.core.json import JsonObject

EventItems = list[tuple[str, JsonObject]]


class EventStoreProtocol(Protocol):
    """会话层需要的存储能力。

    append/aappend 整批提交，返回值只包含已提交事件；read 按 seq 升序。
    """

    def append(
        self,
        session_id: str,
        items: EventItems,
        run_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> list[Event]:
        """原子追加同一会话的一批事件，返回已提交事件；序号按会话单调递增。"""
        ...

    async def aappend(
        self,
        session_id: str,
        items: EventItems,
        run_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> list[Event]:
        """异步追加整批事件；取消须等待底层写入结束，避免终态记录越过未完成写入。"""
        ...

    def read(self, session_id: str, after: int = 0) -> list[Event]:
        """按 seq 升序读取指定会话中序号大于 after 的已提交事件。"""
        ...

    def delete_sessions(self, session_ids: list[str]) -> list[str]:
        """原子删除指定会话及其运行、事件；忽略不存在的 ID，返回实际删除的 ID。"""
        ...

    def session_ids(self) -> list[str]:
        """列出已保存会话的 ID，供启动恢复逐一检查未结束运行。"""
        ...

    def session_summaries(self) -> list[SessionSummary]:
        """返回会话目录需要的摘要，避免调用方为列表页遍历全部消息正文。"""
        ...


@runtime_checkable
class RuntimeLockCheck(Protocol):
    """可选的运行锁连接检查，当前由 PostgreSQL 实现。

    会话层通过结构化运行时判断收窄类型。SQLite 的文件锁由 open_store 持有，
    不需要为满足共同协议而增加一个无意义的空检查方法。
    """

    def check_lock(self) -> None:
        """检查持有运行锁的连接是否仍可用；错误须阻止新运行继续受理。"""
        ...


class AsyncEventWriter(ABC):
    """为同步数据库实现提供统一的异步追加入口。

    子类必须实现 append；不从某个具体数据库类借用绑定方法，避免 self 的
    类型暗中依赖 SQLite。此基类不替代完整的 EventStoreProtocol。
    """

    @abstractmethod
    def append(
        self,
        session_id: str,
        items: EventItems,
        run_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> list[Event]:
        """由子类实现同步原子追加；aappend 在线程中调用此方法并维护取消顺序。"""
        ...

    async def aappend(
        self,
        session_id: str,
        items: EventItems,
        run_id: str | None = None,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> list[Event]:
        """异步追加整批事件；取消时先等写入结束，再向上传播。

        取消 asyncio 任务不会停止已经在跑的数据库线程，所以用 shield 保护写入任务，
        捕获取消后继续等待它结束，维持“当前批次写完，再由管理器记录终态”的顺序。
        """
        task = asyncio.create_task(
            asyncio.to_thread(
                self.append,
                session_id,
                items,
                run_id,
                call_id,
                parent_call_id,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


class EventSink(Protocol):
    """能力客户端需要的日志接口；无需依赖整个会话运行上下文。"""

    async def emit(
        self,
        kind: str,
        payload: JsonObject,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> Event:
        """追加一项带调用关联信息的事件并返回已提交记录；不得在持久化前返回成功。"""
        ...
