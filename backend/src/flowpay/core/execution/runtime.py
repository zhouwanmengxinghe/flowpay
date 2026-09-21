"""一轮运行绑定的能力与追踪，由 SessionManager 创建并交给工作流。"""

from __future__ import annotations

from typing import Literal

from flowpay.core.agent.trace import Tracer
from flowpay.core.data_sources import SourceClient
from flowpay.core.data_sources.reader import SourceResult
from flowpay.core.json import JsonObject
from flowpay.core.knowledge.contracts import DocumentRetriever, Passage
from flowpay.core.llm.chat import ChatModel
from flowpay.core.llm.chat.client import ChatClient
from flowpay.core.tools import Tool, Toolbox


class AgentRuntime:
    """把本轮可用的能力交给工作流，并保证每次调用都被记录。

    chat 和 toolbox 返回已绑定本轮追踪的调用入口；knowledge 与 sources 是可选能力，
    未配置时为 None。检索顺序、调用次数与结束条件都由阶段决定。
    """

    def __init__(
        self,
        tracer: Tracer,
        sources: SourceClient | None = None,
        knowledge: DocumentRetriever | None = None,
        collection_id: str | None = None,
    ):
        """绑定本轮追踪与已装配的只读能力。"""
        self.tracer: Tracer = tracer
        self.sources: SourceClient | None = sources
        self.knowledge: DocumentRetriever | None = knowledge
        self.collection_id: str | None = collection_id

    def chat(self, model: ChatModel) -> ChatClient:
        """把一个模型包成带记录的调用入口；阶段可以为不同用途选择不同模型。"""
        return ChatClient(model, self.tracer)

    def toolbox(self, tools: list[Tool]) -> Toolbox:
        """把本轮工具组成带记录的执行入口；工具的处理函数由阶段提供。"""
        return Toolbox(tools, self.tracer)

    async def search_knowledge(self, text: str, *, limit: int = 5) -> list[Passage]:
        """检索本轮绑定的知识集合。

        Raises:
            RuntimeError: 本次运行没有装配知识能力——这与"检索到零个片段"是两件事。
        """
        if self.knowledge is None or self.collection_id is None:
            raise RuntimeError("Knowledge search requires a collection and embedding configuration")
        return await self.knowledge.search(self.collection_id, text, limit)

    async def query_sql(
        self, source: str, scope: JsonObject, filters: JsonObject | None = None
    ) -> SourceResult:
        """在一个已登记关系上按白名单条件查询。

        Raises:
            RuntimeError: 本次运行没有装配业务数据源。
        """
        if self.sources is None:
            raise RuntimeError("Business queries require a configured data source policy")
        return await self.sources.query_sql(source, scope, self.tracer, filters=filters)

    def field_values(self, kind: Literal["sql", "logs"], source: str) -> dict[str, list[str]]:
        """返回某个登记来源上取值封闭的字段及其合法取值，供工具写进 JSON Schema。

        Raises:
            RuntimeError: 本次运行没有装配业务数据源。
        """
        if self.sources is None:
            raise RuntimeError("Business queries require a configured data source policy")
        return self.sources.field_values(kind, source)

    async def query_logs(
        self,
        source: str,
        *,
        filters: JsonObject | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> SourceResult:
        """读取一个已登记的日志来源。

        Raises:
            RuntimeError: 本次运行没有装配业务数据源。
        """
        if self.sources is None:
            raise RuntimeError("Business queries require a configured data source policy")
        return await self.sources.query_logs(
            source, self.tracer, filters=filters, start=start, end=end
        )

    async def read_document(self, doc_id: str) -> list[Passage]:
        """整篇读回一份文档。

        Raises:
            RuntimeError: 本次运行没有装配知识能力——这与"集合里没有这篇文档"是两件事。
        """
        if self.knowledge is None or self.collection_id is None:
            raise RuntimeError("Reading documents requires a ready knowledge collection")
        return await self.knowledge.read_document(self.collection_id, doc_id)
