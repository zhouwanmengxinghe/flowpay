"""依赖安装后的数据库、嵌入模型与知识集合检查；不创建聊天模型。"""

from __future__ import annotations

import argparse
import asyncio
import os
from urllib.parse import urlsplit

import httpx
import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from flowpay.core.config import Settings, load_settings
from flowpay.core.json import (
    parse_json,
    require_list,
    require_object,
    to_json_object,
)
from flowpay.core.knowledge import PostgresVectorStore
from flowpay.core.llm.embedding.ollama import ollama_identity

from scripts.development.environment import ROOT, PreparationError, execute
from scripts.ingestion.corpus import CorpusSettings, prepare_corpus
from scripts.ingestion.runner import run_preparation


def check_databases() -> None:
    """只读验证迁移版本、运行连接和所需权限；不写入测试会话。"""
    head = ScriptDirectory.from_config(
        Config(str(ROOT / "backend/alembic.ini"))
    ).get_current_head()
    targets = {
        "FLOWPAY_KNOWLEDGE_ADMIN_DSN": ("collections", "documents", "chunks"),
        "FLOWPAY_KNOWLEDGE_DSN": ("collections", "documents", "chunks"),
        "FLOWPAY_KNOWLEDGE_BUILDER_DSN": ("collections", "documents", "chunks"),
    }
    for variable, tables in targets.items():
        dsn = os.environ.get(variable)
        if not dsn:
            raise PreparationError(f"缺少 {variable}；请补齐 .env 后运行 make setup。")
        try:
            with psycopg.connect(
                dsn,
                connect_timeout=5,
                options="-c default_transaction_read_only=on -c statement_timeout=5000",
            ) as db:
                if "ADMIN" in variable:
                    row = db.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()
                    if row is None or row[0] != head:
                        raise PreparationError(
                            "数据库迁移未完成；请运行 make db-setup。"
                        )
                for table in tables:
                    row = db.execute(
                        "SELECT has_table_privilege(current_user,%s,'SELECT')", (table,)
                    ).fetchone()
                    if row != (True,):
                        raise PreparationError(
                            f"{variable} 缺少读取权限；请运行 make db-setup。"
                        )
                if variable == "FLOWPAY_KNOWLEDGE_BUILDER_DSN":
                    for table in tables:
                        if db.execute(
                            "SELECT has_table_privilege(current_user,%s,'INSERT')",
                            (table,),
                        ).fetchone() != (True,):
                            raise PreparationError(
                                "知识构建账号缺少写入权限；请运行 make db-setup。"
                            )
                    for table, column in (
                        ("chunks", "embedding"),
                        ("collections", "status"),
                        ("collections", "ready_at"),
                    ):
                        if db.execute(
                            "SELECT has_column_privilege(current_user,%s,%s,'UPDATE')",
                            (table, column),
                        ).fetchone() != (True,):
                            raise PreparationError(
                                "知识构建账号缺少更新权限；请运行 make db-setup。"
                            )
        except psycopg.Error:
            raise PreparationError(
                f"{variable} 连接或结构检查失败。检查数据库服务、原有密码与 DSN；"
                + "结构未初始化时运行 make db-setup。不会自动修改已有密码。"
            ) from None
    print("✓ 知识库迁移、连接及账号权限可用", flush=True)


async def check_embedding(settings: Settings, download: bool) -> None:
    """检查配置指向的 Ollama；只在明确指定选项且模型缺失时下载，不替换已有模型。"""
    embedding = settings.embedding
    if embedding is None:
        raise PreparationError("local.toml 未配置 embedding。")
    endpoint = urlsplit(embedding.base_url)
    origin = f"{endpoint.scheme}://{endpoint.netloc}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{origin}/api/tags")
            response.raise_for_status()
        models = require_list(require_object(parse_json(response.content))["models"])
        installed = any(
            require_object(model).get("name") == embedding.name for model in models
        )
        if not installed:
            if not download:
                raise PreparationError(
                    f"缺少嵌入模型 {embedding.name}。下载可能数百 MB；"
                    + "执行 make setup SETUP_ARGS=--download-model。"
                )
            if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise PreparationError(
                    "自动下载仅支持本机 Ollama；请在配置的远程服务上准备模型。"
                )
            execute(
                ["ollama", "pull", embedding.name],
                {**os.environ, "OLLAMA_HOST": origin},
                "下载嵌入模型",
            )
        await ollama_identity(embedding)
    except httpx.HTTPError:
        raise PreparationError(
            "Ollama 无法连接；请启动 Ollama，并检查 local.toml 的 embedding.base_url。"
        ) from None
    except ValueError:
        raise PreparationError(
            "嵌入模型 digest 与 local.toml 不匹配；请核对固定版本，不要覆盖旧知识集合。"
        ) from None
    print("✓ 嵌入模型已安装，版本身份匹配", flush=True)


