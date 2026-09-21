"""s1 回归：真实管理器与事件库，模拟检索和模型以验证编排及失败边界。"""

import copy
from collections.abc import Sequence
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, ChatCompletionToolParam

from flowpay.core.config import Settings
from flowpay.core.event_log import EventStore
from flowpay.core.event_log.contracts import EventSink
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.resources import RuntimeResources
from flowpay.core.json import JsonObject, require_object, to_json_object
from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.llm.chat import ChunkSink
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.workflows.s1 import S1Workflow


class Search:
    """模拟来源完整的检索结果，记录实际查询并确认不预设文档类型。"""

    def __init__(self, empty: bool = False, fail: bool = False):
        """选择空命中或故障；二者应产生不同的运行终态。"""
        self.empty: bool = empty
        self.fail: bool = fail
        self.queries: list[str] = []
        self.tracer: EventSink | None = None

    def for_run(self, tracer: EventSink) -> "Search":
        """按轮绑定追踪，与真实检索客户端同样的装配方式。"""
        self.tracer = tracer
        return self

    async def read_document(self, collection_id: str, doc_id: str) -> list[Passage]:
        """s1 不提供工具，因此这条路径不会被调用；保留以满足检索能力接口。"""
        raise AssertionError(f"s1 must not read documents: {collection_id}/{doc_id}")

    async def search(
        self,
        collection_id: str,
        text: str,
        limit: int = 5,
        filters: JsonObject | None = None,
    ) -> list[Passage]:
        """核对调用边界并返回固定原文；失败直接抛出，不返回空结果。"""
        assert collection_id == "fixture-rules" and self.tracer is not None
        assert limit == 10 and filters is None
        self.queries.append(text)
        await self.tracer.emit("knowledge.request", {"query": text})
        if self.fail:
            raise RuntimeError("retrieval unavailable")
        hits: list[Passage] = (
            []
            if self.empty
            else [
                Passage(
                    id="doc:RULE-1#c001",
                    title="通知规则",
                    text="端点返回 HTTP 410 后禁用。",
                    score=0.8,
                    source=PassageSource(
                        collection_id=collection_id,
                        doc_id="RULE-1",
                        chunk_no=1,
                        path="rules/webhooks.md",
                        sha256="fixture",
                        locator={"start": 0},
                        metadata={"doc_type": "troubleshooting"},
                    ),
                )
            ]
        )
        await self.tracer.emit("knowledge.response", {"results": [to_json_object(h) for h in hits]})
        return hits


class Model:
    """捕获生成输入，返回带文档引用的模型响应。"""

    name: str = "fixture"
    capabilities: dict[str, bool] = {}

    def __init__(self, finish: str = "stop"):
        """可用 length 模拟被截断的回答，验证检查点不被提交。"""
        self.finish: str = finish
        self.requests: list[str] = []
        self.message_batches: list[list[ChatCompletionMessageParam]] = []

    def prepare(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
    ) -> JsonObject:
        """断言不声明工具并保存输入，供检索原文与跨轮上下文检查。"""
        assert not tools
        self.requests.append(str(messages))
        self.message_batches.append(copy.deepcopy(list(messages)))
        return {"messages": messages}

    async def invoke(self, request: JsonObject, emit: ChunkSink) -> ChatCompletion:
        """返回固定响应；本测试不评价真实模型的答案质量。"""
        _ = request, emit
        if self.finish == "offline":
            raise RuntimeError("offline")
        return ChatCompletion.model_validate(
            {
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": self.name,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": self.finish,
                        "message": {
                            "role": "assistant",
                            "content": "端点被禁用。\n\n[doc:RULE-1#c001]",
                        },
                    }
                ],
            }
        )


