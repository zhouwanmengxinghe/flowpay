"""Reproducible document-ranking ablations on the explicitly reused benchmark."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from flowpay.core.config import load_settings
from flowpay.core.knowledge.client import KnowledgeClient
from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.knowledge.document_ranking import (
    DocumentRankingConfig,
    document_lexical_order,
    infer_document_route,
    rank_documents,
)
from flowpay.core.knowledge.ranking import lexical_order, reciprocal_rank_fusion
from flowpay.core.knowledge.store import PostgresVectorStore
from flowpay.core.llm.embedding import EmbeddingClient
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel

from scripts.business.experiment_rag_retrieval import (
    NullSink,
    embed_batches,
    load_cases,
    metric,
    passage_section,
    weighted_rrf,
)


async def collect(output: Path) -> None:
    """Snapshot candidates once so subsequent ablations need no embedding requests."""
    settings = load_settings()
    if settings.embedding is None or not settings.knowledge_collection_id:
        raise ValueError("Embedding and knowledge collection must be configured")
    dataset = Path("/dataset/benchmark/resolved/resolved_blueprints.jsonl")
    cases, _ = load_cases(dataset, Path("/dataset/data/index/knowledge_chunks.jsonl"))
    store = PostgresVectorStore(os.environ[settings.knowledge_url_env])
    embedding = EmbeddingClient(OpenAiEmbeddingModel(settings.embedding), NullSink())
    started = time.perf_counter()
    try:
        collection = store.get_collection(settings.knowledge_collection_id)
        corpus = store.list_passages(settings.knowledge_collection_id)
        vectors, requests = await embed_batches(
            embedding, [retrieval_input(row["question"]) for row in cases]
        )
        rankings = {}
        for row, vector in zip(cases, vectors, strict=True):
            dense = store.search(
                settings.knowledge_collection_id,
                SimpleNamespace(vectors=[vector], spec=embedding.identity),
                100,
            )
            lexical = lexical_order(row["question"], corpus)
            rankings[row["question_id"]] = {
                "dense": [{"id": item.id, "score": item.score} for item in dense],
                "bm25": [{"id": item.id, "score": item.score} for item in lexical],
            }
        payload = {
            "schema_version": 1,
            "collection_id": settings.knowledge_collection_id,
            "dataset_manifest_sha256": collection["dataset_manifest_sha256"],
            "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            "embedding_identity": embedding.identity,
            "cases": cases,
            "corpus": [asdict(item) for item in corpus],
            "rankings": rankings,
            "embedding_requests": requests,
            "chat_model_calls": 0,
            "collection_seconds": round(time.perf_counter() - started, 3),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"CANDIDATES {output} cases={len(cases)} corpus={len(corpus)}", flush=True
        )
    finally:
        store.close()


def snapshot_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    return gzip.decompress(raw) if path.suffix == ".gz" else raw


def load_snapshot(path: Path) -> tuple[dict, dict[str, Passage], dict[str, dict]]:
    snapshot = json.loads(snapshot_bytes(path).decode("utf-8"))
    corpus = {
        item["id"]: Passage(**{**item, "source": PassageSource(**item["source"])})
        for item in snapshot["corpus"]
    }
    rankings = {
        question_id: {
            source: [replace(corpus[item["id"]], score=item["score"]) for item in items]
            for source, items in sources.items()
        }
        for question_id, sources in snapshot["rankings"].items()
    }
    return snapshot, corpus, rankings


async def smoke(snapshot_path: Path, report_path: Path) -> None:
    """Verify the actual client against one newly recovered question and live services."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    recovered = next(
        row for row in report["case_results"] if row["selected_hit"] and not row["baseline_hit"]
    )
    settings = load_settings()
    if settings.embedding is None or not settings.knowledge_collection_id:
        raise ValueError("Embedding and collection must be configured")
    store = PostgresVectorStore(os.environ[settings.knowledge_url_env])
    try:
        client = KnowledgeClient(
            store,
            EmbeddingClient(OpenAiEmbeddingModel(settings.embedding), NullSink()),
            NullSink(),
            "hybrid-document",
        )
        started = time.perf_counter()
        results = await client.search(
            settings.knowledge_collection_id, retrieval_input(recovered["question"]), limit=5
        )
        actual = [item.id for item in results]
        acceptance = {
            "question_id": recovered["question_id"],
            "expected_top5": recovered["selected_top5"],
            "actual_top5": actual,
            "matches_snapshot_experiment": actual == recovered["selected_top5"],
            "unique_documents": len({item.source.doc_id for item in results}),
            "gold_document_hit": any(
                item.source.doc_id in recovered["gold_doc_ids"] for item in results
            ),
            "embedding_requests": 1,
            "chat_model_calls": 0,
            "seconds": round(time.perf_counter() - started, 3),
            "snapshot_sha256": hashlib.sha256(snapshot_bytes(snapshot_path)).hexdigest(),
        }
        path = report_path.with_suffix(".runtime-smoke.json")
        path.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
        print("SMOKE " + json.dumps(acceptance, ensure_ascii=False), flush=True)
        if not acceptance["matches_snapshot_experiment"] or not acceptance["gold_document_hit"]:
            raise AssertionError("Live retrieval differs from the recorded document experiment")
    finally:
        store.close()


