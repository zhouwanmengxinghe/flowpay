"""Redis JSON 缓存；缓存故障只降低性能，不改变业务授权。"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass
class CacheMetrics:
    hits: int = 0
    misses: int = 0
    errors: int = 0
    fills: int = 0
    waits: int = 0
    bypasses: int = 0


class RedisJsonCache:
    """为检索缓存提供短超时、大小边界与持有者校验脚本。"""

    _PUBLISH = """
    if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    redis.call('DEL', KEYS[2])
    return 1
    """
    _RELEASE = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
    return 0
    """

    def __init__(self, url: str, timeout_s: float = 0.2, maximum_bytes: int = 262_144):
        self.client = Redis.from_url(
            url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s, decode_responses=False
        )
        self.maximum_bytes = maximum_bytes
        self.metrics = CacheMetrics()

    async def get(self, key: str) -> bytes | None:
        try:
            value = await self.client.get(key)
            if value is None:
                self.metrics.misses += 1
                return None
            if not isinstance(value, bytes) or len(value) > self.maximum_bytes:
                self.metrics.errors += 1
                await self.delete(key)
                return None
            self.metrics.hits += 1
            return value
        except Exception:
            self.metrics.errors += 1
            return None

    async def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        try:
            return bool(await self.client.set(key, token.encode(), nx=True, px=ttl_ms))
        except Exception:
            self.metrics.errors += 1
            return False

    async def publish(self, key: str, lock_key: str, token: str, value: bytes, ttl_s: int) -> bool:
        if len(value) > self.maximum_bytes:
            self.metrics.bypasses += 1
            await self.release(lock_key, token)
            return False
        try:
            written = await self.client.eval(self._PUBLISH, 2, key, lock_key, token, value, ttl_s)
            if written:
                self.metrics.fills += 1
            return bool(written)
        except Exception:
            self.metrics.errors += 1
            return False

    async def release(self, lock_key: str, token: str) -> None:
        try:
            await self.client.eval(self._RELEASE, 1, lock_key, token)
        except Exception:
            self.metrics.errors += 1

    async def delete(self, key: str) -> None:
        try:
            await self.client.delete(key)
        except Exception:
            self.metrics.errors += 1

    async def close(self) -> None:
        await self.client.aclose()
