"""Markdown 知识准备的数据模型；通用知识库存储只接收它们序列化后的 JSON。"""

from dataclasses import dataclass
from typing import Literal

from flowpay.core.knowledge.contracts import PassageLookup
from flowpay.core.knowledge.models import SplitterConfig
from flowpay.core.llm.embedding.settings import EmbeddingIdentity


@dataclass(frozen=True)
class KnowledgeBuildConfig:
    """一次 Markdown 导入的来源、分块和向量空间身份。"""

    release_manifest: str
    release_manifest_sha256: str
    document_sources: dict[str, str]
    splitter: SplitterConfig
    embedding: EmbeddingIdentity
    query_instruction: str
    importer_version: str = "s0-markdown-v1"


@dataclass(frozen=True)
class KnowledgeBuildPlan:
    """已核对来源的构建摘要，不携带原文和向量。"""

    collection_id: str
    release_id: str
    canonical_manifest_sha256: str
    document_count: int
    chunk_count: int
    expected_documents: dict[str, int]
    build_config: KnowledgeBuildConfig


@dataclass(frozen=True)
class DocumentSaved:
    """单篇文档写入完成后的来源与块数记录。"""

    doc_id: str
    source_path: str
    chunk_count: int
    source_sha256: str


@dataclass(frozen=True)
class KnowledgeBuildResult:
    """集合发布成功后的文档、块和本次新增向量计数。"""

    collection_id: str
    document_count: int
    chunk_count: int
    embedded_chunks: int
    status: Literal["ready"] = "ready"


@dataclass(frozen=True)
class RetrievalHitSummary:
    """验收报告只需展示的命中文档、块号和分数。"""

    doc_id: str
    chunk_no: int
    score: float


@dataclass(frozen=True)
class RetrievalCheckResult:
    """一道验收问题的预期来源、实际命中和回查证据。"""

    question: str
    expected_doc_id: str
    top_hits: list[RetrievalHitSummary]
    evidence: PassageLookup


@dataclass(frozen=True)
class KnowledgeVerificationResult:
    """检索、原文回查和空过滤验证的完整结果。"""

    collection_id: str
    checks: list[RetrievalCheckResult]
    empty_filter_verified: bool
