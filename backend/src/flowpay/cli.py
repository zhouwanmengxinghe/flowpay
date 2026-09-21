"""加载公共连接配置，按命令选择工作流并启动 HTTP 服务。"""

import argparse
import logging
from dataclasses import replace
from pathlib import Path

import uvicorn

from flowpay.bootstrap import create_workflow
from flowpay.core.config import Settings, load_settings
from flowpay.core.http import create_app
from flowpay.router import resolve_stage


class Arguments(argparse.Namespace):
    """命令行选择覆盖环境配置。"""

    config: Path | None = None
    stage: str | None = None


def configured_runtime(args: Arguments) -> Settings:
    """显式阶段优先于配置和环境，保留公共连接设置。"""
    settings = load_settings(args.config)
    stage = resolve_stage(args.stage or settings.workflow)
    return replace(settings, workflow=stage)


def app_factory() -> object:
    """给 `uvicorn --factory` 用的入口：配置全部来自环境变量，便于容器里热重载。

    `main` 走参数解析后直接持有 app 对象，没法配合 --reload（它要求导入字符串）。
    两条路径共用 configured_runtime，阶段与配置的解析口径一致。
    """
    settings = configured_runtime(Arguments())
    logging.basicConfig(level=settings.log_level)
    return create_app(settings, create_workflow(settings))


def main() -> None:
    """启动单进程课程服务；默认 s1，模拟验收有独立脚本入口。"""
    parser = argparse.ArgumentParser(description="FlowPay local teaching runtime")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--stage", help="课程阶段，目前支持 s1、s2、s3")
    args = parser.parse_args(namespace=Arguments())
    try:
        settings = configured_runtime(args)
        workflow = create_workflow(settings)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=settings.log_level)
    app = create_app(settings, workflow)
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, timeout_graceful_shutdown=5)
