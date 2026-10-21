# 管理会话和后台任务；恢复依赖持久化事件，页面连接不决定任务生命周期。
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass

from flowpay.core.agent.trace import Tracer
from flowpay.core.chat.session_view import build_session_view
from flowpay.core.chat.types import SessionSummary, SessionView
from flowpay.core.config import Settings
from flowpay.core.event_log import EventStoreProtocol
from flowpay.core.event_log.contracts import EventItems, RuntimeLockCheck
from flowpay.core.event_log.store import new_id
from flowpay.core.execution.contracts import TurnInput, Workflow
from flowpay.core.execution.errors import Conflict
from flowpay.core.execution.resources import RuntimeResources
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.types import TurnRecord, TurnStarted, WorkflowIdentity
from flowpay.core.json import JsonObject, to_json_object

logger = logging.getLogger(__name__)


@dataclass
class PreparedRun:
    """已通过校验、尚未提交的运行；事件输入与工作流输入各有明确用途。"""

    run_id: str
    started_event: TurnStarted
    workflow_input: TurnInput


class SessionManager:
    """协调持久化会话与内存中的异步任务。

    store 是历史事实来源；tasks 只表示本进程正在执行什么，不能用于恢复历史。
    locks 串行化同会话的提交与取消，不阻止不同会话并行运行。
    单后端约束由存储运行锁提供，本类不是分布式任务调度器。
    """

    def __init__(
        self,
        store: EventStoreProtocol,
        workflow: Workflow,
        settings: Settings,
        resources: RuntimeResources | None = None,
    ):
        """接收存储、工作流和配置。

        未提供 resources 时由本管理器创建并在 shutdown 关闭；注入资源时由
        调用方管理关闭。构造本身不执行历史恢复，启动后显式调用 recover。
        """
        self.store: EventStoreProtocol = store
        self.workflow: Workflow = workflow
        self.settings: Settings = settings
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.shutting_down: bool = False
        self.storage_error: bool = False
        self._owns_resources: bool = resources is None
        self.resources: RuntimeResources = (
            resources if resources is not None else RuntimeResources.from_settings(settings)
        )

    @property
    def identity(self) -> WorkflowIdentity:
        """返回当前工作流 ID 与版本，用于创建会话以及阻止不兼容的历史继续运行。"""
        return {"id": self.workflow.id, "version": self.workflow.version}

    # 重启只把未结束的运行标记为中断；是否重新执行由用户显式决定。
    async def recover(self):
        """启动时把历史中没有结束事件的运行追加标记为 interrupted。

        不修改原 run.started，也不自动重发请求：进程退出前外部请求可能已经成功，
        只是结果尚未来得及写入。用户显式继续后才会重新执行该只读回合。
        """
        for session_id in await asyncio.to_thread(self.store.session_ids):
            session = await self.get_session(session_id)
            if session["status"] == "running":
                await self.store.aappend(
                    session_id,
                    [
                        (
                            "run.interrupted",
                            {"reason": "backend_restart", "retry_policy": "explicit"},
                        )
                    ],
                    session["runs"][-1]["id"],
                )

    async def create_session(self) -> SessionView:
        """持久化会话创建事件并返回初始快照；此时尚未执行工作流。"""
        session_id = new_id("session")
        await self.store.aappend(
            session_id,
            [
                (
                    "session.created",
                    {"workflow": self.identity},
                )
            ],
        )
        return await self.get_session(session_id)

    async def get_session(self, session_id: str) -> SessionView:
        """读取事件并重建指定会话快照；不存在的会话由投影层抛 SessionNotFound。"""
        return build_session_view(await asyncio.to_thread(self.store.read, session_id))

    async def list_sessions(self) -> list[SessionSummary]:
        """返回可供会话列表显示的摘要，不读取整段模型请求或回答正文。"""
        return await asyncio.to_thread(self.store.session_summaries)

    async def submit_message(self, session_id: str, request_id: str, content: str) -> TurnRecord:
        """提交一条用户消息；同一 request_id 与内容重试时返回原运行。"""
        return await self._submit_run(session_id, request_id, content, resume=False)

    async def resume_run(self, session_id: str, request_id: str) -> TurnRecord:
        """从上次已提交的检查点重跑中断输入，不重复追加用户消息。"""
        return await self._submit_run(session_id, request_id, None, resume=True)

    async def _submit_run(
        self, session_id: str, request_id: str, content: str | None, *, resume: bool
    ) -> TurnRecord:
        # 整段提交共用会话锁。任务登记完成前，其他提交或取消不能进入。
        """在会话锁内检查重复请求、准备并提交 Run，再登记后台任务，返回受理记录。"""
        async with self.locks[session_id]:
            await self._ensure_runtime_available()
            session = await self.get_session(session_id)
            previous_run = self._find_duplicate(session, request_id, content, resume=resume)
            if previous_run is not None:
                return previous_run

            input_message = self._resolve_run_input(session, content, resume=resume)
            prepared = await self._prepare_run(session, request_id, input_message, resume=resume)
            receipt = await self._persist_run(session_id, prepared)
            # 此处到登记之间没有 await，取消不会留下已提交却没有执行者的运行。
            self.tasks[session_id] = asyncio.create_task(
                self._execute(session_id, prepared.run_id, prepared.workflow_input)
            )
            return receipt

    async def _ensure_runtime_available(self) -> None:
        """检查存储锁连接和管理器状态；关闭或存储异常时抛 Conflict 拒绝新任务。"""
        if isinstance(self.store, RuntimeLockCheck):
            await asyncio.to_thread(self.store.check_lock)
        if self.shutting_down or self.storage_error:
            raise Conflict("Runtime unavailable; inspect server logs")

    @staticmethod
    def _find_duplicate(
        session: SessionView, request_id: str, content: str | None, *, resume: bool
    ) -> TurnRecord | None:
        # 幂等检查必须先于 running 检查，客户端才能取回已受理的原 Run。
        """查找相同 request_id 的原运行；内容或继续标记冲突时拒绝，未找到返回 None。"""
        for run in session["runs"]:
            if run["request_id"] == request_id:
                if run["resumed"] != resume or (not resume and run["input"] != content):
                    raise Conflict("request_id reused with different content")
                return run
        return None

    def _resolve_run_input(self, session: SessionView, content: str | None, *, resume: bool) -> str:
        """检查会话版本与当前状态，返回新输入或待重跑原问题；非法状态抛 Conflict。"""
        if session["workflow"] != self.identity:
            raise Conflict("Workflow version changed; create a new session")
        if session["status"] == "running" or session["id"] in self.tasks:
            raise Conflict("This session already has an active run")
        can_resume = session["status"] in {"interrupted", "canceled", "failed"}
        if resume:
            if not can_resume:
                raise Conflict("Only interrupted, canceled or failed runs can be continued")
            return session["runs"][-1]["input"]
        if content is None:
            raise ValueError("Run input is missing")
        return content

    async def _prepare_run(
        self, session: SessionView, request_id: str, input_message: str, *, resume: bool
    ) -> PreparedRun:
        """核对并记录数据版本，分别构造待写入事件和工作流输入；尚不提交或启动任务。"""
        previous_config = session["runs"][-1].get("config", {}) if session["runs"] else None
        config_snapshot = await self.resources.snapshot(previous_config)
        if self.shutting_down:
            raise Conflict("Runtime is shutting down")
        started_event: TurnStarted = {
            "request_id": request_id,
            "input": input_message,
            "resumed": resume,
            "resumed_from": session["runs"][-1]["id"] if resume else None,
            "config": config_snapshot,
            "workflow": self.identity,
        }
        return PreparedRun(
            run_id=new_id("run"),
            started_event=started_event,
            workflow_input=TurnInput(
                user_message=input_message,
                chat_history=session["messages"],
                saved_state=session["state"],
                resumed=resume,
            ),
        )

    async def _persist_run(self, session_id: str, prepared: PreparedRun) -> TurnRecord:
        """原子提交用户消息与开始事件；继续运行不重复用户消息，提交期间取消需补写中断。"""
        payload = prepared.started_event
        events: EventItems = []
        if not payload["resumed"]:
            events.append(("message.user", {"content": payload["input"]}))
        events.append(("run.started", dict(payload)))
        try:
            committed = await self.store.aappend(session_id, events, prepared.run_id)
        except asyncio.CancelledError:
            # 异步写入会等事务结束再传播取消；此时可能已有 Run，但还没有后台任务。
            await self._record_terminal_event(
                Tracer(self.store, session_id, prepared.run_id),
                "run.interrupted",
                {"reason": "submission_canceled", "retry_policy": "explicit"},
            )
            raise
        return TurnRecord(
            id=prepared.run_id,
            request_id=payload["request_id"],
            input=payload["input"],
            resumed=payload["resumed"],
            resumed_from=payload["resumed_from"],
            config=payload["config"],
            workflow=payload["workflow"],
            status="running",
            started_at=committed[-1]["timestamp"],
        )

    # 回答和阶段状态在同批事件中提交，防止出现有回答却没有对应状态的历史。
    async def _execute(self, session_id: str, run_id: str, value: TurnInput):
        """在后台执行工作流，将其结果转为原子提交的会话事件。

        只有 completed / waiting_user 可作为成功结果；失败不会覆盖上一轮已提交状态。
        关闭服务和用户取消使用不同终态，finally 始终清理内存任务登记。
        即使页面已关闭，此任务也会继续，直到完成、取消或服务退出。
        """
        tracer = Tracer(self.store, session_id, run_id)
        runtime = AgentRuntime(
            tracer,
            self.resources.source_client,
            self.resources.document_retriever(tracer),
            self.settings.knowledge_collection_id,
        )
        try:
            outcome = await self.workflow.run(value, runtime)
            if outcome.status not in {"completed", "waiting_user"}:
                raise ValueError("Invalid workflow outcome")
            await self.store.aappend(
                session_id,
                [
                    (
                        "message.assistant",
                        {
                            "content": outcome.text,
                            "evidence": [to_json_object(p) for p in outcome.evidence],
                        },
                    ),
                    ("run.finished", {"status": outcome.status, "state": outcome.saved_state}),
                ],
                run_id,
            )
        except asyncio.CancelledError:
            await self._record_terminal_event(
                tracer,
                "run.interrupted" if self.shutting_down else "run.canceled",
                {"reason": "shutdown" if self.shutting_down else "user_requested"},
            )
        except Exception as error:
            logger.exception("Run failed: %s", run_id)
            await self._record_terminal_event(
                tracer,
                "run.failed",
                {
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
            )
        finally:
            self.tasks.pop(session_id, None)

    # 终态持久化失败时关闭新运行入口，不能把存储故障伪装成正常完成。
    async def _record_terminal_event(self, tracer: Tracer, kind: str, payload: JsonObject):
        """尝试给仍在运行的目标 Run 写终态，避免重复结束或结束错轮次。

        若存储本身不可用，终态可能无法写入，此时打开 storage_error 标记，
        后续 start 拒绝新请求，health 返回异常。内存标记不能替代数据库中的事件。
        """
        try:
            session = await self.get_session(tracer.session_id)
            if session["runs"][-1]["id"] != tracer.run_id or session["status"] != "running":
                return
            await tracer.emit(kind, payload)
        except Exception:
            self.storage_error = True
            logger.exception("Event persistence failed; new runs disabled")

    async def cancel_run(self, session_id: str):
        """取消当前协程并等待退出，再核对是否仍需补写取消事件。

        等待退出可避免取消响应先返回、旧任务随后又提交成功的竞态。
        取消外部请求不保证远端未处理，因此课程工具只允许只读操作。
        """
        async with self.locks[session_id]:
            await self.get_session(session_id)
            if task := self.tasks.get(session_id):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.tasks.pop(session_id, None)
                session = await self.get_session(session_id)
                await self._record_terminal_event(
                    Tracer(self.store, session_id, session["runs"][-1]["id"]),
                    "run.canceled",
                    {"reason": "user_requested"},
                )

    # 取消后台任务并补齐中断事件，供下次启动后的显式继续使用。
    async def shutdown(self):
        """停止受理并取消、等待当前任务；最后关闭本管理器创建的资源，不关闭外部注入资源。"""
        self.shutting_down = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session_id in await asyncio.to_thread(self.store.session_ids):
            session = await self.get_session(session_id)
            if session["status"] == "running":
                await self._record_terminal_event(
                    Tracer(self.store, session_id, session["runs"][-1]["id"]),
                    "run.interrupted",
                    {"reason": "shutdown"},
                )
        self.tasks.clear()
        if self._owns_resources:
            self.resources.close()

    async def delete_sessions(self, session_ids: list[str]) -> list[str]:
        """按 ID 排序加锁后整批删除；运行中拒绝，重复删除安全，取消等待存储事务结束。"""
        ids = sorted(set(session_ids))
        if not 1 <= len(ids) <= 100 or any(not sid.strip() for sid in ids):
            raise ValueError("Expected 1..100 nonblank session IDs")
        async with AsyncExitStack() as stack:
            for sid in ids:
                await stack.enter_async_context(self.locks[sid])
            await self._ensure_runtime_available()
            for sid in ids:
                events = await asyncio.to_thread(self.store.read, sid)
                if sid in self.tasks or (
                    events and build_session_view(events)["status"] == "running"
                ):
                    raise Conflict("Stop running sessions before deleting; nothing was deleted")
            # 取消 HTTP 请求不会中止数据库线程；保持锁直到整批事务完成。
            task = asyncio.create_task(asyncio.to_thread(self.store.delete_sessions, ids))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
