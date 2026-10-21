"""缓存边界类型。"""

from __future__ import annotations

from typing import Protocol


class AsyncJsonCache(Protocol):
    async def get(self, key: str) -> bytes | None:
        """读取有界字节值；故障由实现转换为 miss。"""
        ...

    async def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        """尝试取得有期限的重建资格。"""
        ...

    async def publish(self, key: str, lock_key: str, token: str, value: bytes, ttl_s: int) -> bool:
        """仅当前持有者可以写值并释放锁。"""
        ...

    async def release(self, lock_key: str, token: str) -> None:
        """仅当前持有者释放锁。"""
        ...

    async def delete(self, key: str) -> None:
        """删除损坏值；失败时允许忽略。"""
        ...
