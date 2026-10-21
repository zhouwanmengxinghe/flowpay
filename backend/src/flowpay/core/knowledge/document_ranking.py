"""Rank source documents and return one original, citable passage per document."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Literal

from .contracts import Passage
from .ranking import lexical_order


@dataclass(frozen=True)
class DocumentRankingConfig:
    method: Literal["collapse", "document-rrf", "whole-document-rrf", "support-sum"] = "collapse"
    dense_depth: int = 50
    lexical_depth: int = 50
    rank_constant: int = 60
    dense_weight: float = 1.0
    support_passages: int = 3
    route_boost: float = 0.0

    def __post_init__(self) -> None:
        if self.method not in {"collapse", "document-rrf", "whole-document-rrf", "support-sum"}:
            raise ValueError("Invalid document ranking method")
        if not 1 <= self.dense_depth <= 100 or not 1 <= self.lexical_depth <= 100:
            raise ValueError("Candidate depths must be 1..100")
        if self.rank_constant < 1 or self.dense_weight <= 0 or self.support_passages < 1:
            raise ValueError("Invalid document ranking weights")
        if not 0 <= self.route_boost <= 2:
            raise ValueError("Route boost must be 0..2")


# Explicitly selected on the reused 56-question benchmark; preserve the old hybrid mode.
DOCUMENT_HYBRID_CONFIG = DocumentRankingConfig(dense_weight=0.5)


def collapse_documents(passages: list[Passage]) -> list[Passage]:
    """Keep the first passage from each source document in the existing order."""
    seen: set[str] = set()
    result: list[Passage] = []
    for passage in passages:
        if passage.source.doc_id not in seen:
            result.append(passage)
            seen.add(passage.source.doc_id)
    return result


def document_lexical_order(query: str, corpus: list[Passage]) -> list[Passage]:
    """Score complete documents with BM25; merged text is for ranking only."""
    grouped: dict[str, list[Passage]] = defaultdict(list)
    for passage in corpus:
        grouped[passage.source.doc_id].append(passage)
    documents: list[Passage] = []
    for chunks in grouped.values():
        ordered = sorted(chunks, key=lambda item: item.source.chunk_no)
        documents.append(replace(ordered[0], text="\n\n".join(item.text for item in ordered)))
    return lexical_order(query, documents)


def infer_document_route(query: str) -> str | None:
    """Use general intent words as a soft route, without question/document ID rules."""
    lowered = query.casefold()
    rules = {
        "migration_guide": ("升级", "迁移", "旧版", "新版", "版本差异", "版本切换"),
        "api_doc": ("请求参数", "对象字段", "请求体", "接口路径", "响应字段", "哪个接口"),
        "business_rule": ("状态机", "租户边界", "所有权", "计费规则", "权限边界"),
        "troubleshooting": ("排查", "诊断", "根因", "报错", "如何处理", "什么原因"),
    }
    scores = {kind: sum(term in lowered for term in terms) for kind, terms in rules.items()}
    best = max(scores, key=lambda kind: scores[kind])
    return best if scores[best] else None


def rank_documents(
    query: str,
    dense: list[Passage],
    lexical: list[Passage],
    config: DocumentRankingConfig,
    *,
    whole_documents: list[Passage] | None = None,
    limit: int = 5,
) -> list[Passage]:
    """Fuse document evidence, preserving an original representative passage and source."""
    if not 1 <= limit <= 100:
        raise ValueError("Document result limit must be 1..100")
    sources = (
        (dense[: config.dense_depth], config.dense_weight),
        (lexical[: config.lexical_depth], 1.0),
    )
    passage_scores: dict[str, float] = defaultdict(float)
    passages: dict[str, Passage] = {}
    for ranking, weight in sources:
        for position, passage in enumerate(ranking, 1):
            passages[passage.id] = passage
            passage_scores[passage.id] += weight / (config.rank_constant + position)
    fused = [
        replace(passages[key], score=passage_scores[key])
        for key in sorted(passages, key=lambda key: (-passage_scores[key], key))
    ]
    representatives = {item.source.doc_id: item for item in collapse_documents(fused)}
    doc_scores: dict[str, float]
    if config.method == "collapse":
        doc_scores = {doc_id: item.score for doc_id, item in representatives.items()}
    elif config.method == "support-sum":
        support: dict[str, list[float]] = defaultdict(list)
        for item in fused:
            support[item.source.doc_id].append(item.score)
        doc_scores = {
            doc_id: sum(values[: config.support_passages]) for doc_id, values in support.items()
        }
    else:
        if config.method == "whole-document-rrf" and whole_documents is None:
            raise ValueError("Whole-document BM25 ranking is required")
        lexical_documents = whole_documents if config.method == "whole-document-rrf" else lexical
        doc_scores = defaultdict(float)
        for ranking, depth, weight in (
            (dense, config.dense_depth, config.dense_weight),
            (lexical_documents or [], config.lexical_depth, 1.0),
        ):
            # Depth is applied before collapse for chunk rankings, as in passage fusion.
            for position, passage in enumerate(collapse_documents(ranking[:depth]), 1):
                doc_id = passage.source.doc_id
                doc_scores[doc_id] += weight / (config.rank_constant + position)
        # A whole-document hit may lie outside the chunk candidate cutoff.
        for passage in lexical:
            if passage.source.doc_id in doc_scores:
                representatives.setdefault(passage.source.doc_id, passage)
    route = infer_document_route(query) if config.route_boost else None
    if route:
        for doc_id in doc_scores:
            if representatives[doc_id].source.metadata.get("doc_type") == route:
                doc_scores[doc_id] *= 1 + config.route_boost
    return [
        replace(representatives[doc_id], score=doc_scores[doc_id])
        for doc_id in sorted(doc_scores, key=lambda key: (-doc_scores[key], key))[:limit]
    ]