async def prepare_knowledge(settings: Settings, build: bool) -> None:
    """核对来源、配置与就绪状态；匹配则跳过，未完成则复用原幂等构建流程。"""
    corpus_settings = CorpusSettings.load(ROOT / "backend/configs/knowledge.toml")
    if corpus_settings.collection_id != settings.knowledge_collection_id:
        raise PreparationError(
            "local.toml 与 knowledge.toml 的集合 ID 不一致；请先统一配置。"
        )
    if settings.embedding is None:
        raise PreparationError("未配置嵌入模型。")
    try:
        corpus = prepare_corpus(
            settings.dataset_path, corpus_settings, settings.embedding
        )
    except (OSError, ValueError, KeyError):
        raise PreparationError(
            "知识来源文件缺失或哈希不匹配；请恢复 dataset 发布文件，"
            + "再运行 scripts/knowledge.py plan 检查。"
        ) from None
    store = PostgresVectorStore(os.environ[settings.knowledge_url_env])
    try:
        try:
            collection = store.get_collection(corpus.collection_id)
        except KeyError:
            collection = None
        if collection is not None:
            if (
                collection["build_config"] != to_json_object(corpus.build_config)
                or collection["dataset_release_id"] != corpus.release_id
                or collection["dataset_manifest_sha256"]
                != corpus.canonical_manifest_sha256
                or collection["dimensions"] != settings.embedding.dimensions
            ):
                raise PreparationError(
                    "已有知识集合与当前来源或模型配置不一致。保留旧集合，请使用新的集合 ID 构建。"
                )
            if collection["status"] == "ready":
                print("✓ 知识集合已就绪且配置匹配，跳过构建与向量生成", flush=True)
                return
    finally:
        store.close()
    if not build:
        raise PreparationError(
            "知识集合不存在或尚未完成；请停止占用同一数据库的后端后执行 make setup。"
        )
    print(
        "… 准备知识集合；仅补齐缺失文档与向量，请先停止占用同一数据库的后端。",
        flush=True,
    )
    try:
        await run_preparation(
            settings, corpus_settings, "build", ROOT / "var/knowledge-build"
        )
    except Exception:  # noqa: BLE001 -- CLI 边界统一脱敏，避免底层异常泄漏连接凭据。
        raise PreparationError(
            "知识构建未完成；检查后端是否占用运行锁及 var/knowledge-build 报告，"
            + "修复后可重跑 make setup。"
        ) from None
    print("✓ 知识集合构建完成", flush=True)


async def run(operation: str, knowledge: bool, download: bool) -> None:
    """按依赖顺序检查，失败时停止下游步骤，模型密钥只检查是否配置。"""
    check_databases()
    settings = load_settings(ROOT / "backend/configs/local.toml")
    if knowledge:
        await check_embedding(settings, download)
        await prepare_knowledge(settings, operation == "setup")
        key = settings.model.api_key_env
        if not os.environ.get(key):
            raise PreparationError(
                f"基础设施已就绪，但缺少 {key}；在 .env 中填写后运行 make doctor，无需重建数据库。"
            )
        print("✓ 聊天模型密钥已配置（未联网验证、未调用付费模型）")
    else:
        print("— 已跳过 RAG 模型与知识集合准备")
    print(
        "\n基础环境已准备完成。\n启动后端：make rag"
        if knowledge
        else "\n已跳过知识准备，可启动模拟验收：make demo"
    )
    print("启动前端：make frontend")


class Arguments(argparse.Namespace):
    """内部检查命令的类型化参数。"""

    operation: str = "doctor"
    skip_knowledge: bool = False
    download_model: bool = False


def main() -> None:
    """内部命令入口，只输出经过整理的故障提示，不泄漏底层连接异常。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["setup", "doctor"])
    parser.add_argument("--skip-knowledge", action="store_true")
    parser.add_argument("--download-model", action="store_true")
    args = parser.parse_args(namespace=Arguments())
    try:
        asyncio.run(
            run(
                str(args.operation),
                not bool(args.skip_knowledge),
                bool(args.download_model),
            )
        )
    except PreparationError as error:
        print(f"✗ {error}\n— 后续准备或检查已跳过。")
        raise SystemExit(1) from None
    except Exception:  # noqa: BLE001 -- CLI 边界统一脱敏，避免底层异常泄漏连接凭据。
        print("✗ 环境检查失败；请核对配置和服务。底层连接异常未输出，以免暴露凭据。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
