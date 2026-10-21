"""无独立会话库的执行服务；授权范围由 Java 提供，查询边界在此再次强制校验。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal

from fastapi import FastAPI, Header, HTTPException
from openai import APIStatusError
from anthropic import APIStatusError as AnthropicStatusError
from pydantic import BaseModel, ConfigDict, Field

from flowpay.bootstrap import create_workflow
from flowpay.business.execution import ExecutionRegistry
from flowpay.core.cache import RedisJsonCache
from flowpay.core.chat.types import ChatMessage
from flowpay.core.config import load_settings
from flowpay.core.data_sources.reader import SourceClient, SourceResult
from flowpay.core.event_log.contracts import EventSink
from flowpay.core.event_log.types import Event
from flowpay.core.execution.contracts import TurnInput
from flowpay.core.execution.resources import RuntimeResources
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.json import JsonObject, to_json_object
from flowpay.core.knowledge.cached_retriever import CachedDocumentRetriever
from flowpay.core.tools import ToolInputError


class HistoryMessage(BaseModel):
    """跨服务只传正文，事件序号由业务后台管理。"""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=50000)


class ExecuteBody(BaseModel):
    """仅内部调用者可以提交检查点；公网不能直接访问这个接口。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    execution_id: str | None = Field(default=None, min_length=1, max_length=64)
    claim_version: int = Field(default=0, ge=0)
    account_ids: list[str] = Field(min_length=1, max_length=1)
    environment: Literal["production", "sandbox"]
    content: str = Field(min_length=1, max_length=20000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=200)
    saved_state: JsonObject = Field(default_factory=dict)


