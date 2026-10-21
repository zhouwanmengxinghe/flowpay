"""真实检索缓存开/关微基准；调用 Ollama embeddings + pgvector，不调用聊天模型。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from flowpay.core.cache.redis_cache import CacheMetrics, RedisJsonCache
from flowpay.core.config import load_settings
from flowpay.core.knowledge.cached_retriever import CachedDocumentRetriever
from flowpay.core.knowledge.client import KnowledgeClient
from flowpay.core.knowledge.store import PostgresVectorStore
from flowpay.core.llm.embedding import EmbeddingClient
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel


class MetricsSink:
    """只计数事件类型，不把查询、密钥、文档正文写入报告。"""

    def __init__(self) -> None:
        self.embedding_requests = 0
        self.knowledge_requests = 0

    async def emit(self, kind, payload, *args, **kwargs):
        if kind == "embedding.request":
            self.embedding_requests += 1
        elif kind == "knowledge.request":
            self.knowledge_requests += 1


QUERIES = (
    ("webhook-retry", "Webhook delivery failed; explain retry behavior and how to verify the signature."),
    ("fp4032-production", "What does FP-4032 mean for Production access, and does it mean the key was revoked?"),
    ("rate-limit-429", "How should a merchant diagnose a 429 response, usage plan limits, and retries?"),
    ("invalid-key-codes", "Explain the difference between FP-4011 and FP-4012 API key errors."),
    ("payment-state-errors", "How should I diagnose an invalid payment state and distinguish it from a declined payment?"),
    ("missing-webhook", "A payment succeeded but the webhook was not received; what should support check?"),
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))]


async def measured_search(retriever, collection_id, text, count: int):
    durations: list[float] = []
    results = []
    for _ in range(count):
        started = time.perf_counter()
        results.append(await retriever.search(collection_id, text, limit=5))
        durations.append((time.perf_counter() - started) * 1000)
    return results, durations


async def main() -> int:
    settings = load_settings()
    if settings.embedding is None or not settings.knowledge_collection_id:
        raise SystemExit("Real retrieval needs the configured embedding and collection")
    key = os.environ.get(settings.embedding.api_key_env)
    if not key:
        raise SystemExit("Embedding API key placeholder is not configured")
    redis_url = os.environ.get("REAL_CACHE_REDIS_URL", "")
    if not redis_url:
        raise SystemExit("Set REAL_CACHE_REDIS_URL to the isolated benchmark Redis")

    sink = MetricsSink()
    store = PostgresVectorStore(os.environ[settings.knowledge_url_env])
    cache = RedisJsonCache(redis_url, timeout_s=0.5)
    scope_id = uuid4().hex
    scope = {
        "tenant_id": "benchmark-" + scope_id,
        "account_ids": ["synthetic-benchmark"],
        "environment": "sandbox",
        "snapshot_digest": "real-retrieval-benchmark-" + scope_id,
    }
    metrics = CacheMetrics()
    base = KnowledgeClient(
        store, EmbeddingClient(OpenAiEmbeddingModel(settings.embedding), sink), sink
    )
    cached = CachedDocumentRetriever(base, cache, sink, scope, metrics)
    keys: list[str] = []
    report = {
        "schema_version": 1,
        "suite": "real-retrieval-cache-comparison",
        "run_id": scope_id,
        "started_at": datetime.now(UTC).isoformat(),
        "host": platform.platform(),
        "mode": "real-ollama-embedding-real-pgvector-no-chat-model",
        "model_calls": 0,
        "embedding_model": settings.embedding.name,
        "embedding_revision": settings.embedding.revision,
        "collection_id": settings.knowledge_collection_id,
        "repeats_per_condition": 5,
        "queries": [],
        "limitations": [
            "Local retrieval microbenchmark; does not measure answer generation or end-to-end Agent latency.",
            "Exact repeated queries are used to isolate cache benefit; paraphrases are not expected to hit this exact-input cache.",
            "Ollama is CPU-limited to one core for this experiment; results are specific to this host and load.",
            "No query text, source content, credentials, or provider response bodies are written to this report.",
        ],
    }
    try:
        collection = store.get_collection(settings.knowledge_collection_id)
        report["dataset_release_id"] = collection["dataset_release_id"]
        report["dataset_manifest_sha256"] = collection["dataset_manifest_sha256"]
        report["vector_dimensions"] = collection["dimensions"]
        for label, text in QUERIES:
            key_value = cached._key(settings.knowledge_collection_id, text, 5, None)
            keys.append(key_value)
            await cache.delete(key_value)
            sink.embedding_requests = 0
            sink.knowledge_requests = 0
            off, off_ms = await measured_search(base, settings.knowledge_collection_id, text, 5)
            off_embed_calls = sink.embedding_requests
            off_ids = [[passage.id for passage in row] for row in off]
            off_hashes = [hashlib.sha256(json.dumps(
                [{"id": p.id, "score": p.score} for p in row], separators=(",", ":")
            ).encode()).hexdigest() for row in off]

            await cache.delete(key_value)
            sink.embedding_requests = 0
            sink.knowledge_requests = 0
            cold, cold_ms = await measured_search(cached, settings.knowledge_collection_id, text, 1)
            warm, warm_ms = await measured_search(cached, settings.knowledge_collection_id, text, 4)
            on = cold + warm
            on_ids = [[passage.id for passage in row] for row in on]
            on_hashes = [hashlib.sha256(json.dumps(
                [{"id": p.id, "score": p.score} for p in row], separators=(",", ":")
            ).encode()).hexdigest() for row in on]
            embedding_calls = sink.embedding_requests
            results_match = all(item == off[0] for item in off + on)
            if off_ids != on_ids or off_hashes != on_hashes or not results_match:
                raise AssertionError(f"Retrieval changed with cache state for {label}")
            if len(on[0]) == 0:
                raise AssertionError(f"Benchmark query returned no passages: {label}")
            report["queries"].append({
                "label": label,
                "result_count": len(off[0]),
                "ordered_passage_ids": off_ids[0],
                "result_hashes_match": True,
                "cache_off": {
                    "samples": len(off_ms),
                    "embedding_calls": off_embed_calls,
                    "p50_ms": round(statistics.median(off_ms), 3),
                    "p95_ms": round(percentile(off_ms, 0.95), 3),
                },
                "cache_on_cold": {"samples": len(cold_ms), "embedding_calls": 1,
                    "p50_ms": round(statistics.median(cold_ms), 3)},
                "cache_on_warm": {"samples": len(warm_ms), "embedding_calls": 0,
                    "p50_ms": round(statistics.median(warm_ms), 3),
                    "p95_ms": round(percentile(warm_ms, 0.95), 3)},
                "cache_on_total_embedding_calls": embedding_calls,
                "embedding_call_reduction_percent": round((1 - embedding_calls / off_embed_calls) * 100, 2),
            })
            print(f"PASS {label}: same passages; embeddings off={off_embed_calls}, on={embedding_calls}", flush=True)

        report["finished_at"] = datetime.now(UTC).isoformat()
        report["summary"] = {
            "queries": len(report["queries"]),
            "passage_results_equal": True,
            "cache_off_embedding_calls": sum(row["cache_off"]["embedding_calls"] for row in report["queries"]),
            "cache_on_embedding_calls": sum(row["cache_on_total_embedding_calls"] for row in report["queries"]),
            "mean_embedding_call_reduction_percent": round(statistics.mean(
                row["embedding_call_reduction_percent"] for row in report["queries"]), 2),
            "mean_off_p50_ms": round(statistics.mean(row["cache_off"]["p50_ms"] for row in report["queries"]), 3),
            "mean_warm_p50_ms": round(statistics.mean(row["cache_on_warm"]["p50_ms"] for row in report["queries"]), 3),
        }
        output = Path("docs/verification/real-retrieval")
        output.mkdir(parents=True, exist_ok=True)
        path = output / f"cache-comparison-{scope_id[:12]}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"REPORT {path.as_posix()}", flush=True)
        print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
        return 0
    finally:
        own_keys = keys + [key + ":lock" for key in keys]
        if own_keys:
            await cache.client.delete(*own_keys)
        await cache.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
