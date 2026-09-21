"""启动确定性 HTTP/SSE 验收服务，不调用真实模型或课程工作流。"""

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

import uvicorn
from flowpay.core.config import ModelSettings, Settings, load_settings
from flowpay.core.http import create_app

# 同时支持 python scripts/demo.py 与 python -m scripts.demo。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.runtime_demo.workflow import DemoWorkflow

ROOT = Path(__file__).resolve().parents[1]


class Arguments(argparse.Namespace):
    """模拟服务参数；显式存储覆盖供隔离 PostgreSQL 回归使用。"""

    config: Path = ROOT / "backend/configs/local.toml"


def demo_settings(settings: Settings) -> Settings:
    """解除真实依赖并固定模拟身份与隔离目录，不受日常工作流环境变量影响。"""
    return replace(
        settings,
        workflow="demo",
        model=ModelSettings(),
        data_dir=ROOT / "var/demo",
        knowledge_collection_id=None,
        embedding=None,
        sources_config=None,
    )


def configured_runtime(args: Arguments) -> Settings:
    """先隔离模拟资源，再应用显式存储参数；不隐式继承日常 PostgreSQL 选择。"""
    settings = demo_settings(load_settings(args.config))
    return settings


def main() -> None:
    """为界面联调和 HTTP/SSE 验收启动单进程模拟服务器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Arguments.config)
    args = parser.parse_args(namespace=Arguments())
    settings = configured_runtime(args)
    logging.basicConfig(level=settings.log_level)
    app = create_app(settings, DemoWorkflow(settings))
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
