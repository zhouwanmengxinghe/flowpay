# 回归测试：数据源白名单与只读边界。测试中的固定输入用于复现边界，不作为业务答案来源。
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from flowpay.core.config import Settings
from flowpay.core.data_sources import DataSources, digest
from flowpay.core.data_sources.policy import SourcePolicy
from flowpay.core.event_log import EventStore
from flowpay.core.execution.contracts import TurnInput, TurnResult
from flowpay.core.execution.errors import Conflict
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.resources import RuntimeResources
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard
from flowpay.core.json import require_object, require_text
from flowpay.core.knowledge import Collection


@pytest.fixture
def source(tmp_path: Path) -> DataSources:
    database = tmp_path / "business.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE objects(id TEXT,account TEXT,value TEXT)")
        db.execute("INSERT INTO objects VALUES('one','a','visible')")
        db.execute("CREATE TABLE audit(answer TEXT)")
        db.execute("INSERT INTO audit VALUES('hidden')")
    log = tmp_path / "requests.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(
                {"id": str(i), "at": f"2026-09-01T0{i}:00:00Z", "status": 503, "hidden": "answer"}
            )
            for i in range(3)
        )
        + "\n"
    )
    plain = tmp_path / "service.log"
    plain.write_text("ready\nfailed\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": {p.name: {"sha256": digest(p)} for p in [database, log, plain]}})
    )
    policy = {
        "release_id": "test",
        "manifest": "manifest.json",
        "manifest_sha256": digest(manifest),
        "sql": {
            "lookup": {
                "path": database.name,
                "relation": "objects",
                "scope": {},
                "filters": {"id": "id"},
                "select": ["id", "value"],
                "order_by": "id",
                "columns": {"objects": ["id", "value"]},
            }
        },
        "logs": {
            "requests": {
                "path": log.name,
                "format": "jsonl",
                "time_field": "at",
                "filter_fields": ["id", "status"],
                "result_fields": ["id", "at", "status"],
            },
            "plain": {"path": plain.name, "format": "text"},
        },
    }
    return DataSources(tmp_path, policy)


def test_sql_filter_values_are_bound_not_interpolated(source: DataSources) -> None:
    """条件值始终是绑定参数；未登记的关系名和过滤字段都会被拒绝。"""
    assert source.sql("lookup", {}, {"id": "one"})["rows"] == [{"id": "one", "value": "visible"}]
    assert source.sql("lookup", {}, {"id": "one' OR 1=1 --"})["rows"] == []
    with pytest.raises(ValueError, match="registered"):
        source.sql("SELECT * FROM audit", {})
    with pytest.raises(ValueError, match="Unsupported SQL filter"):
        source.sql("lookup", {}, {"value": "visible"})
    with pytest.raises(ValueError, match="Scope must give"):
        source.sql("lookup", {"account_id": "a"}, {"id": "one"})


def test_authorizer_denies_unregistered_tables_even_from_the_policy(
    source: DataSources,
) -> None:
    """关系与 select 是策略文件里的原始 SQL 文本，最后一道防线仍是授权器。"""
    before = digest(source.root / "business.sqlite")
    for field, value in [
        ("relation", "audit"),
        ("relation", "sqlite_master"),
        ("relation", "objects; UPDATE objects SET value='changed'"),
        ("select", ["load_extension('missing')"]),
        ("select", ["(SELECT answer FROM audit)"]),
    ]:
        policy = TypeAdapter(SourcePolicy).validate_json(json.dumps(source.policy))
        policy.get("sql", {})["lookup"][field] = value
        with pytest.raises(sqlite3.DatabaseError):
            DataSources(source.root, policy).sql("lookup", {}, {"id": "one"})
    assert digest(source.root / "business.sqlite") == before


def test_logs_filter_projection_range_and_limits(source: DataSources) -> None:
    result = source.logs(
        "requests", start="2026-09-01T00:00:00Z", end="2026-09-01T02:00:00Z", limit=1
    )
    assert result["truncated"]
    assert result["rows"][0]["line"] == 1
    assert "hidden" not in require_object(result["rows"][0]["record"])
    assert source.logs("requests", contains="answer")["rows"] == []
    assert source.logs("requests", filters={"id": "missing"})["rows"] == []
    assert source.logs("plain", contains="failed")["rows"] == [
        {"line": 2, "record": {"text": "failed"}}
    ]
    with pytest.raises(ValueError):
        source.logs("requests", filters={"hidden": "answer"})
    with pytest.raises(ValueError):
        source.logs("../audit/answers.jsonl")


