"""启动时装配只读资源，独立负责每轮数据版本核对。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Protocol

from flowpay.core.agent.trace import Tracer
from flowpay.core.config import Settings
from flowpay.core.data_sources import DataSources, SourceClient
from flowpay.core.json import JsonObject
from flowpay.core.knowledge.client import KnowledgeClient
from flowpay.core.knowledge.contracts import Collection, DocumentRetriever, VectorStore
from flowpay.core.llm.embedding import EmbeddingClient
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel

from .errors import Conflict


class KnowledgeReader(Protocol):
    """运行前核对知识版本所需的最小只读能力，不提供构建或策略性检索。"""

    def get_collection(self, collection_id: str) -> Collection:
        """读取集合的版本与构建身份，用于生成本轮配置快照。"""
        ...

    def close(self) -> None:
        """释放知识连接；运行器结束后由资源所有者调用。"""
        ...


class RuntimeResources:
    """知识连接在生命周期内复用，版本快照则每轮重新读取。

    测试可以注入只读资源；正常启动通过 from_settings 装配。构建知识库的 writer
    不属于聊天服务资源。close 必须在所有 Run 结束后调用。
    """

    def __init__(
        self,
        settings: Settings,
        sources: DataSources | None = None,
        knowledge: KnowledgeReader | None = None,
        vectors: VectorStore | None = None,
        retriever: Callable[[Tracer], DocumentRetriever] | None = None,
    ):
        """保存配置、只读数据源和知识连接。

        检索客户端按轮装配，因为追踪是按轮的；retriever 用于注入替代实现，
        测试和演示据此避免依赖真实向量库。
        """
        self.settings: Settings = settings
        self.sources: DataSources | None = sources
        self.knowledge: KnowledgeReader | None = knowledge
        self.vectors: VectorStore | None = vectors
        self.retriever: Callable[[Tracer], DocumentRetriever] | None = retriever
        self.source_client: SourceClient | None = SourceClient(sources) if sources else None

    def document_retriever(self, tracer: Tracer) -> DocumentRetriever | None:
        """为一轮运行装配带追踪的检索客户端；连接池仍由本对象持有并复用。"""
        if self.retriever is not None:
            return self.retriever(tracer)
        if self.vectors is None or self.settings.embedding is None:
            return None
        embeddings = EmbeddingClient(OpenAiEmbeddingModel(self.settings.embedding), tracer)
        return KnowledgeClient(self.vectors, embeddings, tracer)

    @classmethod
    def from_settings(cls, settings: Settings) -> RuntimeResources:
        """按配置打开只读来源和知识连接；未配置的能力保持 None，返回需显式关闭的资源。"""
        from flowpay.core.knowledge import PostgresVectorStore

        sources = (
            DataSources.from_config(settings.sources_config, settings.dataset_path)
            if settings.sources_config
            else None
        )
        knowledge = (
            PostgresVectorStore(os.environ[settings.knowledge_url_env])
            if settings.knowledge_collection_id
            else None
        )
        # 检索与版本核对共用同一个连接池；管理器结束全部任务后再统一关闭。
        return cls(settings, sources, knowledge, knowledge)

    def close(self) -> None:
        """关闭本对象持有的知识读取器；调用前应结束所有使用它的 Run。"""
        if self.knowledge:
            self.knowledge.close()

    def _snapshot(self) -> JsonObject:
        """同步校验来源与知识版本并形成配置快照；集合未就绪或版本冲突时拒绝运行。"""
        snapshot = self.settings.snapshot()
        if self.sources:
            snapshot["data_sources"] = self.sources.verify()
        if self.knowledge and self.settings.knowledge_collection_id:
            collection = self.knowledge.get_collection(self.settings.knowledge_collection_id)
            if collection["status"] != "ready":
                raise Conflict("Knowledge collection is not ready")
            if self.sources and (
                collection["dataset_release_id"] != self.sources.policy["release_id"]
                or collection["dataset_manifest_sha256"] != self.sources.policy["manifest_sha256"]
            ):
                raise Conflict("Knowledge and business dataset versions differ")
            snapshot["knowledge"] = {
                key: collection[key]
                for key in (
                    "collection_id",
                    "dataset_release_id",
                    "dataset_manifest_sha256",
                    "build_config",
                    "dimensions",
                )
            }
        return snapshot

    async def snapshot(self, previous: JsonObject | None = None) -> JsonObject:
        """只校验并记录来源；查询策略和会话调度不进入资源层。"""
        current = await asyncio.to_thread(self._snapshot)
        if previous is not None and any(
            previous.get(key) != current.get(key) for key in ("data_sources", "knowledge")
        ):
            raise Conflict("Data version changed; create a new session")
        return current
