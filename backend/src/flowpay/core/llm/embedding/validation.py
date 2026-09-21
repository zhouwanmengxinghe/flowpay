"""纯向量与文本摘要校验，不执行网络或数据库操作。"""

import hashlib
import math
from typing import cast


def validate_vector(vector: object, dimensions: int) -> None:
    """验证单个向量可用于当前集合的相似度计算。

    要求长度匹配、元素为有限数值且范数非零；bool 虽是 Python int 子类仍拒绝。
    零向量不能进行有意义的余弦比较，必须在写入或查询前阻止。
    """
    if not isinstance(vector, list):
        raise ValueError("Embedding dimension mismatch")
    values = cast(list[object], vector)
    if len(values) != dimensions:
        raise ValueError("Embedding dimension mismatch")
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in values
    ) or not any(v != 0 for v in values):
        raise ValueError("Embedding must contain finite numbers and have nonzero norm")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
