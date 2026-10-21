"""Lexical hybrid ranking and near-duplicate suppression for retrieved passages."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import cast

from .contracts import Passage


def tokenize(text: str) -> list[str]:
    """Retain technical identifiers and overlapping Chinese bigrams."""
    technical_tokens = cast(list[str], re.findall(r"[A-Za-z0-9_:-]+", text))
    tokens = [item.casefold() for item in technical_tokens]
    chinese_runs = cast(list[str], re.findall(r"[\u3400-\u9fff]+", text))
    for run in chinese_runs:
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def bm25_scores(query: str, passages: list[Passage]) -> dict[str, float]:
    """Score a small knowledge corpus with BM25 over identifiers and Chinese bigrams."""
    documents = [tokenize(item.title + " " + item.text) for item in passages]
    terms = set(tokenize(query))
    if not documents or not terms:
        return {item.id: 0.0 for item in passages}
    average_length = sum(map(len, documents)) / len(documents) or 1
    document_frequency = {
        term: sum(term in set(document) for document in documents) for term in terms
    }
    scores: dict[str, float] = {}
    for passage, document in zip(passages, documents, strict=True):
        frequencies = {term: document.count(term) for term in terms}
        score = 0.0
        for term, frequency in frequencies.items():
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_frequency = max(0.0, math.log1p((len(documents) - df + 0.5) / (df + 0.5)))
            score += (
                inverse_frequency
                * (frequency * 2.5)
                / (frequency + 1.5 * (1 - 0.75 + 0.75 * len(document) / average_length))
            )
        scores[passage.id] = score
    return scores


def reciprocal_rank_fusion(
    rankings: list[list[Passage]], *, candidate_limit: int = 50, rank_constant: int = 60
) -> list[Passage]:
    """Fuse independent rankings by rank, avoiding incompatible raw score scales."""
    scores: dict[str, float] = {}
    passages: dict[str, Passage] = {}
    for ranking in rankings:
        for rank, passage in enumerate(ranking[:candidate_limit], start=1):
            passages[passage.id] = passage
            scores[passage.id] = scores.get(passage.id, 0.0) + 1 / (rank_constant + rank)
    return [
        replace(passages[key], score=scores[key])
        for key in sorted(scores, key=lambda item: (-scores[item], item))
    ]


def rerank_bm25(query: str, passages: list[Passage]) -> list[Passage]:
    """Rerank a candidate pool using lexical evidence, preserving dense score as tie-break."""
    scores = bm25_scores(query, passages)
    return sorted(passages, key=lambda item: (-scores[item.id], -item.score, item.id))


def lexical_order(query: str, passages: list[Passage]) -> list[Passage]:
    """Return whole-corpus candidates in BM25 order with lexical scores attached."""
    scores = bm25_scores(query, passages)
    return [
        replace(item, score=scores[item.id])
        for item in sorted(passages, key=lambda item: (-scores[item.id], item.id))
    ]


def rerank_hybrid(
    query: str,
    dense: list[Passage],
    lexical: list[Passage],
    *,
    candidate_limit: int = 50,
) -> list[Passage]:
    """Fuse dense and lexical recall, then rerank the union with BM25 relevance."""
    fused = reciprocal_rank_fusion([dense, lexical], candidate_limit=candidate_limit)
    return rerank_bm25(query, fused)


def deduplicate_mmr(
    query: str,
    passages: list[Passage],
    *,
    limit: int = 5,
    relevance_weight: float = 0.82,
    duplicate_threshold: float = 0.88,
) -> list[Passage]:
    """Select relevant passages while suppressing near-identical overlapping chunks."""
    if not 0 < relevance_weight <= 1 or not 0 < duplicate_threshold <= 1:
        raise ValueError("Invalid MMR weights")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    query_terms = set(tokenize(query))
    lexical = bm25_scores(query, passages)
    max_lexical = max(lexical.values(), default=0.0) or 1.0
    normalized = {
        item.id: 0.7 * max(0.0, min(1.0, item.score)) + 0.3 * lexical[item.id] / max_lexical
        for item in passages
    }
    term_sets = {item.id: set(tokenize(item.text)) for item in passages}
    selected: list[Passage] = []
    remaining = list(passages)
    while remaining and len(selected) < limit:
        best: Passage | None = None
        best_value = float("-inf")
        for item in remaining:
            terms = term_sets[item.id]
            redundancy = max(
                (
                    len(terms & term_sets[chosen.id]) / max(1, len(terms | term_sets[chosen.id]))
                    for chosen in selected
                ),
                default=0.0,
            )
            overlap = len(terms & query_terms) / max(1, len(query_terms))
            relevance = max(normalized[item.id], min(1.0, overlap))
            value = relevance_weight * relevance - (1 - relevance_weight) * redundancy
            if redundancy >= duplicate_threshold:
                continue
            if value > best_value:
                best, best_value = item, value
        if best is None:
            break
        selected.append(replace(best, score=best_value))
        remaining.remove(best)
    return selected
