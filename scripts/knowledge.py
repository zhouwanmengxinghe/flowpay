"""s0 业务知识准备：plan 检查输入，build 幂等构建，verify 检查真实检索与引用。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Literal

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowpay.core.config import load_settings

from scripts.ingestion.corpus import CorpusSettings
from scripts.ingestion.runner import run_preparation

ROOT = Path(__file__).resolve().parents[1]


class Arguments(argparse.Namespace):
    """本命令解析后的参数类型，供调用入口读取。"""

    operation: Literal["plan", "build", "verify"] = "plan"
    config: Path = ROOT / "backend/configs/local.toml"
    corpus: Path = ROOT / "backend/configs/knowledge.toml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "build", "verify"])
    parser.add_argument(
        "--config", type=Path, default=ROOT / "backend/configs/local.toml"
    )
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / "backend/configs/knowledge.toml"
    )
    args = parser.parse_args(namespace=Arguments())
    result = asyncio.run(
        run_preparation(
            load_settings(args.config),
            CorpusSettings.load(args.corpus),
            args.operation,
            ROOT / "var/knowledge-build",
        )
    )
    if args.operation == "plan":
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