def evaluate(snapshot_path: Path, output: Path) -> None:
    snapshot, corpus, candidates = load_snapshot(snapshot_path)
    reused_rows = [row for row in snapshot["cases"] if row["split"] == "test"]
    dev_rows = [row for row in snapshot["cases"] if row["split"] == "dev"]
    lexical_documents = {
        row["question_id"]: document_lexical_order(
            row["question"], list(corpus.values())
        )
        for row in snapshot["cases"]
    }
    baseline = {
        key: reciprocal_rank_fusion([sources["dense"], sources["bm25"]])[:5]
        for key, sources in candidates.items()
    }

    def run(
        config: DocumentRankingConfig, rows: list[dict]
    ) -> dict[str, list[Passage]]:
        return {
            row["question_id"]: rank_documents(
                row["question"],
                candidates[row["question_id"]]["dense"],
                candidates[row["question_id"]]["bm25"],
                config,
                whole_documents=lexical_documents[row["question_id"]],
            )
            for row in rows
        }

    def score(rows: list[dict], rankings: dict[str, list[Passage]]) -> dict:
        hit, reciprocal, precision, section_hit = [], [], [], []
        for row in rows:
            result = rankings[row["question_id"]][:5]
            docs = list(dict.fromkeys(item.source.doc_id for item in result))
            positions = [
                i for i, doc in enumerate(docs, 1) if doc in row["gold_doc_ids"]
            ]
            hit.append(bool(positions))
            reciprocal.append(1 / positions[0] if positions else 0)
            precision.append(len(positions) / len(docs) if docs else 0)
            section_hit.append(
                any(passage_section(item) in row["gold_sections"] for item in result)
            )
        count = len(rows)
        return {
            "queries": count,
            "hit_count": sum(hit),
            "document_hit_at_5": round(sum(hit) / count, 4),
            "document_mrr_at_5": round(sum(reciprocal) / count, 4),
            "document_precision_at_5": round(sum(precision) / count, 4),
            "section_hit_at_5": round(sum(section_hit) / count, 4),
        }

    started = time.perf_counter()
    sweep = []
    for method in ("collapse", "document-rrf", "whole-document-rrf", "support-sum"):
        for dense_depth in (25, 50, 100):
            for lexical_depth in (25, 50, 100):
                for constant in (10, 60):
                    for weight in (0.5, 1.0, 2.0):
                        for route_boost in (0.0, 0.5):
                            config = DocumentRankingConfig(
                                method=method,
                                dense_depth=dense_depth,
                                lexical_depth=lexical_depth,
                                rank_constant=constant,
                                dense_weight=weight,
                                route_boost=route_boost,
                            )
                            sweep.append(
                                {
                                    "config": asdict(config),
                                    "metrics": score(
                                        reused_rows, run(config, reused_rows)
                                    ),
                                }
                            )
        print(f"SWEEP {method} complete", flush=True)
    selected = max(
        sweep,
        key=lambda entry: (
            entry["metrics"]["document_hit_at_5"],
            entry["metrics"]["document_mrr_at_5"],
            entry["metrics"]["section_hit_at_5"],
            -entry["config"]["route_boost"],
        ),
    )
    config = DocumentRankingConfig(**selected["config"])
    final = run(config, snapshot["cases"])
    ablations = {
        "passage-rrf-baseline": score(reused_rows, baseline),
        "weighted-passage-rrf": score(
            reused_rows,
            {
                key: weighted_rrf(
                    sources["dense"],
                    sources["bm25"],
                    dense_depth=50,
                    lexical_depth=50,
                    rank_constant=60,
                    dense_weight=0.5,
                )[:5]
                for key, sources in candidates.items()
            },
        ),
        "dense-baseline": score(
            reused_rows,
            {key: sources["dense"][:5] for key, sources in candidates.items()},
        ),
    }
    for method in ("collapse", "document-rrf", "whole-document-rrf", "support-sum"):
        ablations[method] = score(
            reused_rows, run(DocumentRankingConfig(method=method), reused_rows)
        )
        ablations[method + "-route"] = score(
            reused_rows,
            run(DocumentRankingConfig(method=method, route_boost=0.5), reused_rows),
        )
    case_results = []
    for row in reused_rows:
        key = row["question_id"]
        original, result = baseline[key], final[key]
        gold_docs = set(row["gold_doc_ids"])
        case_results.append(
            {
                **row,
                "route": infer_document_route(row["question"]),
                "baseline_hit": any(
                    item.source.doc_id in gold_docs for item in original
                ),
                "selected_hit": any(item.source.doc_id in gold_docs for item in result),
                "baseline_unique_documents_in_top5": len(
                    {item.source.doc_id for item in original}
                ),
                "baseline_top5": [item.id for item in original],
                "selected_top5": [item.id for item in result],
                "target_first_dense_passage_rank": next(
                    (
                        i
                        for i, item in enumerate(candidates[key]["dense"], 1)
                        if item.source.doc_id in gold_docs
                    ),
                    None,
                ),
                "target_first_bm25_passage_rank": next(
                    (
                        i
                        for i, item in enumerate(candidates[key]["bm25"], 1)
                        if item.source.doc_id in gold_docs
                    ),
                    None,
                ),
            }
        )
    report = {
        "schema_version": 1,
        "suite": "document-aggregation-routing-reused-test",
        "evaluation_status": "adaptive comparison on the explicitly reused 56-question test set; not an independent holdout",
        "selection_set": "56 reused test questions, at user's explicit request",
        "snapshot_sha256": hashlib.sha256(snapshot_bytes(snapshot_path)).hexdigest(),
        "dataset_sha256": snapshot["dataset_sha256"],
        "collection_id": snapshot["collection_id"],
        "dataset_manifest_sha256": snapshot["dataset_manifest_sha256"],
        "candidate_snapshot": str(snapshot_path),
        "corpus_passages": len(corpus),
        "corpus_documents": len({item.source.doc_id for item in corpus.values()}),
        "ablations_default_parameters": ablations,
        "selected_config_on_reused_56": selected["config"],
        "selected_metrics_reused_56": metric(reused_rows, final),
        "selected_metrics_dev14_diagnostic": metric(dev_rows, final),
        "sweep": sweep,
        "case_results": case_results,
        "embedding_requests_collection": snapshot["embedding_requests"],
        "embedding_requests_ranking_sweep": 0,
        "chat_model_calls": 0,
        "ranking_seconds": round(time.perf_counter() - started, 3),
        "limitations": [
            "The 56 questions are deliberately reused for selecting parameters; report as in-sample adaptive results, not independent test performance.",
            "Top-5 still contains five original passages, but document aggregation returns at most one passage per source document.",
            "Document hit means the labeled source document is present; section hit and answer correctness are separate metrics.",
            "Rules never inspect gold labels, question IDs or hardcoded document IDs while ranking.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "SUMMARY "
        + json.dumps(
            {
                "config": asdict(config),
                "ablations": ablations,
                "selected": report["selected_metrics_reused_56"],
                "dev14_diagnostic": report["selected_metrics_dev14_diagnostic"],
                "sweep_size": len(sweep),
                "ranking_seconds": report["ranking_seconds"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("collect", "evaluate", "smoke"))
    parser.add_argument(
        "--snapshot", type=Path, default=Path("/app/var/rag-document-candidates.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("/app/var/rag-document-ranking.json")
    )
    args = parser.parse_args()
    if args.action == "collect":
        asyncio.run(collect(args.snapshot))
    elif args.action == "evaluate":
        evaluate(args.snapshot, args.output)
    else:
        asyncio.run(smoke(args.snapshot, args.output))


if __name__ == "__main__":
    main()
