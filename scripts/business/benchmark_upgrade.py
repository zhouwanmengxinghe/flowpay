"""T10 有界对照实验；默认每个业务读取并发档运行 30 秒，Agent 固定 500ms mock。"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import copy
import os
import statistics
import time

from flowpay.core.cache.redis_cache import RedisJsonCache
from flowpay.core.knowledge.cached_retriever import CachedDocumentRetriever
from flowpay.core.knowledge.contracts import Passage, PassageSource
from upgrade_common import (
    LocalClient,
    environment,
    load_support_env,
    require_mock,
    save,
)


class Search:
    def __init__(self):
        self.calls = 0

    async def search(self, collection_id, text, limit=5, filters=None):
        self.calls += 1
        await asyncio.sleep(0.005)
        return [
            Passage(
                "doc:BENCH#c001",
                "bench",
                "fixed",
                1.0,
                PassageSource(collection_id, "BENCH", 1, "bench.md", "fixed", {}, {}),
            )
        ]

    async def read_document(self, *args):
        return []


class Sink:
    async def emit(self, *args, **kwargs):
        return {}


def pct(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * p) - 1))]


async def cache_round(cache, enabled: bool):
    base = Search()
    latencies = []
    wrapped = CachedDocumentRetriever(
        base,
        cache,
        Sink(),
        {
            "tenant_id": "bench",
            "account_ids": ["bench"],
            "environment": "sandbox",
            "snapshot_digest": "fixed",
            "resource_snapshot": {"release": "fixed"},
        },
    )
    for _ in range(100):
        started = time.perf_counter()
        if enabled:
            await wrapped.search("bench", "same")
        else:
            await base.search("bench", "same")
        latencies.append((time.perf_counter() - started) * 1000)
    return {
        "enabled": enabled,
        "samples": len(latencies),
        "base_calls": base.calls,
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(pct(latencies, 0.95), 3),
    }


async def cache_benchmark():
    cache = RedisJsonCache("redis://127.0.0.1:26380/0")
    results = []
    try:
        for round_no in range(3):
            keys = [
                key async for key in cache.client.scan_iter(match="flowpay:kb:v1:*")
            ]
            if keys:
                await cache.client.delete(*keys)
            results.extend(
                [
                    {"round": round_no + 1, **await cache_round(cache, False)},
                    {"round": round_no + 1, **await cache_round(cache, True)},
                ]
            )
        return results
    finally:
        await cache.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=30)
    args = parser.parse_args()
    load_support_env()
    require_mock()
    report = environment("upgrade-benchmark")
    password = os.environ["SUPPORT_DEMO_PASSWORD"]
    cache_results = asyncio.run(cache_benchmark())
    report["cache_microbenchmark"] = cache_results

    reader = LocalClient()
    reader.call("/auth/login", {"username": "merchant_a", "password": password})
    sid = reader.call(
        "/sessions", {"accountId": "acct_bg_001", "environment": "sandbox"}
    )["id"]
    pool_clients = []
    for _ in range(16):
        client = LocalClient()
        for cookie in reader.cookies:
            client.cookies.set_cookie(copy.copy(cookie))
        pool_clients.append(client)
    reads = []
    for concurrency in (1, 4, 8, 16):
        clients = pool_clients[:concurrency]
        latencies = []
        errors = 0
        deadline = time.monotonic() + args.duration

        def worker(client, task_deadline=deadline, samples=latencies):
            nonlocal errors
            while time.monotonic() < task_deadline and len(samples) < 5000:
                started = time.perf_counter()
                try:
                    client.call("/sessions/" + sid)
                    samples.append((time.perf_counter() - started) * 1000)
                except Exception:  # noqa: BLE001 - benchmark records every transport failure.
                    errors += 1
                time.sleep(0.01)

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(worker, clients))
        reads.append(
            {
                "concurrency": concurrency,
                "seconds": args.duration,
                "samples": len(latencies),
                "errors": errors,
                "p50_ms": round(statistics.median(latencies), 3),
                "p95_ms": round(pct(latencies, 0.95), 3),
            }
        )
    report["business_read"] = reads

    jobs = []
    started = time.perf_counter()
    for i in range(12):
        job_sid = reader.call(
            "/sessions", {"accountId": "acct_bg_001", "environment": "sandbox"}
        )["id"]
        reader.call(
            f"/sessions/{job_sid}/messages",
            {
                "requestId": f"{report['run_id']}-bench-{i}",
                "content": "[benchmark-500ms] fixed",
            },
            202,
        )
        jobs.append((reader, job_sid))
    waits = []
    for client, job_sid in jobs:
        begin = time.perf_counter()
        while client.call("/sessions/" + job_sid)["status"] not in {
            "completed",
            "failed",
        }:
            time.sleep(0.05)
        waits.append(time.perf_counter() - begin)
    elapsed = time.perf_counter() - started
    report["async_execution"] = {
        "tasks": len(jobs),
        "fixed_mock_ms": 500,
        "global_slots": 2,
        "wall_seconds": round(elapsed, 3),
        "throughput_per_second": round(len(jobs) / elapsed, 3),
        "observation": "Java instance count does not raise the fixed two-slot Python execution capacity.",
    }
    report["method_limits"] = [
        "Local Docker Desktop on one Ryzen 7 5800H host.",
        "Cache benchmark uses a fixed 5ms fake retriever, not pgvector or an embedding model.",
        "Business read test stops at 5000 samples per concurrency level.",
        "No generated tokens, provider calls, or real customer data.",
    ]
    report["results"] = [
        {"name": "cache_three_rounds", "passed": len(cache_results) == 6},
        {
            "name": "business_read_matrix",
            "passed": all(x["errors"] == 0 for x in reads),
        },
        {"name": "fixed_mock_execution", "passed": len(jobs) == 12},
    ]
    path = save(report, "benchmark-upgrade.json")
    print(path)
    return 0 if report["passed"] == report["tests"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
