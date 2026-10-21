# 协调嵌入构建与检索并记录事件；文档准备、切块策略和查询内容由调用方提供。
from __future__ import annotations

import asyncio
from typing import Literal

from flowpay.core.event_log.contracts import EventSink
from flowpay.core.event_log.store import new_id
from flowpay.core.json import JsonObject, require_object, to_json_object
from flowpay.core.llm.embedding import EmbeddingClient
from flowpay.core.llm.embedding.ollama import retrieval_question
from flowpay.core.llm.embedding.settings import same_vector_space

from .contracts import Passage, VectorStore
from .document_ranking import DOCUMENT_HYBRID_CONFIG, rank_documents
from .ranking import (
    deduplicate_mmr,
    lexical_order,
    reciprocal_rank_fusion,
    rerank_bm25,
)

RetrievalMode = Literal["dense", "hybrid", "hybrid-document", "hybrid-rerank", "hybrid-rerank-mmr"]


class KnowledgeClient:
    """负责知识构建、检索日志和可配置的 dense/hybrid 排序流程。"""

    def __init__(
        self,
        store: VectorStore,
        embeddings: EmbeddingClient,
        tracer: EventSink,
        retrieval_mode: RetrievalMode = "hybrid",
    ):
        """接收向量存储、嵌入客户端与本轮追踪；存储连接的生命周期由调用方负责。"""
        self.store: VectorStore = store
        self.embeddings: EmbeddingClient = embeddings
        self.tracer: EventSink = tracer
        self.retrieval_mode: RetrievalMode = retrieval_mode

    # 已成功入库的批次不会重算；外部请求成功但入库失败的批次重试时可能再次请求。
    async def embed_pending(self, collection_id: str) -> int:
        """反复读取缺失向量的分块，按适配器批大小生成并原子写入。

        先验证集合模型身份，避免花费外部调用后才发现空间不匹配。
        每轮成功入库即形成可续跑进度；外部调用成功但写库失败仍可能再次计费。
        返回本次实际补齐的分块数，不自动将集合标为 ready。
        """
        collection = await asyncio.to_thread(self.store.get_collection, collection_id)
        if not same_vector_space(collection["build_config"]["embedding"], self.embeddings.identity):
            raise ValueError("Embedding configuration differs from collection")
        completed = 0
        # 不使用仅存在内存中的分页游标，而是每次查询 embedding IS NULL。
        # 重启后数据库本身就是构建进度，已写入的批次不会再次被选出。
        while rows := await asyncio.to_thread(
            self.store.missing_embeddings,
            collection_id,
            self.embeddings.batch_size,
        ):
            batch = await self.embeddings.embed([r["content"] for r in rows])
            await asyncio.to_thread(
                self.store.save_embeddings,
                collection_id,
                [(r["doc_id"], r["chunk_no"]) for r in rows],
                batch,
            )
            completed += len(rows)
        return completed

    # 检索与嵌入分别记录事件，便于区分向量生成失败和数据库查询失败。
    async def search(
        self,
        collection_id: str,
        text: str,
        limit: int = 5,
        filters: JsonObject | None = None,
    ) -> list[Passage]:
        """执行有日志的查询嵌入与向量检索，返回来源完整的候选分块。

        请求日志保留原查询与过滤条件；参数和集合校验在调用嵌入前完成。
        knowledge 与 embedding 事件分别记录检索过程和嵌入请求，便于定位失败环节。
        取消与异常记录后继续上抛，不返回空结果掩盖系统故障。
        """
        call_id = new_id("retrieval")
        await self.tracer.emit(
            "knowledge.request",
            {
                "collection_id": collection_id,
                "query": text,
                "limit": limit,
                "filters": filters or {},
                "retrieval_mode": self.retrieval_mode,
            },
            call_id,
        )
        try:
            collection = await asyncio.to_thread(self.store.get_collection, collection_id)
            if collection["status"] != "ready":
                raise ValueError("Collection is not ready")
            if not same_vector_space(
                collection["build_config"]["embedding"], self.embeddings.identity
            ):
                raise ValueError("Embedding configuration differs from collection")
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("Search limit must be 1..100")
            if filters is not None:
                require_object(filters)
            # 查询文本和文档分块必须处于同一向量空间；前面已核对完整模型身份。
            # 这里不会自动改写问题或拼接业务提示，那些策略由 workflows 决定。
            batch = await self.embeddings.embed([text])
            candidate_limit = min(100, max(limit, 50)) if self.retrieval_mode != "dense" else limit
            dense = await asyncio.to_thread(
                self.store.search, collection_id, batch, candidate_limit, filters
            )
            if self.retrieval_mode != "dense":
                corpus = await asyncio.to_thread(self.store.list_passages, collection_id, filters)
                lexical_text = retrieval_question(text)
                lexical = await asyncio.to_thread(lexical_order, lexical_text, corpus)
                if self.retrieval_mode == "hybrid-document":
                    fused = await asyncio.to_thread(
                        rank_documents,
                        lexical_text,
                        dense,
                        lexical,
                        DOCUMENT_HYBRID_CONFIG,
                        limit=limit,
                    )
                else:
                    fused = await asyncio.to_thread(
                        reciprocal_rank_fusion,
                        [dense, lexical[:candidate_limit]],
                        candidate_limit=candidate_limit,
                    )
                if self.retrieval_mode in {"hybrid-rerank", "hybrid-rerank-mmr"}:
                    fused = await asyncio.to_thread(rerank_bm25, lexical_text, fused)
                if self.retrieval_mode == "hybrid-rerank-mmr":
                    result = await asyncio.to_thread(
                        deduplicate_mmr, lexical_text, fused, limit=limit
                    )
                else:
                    result = fused[:limit]
            else:
                result = dense
            # 事件载荷保留完整溯源；只有交给模型的那一份会被投影成四个字段。
            await self.tracer.emit(
                "knowledge.response",
                {
                    "collection_id": collection_id,
                    "results": [to_json_object(passage) for passage in result],
                    "retrieval_mode": self.retrieval_mode,
                },
                call_id,
            )
            return result
        except asyncio.CancelledError:
            await self.tracer.emit("knowledge.canceled", {}, call_id)
            raise
        except Exception as error:
            await self.tracer.emit(
                "knowledge.error",
                {"error_type": type(error).__name__, "message": str(error)},
                call_id,
            )
            raise

    # 整篇读回与检索分别记录，便于在轨迹里区分"搜了什么"和"读了哪一篇"。
    async def read_document(self, collection_id: str, doc_id: str) -> list[Passage]:
        """整篇取回一份文档的全部分块并记录过程。

        不经过嵌入与向量排序：模型已经知道文档编号，这一步只是把它读完。
        文档不存在时返回空列表，由调用方决定如何把这件事回传给模型。
        """
        call_id = new_id("retrieval")
        await self.tracer.emit(
            "document.request", {"collection_id": collection_id, "doc_id": doc_id}, call_id
        )
        try:
            passages = await asyncio.to_thread(self.store.read_document, collection_id, doc_id)
            await self.tracer.emit(
                "document.response",
                {"doc_id": doc_id, "passages": [to_json_object(p) for p in passages]},
                call_id,
            )
            return passages
        except asyncio.CancelledError:
            await self.tracer.emit("document.canceled", {"doc_id": doc_id}, call_id)
            raise
        except Exception as error:
            await self.tracer.emit(
                "document.error",
                {"doc_id": doc_id, "error_type": type(error).__name__, "message": str(error)},
                call_id,
            )
            raise
