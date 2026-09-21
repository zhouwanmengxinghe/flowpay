"""只读数据源公共入口。"""

from .reader import DataSources, SourceClient, SourceResult, digest, timestamp, within

__all__ = ["DataSources", "SourceClient", "SourceResult", "digest", "within", "timestamp"]