def test_manifest_file_and_path_changes_are_rejected(source: DataSources, tmp_path: Path) -> None:
    policy = TypeAdapter(SourcePolicy).validate_json(json.dumps(source.policy))
    policy.setdefault("logs", {})["escape"] = {"path": "../outside.jsonl"}
    with pytest.raises(ValueError, match="relative"):
        DataSources(source.root, policy)
    outside = tmp_path.parent / "outside-source-test"
    # A symlink to a directory outside root is refused before reading its contents.
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    policy.setdefault("logs", {})["escape"] = {
        "path": "escape/" + tmp_path.name + "/requests.jsonl"
    }
    # Resolving back inside root is okay; a final outside file is not.
    policy.setdefault("logs", {})["escape"] = {"path": "escape/" + outside.name}
    outside.write_text("not agent data")
    try:
        with pytest.raises(ValueError, match="escapes"):
            DataSources(source.root, policy)
    finally:
        outside.unlink()
    (source.root / "requests.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="changed"):
        source.logs("requests")
    with pytest.raises(ValueError, match="hash mismatch"):
        source.verify()


async def test_run_pins_versions_and_records_source_results(
    source: DataSources, tmp_path: Path
) -> None:
    class Workflow:
        id: str = "source-test"
        version: str = "1"
        card: StageCard = StageCard(label="Sources", description="Fixture")

        async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
            _ = turn
            assert runtime.sources is not None
            result = await runtime.sources.query_sql(
                "lookup", {}, runtime.tracer, filters={"id": "one"}
            )
            return TurnResult(require_text(result["rows"][0]["value"]), {})

    settings = Settings(tmp_path, tmp_path)
    manager = SessionManager(EventStore(tmp_path / "sessions.sqlite"), Workflow(), settings)
    manager.resources = RuntimeResources(settings, source, manager.resources.knowledge)
    sid = (await manager.create_session())["id"]
    await manager.submit_message(sid, "one", "question")
    await manager.tasks[sid]
    session = await manager.get_session(sid)
    assert "config" in session["runs"][0]
    assert session["runs"][0]["config"]["data_sources"] == source.snapshot()
    assert any(e["type"] == "source.response" for e in manager.store.read(sid))
    policy = TypeAdapter(SourcePolicy).validate_json(json.dumps(source.policy))
    policy["release_id"] = "different"
    manager.resources = RuntimeResources(settings, DataSources(source.root, policy))
    with pytest.raises(Conflict, match="version changed"):
        await manager.submit_message(sid, "two", "next")
    assert len((await manager.get_session(sid))["runs"]) == 1


def test_source_configuration_is_relative(tmp_path: Path) -> None:
    from flowpay.core.config import load_settings

    config = tmp_path / "config.toml"
    config.write_text('data_dir="."\ndataset_path="."\nsources_config="access.toml"\n')
    settings = load_settings(config)
    assert settings.sources_config == tmp_path / "access.toml"
    assert settings.snapshot()["sources_config"] == str(tmp_path / "access.toml")


async def test_knowledge_version_must_match_sources(
    source: DataSources, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    class Workflow:
        id: str = "pin-test"
        version: str = "1"
        card: StageCard = StageCard(label="Pin", description="Fixture")

        async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
            _ = turn, runtime
            return TurnResult("ok", {})

    collection: Collection = {
        "created_at": datetime.now(UTC),
        "ready_at": datetime.now(UTC),
        "collection_id": "kb",
        "status": "ready",
        "dataset_release_id": "wrong",
        "dataset_manifest_sha256": source.policy["manifest_sha256"],
        "dimensions": 3,
        "build_config": {},
    }

    class Store:
        def __init__(self, dsn: str) -> None:
            _ = dsn

        def get_collection(self, cid: str) -> Collection:
            _ = cid
            return collection

        def close(self):
            pass

    monkeypatch.setenv("TEST_KNOWLEDGE_DSN", "fixture")
    monkeypatch.setattr("flowpay.core.knowledge.PostgresVectorStore", Store)
    settings = replace(
        Settings(tmp_path, tmp_path),
        knowledge_collection_id="kb",
        knowledge_url_env="TEST_KNOWLEDGE_DSN",
    )
    manager = SessionManager(EventStore(tmp_path / "sessions.sqlite"), Workflow(), settings)
    manager.resources = RuntimeResources(settings, source, manager.resources.knowledge)
    sid = (await manager.create_session())["id"]
    with pytest.raises(Conflict, match="versions differ"):
        await manager.submit_message(sid, "first", "question")
    assert (await manager.get_session(sid))["runs"] == []
    collection["dataset_release_id"] = source.policy["release_id"]
    await manager.submit_message(sid, "first", "question")
    await manager.tasks[sid]
    run = (await manager.get_session(sid))["runs"][0]
    assert "config" in run
    assert require_object(run["config"]["knowledge"])["collection_id"] == "kb"


async def test_runtime_resources_reuses_reader_and_closes_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多轮仅刷新版本快照，不重新连接知识库；停机时统一释放。"""
    from flowpay.core import knowledge
    from tests.core.fixtures import FixtureWorkflow

    opened: list[str] = []
    reads: list[str] = []
    closed: list[bool] = []

    class Reader:
        def __init__(self, dsn: str) -> None:
            opened.append(dsn)

        def get_collection(self, cid: str) -> Collection:
            reads.append(cid)
            return {
                "created_at": datetime.now(UTC),
                "ready_at": datetime.now(UTC),
                "status": "ready",
                "collection_id": cid,
                "dataset_release_id": "test",
                "dataset_manifest_sha256": "a" * 64,
                "build_config": {},
                "dimensions": 3,
            }

        def close(self):
            closed.append(True)

    monkeypatch.setattr(knowledge, "PostgresVectorStore", Reader)
    settings = Settings(tmp_path, tmp_path, mock_delay_ms=0, knowledge_collection_id="docs")
    monkeypatch.setenv(settings.knowledge_url_env, "test-reader")
    manager = SessionManager(
        EventStore(tmp_path / "events.sqlite"), FixtureWorkflow(settings), settings
    )
    try:
        sid = (await manager.create_session())["id"]
        for i in range(2):
            await manager.submit_message(sid, str(i), "question")
            await manager.tasks[sid]
        assert opened == ["test-reader"]
        assert reads == ["docs", "docs"]
        assert not closed
    finally:
        await manager.shutdown()
    assert closed == [True]