class EvidenceSink:
    """只收集有用证据；不把原始 token 帧和向量塞进跨服务响应。"""

    def __init__(self) -> None:
        self.evidence: list[JsonObject] = []
        self.size = 0
        self.metrics: dict[str, int] = {
            "model_calls": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    async def emit(
        self,
        kind: str,
        payload: JsonObject,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> Event:
        """记录有界证据，保持 EventSink 接口供现有能力复用。"""
        if kind == "tool.started":
            self.metrics["tool_calls"] += 1
        if kind == "model.request":
            self.metrics["model_calls"] += 1
        if kind == "model.response":
            usage = payload.get("completion", {}).get("usage") or {}
            self.metrics["input_tokens"] += int(usage.get("prompt_tokens", 0))
            self.metrics["output_tokens"] += int(usage.get("completion_tokens", 0))
        if kind == "model.summary":
            for name in ("model_calls", "input_tokens", "output_tokens"):
                self.metrics[name] += int(payload.get(name, 0))
        if kind in {"knowledge.response", "document.response", "source.response"}:
            self.size += len(json.dumps(payload, ensure_ascii=False).encode())
            if self.size > 2_000_000:
                raise RuntimeError("Evidence budget exceeded")
            self.evidence.append({"kind": kind, "data": payload})
        return {
            "schema_version": 1,
            "event_id": "internal",
            "session_id": "internal",
            "seq": 0,
            "run_id": None,
            "call_id": call_id,
            "parent_call_id": parent_call_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "type": kind,
            "payload": payload,
        }


class AuthorizedSources(SourceClient):
    """限制固定账户与环境，投递日志只能使用已在授权查询中返回的投递编号。"""

    def __init__(self, base: SourceClient, body: ExecuteBody) -> None:
        super().__init__(base.sources)
        self.accounts = set(body.account_ids)
        self.environment = body.environment
        # 此状态由 Java 从已提交检查点提供，不接受浏览器提交的状态。
        restored = body.saved_state.get("authorized_deliveries", [])
        self.deliveries = (
            {x for x in restored if isinstance(x, str)} if isinstance(restored, list) else set()
        )

    def check_scope(self, scope: JsonObject) -> None:
        """模型给出的账户不是授权凭据，必须匹配当前执行的范围。"""
        if (
            scope.get("account_id") not in self.accounts
            or scope.get("environment") != self.environment
        ):
            raise ToolInputError("查询超出当前已授权账户或环境，请使用当前调查的范围。")

    async def query_sql(
        self,
        source: str,
        scope: JsonObject,
        context: EventSink,
        *,
        filters: JsonObject | None = None,
        limit: int = 50,
    ) -> SourceResult:
        """先授权再读取，只登记授权查询实际返回的投递编号。"""
        self.check_scope(scope)
        result = await super().query_sql(source, scope, context, filters=filters, limit=limit)
        if source == "webhook_deliveries":
            self.deliveries.update(
                str(row["delivery_id"]) for row in result["rows"] if "delivery_id" in row
            )
        return result

    async def query_logs(
        self,
        source: str,
        context: EventSink,
        *,
        filters: JsonObject | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
        contains: str | None = None,
    ) -> SourceResult:
        """业务请求强制范围；平台事故是公开资料；未知日志默认拒绝。"""
        chosen = dict(filters or {})
        if source == "api_requests":
            self.check_scope(chosen)
        elif source == "delivery_attempts":
            if chosen.get("delivery_id") not in self.deliveries:
                raise ToolInputError("请先在当前授权范围内查询投递记录，再查询其尝试日志。")
        elif source != "service_incidents":
            raise ToolInputError("此日志未向业务 Agent 开放。")
        return await super().query_logs(
            source, context, filters=chosen, start=start, end=end, limit=limit, contains=contains
        )


def app_factory() -> FastAPI:
    """工厂默认真实模式；模拟验收必须显式启用，不请求外部模型。"""
    secret = os.environ.get("SUPPORT_AGENT_KEY", "")
    if len(secret) < 24:
        raise ValueError("SUPPORT_AGENT_KEY must contain at least 24 characters")
    mode = os.environ.get("SUPPORT_AGENT_MODE", "real")
    if mode not in {"real", "mock"}:
        raise ValueError("Unsupported Agent mode")
    settings = replace(load_settings(), workflow="s4") if mode == "real" else None
    resources = RuntimeResources.from_settings(settings) if settings else None
    cache_enabled = os.environ.get("SUPPORT_RETRIEVAL_CACHE_ENABLED", "false").lower() == "true"
    cache_url = os.environ.get("SUPPORT_CACHE_REDIS_URL", "")
    if cache_enabled and (not resources or not cache_url):
        raise ValueError("Retrieval cache requires real resources and SUPPORT_CACHE_REDIS_URL")
    retrieval_cache = RedisJsonCache(cache_url) if cache_enabled else None
    cache_rebuilds = asyncio.Semaphore(2)
    engine = os.environ.get("SUPPORT_AGENT_ENGINE", "s4")
    if engine not in {"s4", "langchain"}:
        raise ValueError("Unsupported Agent engine")
    if settings and engine == "langchain":
        from flowpay.business.bootstrap import create_langchain_workflow

        workflow = create_langchain_workflow(settings)
    else:
        workflow = create_workflow(settings) if settings else None
        if workflow is not None and hasattr(workflow, "max_model_calls"):
            workflow.max_model_calls = 6
    registry = ExecutionRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await registry.close()
        if retrieval_cache:
            await retrieval_cache.close()
        if resources:
            resources.close()

    app = FastAPI(title="FlowPay internal execution", lifespan=lifespan)

    @app.get("/health")
    async def health() -> JsonObject:
        result: JsonObject = {
            "status": "ok",
            "mode": mode,
            "engine": os.environ.get("SUPPORT_AGENT_ENGINE", "s4"),
            "active": sum(not item.task.done() for item in registry.executions.values()),
            "canceled": registry.cancel_count,
        }
        if retrieval_cache:
            result["cache"] = retrieval_cache.metrics.__dict__
        return result

    @app.post("/internal/runs/{run_id}/cancel")
    async def cancel(run_id: str, x_agent_key: Annotated[str, Header()]) -> JsonObject:
        if not hmac.compare_digest(x_agent_key, secret):
            raise HTTPException(401, "Invalid internal credential")
        if len(run_id) > 64:
            raise HTTPException(400, "Invalid run ID")
        await registry.cancel(run_id)
        return {"canceled": True}

    @app.post("/internal/executions/{execution_id}/cancel")
    async def cancel_execution(
        execution_id: str, x_agent_key: Annotated[str, Header()]
    ) -> JsonObject:
        if not hmac.compare_digest(x_agent_key, secret):
            raise HTTPException(401, "Invalid internal credential")
        if len(execution_id) > 64:
            raise HTTPException(400, "Invalid execution ID")
        await registry.cancel(execution_id)
        return {"canceled": True}

    @app.post("/internal/execute")
    async def execute(body: ExecuteBody, x_agent_key: Annotated[str, Header()]) -> JsonObject:
        if not hmac.compare_digest(x_agent_key, secret):
            raise HTTPException(401, "Invalid internal credential")
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        try:
            execution_key = body.execution_id or body.run_id
            return await registry.run(execution_key, digest, lambda: perform(body))
        except (APIStatusError, AnthropicStatusError) as error:
            raise HTTPException(
                429 if error.status_code == 429 else 502, "Model provider request failed"
            ) from error

    async def perform(body: ExecuteBody) -> JsonObject:
        if len(json.dumps(body.saved_state, ensure_ascii=False).encode()) > 1_000_000:
            raise HTTPException(413, "Checkpoint budget exceeded")
        if body.saved_state.get("_execution_mode", mode) != mode:
            raise HTTPException(409, "Create a new session when switching Agent mode")
        if body.saved_state.get("_execution_engine", engine) != engine:
            raise HTTPException(409, "Create a new session when switching Agent engine")
        if mode == "mock":
            if "[fail]" in body.content:
                raise HTTPException(503, "Simulated failure")
            delay = (
                10
                if "[cancel-probe]" in body.content
                else 2
                if "[slow]" in body.content
                else 0.5
                if "[benchmark-500ms]" in body.content
                else 0.05
            )
            await asyncio.sleep(delay)
            return {
                "answer": (
                    "【模拟验收】已接收当前商户的排查问题。"
                    "此回答用于验证后端会话与工单流程，未查询真实业务或调用模型。"
                ),
                "status": "completed",
                "checkpoint": {"_execution_mode": "mock", "_execution_engine": engine},
                "metrics": {"model_calls": 0, "tool_calls": 0},
                "evidence": [
                    {
                        "kind": "simulation",
                        "account_id": body.account_ids[0],
                        "environment": body.environment,
                    }
                ],
            }
        assert resources is not None and workflow is not None
        snapshot = await resources.snapshot(body.saved_state.get("_resource_snapshot"))
        sink = EvidenceSink()
        assert resources.source_client is not None
        scoped = AuthorizedSources(resources.source_client, body)
        retriever = resources.document_retriever(sink)
        if retrieval_cache and retriever:
            snapshot_digest = hashlib.sha256(
                json.dumps(
                    snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
            cache_scope: JsonObject = {
                "tenant_id": body.tenant_id,
                "account_ids": sorted(body.account_ids),
                "environment": body.environment,
                "snapshot_digest": snapshot_digest,
                "resource_snapshot": snapshot,
            }
            retriever = CachedDocumentRetriever(
                retriever,
                retrieval_cache,
                sink,
                cache_scope,
                retrieval_cache.metrics,
                cache_rebuilds,
            )
        runtime = AgentRuntime(sink, scoped, retriever, settings.knowledge_collection_id)
        scope_note = (
            f"当前服务端授权范围：account_id={body.account_ids[0]}，environment={body.environment}。"
            "此范围不接受用户改口切换；需在业务工作台创建另一条已授权调查。\n"
        )
        history = [
            ChatMessage(role=item.role, content=item.content, run_id=None, seq=index)
            for index, item in enumerate(body.history)
        ]
        turn = TurnInput(scope_note + body.content, history, dict(body.saved_state))
        try:
            result = await asyncio.wait_for(workflow.run(turn, runtime), timeout=150)
        except TimeoutError as error:
            raise HTTPException(504, "Agent execution deadline exceeded") from error
        result.saved_state["authorized_deliveries"] = sorted(scoped.deliveries)
        result.saved_state["_execution_mode"] = "real"
        result.saved_state["_execution_engine"] = engine
        result.saved_state["_resource_snapshot"] = snapshot
        return {
            "answer": result.text,
            "status": result.status,
            "checkpoint": result.saved_state,
            "metrics": sink.metrics,
            "evidence": [*sink.evidence, *[to_json_object(p) for p in result.evidence]],
        }

    return app