@pytest.mark.parametrize("mode", ["answer", "empty", "retrieval_error", "truncated"])
async def test_rag_pipeline_and_failure_boundaries(tmp_path: Path, mode: str) -> None:
    """检索先于生成；空结果正常结束，故障与截断失败，引用可回查，回答上下文包含历史而检索仍使用当前消息。"""
    settings = Settings(tmp_path, tmp_path, knowledge_collection_id="fixture-rules")
    search = Search(empty=mode == "empty", fail=mode == "retrieval_error")
    model = Model("length" if mode == "truncated" else "stop")
    store = EventStore(tmp_path / "events.sqlite")
    resources = RuntimeResources(settings, retriever=search.for_run)
    manager = SessionManager(store, S1Workflow(model, retrieval_input), settings, resources)
    try:
        sid = (await manager.create_session())["id"]
        await manager.submit_message(sid, "first", "HTTP 410 会怎样？")
        await manager.tasks[sid]
        session = await manager.get_session(sid)
        assert session["status"] == (
            "failed" if mode in {"retrieval_error", "truncated"} else "completed"
        )
        assert session["state"] == {}
        assert search.queries == [retrieval_input("HTTP 410 会怎样？")]
        if mode in {"empty", "retrieval_error"}:
            assert not model.requests
        if mode == "empty":
            assert session["messages"][-1].get("evidence", []) == []
            assert "没有检索到" in session["messages"][-1]["content"]
        if mode == "answer":
            assert "端点返回 HTTP 410 后禁用。" in model.requests[0]
            evidence = session["messages"][-1].get("evidence", [])[0]
            assert evidence["id"] == "doc:RULE-1#c001"
            # 溯源收进 source，模型上下文不再看到它，前端和评测仍能读到。
            assert require_object(evidence["source"])["collection_id"] == "fixture-rules"
            kinds = [e["type"] for e in store.read(sid)]
            assert kinds.index("knowledge.response") < kinds.index("model.request")
            assert kinds[-2:] == ["message.assistant", "run.finished"]
            await manager.submit_message(sid, "second", "那还能恢复吗？")
            await manager.tasks[sid]
            assert model.message_batches[1][1:3] == [
                {"role": "user", "content": "HTTP 410 会怎样？"},
                {"role": "assistant", "content": "端点被禁用。\n\n[doc:RULE-1#c001]"},
            ]
            assert "那还能恢复吗？" in model.requests[1]
            assert "HTTP 410 会怎样？" in search.queries[-1]
            assert "那还能恢复吗？" in search.queries[-1]
            assert model.requests[1].count("本轮资料片段：") == 1
            assert len(model.requests) == 2
    finally:
        await manager.shutdown()
        resources.close()


async def test_current_stage_can_continue_its_saved_session(tmp_path: Path) -> None:
    """磁盘里的当前工作流身份可以继续提交下一轮。"""
    from flowpay.router import create_stage

    settings = Settings(tmp_path, tmp_path, knowledge_collection_id="fixture-rules")
    store = EventStore(tmp_path / "events.sqlite")
    sid = "session_before_stage_migration"
    await store.aappend(
        sid,
        [
            ("session.created", {"workflow": {"id": "flowpay-document-rag", "version": "4"}}),
        ],
    )
    resources = RuntimeResources(settings, retriever=Search().for_run)
    manager = SessionManager(
        store, create_stage("s1", Model(), retrieval_input), settings, resources
    )
    try:
        await manager.recover()
        await manager.submit_message(sid, "after_migration", "HTTP 410 会怎样？")
        await manager.tasks[sid]
        session = await manager.get_session(sid)
        assert session["workflow"] == {"id": "flowpay-document-rag", "version": "4"}
        assert session["status"] == "completed"
        assert session["messages"][-1]["content"] == "端点被禁用。\n\n[doc:RULE-1#c001]"
    finally:
        await manager.shutdown()
        resources.close()


async def test_history_survives_restart_and_resume_without_duplicate_question(
    tmp_path: Path,
) -> None:
    """历史从事件恢复；失败后继续保留相同模型输入，不能把当前问题追加两遍。"""
    settings = Settings(tmp_path, tmp_path, knowledge_collection_id="fixture-rules")
    path = tmp_path / "events.sqlite"
    first_model = Model()
    first_resources = RuntimeResources(settings, retriever=Search().for_run)
    first = SessionManager(
        EventStore(path), S1Workflow(first_model, retrieval_input), settings, first_resources
    )
    try:
        sid = (await first.create_session())["id"]
        await first.submit_message(sid, "first", "HTTP 410 会怎样？")
        await first.tasks[sid]
    finally:
        await first.shutdown()
        first_resources.close()

    model = Model("offline")
    resources = RuntimeResources(settings, retriever=Search().for_run)
    manager = SessionManager(
        EventStore(path), S1Workflow(model, retrieval_input), settings, resources
    )
    try:
        await manager.recover()
        await manager.submit_message(sid, "second", "那还能恢复吗？")
        await manager.tasks[sid]
        assert (await manager.get_session(sid))["status"] == "failed"
        model.finish = "stop"
        await manager.resume_run(sid, "retry")
        await manager.tasks[sid]
        view = await manager.get_session(sid)
        assert view["status"] == "completed"
        assert model.message_batches[0] == model.message_batches[1]
        assert model.requests[1].count("那还能恢复吗？") == 1
        assert model.message_batches[1][1:3] == [
            {"role": "user", "content": "HTTP 410 会怎样？"},
            {"role": "assistant", "content": "端点被禁用。\n\n[doc:RULE-1#c001]"},
        ]
        assert [m["content"] for m in view["messages"] if m["role"] == "user"] == [
            "HTTP 410 会怎样？",
            "那还能恢复吗？",
        ]
        # 新会话使用同一个工作流实例，也不能看见上一个会话的历史。
        other_sid = (await manager.create_session())["id"]
        await manager.submit_message(other_sid, "other", "退款需要什么条件？")
        await manager.tasks[other_sid]
        assert "HTTP 410 会怎样？" not in model.requests[-1]
        assert "那还能恢复吗？" not in model.requests[-1]
    finally:
        await manager.shutdown()
        resources.close()
