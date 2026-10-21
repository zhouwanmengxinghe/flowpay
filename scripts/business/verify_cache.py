"""C1-C5：真实 Redis + 假检索器，不调用嵌入或生成模型。"""

from __future__ import annotations

import asyncio
import time

from flowpay.core.cache.redis_cache import RedisJsonCache
from flowpay.core.knowledge.cached_retriever import CachedDocumentRetriever
from flowpay.core.knowledge.contracts import Passage, PassageSource
from upgrade_common import environment, save


class Search:
    def __init__(self, delay: float = 0.0):
        self.calls, self.delay = 0, delay

    async def search(self, collection_id, text, limit=5, filters=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if text == "empty":
            return []
        return [
            Passage(
                "doc:CACHE#c001",
                "缓存",
                "受控片段",
                0.9,
                PassageSource(collection_id, "CACHE", 1, "cache.md", "release", {}, {}),
            )
        ]

    async def read_document(self, collection_id, doc_id):
        return await self.search(collection_id, doc_id)


class Sink:
    def __init__(self):
        self.events = []

    async def emit(self, kind, payload, call_id=None, parent_call_id=None):
        self.events.append((kind, payload))
        return {"type": kind, "payload": payload}


def scope(**extra):
    data = {
        "tenant_id": "ta",
        "account_ids": ["aa"],
        "environment": "production",
        "snapshot_digest": "release",
        "resource_snapshot": {"manifest": "m1", "dimension": 1024},
    }
    data.update(extra)
    return data


async def run() -> int:
    report = environment("cache-correctness")
    cache = RedisJsonCache("redis://127.0.0.1:26380/0")

    async def clear_test_keys() -> None:
        """只删除本验收创建的 key，禁止用 FLUSHDB 影响同库其他调用者。"""
        keys: list[bytes] = []
        async for key in cache.client.scan_iter(match="flowpay:kb:v1:*"):
            keys.append(key)
        async for key in cache.client.scan_iter(match="probe:*"):
            keys.append(key)
        if keys:
            await cache.client.delete(*keys)

    await clear_test_keys()

    async def c1():
        direct = Search()
        without_cache = await direct.search("kb", "same", 5, {"type": "guide"})
        base, sink = Search(), Sink()
        wrapped = CachedDocumentRetriever(base, cache, sink, scope(), cache.metrics)
        cold = await wrapped.search("kb", "same", 5, {"type": "guide"})
        hot = await wrapped.search("kb", "same", 5, {"type": "guide"})
        assert without_cache == cold == hot
        assert base.calls == 1 and sink.events[-1][1]["cache"] == "hit"
        return {
            "cache_off_calls": direct.calls,
            "cache_on_calls": base.calls,
            "ordered_ids": [p.id for p in hot],
        }

    async def c2():
        base = Search()
        variants = [
            scope(),
            scope(tenant_id="tb"),
            scope(account_ids=["ab"]),
            scope(environment="sandbox"),
            scope(snapshot_digest="release2"),
        ]
        for item in variants:
            await CachedDocumentRetriever(base, cache, Sink(), item).search(
                "kb", "isolation", 5, {"x": 1}
            )
        first = CachedDocumentRetriever(base, cache, Sink(), variants[0])
        await first.search("other", "isolation", 5, {"x": 1})
        await first.search("kb", "isolation", 6, {"x": 1})
        await first.search("kb", "isolation", 5, {"x": 2})
        assert base.calls == len(variants) + 3
        return {"isolated_scope_variants": len(variants), "input_variants": 3}

    async def c3():
        base = Search()
        wrapped = CachedDocumentRetriever(base, cache, Sink(), scope())
        key = wrapped._key("kb", "bad", 5, None)
        await cache.client.set(key, b"not-json")
        await wrapped.search("kb", "bad")
        empty_key = wrapped._key("kb", "empty", 5, None)
        assert await wrapped.search("kb", "empty") == []
        empty_ttl = await cache.client.ttl(empty_key)
        assert 0 < empty_ttl <= 20
        huge = b"x" * (262145)
        await cache.client.set(key, huge)
        assert await cache.get(key) is None
        offline = RedisJsonCache("redis://127.0.0.1:26999/0", timeout_s=0.05)
        degraded = Search()
        await CachedDocumentRetriever(degraded, offline, Sink(), scope()).search(
            "kb", "offline"
        )
        await offline.close()
        assert degraded.calls == 1
        return {
            "bad_json_fallback": True,
            "empty_ttl_seconds": empty_ttl,
            "oversize_deleted": True,
            "outage_fallback": True,
        }

    async def c4():
        await clear_test_keys()
        base = Search(0.1)
        wrapped = CachedDocumentRetriever(base, cache, Sink(), scope())
        rows = await asyncio.gather(*(wrapped.search("kb", "hot") for _ in range(20)))
        assert base.calls == 1 and all(row == rows[0] for row in rows)
        return {"concurrency": 20, "base_calls": base.calls}

    async def c5():
        await clear_test_keys()
        assert await cache.acquire("probe:lock", "old", 50)
        await asyncio.sleep(0.07)
        assert await cache.acquire("probe:lock", "new", 5000)
        assert not await cache.publish("probe", "probe:lock", "old", b"old", 30)
        await cache.release("probe:lock", "old")
        assert await cache.client.get("probe:lock") == b"new"
        assert await cache.publish("probe", "probe:lock", "new", b"new-value", 30)
        return {
            "stale_publish_rejected": True,
            "new_value": (await cache.client.get("probe")).decode(),
        }

    for name, action in [
        ("C1_exact_cold_hot", c1),
        ("C2_scope_and_input_isolation", c2),
        ("C3_corruption_size_outage", c3),
        ("C4_twenty_request_single_rebuild", c4),
        ("C5_stale_holder_fenced", c5),
    ]:
        started = time.perf_counter()
        try:
            detail = await action()
            report["results"].append(
                {
                    "name": name,
                    "passed": True,
                    "seconds": round(time.perf_counter() - started, 4),
                    "detail": detail,
                }
            )
            print("PASS", name, flush=True)
        except Exception as error:  # noqa: BLE001 - report all invariant failures.
            report["results"].append(
                {
                    "name": name,
                    "passed": False,
                    "seconds": round(time.perf_counter() - started, 4),
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            print("FAIL", name, error, flush=True)
    report["metrics"] = cache.metrics.__dict__
    await cache.close()
    path = save(report, "cache-correctness.json")
    print(path)
    return 0 if report["passed"] == report["tests"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
