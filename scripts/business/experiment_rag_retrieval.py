"""Run a local, no-chat-model A/B study of chunking and retrieval strategies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import statistics
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from flowpay.core.config import load_settings
from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.knowledge.ranking import (
    deduplicate_mmr,
    lexical_order,
    reciprocal_rank_fusion,
    rerank_bm25,
    tokenize,
)
from flowpay.core.knowledge.store import PostgresVectorStore
from flowpay.core.llm.embedding import EmbeddingClient
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel

from scripts.ingestion.corpus import CorpusSettings, prepare_corpus

ROOT = Path(__file__).resolve().parents[2]
VARIANTS = (
    ("baseline-splitter-1000-overlap0", 1000, 0, False),
    ("structure-800-overlap80", 800, 80, True),
    ("structure-600-overlap60", 600, 60, True),
)
RRF_DEPTHS = (10, 25, 50, 100)
RRF_CONSTANTS = (10, 30, 60, 100)
RRF_DENSE_WEIGHTS = (0.5, 1.0, 2.0)


class NullSink:
    async def emit(self, *args, **kwargs):
        return None


def section_key(doc_id: str, heading: str) -> str:
    section = heading.split(" / ", 1)[1] if " / " in heading else heading
    return f"{doc_id}#{section.strip()}"


def load_cases(path: Path, index_path: Path) -> tuple[list[dict], dict[str, dict]]:
    index = {
        item["evidence_id"]: item
        for line in index_path.read_text(encoding="utf-8").splitlines()
        if (item := json.loads(line)).get("evidence_id")
    }
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    cases = []
    for row in rows:
        evidence = [
            index[key]
            for key in row.get("resolved_evidence_ids", [])
            if key in index and key.startswith("doc:")
        ]
        if (
            row.get("split") not in {"dev", "test"}
            or row.get("question_type") != "simple_retrieval"
            or not evidence
        ):
            continue
        cases.append(
            {
                "question_id": row["question_id"],
                "split": row["split"],
                "question": row["question"],
                "gold_sections": sorted(
                    {section_key(item["doc_id"], item["section"]) for item in evidence}
                ),
                "gold_doc_ids": sorted({item["doc_id"] for item in evidence}),
            }
        )
    return cases, index


def passage_section(passage: Passage) -> str:
    heading = passage.source.locator.get("heading", "")
    return section_key(
        passage.source.doc_id, heading if isinstance(heading, str) else ""
    )


def weighted_rrf(
    dense: list[Passage],
    lexical: list[Passage],
    *,
    dense_depth: int,
    lexical_depth: int,
    rank_constant: int,
    dense_weight: float,
) -> list[Passage]:
    """Fuse independently tuned candidate depths and dense/BM25 rank weights."""
    scores: dict[str, float] = {}
    passages: dict[str, Passage] = {}
    for ranking, depth, weight in (
        (dense, dense_depth, dense_weight),
        (lexical, lexical_depth, 1.0),
    ):
        for rank, passage in enumerate(ranking[:depth], start=1):
            passages[passage.id] = passage
            scores[passage.id] = scores.get(passage.id, 0.0) + weight / (
                rank_constant + rank
            )
    return [
        replace(passages[key], score=scores[key])
        for key in sorted(scores, key=lambda item: (-scores[item], item))
    ]


def miss_diagnostics(
    rows: list[dict],
    dense_rankings: dict[str, list[Passage]],
    lexical_rankings: dict[str, list[Passage]],
    final_rankings: dict[str, list[Passage]],
) -> list[dict]:
    """Explain document-level misses without using them to tune parameters."""
    diagnostics = []
    for row in rows:
        question_id = row["question_id"]
        gold_docs = set(row["gold_doc_ids"])
        final = final_rankings[question_id][:5]
        if any(item.source.doc_id in gold_docs for item in final):
            continue
        dense = dense_rankings[question_id]
        lexical = lexical_rankings[question_id]
        dense_docs = {item.source.doc_id for item in dense[:100]}
        lexical_docs = {item.source.doc_id for item in lexical[:100]}
        candidates = dense_docs | lexical_docs
        diagnostics.append(
            {
                "question_id": question_id,
                "split": row["split"],
                "question": row["question"],
                "gold_doc_ids": sorted(gold_docs),
                "gold_sections": row["gold_sections"],
                "diagnosis": "candidate_pool_miss"
                if not candidates.intersection(gold_docs)
                else "fusion_rank_miss",
                "gold_doc_in_dense_top100": bool(dense_docs.intersection(gold_docs)),
                "gold_doc_in_bm25_top100": bool(lexical_docs.intersection(gold_docs)),
                "dense_top5": [item.id for item in dense[:5]],
                "bm25_top5": [item.id for item in lexical[:5]],
                "fused_top5": [item.id for item in final],
            }
        )
    return diagnostics


def metric(
    rows: list[dict], rankings: dict[str, list[Passage]], limit: int = 5
) -> dict:
    hits, recalls, reciprocal, precisions, duplicates = [], [], [], [], []
    document_hits, document_reciprocal, document_precisions = [], [], []
    for row in rows:
        ranked = rankings[row["question_id"]][:limit]
        gold = set(row["gold_sections"])
        found_positions = [
            position
            for position, item in enumerate(ranked, 1)
            if passage_section(item) in gold
        ]
        relevant_found = {passage_section(item) for item in ranked} & gold
        hits.append(bool(relevant_found))
        recalls.append(len(relevant_found) / len(gold))
        reciprocal.append(1 / found_positions[0] if found_positions else 0.0)
        precisions.append(len(found_positions) / len(ranked) if ranked else 0.0)
        ranked_docs = list(dict.fromkeys(item.source.doc_id for item in ranked))
        relevant_doc_positions = [
            position
            for position, doc_id in enumerate(ranked_docs, 1)
            if doc_id in row["gold_doc_ids"]
        ]
        document_hits.append(bool(relevant_doc_positions))
        document_reciprocal.append(
            1 / relevant_doc_positions[0] if relevant_doc_positions else 0.0
        )
        document_precisions.append(
            sum(doc_id in row["gold_doc_ids"] for doc_id in ranked_docs)
            / len(ranked_docs)
            if ranked_docs
            else 0.0
        )
        sets = [set(tokenize(item.text)) for item in ranked]
        pairs = [
            len(left & right) / max(1, len(left | right))
            for i, left in enumerate(sets)
            for right in sets[i + 1 :]
        ]
        duplicates.append(sum(value >= 0.88 for value in pairs) / max(1, len(pairs)))
    return {
        "queries": len(rows),
        "hit_at_5": round(statistics.mean(hits), 4) if rows else 0,
        "evidence_recall_at_5": round(statistics.mean(recalls), 4) if rows else 0,
        "mrr_at_5": round(statistics.mean(reciprocal), 4) if rows else 0,
        "precision_at_5": round(statistics.mean(precisions), 4) if rows else 0,
        "document_level": {
            "hit_at_5": round(statistics.mean(document_hits), 4) if rows else 0,
            "mrr_at_5": round(statistics.mean(document_reciprocal), 4) if rows else 0,
            "precision_at_5": round(statistics.mean(document_precisions), 4)
            if rows
            else 0,
        },
        "near_duplicate_pair_rate_at_5": round(statistics.mean(duplicates), 4)
        if rows
        else 0,
    }


def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return numerator / (left_norm * right_norm)


def make_passages(corpus, collection_id: str) -> list[Passage]:
    passages = []
    for document, chunks in corpus.documents:
        digest = corpus.build_config.document_sources[document.source_path]
        for chunk in chunks:
            passages.append(
                Passage(
                    id=f"doc:{document.doc_id}#c{chunk.chunk_no:03}",
                    title=document.title,
                    text=chunk.content,
                    score=0.0,
                    source=PassageSource(
                        collection_id=collection_id,
                        doc_id=document.doc_id,
                        chunk_no=chunk.chunk_no,
                        path=document.source_path,
                        sha256=digest,
                        locator=chunk.source_locator,
                        metadata=document.metadata,
                    ),
                )
            )
    return passages


async def embed_batches(
    client: EmbeddingClient, texts: list[str]
) -> tuple[list[list[float]], int]:
    vectors: list[list[float]] = []
    requests = 0
    size = client.batch_size
    for offset in range(0, len(texts), size):
        batch = await client.embed(texts[offset : offset + size])
        vectors.extend(batch.vectors)
        requests += 1
        print(f"embedded {min(offset + size, len(texts))}/{len(texts)}", flush=True)
    return vectors, requests


async def main() -> int:
    settings = load_settings()
    if settings.embedding is None or not settings.knowledge_collection_id:
        raise SystemExit("Embedding settings and the current collection are required")
    if not os.environ.get(settings.embedding.api_key_env):
        raise SystemExit("The local embedding API key placeholder is not configured")
    dsn = os.environ.get(settings.knowledge_url_env)
    if not dsn:
        raise SystemExit("Knowledge DSN is not configured")

    dataset = Path(
        os.environ.get(
            "RAG_QUALITY_BLUEPRINTS",
            "/dataset/benchmark/resolved/resolved_blueprints.jsonl",
        )
    )
    static_index = Path(
        os.environ.get(
            "RAG_QUALITY_STATIC_INDEX", "/dataset/data/index/knowledge_chunks.jsonl"
        )
    )
    cases, _ = load_cases(dataset, static_index)
    train_rows = [row for row in cases if row["split"] == "dev"]
    test_rows = [row for row in cases if row["split"] == "test"]
    if not train_rows or not test_rows:
        raise SystemExit("Both dev and test simple_retrieval cases are required")

    base_config = CorpusSettings.load(ROOT / "backend/configs/knowledge.toml")
    settings_dataset = settings.dataset_path
    store = PostgresVectorStore(dsn)
    batch_size = int(os.environ.get("RAG_EMBED_BATCH_SIZE", "8"))
    if not 1 <= batch_size <= 128:
        raise SystemExit("RAG_EMBED_BATCH_SIZE must be between 1 and 128")
    embedding_settings = replace(settings.embedding, batch_size=batch_size)
    embedding = EmbeddingClient(OpenAiEmbeddingModel(embedding_settings), NullSink())
    collection = store.get_collection(settings.knowledge_collection_id)
    all_rankings: dict[str, dict[str, list[Passage]]] = {
        name: {} for name, _, _, _ in VARIANTS
    }
    all_rankings.update(
        {
            name: {}
            for name in (
                "current-dense",
                "current-hybrid",
                "current-rerank",
                "current-rerank-mmr",
                "tuned-hybrid",
                "selected-hybrid",
                "selected-rerank",
                "selected-rerank-mmr",
            )
        }
    )
    embedding_requests = 0
    lexical_rankings: dict[str, list[Passage]] = {}
    variant_stats: dict[str, dict[str, int]] = {}
    started = time.perf_counter()
    try:
        # Query vectors use exactly the same instruction as production s1.
        query_vectors, requests = await embed_batches(
            embedding, [retrieval_input(row["question"]) for row in cases]
        )
        embedding_requests += requests
        query_by_id = {
            row["question_id"]: query_vectors[i] for i, row in enumerate(cases)
        }

        for row in cases:
            vector = SimpleNamespace(
                vectors=[query_by_id[row["question_id"]]], spec=embedding.identity
            )
            ranked = await asyncio.to_thread(
                store.search, settings.knowledge_collection_id, vector, 100
            )
            all_rankings["current-dense"][row["question_id"]] = ranked

        base_prepared = await asyncio.to_thread(
            prepare_corpus, settings_dataset, base_config, settings.embedding
        )
        # The variant named 1000/0 must match the current collection's persisted splitter.
        expected_config = collection["build_config"]["splitter"]
        if (
            expected_config.get("name") != "markdown-heading-character-v1"
            or expected_config.get("max_chunk_chars") != 1000
        ):
            raise SystemExit(
                "Current collection splitter differs; update baseline identity before comparison"
            )
        current_passages = await asyncio.to_thread(
            store.list_passages, settings.knowledge_collection_id
        )
        current_content = {
            item.id: (item.text, item.source.locator) for item in current_passages
        }
        planned_content = {
            f"doc:{document.doc_id}#c{chunk.chunk_no:03}": (
                chunk.content,
                chunk.source_locator,
            )
            for document, chunks in base_prepared.documents
            for chunk in chunks
        }
        if current_content != planned_content:
            raise SystemExit(
                "Shared baseline splitter does not reproduce the current collection"
            )
        for row in cases:
            question_id = row["question_id"]
            dense_candidates = all_rankings["current-dense"][question_id]
            lexical_candidates = lexical_order(row["question"], current_passages)
            lexical_rankings[question_id] = lexical_candidates
            fused = reciprocal_rank_fusion(
                [dense_candidates, lexical_candidates], candidate_limit=50
            )
            reranked = rerank_bm25(row["question"], fused)
            all_rankings["current-hybrid"][question_id] = fused[:5]
            all_rankings["current-rerank"][question_id] = reranked[:10]
            all_rankings["current-rerank-mmr"][question_id] = deduplicate_mmr(
                row["question"], reranked, limit=5
            )
        with store.pool.connection() as db:
            persisted_vectors = db.execute(
                "SELECT content,embedding::text AS embedding FROM chunks "
                "WHERE collection_id=%s AND embedding IS NOT NULL",
                (settings.knowledge_collection_id,),
            ).fetchall()
        vector_cache = {
            row["content"]: [
                float(value) for value in row["embedding"].strip("[]").split(",")
            ]
            for row in persisted_vectors
        }
        for name, size, overlap, boundary_aware in VARIANTS:
            config = replace(
                base_config,
                collection_id=f"rag-experiment-{name}",
                max_chunk_chars=size,
                overlap_chars=overlap,
                boundary_aware=boundary_aware,
            )
            prepared = (
                base_prepared
                if size == 1000 and overlap == 0
                else await asyncio.to_thread(
                    prepare_corpus, settings_dataset, config, settings.embedding
                )
            )
            passages = make_passages(prepared, config.collection_id)
            missing_texts = list(
                dict.fromkeys(
                    item.text for item in passages if item.text not in vector_cache
                )
            )
            variant_stats[name] = {
                "chunks": len(passages),
                "reused_existing_vectors": len(passages) - len(missing_texts),
                "new_unique_texts_embedded": len(missing_texts),
            }
            new_vectors, requests = (
                await embed_batches(embedding, missing_texts)
                if missing_texts
                else ([], 0)
            )
            embedding_requests += requests
            vector_cache.update(zip(missing_texts, new_vectors, strict=True))
            vectors = [vector_cache[item.text] for item in passages]
            by_question = all_rankings[name]
            for row in cases:
                query = query_by_id[row["question_id"]]
                dense = sorted(
                    (
                        replace(item, score=cosine(query, vector))
                        for item, vector in zip(passages, vectors, strict=True)
                    ),
                    key=lambda item: (-item.score, item.id),
                )[:50]
                by_question[row["question_id"]] = dense
                if name == "structure-600-overlap60":
                    lexical = lexical_order(row["question"], passages)[:50]
                    fused = reciprocal_rank_fusion([dense, lexical], candidate_limit=50)
                    reranked = rerank_bm25(row["question"], fused)
                    all_rankings["selected-hybrid"][row["question_id"]] = fused[:5]
                    all_rankings["selected-rerank"][row["question_id"]] = reranked[:10]
                    all_rankings["selected-rerank-mmr"][row["question_id"]] = (
                        deduplicate_mmr(row["question"], reranked, limit=5)
                    )
            print(f"variant {name}: {len(passages)} chunks", flush=True)
    finally:
        store.close()

    elapsed = time.perf_counter() - started
    dev_sweep: list[dict] = []
    for dense_depth in RRF_DEPTHS:
        for lexical_depth in RRF_DEPTHS:
            for rank_constant in RRF_CONSTANTS:
                for dense_weight in RRF_DENSE_WEIGHTS:
                    name = (
                        f"d{dense_depth}-b{lexical_depth}-k{rank_constant}"
                        f"-dw{dense_weight:g}"
                    )
                    rankings = {
                        row["question_id"]: weighted_rrf(
                            all_rankings["current-dense"][row["question_id"]],
                            lexical_rankings[row["question_id"]],
                            dense_depth=dense_depth,
                            lexical_depth=lexical_depth,
                            rank_constant=rank_constant,
                            dense_weight=dense_weight,
                        )[:5]
                        for row in train_rows
                    }
                    metrics = metric(train_rows, rankings)
                    dev_sweep.append(
                        {
                            "name": name,
                            "dense_depth": dense_depth,
                            "bm25_depth": lexical_depth,
                            "rank_constant": rank_constant,
                            "dense_weight": dense_weight,
                            "bm25_weight": 1.0,
                            "metrics": metrics,
                        }
                    )
    default_sweep_name = "d50-b50-k60-dw1"
    selected_sweep = max(
        dev_sweep,
        key=lambda item: (
            item["metrics"]["document_level"]["hit_at_5"],
            item["metrics"]["document_level"]["mrr_at_5"],
            item["metrics"]["hit_at_5"],
            item["metrics"]["document_level"]["precision_at_5"],
            item["name"] == default_sweep_name,
        ),
    )
    selected_parameters = {
        key: selected_sweep[key]
        for key in (
            "dense_depth",
            "bm25_depth",
            "rank_constant",
            "dense_weight",
            "bm25_weight",
        )
    }
    for row in cases:
        question_id = row["question_id"]
        all_rankings["tuned-hybrid"][question_id] = weighted_rrf(
            all_rankings["current-dense"][question_id],
            lexical_rankings[question_id],
            dense_depth=selected_parameters["dense_depth"],
            lexical_depth=selected_parameters["bm25_depth"],
            rank_constant=selected_parameters["rank_constant"],
            dense_weight=selected_parameters["dense_weight"],
        )[:5]
    dev_rankings = {
        name: rank for name, rank in all_rankings.items() if name != "current-dense"
    }
    dev_metrics = {
        name: metric(train_rows, rankings) for name, rankings in dev_rankings.items()
    }
    selected_chunker = max(
        VARIANTS,
        key=lambda item: dev_metrics[item[0]]["evidence_recall_at_5"],
    )[0]
    test_metrics = {
        name: metric(test_rows, rankings) for name, rankings in all_rankings.items()
    }
    dev_misses = miss_diagnostics(
        train_rows,
        all_rankings["current-dense"],
        lexical_rankings,
        all_rankings["current-hybrid"],
    )
    test_misses = miss_diagnostics(
        test_rows,
        all_rankings["current-dense"],
        lexical_rankings,
        all_rankings["tuned-hybrid"],
    )
    report = {
        "schema_version": 1,
        "suite": "rag-chunking-hybrid-rerank-mmr-ablation",
        "collection_id": settings.knowledge_collection_id,
        "dataset_release_id": collection["dataset_release_id"],
        "dataset_manifest_sha256": collection["dataset_manifest_sha256"],
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "evidence_label": "document ID + published Markdown section; independent of chunk ordinal",
        "splitter": "shared scripts.ingestion.corpus.prepare_corpus and core split_document",
        "baseline_compatibility": "baseline splitter output was checked byte-for-byte against current collection; queries use the same production instruction",
        "chunk_variants": [
            {
                "name": name,
                "max_chunk_chars": size,
                "overlap_chars": overlap,
                "boundary_aware": boundary_aware,
                **variant_stats[name],
            }
            for name, size, overlap, boundary_aware in VARIANTS
        ],
        "selected_chunker_by_dev_evidence_recall_at_5": selected_chunker,
        "dev": dev_metrics,
        "dev_parameter_sweep": dev_sweep,
        "selected_parameters_on_dev": selected_parameters,
        "dev_current_hybrid_miss_diagnostics": dev_misses,
        "test": test_metrics,
        "test_tuned_hybrid_miss_diagnostics": test_misses,
        "chat_model_calls": 0,
        "embedding_requests": embedding_requests,
        "embedding_batch_size": batch_size,
        "elapsed_seconds": round(elapsed, 3),
        "limitations": [
            "Section metrics require the resolved Markdown section; document-level metrics count any retrieved passage from a gold source document as relevant.",
            "The current immutable collection is the baseline; experimental chunks are embedded in memory and are not published to PostgreSQL.",
            "RRF parameters are selected only on the 14-question dev split; the 56-question test split is scored once after selection.",
            "BM25/RRF and MMR are evaluated locally; no generated answer correctness is measured.",
            "Only simple_retrieval questions with resolved document evidence are included.",
        ],
    }
    output = Path(
        os.environ.get("RAG_EXPERIMENT_OUTPUT", "/app/var/rag-quality-experiment.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "SUMMARY "
        + json.dumps(
            {
                "selected_chunker": selected_chunker,
                "dev": dev_metrics,
                "test": test_metrics,
                "selected_parameters_on_dev": selected_parameters,
                "dev_sweep_size": len(dev_sweep),
                "dev_miss_count": len(dev_misses),
                "test_tuned_miss_count": len(test_misses),
                "embedding_requests": embedding_requests,
                "elapsed_seconds": round(elapsed, 3),
                "output": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
