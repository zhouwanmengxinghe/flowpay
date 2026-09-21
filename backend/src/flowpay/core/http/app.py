# 提供会话 HTTP 接口与 SSE 事件订阅；只推送已持久化事件，重连按序号补齐。
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager
from typing import Annotated, ClassVar, cast

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from flowpay.core.chat.errors import SessionNotFound
from flowpay.core.chat.types import SessionSummary, SessionView
from flowpay.core.config import Settings
from flowpay.core.event_log.runtime import open_store
from flowpay.core.event_log.types import Event
from flowpay.core.execution.contracts import Workflow
from flowpay.core.execution.errors import Conflict
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.resources import RuntimeResources
from flowpay.core.execution.types import TurnRecord


class MessageBody(BaseModel):
    """发送消息 HTTP 接口的请求体；校验幂等 ID 与正文长度并拒绝额外字段。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=20000)


class ContinueBody(BaseModel):
    """继续运行 HTTP 接口的请求体；只接收新的幂等 ID，原问题从会话记录读取。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)


class DeleteSessionsBody(BaseModel):
    """批量删除请求；限制数量并拒绝空白 ID，避免无界批量操作。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    session_ids: list[Annotated[str, Field(min_length=1, pattern=r"\S")]] = Field(
        min_length=1, max_length=100
    )


class DeleteSessionsResult(BaseModel):
    """返回实际删除的会话 ID；不存在的会话不影响幂等重试。"""

    deleted_ids: list[str]


class WorkflowInfo(BaseModel):
    """前端展示的工作流身份和说明。"""

    id: str
    version: str
    label: str
    description: str


class ApplicationInfo(BaseModel):
    """页面初始化所需的协议版本、工作流与输入提示。"""

    protocol_version: int = 1
    workflow: WorkflowInfo
    model: str
    examples: list[str]
    reply_hint: str | None


# 应用生命周期负责存储与任务恢复，HTTP 连接仅负责提交和读取。
def create_app(settings: Settings, workflow: Workflow) -> FastAPI:
    """构造与业务工作流解耦的 HTTP 应用。

    启动时获取存储锁并恢复中断状态，关闭时先结束管理器任务再释放存储。
    消息接口仅受理任务并返回 202；SSE 单独订阅持久化事件，不承载模型执行。
    网页断开连接只停止订阅，用户取消必须走 cancel 接口。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        with ExitStack() as stack:
            store = stack.enter_context(open_store(settings))
            resources = RuntimeResources.from_settings(settings)
            stack.callback(resources.close)
            manager = SessionManager(store, workflow, settings, resources)
            app.state.manager = manager
            # 先取得独占运行锁，再补齐遗留运行的中断状态，之后才开始接受请求。
            await manager.recover()
            try:
                yield
            finally:
                await manager.shutdown()

    app = FastAPI(title="FlowPay Foundation", version="0.1.0", lifespan=lifespan)

    def get_manager() -> SessionManager:
        """生命周期已在 app.state 写入管理器；在框架的动态 State 边界恢复类型。"""
        return cast(SessionManager, app.state.manager)

    async def not_found(_: Request, _error: Exception) -> JSONResponse:
        return JSONResponse({"detail": "Session not found"}, status_code=404)

    async def conflict(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    async def health() -> JSONResponse:
        healthy = not get_manager().storage_error
        return JSONResponse(
            {"status": "ok" if healthy else "storage_error"}, status_code=200 if healthy else 503
        )

    async def meta() -> ApplicationInfo:
        """返回有明确响应模式的页面初始化信息。"""
        return ApplicationInfo(
            workflow=WorkflowInfo(
                id=workflow.id,
                version=workflow.version,
                label=workflow.card.label,
                description=workflow.card.description,
            ),
            model=settings.model.name,
            examples=workflow.card.examples,
            reply_hint=workflow.card.reply_hint,
        )

    async def sessions() -> list[SessionSummary]:
        return await get_manager().list_sessions()

    async def create_session() -> SessionView:
        return await get_manager().create_session()

    async def session(session_id: str) -> SessionView:
        return await get_manager().get_session(session_id)

    async def message(session_id: str, body: MessageBody) -> TurnRecord:
        content = body.content.strip()
        if not content:
            raise HTTPException(422, "Message cannot be blank")
        return await get_manager().submit_message(session_id, body.request_id, content)

    async def resume(session_id: str, body: ContinueBody) -> TurnRecord:
        return await get_manager().resume_run(session_id, body.request_id)

    async def cancel(session_id: str) -> SessionView:
        await get_manager().cancel_run(session_id)
        return await get_manager().get_session(session_id)

    async def delete_session(session_id: str) -> DeleteSessionsResult:
        if not session_id.strip():
            raise HTTPException(422, "Session ID cannot be blank")
        return DeleteSessionsResult(deleted_ids=await get_manager().delete_sessions([session_id]))

    async def delete_sessions(body: DeleteSessionsBody) -> DeleteSessionsResult:
        return DeleteSessionsResult(
            deleted_ids=await get_manager().delete_sessions(body.session_ids)
        )

    async def events(session_id: str, after: Annotated[int, Query(ge=0)] = 0) -> list[Event]:
        await get_manager().get_session(session_id)
        return await asyncio.to_thread(get_manager().store.read, session_id, after)

    async def export(session_id: str) -> Response:
        await get_manager().get_session(session_id)
        rows = await asyncio.to_thread(get_manager().store.read, session_id)
        return Response(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="{session_id}.jsonl"',
            },
        )

    async def stream(
        request: Request,
        session_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        await get_manager().get_session(session_id)
        try:
            # 首次连接用查询参数，EventSource 自动重连会带 Last-Event-ID。
            # 取较大值避免退回旧游标；客户端仍需去重以应对边界处的重复交付。
            cursor = max(after, int(last_event_id or "0"))
        except ValueError as error:
            raise HTTPException(400, "Invalid Last-Event-ID") from error

        async def generate() -> AsyncGenerator[str]:
            nonlocal cursor
            heartbeat = 0
            while not await request.is_disconnected():
                # 从存储读已提交事件，不直接订阅模型内存回调；页面看到的每条消息均可回放。
                # 读取与下一次轮询之间产生的事件会在后续按 seq 补齐。
                rows = await asyncio.to_thread(get_manager().store.read, session_id, cursor)
                if not rows and not await asyncio.to_thread(get_manager().store.read, session_id):
                    yield "event: session-deleted\ndata: {}\n\n"
                    return
                for event in rows:
                    cursor = event["seq"]
                    yield (
                        f"id: {cursor}\nevent: trace\ndata: "
                        f"{json.dumps(event, ensure_ascii=False)}\n\n"
                    )
                heartbeat += 1
                # SSE 注释心跳不消耗事件序号，也不写会话日志，仅用于维持空闲连接。
                if heartbeat % 40 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    app.exception_handler(SessionNotFound)(not_found)
    app.exception_handler(Conflict)(conflict)
    app.get("/api/health")(health)
    app.get("/api/meta")(meta)
    app.get("/api/sessions")(sessions)
    app.post("/api/sessions", status_code=201)(create_session)
    app.post("/api/sessions/batch-delete")(delete_sessions)
    app.delete("/api/sessions/{session_id}")(delete_session)
    app.get("/api/sessions/{session_id}")(session)
    app.post("/api/sessions/{session_id}/messages", status_code=202)(message)
    app.post("/api/sessions/{session_id}/continue", status_code=202)(resume)
    app.post("/api/sessions/{session_id}/cancel")(cancel)
    app.get("/api/sessions/{session_id}/events")(events)
    app.get("/api/sessions/{session_id}/export")(export)
    app.get("/api/sessions/{session_id}/stream")(stream)

    return app
