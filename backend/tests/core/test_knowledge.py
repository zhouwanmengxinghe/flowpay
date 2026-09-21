# 回归测试：知识存储与引用回查。测试中的固定输入用于复现边界，不作为业务答案来源。
import os
from dataclasses import replace
from pathlib import Path

import httpx
import psycopg
import pytest

from flowpay.core.agent.trace import Tracer
from flowpay.core.event_log import EventStore
from flowpay.core.event_log.store import new_id
from flowpay.core.json import (
    JsonObject,
    parse_json,
    require_list,
    require_object,
    require_text,
)
from flowpay.core.knowledge import Chunk, Document, PostgresVectorStore
from flowpay.core.knowledge.client import KnowledgeClient
from flowpay.core.llm.embedding import EmbeddingClient, EmbeddingSettings
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel

pytestmark = pytest.mark.skipif(not os.getenv("FLOWPAY_TEST_POSTGRES"), reason="Opt-in PostgreSQL")


@pytest.fixture
def stores():
    builder = PostgresVectorStore(os.environ["FLOWPAY_KNOWLEDGE_BUILDER_DSN"])
    reader = PostgresVectorStore(os.environ["FLOWPAY_KNOWLEDGE_DSN"])
    yield builder, reader
    builder.close()
    reader.close()


def model_settings():
    return EmbeddingSettings(
        name="fixture",
        base_url="https://embedding.test",
        dimensions=3,
        api_key_env="TEST_EMBEDDING_KEY",
        batch_size=1,
    )


def collection(store: PostgresVectorStore, settings: EmbeddingSettings):
    cid = new_id("test_knowledge")
    config: JsonObject = {"embedding": settings.identity(), "splitter": {"version": "fixture"}}
    store.create_collection(cid, "fixture", "a" * 64, config, settings.dimensions)
    store.create_collection(cid, "fixture", "a" * 64, config, settings.dimensions)
    return cid


async def test_build_resume_query_and_historical_evidence(
    stores: tuple[PostgresVectorStore, PostgresVectorStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    builder, reader = stores
    settings = model_settings()
    cid = collection(builder, settings)
    for name in ["alpha", "beta"]:
        doc = Document(name, name, name + ".md", name, {"kind": name})
        builder.save_document(cid, doc, [Chunk(1, name, {"section": name})])
        builder.save_document(cid, doc, [Chunk(1, name, {"section": name})])
    with pytest.raises(ValueError, match="differ"):
        builder.save_document(
            cid, Document("alpha", "alpha", "alpha.md", "changed"), [Chunk(1, "changed")]
        )
    calls: list[str] = []
    fail = True

    def handler(request: httpx.Request) -> httpx.Response:
        text = require_text(require_list(require_object(parse_json(request.content))["input"])[0])
        calls.append(text)
        if text == "beta" and fail:
            return httpx.Response(503, json={"error": "fixture failure"})
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1, 0, 0] if text == "alpha" else [0, 1, 0]}]},
        )

    events = EventStore(tmp_path / "trace.sqlite")
    tracer = Tracer(events, "build", "run")
    embeddings = EmbeddingClient(
        OpenAiEmbeddingModel(settings, httpx.MockTransport(handler)), tracer
    )
    client = KnowledgeClient(builder, embeddings, tracer)
    with pytest.raises(RuntimeError, match="503"):
        await client.embed_pending(cid)
    assert [r["doc_id"] for r in builder.missing_embeddings(cid)] == ["beta"]
    with pytest.raises(psycopg.Error):
        builder.mark_ready(cid, {"alpha": 1, "beta": 1})
    fail = False
    assert await client.embed_pending(cid) == 1
    assert calls.count("alpha") == 1
    with pytest.raises(ValueError, match="manifest"):
        builder.mark_ready(cid, {"alpha": 1})
    builder.mark_ready(cid, {"alpha": 1, "beta": 1})
    builder.mark_ready(cid, {"alpha": 1, "beta": 1})
    query_client = KnowledgeClient(reader, embeddings, tracer)
    rows = await query_client.search(cid, "alpha")
    assert rows[0].source.doc_id == "alpha"
    assert rows[0].score == pytest.approx(1)
    filtered = await query_client.search(cid, "alpha", filters={"kind": "beta"})
    assert filtered[0].source.doc_id == "beta"
    assert await query_client.search(cid, "alpha", filters={"kind": "absent"}) == []
    assert reader.read_passage(cid, "alpha", 1).document_text == "alpha"
    assert any(e["type"] == "knowledge.response" for e in events.read("build"))
    with reader.pool.connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("UPDATE chunks SET embedding=NULL WHERE collection_id=%s", (cid,))
    with pytest.raises(ValueError, match="immutable"):
        builder.save_document(cid, Document("new", "new", "new.md", "new"), [Chunk(1, "new")])
    # Same-dimensional but different model identities must be rejected before spending a request.
    wrong = replace(settings, name="another-model")
    wrong_client = KnowledgeClient(
        reader,
        EmbeddingClient(OpenAiEmbeddingModel(wrong, httpx.MockTransport(handler)), tracer),
        tracer,
    )
    before = len(calls)
    with pytest.raises(ValueError, match="configuration"):
        await wrong_client.search(cid, "alpha")
    assert len(calls) == before


async def test_wrong_input_batch_rolls_back(
    stores: tuple[PostgresVectorStore, PostgresVectorStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    builder, _ = stores
    settings = model_settings()
    cid = collection(builder, settings)
    builder.save_document(
        cid, Document("doc", "doc", "doc.md", "a b"), [Chunk(1, "a"), Chunk(2, "b")]
    )
    adapter = OpenAiEmbeddingModel(
        settings,
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 1, 1]}]})
        ),
    )
    trace = Tracer(EventStore(tmp_path / "trace.sqlite"), "s", "r")
    batch = await EmbeddingClient(adapter, trace).embed(["a", "WRONG"])
    with pytest.raises(ValueError, match="input differs"):
        builder.save_embeddings(cid, [("doc", 1), ("doc", 2)], batch)
    assert len(builder.missing_embeddings(cid)) == 2
