"""精确输入、版本化且按授权范围隔离的检索缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import uuid
from datetime import UTC, datetime

from pydantic import TypeAdapter

from flowpay.core.cache.contracts import AsyncJsonCache
from flowpay.core.cache.redis_cache import CacheMetrics
from flowpay.core.event_log.contracts import EventSink
from flowpay.core.event_log.store import new_id
from flowpay.core.json import JsonObject, to_json_object

from .contracts import DocumentRetriever, Passage


class CachedDocumentRetriever:
    """包装检索接口；命中时仍为当前运行生成证据事件。"""

    def __init__(
        self,
        base: DocumentRetriever,
        cache: AsyncJsonCache,
        tracer: EventSink,
        scope: JsonObject,
        metrics: CacheMetrics | None = None,
        rebuilds: asyncio.Semaphore | None = None,
    ):
        self.base = base
        self.cache = cache
        self.tracer = tracer
        self.scope = scope
        self.metrics = metrics
        # 由应用工厂共享，保证上限针对整个 Agent 进程，而不是单次运行。
        self._rebuilds = rebuilds or asyncio.Semaphore(2)

    def _key(self, collection_id: str, text: str, limit: int, filters: JsonObject | None) -> str:
        material = {
            "schema_version": 1,
            **self.scope,
            "collection_id": collection_id,
            "text": text,
            "limit": limit,
            "filters": filters or {},
        }
        encoded = json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return "flowpay:kb:v1:" + hashlib.sha256(encoded).hexdigest()

    async def _read(self, key: str, collection_id: str) -> list[Passage] | None:
        raw = await self.cache.get(key)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if payload.get("schema_version") != 1 or payload.get(
                "snapshot_digest"
            ) != self.scope.get("snapshot_digest"):
                raise ValueError("Cached snapshot differs")
            passages = TypeAdapter(list[Passage]).validate_python(payload.get("passages"))
            if any(item.source.collection_id != collection_id for item in passages):
                raise ValueError("Cached passage belongs to another collection")
            return passages
        except Exception:
            if self.metrics:
                self.metrics.errors += 1
            await self.cache.delete(key)
            return None

    async def _emit_hit(self, collection_id: str, passages: list[Passage]) -> None:
        await self.tracer.emit(
            "knowledge.response",
            {
                "collection_id": collection_id,
                "results": [to_json_object(item) for item in passages],
                "cache": "hit",
            },
            new_id("retrieval"),
        )

    async def search(
        self, collection_id: str, text: str, limit: int = 5, filters: JsonObject | None = None
    ) -> list[Passage]:
        # A cache connection error should bypass quickly; a healthy cache whose
        # lock is held should give the in-flight rebuild time to publish.
        cache_metrics = getattr(self.cache, "metrics", None)
        errors_before = getattr(cache_metrics, "errors", None)
        key = self._key(collection_id, text, limit, filters)
        hit = await self._read(key, collection_id)
        if hit is not None:
            await self._emit_hit(collection_id, hit)
            return hit
        lock_key, token = key + ":lock", uuid.uuid4().hex
        acquired = await self.cache.acquire(lock_key, token, 5000)
        if not acquired:
            if self.metrics:
                self.metrics.waits += 1
            # 首次真实嵌入可能超过 200ms；等待最多约 2.3s，仍短于 5s 锁租期。
            # Redis 不可用时由 RedisJsonCache.errors 检出并立即 fail-open。
            for delay in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.4):
                await asyncio.sleep(delay + random.random() * 0.01)
                current_errors = getattr(cache_metrics, "errors", None)
                if (
                    errors_before is not None
                    and current_errors is not None
                    and current_errors > errors_before
                ):
                    break
                hit = await self._read(key, collection_id)
                if hit is not None:
                    await self._emit_hit(collection_id, hit)
                    return hit
                current_errors = getattr(cache_metrics, "errors", None)
                if (
                    errors_before is not None
                    and current_errors is not None
                    and current_errors > errors_before
                ):
                    break
            async with self._rebuilds:
                return await self.base.search(collection_id, text, limit, filters)
        try:
            hit = await self._read(key, collection_id)
            if hit is not None:
                await self.cache.release(lock_key, token)
                await self._emit_hit(collection_id, hit)
                return hit
            async with self._rebuilds:
                result = await self.base.search(collection_id, text, limit, filters)
            payload = {
                "schema_version": 1,
                "snapshot_digest": self.scope.get("snapshot_digest"),
                "cached_at": datetime.now(UTC).isoformat(),
                "passages": [to_json_object(item) for item in result],
            }
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            ttl = (15 + random.randint(0, 5)) if not result else (300 + random.randint(0, 60))
            await self.cache.publish(key, lock_key, token, raw, ttl)
            return result
        except BaseException:
            await self.cache.release(lock_key, token)
            raise

    async def read_document(self, collection_id: str, doc_id: str) -> list[Passage]:
        return await self.base.read_document(collection_id, doc_id)
