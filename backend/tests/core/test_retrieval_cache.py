"""检索缓存只优化精确同输入；这些测试不连接模型或外部服务。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from flowpay.core.cache.redis_cache import CacheMetrics
from flowpay.core.knowledge.cached_retriever import CachedDocumentRetriever
from flowpay.core.knowledge.contracts import Passage, PassageSource


def passage(collection: str = "fixture") -> Passage:
    return Passage(
        id="doc:KB-1#c001",
        title="缓存验收",
        text="相同输入返回相同片段。",
        score=0.8,
        source=PassageSource(
            collection_id=collection,
            doc_id="KB-1",
            chunk_no=1,
            path="knowledge/cache.md",
            sha256="release-1",
            locator={"start_char": 0},
            metadata={"doc_type": "guide"},
        ),
    )


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.locks: dict[str, str] = {}
        self.available = True
        self.metrics = CacheMetrics()

    async def get(self, key: str) -> bytes | None:
        if not self.available:
            self.metrics.errors += 1
            return None
        return self.values.get(key)

    async def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        if not self.available:
            self.metrics.errors += 1
            return False
        if key in self.locks:
            return False
        self.locks[key] = token
        return True

    async def publish(self, key: str, lock_key: str, token: str, value: bytes, ttl_s: int) -> bool:
        if self.locks.get(lock_key) != token:
            return False
        self.values[key] = value
        self.locks.pop(lock_key)
        return True

    async def release(self, lock_key: str, token: str) -> None:
        if self.locks.get(lock_key) == token:
            self.locks.pop(lock_key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class Search:
    def __init__(self, delay: float = 0) -> None:
        self.calls = 0
        self.delay = delay
        self.started = asyncio.Event()

    async def search(self, collection_id: str, text: str, limit: int = 5, filters=None):
        self.calls += 1
        self.started.set()
        await asyncio.sleep(self.delay)
        return [passage(collection_id)]

    async def read_document(self, collection_id: str, doc_id: str):
        self.calls += 1
        return [passage(collection_id)]


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, kind, payload, call_id=None, parent_call_id=None):
        self.events.append((kind, payload))
        return {"type": kind, "payload": payload}


def wrapper(base, cache, sink, **scope):
    complete = {
        "tenant_id": "tenant-a",
        "account_ids": ["acct-a"],
        "environment": "production",
        "snapshot_digest": "release-1",
        "resource_snapshot": {"manifest_sha256": "m1", "vector_dimension": 1024},
    }
    complete.update(scope)
    return CachedDocumentRetriever(base, cache, sink, complete, CacheMetrics())


async def test_hit_skips_base_and_re_emits_current_evidence():
    base, cache, sink = Search(), MemoryCache(), Sink()
    cached = wrapper(base, cache, sink)
    first = await cached.search("fixture", "same", 5, {"kind": "guide"})
    second = await cached.search("fixture", "same", 5, {"kind": "guide"})
    assert second == first and base.calls == 1
    assert sink.events[-1][0] == "knowledge.response"
    assert sink.events[-1][1]["cache"] == "hit"


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tenant-b"},
        {"account_ids": ["acct-b"]},
        {"environment": "sandbox"},
        {"snapshot_digest": "release-2"},
    ],
)
async def test_authorization_and_version_scope_are_part_of_key(change):
    base, cache = Search(), MemoryCache()
    await wrapper(base, cache, Sink()).search("fixture", "same", 5, {"kind": "guide"})
    await wrapper(base, cache, Sink(), **change).search("fixture", "same", 5, {"kind": "guide"})
    assert base.calls == 2


async def test_collection_filter_and_limit_are_part_of_key_and_read_is_passthrough():
    base, cache, sink = Search(), MemoryCache(), Sink()
    cached = wrapper(base, cache, sink)
    await cached.search("fixture", "same", 5, {"kind": "a"})
    await cached.search("fixture", "same", 6, {"kind": "a"})
    await cached.search("fixture", "same", 5, {"kind": "b"})
    await cached.search("other", "same", 5, {"kind": "a"})
    await cached.read_document("fixture", "KB-1")
    assert base.calls == 5


async def test_bad_json_and_cache_outage_fall_back_safely():
    base, cache, sink = Search(), MemoryCache(), Sink()
    cached = wrapper(base, cache, sink)
    key = cached._key("fixture", "same", 5, None)
    cache.values[key] = b"not json"
    assert await cached.search("fixture", "same") == [passage()]
    cache.available = False
    assert await cached.search("fixture", "another") == [passage()]
    assert base.calls == 2


async def test_twenty_concurrent_requests_share_one_slow_rebuild():
    # The original 200ms waiter window let real-model requests stampede.
    base, cache, sink = Search(0.45), MemoryCache(), Sink()
    cached = wrapper(base, cache, sink)
    results = await asyncio.gather(*(cached.search("fixture", "hot") for _ in range(20)))
    assert all(row == [passage()] for row in results)
    assert base.calls == 1


async def test_cancel_releases_only_its_own_lock():
    base, cache, sink = Search(60), MemoryCache(), Sink()
    cached = wrapper(base, cache, sink)
    task = asyncio.create_task(cached.search("fixture", "cancel"))
    await base.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache.locks == {}


async def test_stale_holder_cannot_publish_or_release_new_lock():
    cache = MemoryCache()
    assert await cache.acquire("k:lock", "old", 5)
    cache.locks["k:lock"] = "new"  # 模拟旧锁过期后新持有者已经取得锁。
    assert not await cache.publish("k", "k:lock", "old", b"old", 30)
    await cache.release("k:lock", "old")
    assert cache.locks["k:lock"] == "new" and "k" not in cache.values


async def test_cached_source_must_match_collection():
    base, cache, sink = Search(), MemoryCache(), Sink()
    cached = wrapper(base, cache, sink)
    key = cached._key("fixture", "same", 5, None)
    wrong = replace(passage(), source=replace(passage().source, collection_id="other"))
    cache.values[key] = json.dumps(
        {"schema_version": 1, "snapshot_digest": "release-1", "passages": [wrong.__dict__]},
        default=lambda value: value.__dict__,
    ).encode()
    assert await cached.search("fixture", "same") == [passage()]
    assert base.calls == 1
