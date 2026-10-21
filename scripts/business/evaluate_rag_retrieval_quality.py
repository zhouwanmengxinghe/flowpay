"""Measure dense retrieval baseline and a local BM25+RRF improvement without chat calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

from flowpay.core.config import load_settings
from flowpay.core.knowledge.store import PostgresVectorStore
from flowpay.core.llm.embedding import EmbeddingClient
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel


class NullSink:
    async def emit(self, *args, **kwargs):
        return None


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        evidence_ids = sorted(
            {
                item
                for item in row.get("resolved_evidence_ids", [])
                if item.startswith("doc:") and "#c" in item
            }
        )
        if (
            row.get("split") == "test"
            and row.get("question_type") == "simple_retrieval"
            and evidence_ids
        ):
            cases.append(
                {
                    "question_id": row["question_id"],
                    "scenario_family_id": row["scenario_family_id"],
                    "question": row["question"],
                    "benchmark_evidence_ids": evidence_ids,
                    "gold_doc_ids": sorted(
                        {item.split("#", 1)[0] for item in evidence_ids}
                    ),
                }
            )
    return cases


def align_gold_to_runtime(cases: list[dict], index_path: Path, corpus: list) -> dict:
    """Map static benchmark evidence text to runtime chunks; chunk numbers are not portable."""
    static = {
        item["evidence_id"]: item
        for line in index_path.read_text(encoding="utf-8").splitlines()
        if (item := json.loads(line)).get("evidence_id")
    }
    by_doc = {}
    for passage in corpus:
        by_doc.setdefault(passage.id.split(":", 1)[1].split("#", 1)[0], []).append(
            passage
        )
    counts = {"unique": 0, "multiple": 0, "unmatched": 0}
    aligned_cases = 0
    for case in cases:
        runtime_gold = []
        for evidence_id in case["benchmark_evidence_ids"]:
            source = static.get(evidence_id)
            matches = []
            if source:
                normalize = lambda value: re.sub(r"\s+", "", value)
                needle = normalize(source["content"])
                matches = [
                    passage.id
                    for passage in by_doc.get(source["doc_id"], [])
                    if needle and needle in normalize(passage.text)
                ]
            counts[
                "unique"
                if len(matches) == 1
                else "multiple"
                if matches
                else "unmatched"
            ] += 1
            runtime_gold.extend(matches)
        case["gold_runtime_passage_ids"] = sorted(set(runtime_gold))
        if runtime_gold:
            aligned_cases += 1
    return {
        "method": "exact normalized evidence-text containment within a runtime passage from the same document",
        "unique_evidence_mappings": counts["unique"],
        "multiple_evidence_mappings": counts["multiple"],
        "unmatched_evidence_mappings": counts["unmatched"],
        "aligned_queries": aligned_cases,
        "total_queries": len(cases),
    }


def rank_metrics(rows: list[dict], field: str, k: int) -> dict:
    relevant_total = 0
    retrieved_relevant = 0
    hits = 0
    reciprocal_ranks = []
    precisions = []
    for row in rows:
        gold = set(row["gold_runtime_passage_ids"])
        ranked = row[field][:k]
        found = [i for i, passage_id in enumerate(ranked, 1) if passage_id in gold]
        relevant_total += len(gold)
        retrieved_relevant += len(found)
        hits += bool(found)
        reciprocal_ranks.append(1 / found[0] if found else 0)
        precisions.append(len(found) / len(ranked) if ranked else 0)
    count = len(rows)
    return {
        "k": k,
        "queries": count,
        "hit_rate": round(hits / count, 4),
        "recall": round(retrieved_relevant / relevant_total, 4)
        if relevant_total
        else 0,
        "mrr": round(statistics.mean(reciprocal_ranks), 4) if count else 0,
        "precision": round(statistics.mean(precisions), 4) if count else 0,
        "gold_passages": relevant_total,
        "retrieved_gold_passages": retrieved_relevant,
    }


def document_metrics(rows: list[dict], field: str, k: int) -> dict:
    """Score whether any chunk from an expected source document was retrieved."""
    hits = []
    reciprocal_ranks = []
    precisions = []
    for row in rows:
        gold = set(row["gold_doc_ids"])
        ranked_docs = list(
            dict.fromkeys(passage_id.split("#", 1)[0] for passage_id in row[field][:k])
        )
        relevant = [i for i, doc_id in enumerate(ranked_docs, 1) if doc_id in gold]
        hits.append(bool(relevant))
        reciprocal_ranks.append(1 / relevant[0] if relevant else 0)
        precisions.append(len(relevant) / len(ranked_docs) if ranked_docs else 0)
    return {
        "hit_rate": round(statistics.mean(hits), 4),
        "mrr": round(statistics.mean(reciprocal_ranks), 4),
        "precision": round(statistics.mean(precisions), 4),
    }


def tokenize(text: str) -> list[str]:
    """Keep technical identifiers and use overlapping bigrams for Chinese text."""
    tokens = [item.casefold() for item in re.findall(r"[A-Za-z0-9_:-]+", text)]
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def bm25_order(query: str, passages: list) -> list[str]:
    """Corpus-wide BM25 rank, used for an offline hybrid retrieval ablation."""
    docs = [tokenize(item.title + " " + item.text) for item in passages]
    query_terms = set(tokenize(query))
    if not docs or not query_terms:
        return [item.id for item in passages]
    avg_len = sum(map(len, docs)) / len(docs) or 1
    document_frequency = {
        term: sum(term in set(doc) for doc in docs) for term in query_terms
    }
    scores = []
    for passage, doc in zip(passages, docs, strict=True):
        frequencies = {term: doc.count(term) for term in query_terms}
        score = 0.0
        for term, frequency in frequencies.items():
            if frequency == 0:
                continue
            idf = max(
                0.0,
                math.log1p(
                    (len(docs) - document_frequency[term] + 0.5)
                    / (document_frequency[term] + 0.5)
                ),
            )
            score += (
                idf
                * (frequency * 2.5)
                / (frequency + 1.5 * (1 - 0.75 + 0.75 * len(doc) / avg_len))
            )
        scores.append((score, passage.id))
    return [passage_id for _, passage_id in sorted(scores, reverse=True)]


def reciprocal_rank_fusion(
    dense_ids: list[str], lexical_ids: list[str], limit: int
) -> list[str]:
    scores = {}
    for ranking in (dense_ids, lexical_ids):
        for rank, passage_id in enumerate(ranking, start=1):
            scores[passage_id] = scores.get(passage_id, 0) + 1 / (60 + rank)
    return [
        item
        for item, _ in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[
            :limit
        ]
    ]


async def main() -> int:
    settings = load_settings()
    if settings.embedding is None or not settings.knowledge_collection_id:
        raise SystemExit("A real embedding model and knowledge collection are required")
    if not os.environ.get(settings.embedding.api_key_env):
        raise SystemExit("Embedding API key placeholder is not configured")
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
    cases = load_cases(dataset)
    if not cases:
        raise SystemExit(f"No resolved test cases found in {dataset}")

    store = PostgresVectorStore(dsn)
    embeddings = EmbeddingClient(OpenAiEmbeddingModel(settings.embedding), NullSink())
    collection = store.get_collection(settings.knowledge_collection_id)
    with store.pool.connection() as db:
        corpus_rows = db.execute(
            "SELECT c.doc_id,c.chunk_no,c.content,d.title FROM chunks c "
            "JOIN documents d USING(collection_id,doc_id) "
            "WHERE c.collection_id=%s ORDER BY c.doc_id,c.chunk_no",
            (settings.knowledge_collection_id,),
        ).fetchall()
    corpus = [
        SimpleNamespace(
            id=f"doc:{row['doc_id']}#c{row['chunk_no']:03}",
            title=row["title"],
            text=row["content"],
        )
        for row in corpus_rows
    ]
    gold_alignment = align_gold_to_runtime(cases, static_index, corpus)
    aligned_cases = [case for case in cases if case["gold_runtime_passage_ids"]]
    rows = []
    started = time.perf_counter()
    try:
        for index, case in enumerate(cases, start=1):
            query = await embeddings.embed([retrieval_input(case["question"])])
            candidates = await asyncio.to_thread(
                store.search, settings.knowledge_collection_id, query, 50
            )
            baseline_ids = [item.id for item in candidates[:5]]
            top10_ids = [item.id for item in candidates[:10]]
            candidate_ids = [item.id for item in candidates]
            lexical_ids = bm25_order(case["question"], corpus)
            lexical_top50 = lexical_ids[:50]
            hybrid_ids = reciprocal_rank_fusion(candidate_ids, lexical_top50, 5)
            rows.append(
                {
                    **case,
                    "baseline_top5_ids": baseline_ids,
                    "top10_ids": top10_ids,
                    "dense_top50_ids": candidate_ids,
                    "bm25_top50_ids": lexical_top50,
                    "hybrid_top5_ids": hybrid_ids,
                }
            )
            print(f"{index}/{len(cases)} {case['question_id']}", flush=True)
    finally:
        store.close()

    elapsed = time.perf_counter() - started
    report = {
        "schema_version": 1,
        "suite": "rag-retrieval-quality-dense-hybrid-ablation",
        "embedding_model": settings.embedding.name,
        "embedding_revision": settings.embedding.revision,
        "collection_id": settings.knowledge_collection_id,
        "dataset_release_id": collection["dataset_release_id"],
        "dataset_manifest_sha256": collection["dataset_manifest_sha256"],
        "eval_source": "resolved benchmark test split / simple_retrieval",
        "query_embedding_input": "production retrieval_input(question)",
        "eval_source_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "case_count": len(rows),
        "gold_alignment": gold_alignment,
        "corpus_passages": len(corpus),
        "model_calls": 0,
        "embedding_requests": len(rows),
        "baseline": {
            "retrieval_top_k": 5,
            **rank_metrics(aligned_cases, "baseline_top5_ids", 5),
            "document_level": document_metrics(rows, "baseline_top5_ids", 5),
        },
        "top10_diagnostic": {
            "retrieval_top_k": 10,
            **rank_metrics(aligned_cases, "top10_ids", 10),
            "document_level": document_metrics(rows, "top10_ids", 10),
        },
        "dense_candidate_top50": {
            **rank_metrics(aligned_cases, "dense_top50_ids", 50),
        },
        "bm25_candidate_top50": {
            **rank_metrics(aligned_cases, "bm25_top50_ids", 50),
        },
        "improved": {
            "method": "fuse full-corpus BM25 top-50 with dense top-50 using reciprocal-rank fusion, then return top-5",
            **rank_metrics(aligned_cases, "hybrid_top5_ids", 5),
            "document_level": document_metrics(rows, "hybrid_top5_ids", 5),
        },
        "elapsed_seconds": round(elapsed, 3),
        "limitations": [
            "This measures retrieval evidence coverage, not generated answer correctness.",
            "Static evidence chunk IDs are mapped by exact text into runtime chunks before chunk-level scoring; document-level metrics include all 56 queries.",
            "Baseline is dense top-5. The improvement fuses full-corpus BM25 top-50 with dense top-50, then returns 5 passages.",
            "The test split contains synthetic benchmark questions and exact resolved passage labels.",
            "Precision is expected to trade off against recall as more context is admitted; no LLM judge or chat model is used.",
        ],
        "cases": rows,
    }
    output = Path(
        os.environ.get("RAG_QUALITY_OUTPUT", "/tmp/rag-retrieval-quality.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "SUMMARY "
        + json.dumps(
            {
                "baseline": report["baseline"],
                "improved": report["improved"],
                "elapsed_seconds": report["elapsed_seconds"],
                "output": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
