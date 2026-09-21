"""数据集位置的唯一解析点。

数据集不随仓库分发：解包 flowpay-dataset-v1.tar.gz 后，把 FLOWPAY_DATASET_ROOT
指向解出来的 dataset 目录。业务数据在它的 data/ 子目录，题库在 benchmark/，
生成器在 src/。测试、脚本和 benchmark harness 都从这里取路径。

FLOWPAY_DATASET_PATH 是 Settings.dataset_path 的通用覆盖名，指向 data/ 本身；
配置了它而没配 ROOT 时按父目录反推，两种写法都能用。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_VARIABLE = "FLOWPAY_DATASET_ROOT"
PATH_VARIABLE = "FLOWPAY_DATASET_PATH"

MISSING = (
    f"{ROOT_VARIABLE} 未设置。数据集不随仓库分发：解包 flowpay-dataset-v1.tar.gz 后，"
    f"把 {ROOT_VARIABLE} 指向解出来的 dataset 目录（见 README 的“准备数据集”）。"
)


def dataset_root() -> Path | None:
    """解包出来的 dataset 目录；两个变量都没配时返回 None。"""
    if value := os.getenv(ROOT_VARIABLE):
        return Path(value).expanduser().resolve()
    if value := os.getenv(PATH_VARIABLE):
        return Path(value).expanduser().resolve().parent
    return None


def dataset_path() -> Path | None:
    """业务数据根目录，即 dataset/data；未配置时返回 None。"""
    root = dataset_root()
    return root / "data" if root else None


# 随包分发、必须存在的三样；缺任何一样都说明指错了目录或解包不全。
EXPECTED = ("data/knowledge", "benchmark/design", "src/flowpay_simulator")

INCOMPLETE = (
    "{root} 不像一份完整的数据集，缺少：{missing}。"
    "请确认 {variable} 指向的是解包出来的 dataset 目录本身。"
)


def missing_parts(root: Path) -> list[str]:
    """登记项里缺了哪几样；完整时返回空列表。"""
    return [name for name in EXPECTED if not (root / name).exists()]


def require_dataset() -> Path:
    """取 dataset 目录；没配置或目录不完整都带着修复方法退出。

    只检查结构，不校验哈希——文件内容由 sources.toml 的 manifest 和知识集合的
    发布清单各自把关。这里拦的是"指错目录"和"解包不全"，让它们立刻失败，
    而不是等到检索返回空、业务查询却正常时才发现半份数据集。
    """
    root = dataset_root()
    if root is None:
        raise SystemExit(MISSING)
    if missing := missing_parts(root):
        raise SystemExit(
            INCOMPLETE.format(root=root, missing="、".join(missing), variable=ROOT_VARIABLE)
        )
    return root


def snapshot_ready() -> bool:
    """数据集完整且业务快照已经生成；任一不满足时 s3/s4 与相关测试无法运行。"""
    root = dataset_root()
    if root is None or missing_parts(root):
        return False
    return (root / "data/generated/canonical/flowpay.sqlite").exists()
