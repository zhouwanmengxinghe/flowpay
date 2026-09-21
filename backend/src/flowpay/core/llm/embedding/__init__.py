"""嵌入能力公共入口。"""

from .client import EmbeddingClient
from .contracts import EmbeddedBatch, EmbeddingModel, EmbeddingReply
from .settings import EmbeddingIdentity, EmbeddingSettings
from .validation import text_hash, validate_vector

__all__ = [
    "EmbeddingSettings",
    "EmbeddingIdentity",
    "EmbeddingReply",
    "EmbeddedBatch",
    "EmbeddingModel",
    "EmbeddingClient",
    "text_hash",
    "validate_vector",
]
