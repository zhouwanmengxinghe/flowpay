"""有界缓存能力；业务授权和检索策略由调用方负责。"""

from .redis_cache import CacheMetrics, RedisJsonCache

__all__ = ["CacheMetrics", "RedisJsonCache"]
