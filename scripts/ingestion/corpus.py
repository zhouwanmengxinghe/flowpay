"""从冻结发布清单选择知识文档，核对字节哈希并生成完整构建计划。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomllib
from flowpay.core.json import require_text
from flowpay.core.knowledge import Chunk, Document
from flowpay.core.knowledge.documents import parse_document, split_document
from flowpay.core.knowledge.models import SplitterConfig
from flowpay.core.llm.embedding import EmbeddingSettings
from flowpay.core.llm.embedding.ollama import QUERY_INSTRUCTION
from pydantic import BaseModel, TypeAdapter

from .models import KnowledgeBuildConfig, KnowledgeBuildPlan


class ReleaseArtifact(BaseModel):
    """只解析构建所需的发布文件字段，其余发布元信息不进入模型上下文。"""

    path: str
    sha256: str
    bytes: int


class ReleaseGroup(BaseModel):
    """发布清单中的文件组，包含可见范围、声明数量和具体文件；用于选择公开知识。"""

    group_id: str
    visibility: str
    file_count: int
    artifacts: list[ReleaseArtifact]


class DatasetRelease(BaseModel):
    """知识准备所需的发布清单视图；仅冻结发布且来源校验通过后才能导入。"""

    status: str
    release_id: str
    artifact_groups: list[ReleaseGroup]


@dataclass(frozen=True)
class CorpusSettings:
    """知识构建输入配置，固定集合 ID、发布清单哈希和分块长度。"""

    collection_id: str
    release_manifest: str
    release_manifest_sha256: str
    max_chunk_chars: int = 1000

    @classmethod
    def load(cls, path: Path) -> CorpusSettings:
        """读取指定 TOML 构造知识准备设置；未知参数由构造函数拒绝。"""
        with path.open("rb") as handle:
            values = tomllib.load(handle)
            if unknown := set(values) - set(cls.__dataclass_fields__):
                raise ValueError(
                    f"Unknown corpus configuration fields: {sorted(unknown)}"
                )
            return TypeAdapter(cls).validate_python(values)


@dataclass
class PreparedCorpus:
    """通过文件校验并完成分块的导入计划；尚不表示数据库写入或嵌入已经完成。"""

    collection_id: str
    release_id: str
    canonical_manifest_sha256: str
    build_config: KnowledgeBuildConfig
    documents: list[tuple[Document, list[Chunk]]]

    @property
    def expected_documents(self) -> dict[str, int]:
        """返回每篇文档应有的块数，供集合发布时逐项核对完整性。"""
        return {document.doc_id: len(chunks) for document, chunks in self.documents}

    def summary(self) -> KnowledgeBuildPlan:
        """返回构建摘要模型；只在写事件、数据库或报告时转换为 JSON。"""
        return KnowledgeBuildPlan(
            collection_id=self.collection_id,
            release_id=self.release_id,
            canonical_manifest_sha256=self.canonical_manifest_sha256,
            document_count=len(self.documents),
            chunk_count=sum(self.expected_documents.values()),
            expected_documents=self.expected_documents,
            build_config=self.build_config,
        )


def checked_bytes(root: Path, relative: str, expected_hash: str) -> bytes:
    """只读取数据根目录内的登记文件；返回已校验字节，避免校验后再次读取。"""
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("Invalid source path")
    resolved = (root / relative).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("Source escapes dataset root")
    content = resolved.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError(f"Source hash mismatch: {relative}")
    return content


def prepare_corpus(
    root: Path, config: CorpusSettings, embedding: EmbeddingSettings
) -> PreparedCorpus:
    """清单之外的 audit、benchmark、旧索引和业务文件不进入文档输入。"""
    release = DatasetRelease.model_validate_json(
        checked_bytes(root, config.release_manifest, config.release_manifest_sha256)
    )
    if release.status != "frozen":
        raise ValueError("Knowledge ingestion requires a frozen release")
    groups = {group.group_id: group for group in release.artifact_groups}
    if len(groups) != len(release.artifact_groups):
        raise ValueError("Duplicate release group")
    published = groups["published_knowledge"]
    if published.visibility != "agent_knowledge":
        raise ValueError("Knowledge group visibility differs")
    artifacts = published.artifacts
    if not artifacts or len(artifacts) != published.file_count:
        raise ValueError("Published document count differs")
    canonical = next(
        item
        for item in groups["canonical_agent_data"].artifacts
        if item.path == "data/generated/canonical/manifest.json"
    )
    checked_bytes(root, "generated/canonical/manifest.json", canonical.sha256)
    documents: list[tuple[Document, list[Chunk]]] = []
    seen_ids: set[str] = set()
    source_hashes: dict[str, str] = {}
    for artifact in sorted(artifacts, key=lambda item: item.path):
        source = artifact.path
        if not source.startswith("data/knowledge/") or not source.endswith(".md"):
            raise ValueError("Published artifact is not a knowledge Markdown file")
        relative = source.removeprefix("data/")
        content = checked_bytes(root, relative, artifact.sha256)
        if len(content) != artifact.bytes or relative in source_hashes:
            raise ValueError("Duplicate source or byte count mismatch")
        document, body_start = parse_document(relative, content.decode("utf-8"))
        if document.metadata.get("indexing") != "include":
            raise ValueError(f"Published document is not indexable: {relative}")
        if not require_text(document.metadata.get("doc_type")).strip():
            raise ValueError(f"Missing doc_type: {relative}")
        if document.doc_id in seen_ids:
            raise ValueError(f"Duplicate doc_id: {document.doc_id}")
        seen_ids.add(document.doc_id)
        source_hashes[relative] = artifact.sha256
        documents.append(
            (document, split_document(document, body_start, config.max_chunk_chars))
        )
    build_config = KnowledgeBuildConfig(
        release_manifest=config.release_manifest,
        release_manifest_sha256=config.release_manifest_sha256,
        document_sources=source_hashes,
        splitter=SplitterConfig(max_chunk_chars=config.max_chunk_chars),
        embedding=embedding.identity(),
        query_instruction=QUERY_INSTRUCTION,
    )
    return PreparedCorpus(
        config.collection_id,
        release.release_id,
        canonical.sha256,
        build_config,
        documents,
    )
