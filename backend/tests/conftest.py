"""测试统一从干净的配置环境出发。

`load_settings` 把 `FLOWPAY_<字段>` 当覆盖读，因此宿主机或容器里设的任何一个都会
渗进构造最小配置的用例——compose 给后端设了 FLOWPAY_EMBEDDING_BASE_URL，就足以让
三个用例失败。这里只清配置覆盖，数据集位置与数据库 DSN 保留，它们决定用例跳不跳。
"""

import os
from collections.abc import Iterator

import pytest

from flowpay.core.config.settings import ModelSettings, Settings
from flowpay.core.llm.embedding import EmbeddingSettings

# 数据集位置不清：它不是测试污染，而是"数据在哪"的唯一答案，决定用例跳不跳。
KEEP = frozenset(["FLOWPAY_DATASET_PATH"])

OVERRIDES = (
    frozenset(
        [
            *(f"FLOWPAY_{name.upper()}" for name in Settings.__dataclass_fields__),
            *(f"FLOWPAY_MODEL_{name.upper()}" for name in ModelSettings.__dataclass_fields__),
            *(
                f"FLOWPAY_EMBEDDING_{name.upper()}"
                for name in EmbeddingSettings.__dataclass_fields__
            ),
        ]
    )
    - KEEP
)


@pytest.fixture(autouse=True)
def isolated_configuration() -> Iterator[None]:
    """清掉配置覆盖变量；用例自己设的仍然生效，退出时恢复原值。"""
    saved = {name: os.environ.pop(name) for name in OVERRIDES if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
