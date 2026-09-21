"""s0 文档构建与验收流程，所有嵌入和检索调用均绑定持久化 Run。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from flowpay.core.execution.contracts import TurnInput, TurnResult
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard
from flowpay.core.json import to_json_object
from flowpay.core.knowledge import PostgresVectorStore
from flowpay.core.knowledge.client import KnowledgeClient
from flowpay.core.knowledge.models import MarkdownLocator
from flowpay.core.llm.embedding import EmbeddingClient, EmbeddingSettings
from flowpay.core.llm.embedding.ollama import ollama_identity, retrieval_input
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel
from pydantic import TypeAdapter

from .corpus import PreparedCorpus, checked_bytes
from .models import (
    DocumentSaved,
    KnowledgeBuildResult,
    KnowledgeVerificationResult,
    RetrievalCheckResult,
    RetrievalHitSummary,
)

# 验收问题只检查公开规则，不从隐藏评测题或实例答案提取查询。
RETRIEVAL_CHECKS = [
    ("Webhook 投递收到 HTTP 410 时，会如何处理 Endpoint？", "KB-BR-015", "410"),
    ("同一个幂等键搭配不同请求参数会怎样？", "KB-BR-008", "幂等"),
    ("退款必须满足什么状态和时间窗口限制？", "KB-BR-012", "退款"),
]


class KnowledgePreparation:
    """s0 公开知识文档的构建或检索验收流程。

    使用已经准备的 corpus 和独立读写连接，通过 AgentRuntime 记录所有外部调用；
    不调用聊天模型，不生成 s1 的业务答案。
    """

    id: str = "s0-knowledge-preparation"
    version: str = "1"
    card: StageCard = StageCard(
        label="业务知识准备", description="公开文档构建与检索验收，不调用聊天模型"
    )

    def __init__(
        self,
        corpus: PreparedCorpus,
        embedding: EmbeddingSettings,
        builder: PostgresVectorStore,
        reader: PostgresVectorStore,
        dataset_root: Path,
        operation: Literal["build", "verify"],
    ) -> None:
        """保存导入计划、嵌入配置、读写存储和操作类型；连接由外部入口负责关闭。"""
        self.corpus: PreparedCorpus = corpus
        self.embedding: EmbeddingSettings = embedding
        self.builder: PostgresVectorStore = builder
        self.reader: PostgresVectorStore = reader
        self.dataset_root: Path = dataset_root
        self.operation: Literal["build", "verify"] = operation

    async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
        """核对 Ollama 部署并执行 build 或 verify；过程与结果写入当前 Run，失败上抛。"""
        _ = turn  # 构建对象由配置确定，运行输入仅用于记录任务名称。
        deployment = await ollama_identity(self.embedding)
        await runtime.tracer.emit(
            "knowledge.build.plan", to_json_object(self.corpus.summary())
        )
        await runtime.tracer.emit("embedding.deployment", to_json_object(deployment))
        client = EmbeddingClient(OpenAiEmbeddingModel(self.embedding), runtime.tracer)
        if self.operation == "build":
            result = await self.build(client, runtime)
        else:
            result = await self.verify(client, runtime)
        if await ollama_identity(self.embedding) != deployment:
            raise RuntimeError("Model deployment changed during operation")
        await runtime.tracer.emit(
            "knowledge.preparation.result", to_json_object(result)
        )
        return TurnResult(
            "知识构建完成" if self.operation == "build" else "知识验收通过",
            to_json_object(result),
        )

    async def build(
        self, embedding: EmbeddingClient, runtime: AgentRuntime
    ) -> KnowledgeBuildResult:
        """幂等写入文档并补齐缺失向量；再次核对来源后发布集合，返回文档和向量计数。"""
        corpus = self.corpus
        await asyncio.to_thread(
            self.builder.create_collection,
            corpus.collection_id,
            corpus.release_id,
            corpus.canonical_manifest_sha256,
            to_json_object(corpus.build_config),
            self.embedding.dimensions,
        )
        for document, chunks in corpus.documents:
            await asyncio.to_thread(
                self.builder.save_document, corpus.collection_id, document, chunks
            )
            await runtime.tracer.emit(
                "knowledge.document.saved",
                to_json_object(
                    DocumentSaved(
                        doc_id=document.doc_id,
                        source_path=document.source_path,
                        chunk_count=len(chunks),
                        source_sha256=corpus.build_config.document_sources[
                            document.source_path
                        ],
                    )
                ),
            )
        embedded = await KnowledgeClient(
            self.builder, embedding, runtime.tracer
        ).embed_pending(corpus.collection_id)
        # 发布前再次核对来源字节和模型 digest；变化时保留 building 集合供诊断。
        await asyncio.to_thread(self.verify_source_files)
        await ollama_identity(self.embedding)
        await asyncio.to_thread(
            self.builder.mark_ready, corpus.collection_id, corpus.expected_documents
        )
        return KnowledgeBuildResult(
            collection_id=corpus.collection_id,
            document_count=len(corpus.documents),
            chunk_count=sum(corpus.expected_documents.values()),
            embedded_chunks=embedded,
        )

    def verify_source_files(self) -> None:
        """重读发布清单和文档以核对哈希，防止构建期间来源被替换。"""
        corpus = self.corpus
        checked_bytes(
            self.dataset_root,
            corpus.build_config.release_manifest,
            corpus.build_config.release_manifest_sha256,
        )
        checked_bytes(
            self.dataset_root,
            "generated/canonical/manifest.json",
            corpus.canonical_manifest_sha256,
        )
        for source, digest in corpus.build_config.document_sources.items():
            checked_bytes(self.dataset_root, source, digest)

    async def verify(
        self, embedding: EmbeddingClient, runtime: AgentRuntime
    ) -> KnowledgeVerificationResult:
        """检验指定规则进入前列、引用可回查及空过滤结果，返回记录摘要；不评测聊天答案。"""
        corpus = self.corpus
        collection = await asyncio.to_thread(
            self.reader.get_collection, corpus.collection_id
        )
        if collection["status"] != "ready" or collection[
            "build_config"
        ] != to_json_object(corpus.build_config):
            raise ValueError(
                "Published collection differs from the current preparation plan"
            )
        if (
            collection["dataset_release_id"] != corpus.release_id
            or collection["dataset_manifest_sha256"] != corpus.canonical_manifest_sha256
        ):
            raise ValueError("Collection release identity differs")
        search = KnowledgeClient(self.reader, embedding, runtime.tracer)
        results: list[RetrievalCheckResult] = []
        documents = {doc.doc_id: (doc, chunks) for doc, chunks in corpus.documents}
        for question, expected_id, expected_text in RETRIEVAL_CHECKS:
            hits = await search.search(
                corpus.collection_id,
                retrieval_input(question),
                limit=3,
                filters={"doc_type": "business_rule"},
            )
            matches = [
                hit
                for hit in hits
                if hit.source.doc_id == expected_id and expected_text in hit.text
            ]
            if not matches:
                raise RuntimeError(
                    f"Expected rule passage missing from top 3: {expected_id}"
                )
            hit = matches[0]
            lookup = await asyncio.to_thread(
                self.reader.read_passage,
                corpus.collection_id,
                hit.source.doc_id,
                hit.source.chunk_no,
            )
            original, chunks = documents[hit.source.doc_id]
            expected_chunk = chunks[hit.source.chunk_no - 1]
            if (
                lookup.document_text != original.content
                or lookup.passage.text != expected_chunk.content
            ):
                raise RuntimeError("Passage round trip differs from source")
            locator = TypeAdapter(MarkdownLocator).validate_python(
                lookup.passage.source.locator
            )
            reconstructed = (
                locator.prefix + original.content[locator.start_char : locator.end_char]
            )
            if reconstructed != hit.text:
                raise RuntimeError(
                    "Chunk locator does not identify the original excerpt"
                )
            result = RetrievalCheckResult(
                question=question,
                expected_doc_id=expected_id,
                top_hits=[
                    RetrievalHitSummary(
                        item.source.doc_id, item.source.chunk_no, item.score
                    )
                    for item in hits
                ],
                evidence=lookup,
            )
            results.append(result)
            await runtime.tracer.emit("knowledge.check.passed", to_json_object(result))
        empty = await search.search(
            corpus.collection_id,
            retrieval_input("支付通知"),
            filters={"doc_type": "not-a-published-type"},
        )
        if empty:
            raise RuntimeError("Metadata filter unexpectedly matched documents")
        await asyncio.to_thread(self.verify_source_files)
        return KnowledgeVerificationResult(corpus.collection_id, results, True)
