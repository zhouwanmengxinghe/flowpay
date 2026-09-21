"""阶段回归共用的受控模型与检索；不调用外部服务，也不替代阶段决策。"""

import asyncio
import copy
from collections.abc import Sequence
from dataclasses import replace

from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, ChatCompletionToolParam

from flowpay.core.event_log.contracts import EventSink
from flowpay.core.json import JsonObject, to_json_object
from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.llm.chat import ChunkSink

ANSWER = "HTTP 410 后禁用端点。\n\n[doc:RULE-1#c001]"


def completion(
    content: str | None = None, calls: list[JsonObject] | None = None, finish: str | None = None
) -> ChatCompletion:
    """构造原生模型响应；附加供应商字段用于检查回填时没有丢失。"""
    return ChatCompletion.model_validate(
        {
            "id": "fixture",
            "object": "chat.completion",
            "created": 0,
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish or ("tool_calls" if calls else "stop"),
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": calls,
                        "reasoning_content": "公开的测试推理字段",
                    },
                }
            ],
        }
    )


def read_call(identifier: str, doc_id: str) -> JsonObject:
    """创建一次 read_document 调用，用于检验模型在两个工具之间的选择。"""
    import json

    return {
        "id": identifier,
        "type": "function",
        "function": {"name": "read_document", "arguments": json.dumps({"doc_id": doc_id})},
    }


def search_call(identifier: str, query: str, limit: int = 10) -> JsonObject:
    """创建实际 SDK function 调用，arguments 仍是协议要求的 JSON 字符串。"""
    import json

    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "search_documents",
            "arguments": json.dumps({"query": query, "limit": limit}),
        },
    }


def hit() -> Passage:
    """返回可被前端回查的片段，非 business_rule 类型也允许被检索。"""
    return Passage(
        id="doc:RULE-1#c001",
        title="端点排查",
        text="HTTP 410 后禁用端点。",
        score=0.8,
        source=PassageSource(
            collection_id="fixture",
            doc_id="RULE-1",
            chunk_no=1,
            path="knowledge/endpoint.md",
            sha256="fixture",
            locator={"start_char": 0},
            metadata={"doc_type": "troubleshooting"},
        ),
    )


def other_hit(identifier: str, doc_id: str) -> Passage:
    """另一个片段，用于检验去重、引用选择与跨片段反馈。"""
    base = hit()
    return replace(base, id=identifier, source=replace(base.source, doc_id=doc_id))


class Search:
    """记录模型实际查询；可以返回空结果、故障或暂停以测试取消。"""

    def __init__(self) -> None:
        """每个测试持有独立的调用记录与阻塞信号。"""
        self.queries: list[tuple[str, int]] = []
        self.reads: list[str] = []
        self.fail: bool = False
        self.block: bool = False
        self.started: asyncio.Event = asyncio.Event()
        self.tracer: EventSink | None = None
        self.documents: dict[str, list[Passage]] = {"RULE-1": [hit()]}

    def for_run(self, tracer: EventSink) -> "Search":
        """按轮绑定追踪，与真实检索客户端同样的装配方式；调用记录跨轮累积。"""
        self.tracer = tracer
        return self

    async def search(
        self,
        collection_id: str,
        text: str,
        limit: int = 5,
        filters: JsonObject | None = None,
    ) -> list[Passage]:
        """核对集合和无文档类型过滤；包含 missing 的查询模拟零命中。"""
        assert collection_id == "fixture" and filters is None and self.tracer is not None
        self.queries.append((text, limit))
        await self.tracer.emit("knowledge.request", {"query": text, "limit": limit})
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("search offline")
        results = [] if "missing" in text else [hit()]
        await self.tracer.emit(
            "knowledge.response", {"results": [to_json_object(r) for r in results]}
        )
        return results

    async def read_document(self, collection_id: str, doc_id: str) -> list[Passage]:
        """按编号整篇读回；未登记的编号返回空列表，与真实存储一致。"""
        assert collection_id == "fixture" and self.tracer is not None
        self.reads.append(doc_id)
        passages = self.documents.get(doc_id, [])
        await self.tracer.emit(
            "document.response",
            {"doc_id": doc_id, "passages": [to_json_object(p) for p in passages]},
        )
        return passages


class Model:
    """逐次返回预设动作，完整保存请求；不调用网络，不评价真实模型能力。"""

    name: str = "fixture"
    capabilities: dict[str, bool] = {"tools": True, "streaming": True}

    def __init__(self) -> None:
        """测试显式安排每次调用结果，不用固定业务结论代替真实课程实现。"""
        self.responses: list[ChatCompletion | Exception] = []
        self.requests: list[JsonObject] = []

    def prepare(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
    ) -> JsonObject:
        """保存深拷贝，避免工作流追加消息时污染已记录的请求。"""
        request: JsonObject = {"messages": copy.deepcopy(list(messages)), "tools": list(tools)}
        self.requests.append(request)
        return request

    async def invoke(self, request: JsonObject, emit: ChunkSink) -> ChatCompletion:
        """发出正文片段，让事件日志同时覆盖流式正文和最终协议响应。"""
        _ = request
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if result.choices[0].message.content:
            await emit({"kind": "content_delta", "text": result.choices[0].message.content})
        return result


def grounded(
    text: str = "HTTP 410 后禁用端点。", sources: list[str] | None = None
) -> ChatCompletion:
    """模拟原生 Markdown 回答，引用工具返回的真实文档 ID。"""
    identifiers = ["doc:RULE-1#c001"] if sources is None else sources
    return completion(text + "\n\n" + "".join(f"[{source}]" for source in identifiers))


def conversation(text: str) -> ChatCompletion:
    """模拟无需引用的原生文本回复。"""
    return completion(text)


def insufficient(missing: str = "端点恢复的适用条件") -> ChatCompletion:
    """依据不足时模型直接解释限制，不再切换回答协议。"""
    return completion("当前资料无法确认：" + missing)
