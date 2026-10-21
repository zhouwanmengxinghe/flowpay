"""Real pgvector/Ollama retrieval tests for hotspot collapse and cache recovery."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from flowpay.core.cache.redis_cache import CacheMetrics, RedisJsonCache
from flowpay.core.knowledge.cached_retriever import CachedDocumentRetriever
from flowpay.core.knowledge.client import KnowledgeClient
from flowpay.core.knowledge.store import PostgresVectorStore
from flowpay.core.llm.embedding import EmbeddingClient
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel
from flowpay.core.config import load_settings


class MetricsSink:
    def __init__(self) -> None:
        self.embedding_requests = 0

    async def emit(self, kind, payload, *args, **kwargs):
        if kind == "embedding.request":
            self.embedding_requests += 1


def p95(values: list[float]) -> float:
    return sorted(values)[max(0, int(len(values) * 0.95) - 1)]


async def main() -> int:
    settings = load_settings()
    redis_url = os.environ["REAL_CACHE_REDIS_URL"]
    unavailable_url = os.environ.get("REAL_CACHE_UNAVAILABLE_URL", "redis://127.0.0.1:6398/0")
    query = "Webhook signature verification fails after a delivery retry; what should support check?"
    run_id = uuid4().hex
    store = PostgresVectorStore(os.environ[settings.knowledge_url_env])
    sink = MetricsSink()
    base = KnowledgeClient(store, EmbeddingClient(OpenAiEmbeddingModel(settings.embedding), sink), sink)
    report = {
        "schema_version": 1,
        "suite": "real-retrieval-hotspot-and-recovery",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "host": platform.platform(),
        "mode": "real-ollama-embedding-real-pgvector-no-chat-model",
        "model_calls": 0,
        "embedding_model": settings.embedding.name,
        "collection_id": settings.knowledge_collection_id,
        "limitations": [
            "Local single-host test; not a multi-node production capacity result.",
            "Hotspot callers use the same exact input and authorization scope.",
            "Redis outage is simulated with an unreachable local port; external model and database remain healthy.",
            "No query text, source content, credentials, or provider response bodies are written.",
        ],
    }
    cache = RedisJsonCache(redis_url, timeout_s=0.5)
    scope = {
        "tenant_id": "benchmark-" + run_id,
        "account_ids": ["synthetic-benchmark"],
        "environment": "sandbox",
        "snapshot_digest": "retrieval-resilience-" + run_id,
    }
    cached = CachedDocumentRetriever(base, cache, sink, scope, cache.metrics)
    keys: list[str] = []
    try:
        collection_id = settings.knowledge_collection_id
        key = cached._key(collection_id, query, 5, None)
        keys.append(key)
        await cache.delete(key)
        started = time.perf_counter()
        rows = await asyncio.gather(*(cached.search(collection_id, query, 5) for _ in range(20)))
        elapsed_ms = (time.perf_counter() - started) * 1000
        ids = [[passage.id for passage in row] for row in rows]
        if not rows[0] or any(row != rows[0] for row in rows):
            raise AssertionError("Hotspot callers did not receive identical retrieval results")
        report["hotspot"] = {
            "concurrent_requests": len(rows),
            "base_embedding_calls": sink.embedding_requests,
            "cache_hits": cache.metrics.hits,
            "cache_waits": cache.metrics.waits,
            "cache_fills": cache.metrics.fills,
            "cache_errors": cache.metrics.errors,
            "elapsed_ms": round(elapsed_ms, 3),
            "result_count": len(rows[0]),
            "ordered_passage_ids": ids[0],
            "all_results_equal": True,
            "singleflight_saved_embedding_calls": max(0, len(rows) - sink.embedding_requests),
        }
        print(f"HOTSPOT requests=20 embedding_calls={sink.embedding_requests} hits={cache.metrics.hits} waits={cache.metrics.waits}", flush=True)
    finally:
        await cache.client.delete(*(keys + [key + ":lock" for key in keys]))
        await cache.close()

    # Fail-open: an unreachable cache must still return real retrieval results.
    outage_sink = MetricsSink()
    outage_base = KnowledgeClient(store, EmbeddingClient(OpenAiEmbeddingModel(settings.embedding), outage_sink), outage_sink)
    outage_cache = RedisJsonCache(unavailable_url, timeout_s=0.05)
    outage_scope = {**scope, "snapshot_digest": scope["snapshot_digest"] + "-outage"}
    outage = CachedDocumentRetriever(outage_base, outage_cache, outage_sink, outage_scope, outage_cache.metrics)
    try:
        started = time.perf_counter()
        outage_rows = await outage.search(settings.knowledge_collection_id, query, 5)
        report["cache_unavailable"] = {
            "retrieval_succeeded": bool(outage_rows),
            "embedding_calls": outage_sink.embedding_requests,
            "cache_errors": outage_cache.metrics.errors,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "ordered_passage_ids": [passage.id for passage in outage_rows],
        }
        if not outage_rows or outage_sink.embedding_requests != 1:
            raise AssertionError("Cache outage did not fail open to one real retrieval")
        print("OUTAGE cache-unreachable retrieval=PASS", flush=True)
    finally:
        await outage_cache.close()

    # Recovery: healthy Redis cold fill, then a warm read without another embedding call.
    recovery_sink = MetricsSink()
    recovery_base = KnowledgeClient(store, EmbeddingClient(OpenAiEmbeddingModel(settings.embedding), recovery_sink), recovery_sink)
    recovery_cache = RedisJsonCache(redis_url, timeout_s=0.5)
    recovery_scope = {**scope, "snapshot_digest": scope["snapshot_digest"] + "-recovery"}
    recovery = CachedDocumentRetriever(recovery_base, recovery_cache, recovery_sink, recovery_scope, recovery_cache.metrics)
    recovery_key = recovery._key(settings.knowledge_collection_id, query, 5, None)
    try:
        await recovery_cache.delete(recovery_key)
        cold = await recovery.search(settings.knowledge_collection_id, query, 5)
        warm = await recovery.search(settings.knowledge_collection_id, query, 5)
        report["cache_recovered"] = {
            "cold_then_warm_succeeded": bool(cold) and cold == warm,
            "embedding_calls_total": recovery_sink.embedding_requests,
            "cache_hits": recovery_cache.metrics.hits,
            "cache_fills": recovery_cache.metrics.fills,
            "ordered_passage_ids": [passage.id for passage in warm],
        }
        if not cold or cold != warm or recovery_sink.embedding_requests != 1:
            raise AssertionError("Recovered cache did not fill and serve a warm read")
        print("RECOVERY cold-fill + warm-hit=PASS", flush=True)
    finally:
        await recovery_cache.delete(recovery_key)
        await recovery_cache.delete(recovery_key + ":lock")
        await recovery_cache.close()
        store.close()

    report["finished_at"] = datetime.now(UTC).isoformat()
    out = Path("docs/verification/real-retrieval")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"resilience-{run_id[:12]}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT {path.as_posix()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
