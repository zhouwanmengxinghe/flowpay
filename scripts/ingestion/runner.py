"""构建命令的资源装配与报告输出；沿用项目事件库及单运行器锁。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Literal

from flowpay.core.config import Settings
from flowpay.core.event_log.runtime import open_store
from flowpay.core.event_log.store import new_id
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.types import TurnRecord
from flowpay.core.json import JsonObject, to_json_object
from flowpay.core.knowledge import PostgresVectorStore
from typing_extensions import TypedDict

from .corpus import CorpusSettings, prepare_corpus
from .models import KnowledgeBuildPlan
from .pipeline import KnowledgePreparation


class KnowledgePreparationReport(TypedDict):
    """一次准备命令的运行结果、计划及日志位置；失败状态仍保留原始检查点。"""

    passed: bool
    operation: Literal["build", "verify"]
    session_id: str
    plan: KnowledgeBuildPlan
    result: JsonObject
    last_run: TurnRecord
    log: str
    embedding_requests: int


async def run_preparation(
    settings: Settings,
    corpus_settings: CorpusSettings,
    operation: Literal["plan", "build", "verify"],
    output_dir: Path,
) -> JsonObject:
    embedding = settings.embedding
    if embedding is None:
        raise ValueError("Knowledge preparation requires embedding configuration")
    corpus = await asyncio.to_thread(
        prepare_corpus, settings.dataset_path, corpus_settings, embedding
    )
    if operation == "plan":
        return to_json_object(corpus.summary())
    # 目标集合可能尚未 ready；构建运行不把它当作启动前置资源。
    runtime_settings = replace(settings, knowledge_collection_id=None)
    output_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        store = stack.enter_context(open_store(runtime_settings))
        builder = PostgresVectorStore(os.environ[settings.knowledge_builder_url_env])
        stack.callback(builder.close)
        reader = PostgresVectorStore(os.environ[settings.knowledge_url_env])
        stack.callback(reader.close)
        workflow = KnowledgePreparation(
            corpus, embedding, builder, reader, settings.dataset_path, operation
        )
        manager = SessionManager(store, workflow, runtime_settings)
        try:
            await manager.recover()
            sid = (await manager.create_session())["id"]
            await manager.submit_message(
                sid,
                new_id("request"),
                f"s0 knowledge {operation}: {corpus.collection_id}",
            )
            print(
                json.dumps(
                    {
                        "operation": operation,
                        "session_id": sid,
                        "documents": len(corpus.documents),
                        "chunks": sum(corpus.expected_documents.values()),
                    }
                ),
                flush=True,
            )
            await manager.tasks[sid]
            session = await manager.get_session(sid)
            events = await asyncio.to_thread(store.read, sid)
            log = output_dir / f"{sid}.jsonl"
            log.write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            )
            report: KnowledgePreparationReport = {
                "passed": session["status"] == "completed",
                "operation": operation,
                "session_id": sid,
                "plan": corpus.summary(),
                "result": session["state"],
                "last_run": session["runs"][-1],
                "log": str(log),
                "embedding_requests": sum(
                    e["type"] == "embedding.request" for e in events
                ),
            }
            path = output_dir / f"{sid}-report.json"
            path.write_text(
                json.dumps(to_json_object(report), ensure_ascii=False, indent=2) + "\n"
            )
            print(
                json.dumps({"passed": report["passed"], "report": str(path)}),
                flush=True,
            )
            if not report["passed"]:
                raise RuntimeError(f"Knowledge {operation} failed; inspect {path}")
            return to_json_object(report)
        finally:
            await manager.shutdown()
