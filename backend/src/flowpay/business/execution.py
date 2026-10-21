"""单 Python 实例内的执行合并和取消；业务权威状态仍在 Java/PostgreSQL。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException

from flowpay.core.json import JsonObject


@dataclass
class Execution:
    """结果短期缓存，Java 重连同一 run 时不重复发起模型调用。"""

    digest: str
    task: asyncio.Task[JsonObject]
    created: float


class ExecutionRegistry:
    """取消墓碑先于执行到达也生效；进程重启不提供供应商计费 exactly-once。"""

    def __init__(self) -> None:
        self.executions: dict[str, Execution] = {}
        self.canceled: dict[str, float] = {}
        self.cancel_count = 0

    def prune(self) -> None:
        now = time.monotonic()
        self.executions = {
            key: value
            for key, value in self.executions.items()
            if not value.task.done() or now - value.created < 600
        }
        self.canceled = {key: at for key, at in self.canceled.items() if now - at < 600}

    async def run(
        self, run_id: str, digest: str, action: Callable[[], Awaitable[JsonObject]]
    ) -> JsonObject:
        """复用同一执行的结果；不同正文复用编号会明确冲突。"""
        self.prune()
        if run_id in self.canceled:
            raise HTTPException(409, "Execution canceled")
        execution = self.executions.get(run_id)
        if execution and execution.digest != digest:
            raise HTTPException(409, "Run ID reused with different input")
        if execution is None:
            if sum(not item.task.done() for item in self.executions.values()) >= 2:
                raise HTTPException(503, "Agent concurrency budget exceeded")
            if len(self.executions) >= 512:
                oldest = next(
                    (key for key, value in self.executions.items() if value.task.done()), None
                )
                if oldest is not None:
                    del self.executions[oldest]
            execution = Execution(digest, asyncio.create_task(action()), time.monotonic())
            self.executions[run_id] = execution
        try:
            return await asyncio.shield(execution.task)
        except asyncio.CancelledError as error:
            raise HTTPException(409, "Execution canceled") from error

    async def cancel(self, run_id: str) -> None:
        """中断 await 中的模型、工具协程；已交给线程的同步 I/O 只能等待其自身超时。"""
        self.prune()
        if len(self.canceled) >= 2048 and run_id not in self.canceled:
            raise HTTPException(503, "Cancellation tombstone budget exceeded")
        self.canceled[run_id] = time.monotonic()
        execution = self.executions.get(run_id)
        if execution and not execution.task.done():
            execution.task.cancel()
            self.cancel_count += 1
            await asyncio.gather(execution.task, return_exceptions=True)

    async def close(self) -> None:
        """先停止所有计算，再释放共享检索和模型连接。"""
        tasks = [item.task for item in self.executions.values() if not item.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
