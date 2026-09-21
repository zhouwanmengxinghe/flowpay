# 回归测试：嵌入批处理与错误记录。测试中的固定输入用于复现边界，不作为业务答案来源。
import json
from pathlib import Path

import httpx
import pytest

from flowpay.core.agent.trace import Tracer
from flowpay.core.event_log import EventStore
from flowpay.core.json import JsonObject, parse_json, require_list, require_object
from flowpay.core.llm.embedding import EmbeddingClient, EmbeddingSettings
from flowpay.core.llm.embedding.openai_embedding import OpenAiEmbeddingModel
from flowpay.core.llm.embedding.settings import same_vector_space


def settings(*, request_options: JsonObject | None = None) -> EmbeddingSettings:
    return EmbeddingSettings(
        name="fixture",
        base_url="https://embedding.test/v1",
        dimensions=3,
        api_key_env="TEST_EMBEDDING_KEY",
        batch_size=2,
        request_options=request_options or {},
    )


async def test_embedding_batches_order_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret-value")
    requests: list[JsonObject] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = require_object(parse_json(request.content))
        requests.append(body)
        assert request.headers["authorization"] == "Bearer secret-value"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [i + 1, 0, 1]}
                    for i in reversed(range(len(require_list(body["input"]))))
                ],
                "usage": {"total_tokens": 7},
            },
        )

    store = EventStore(tmp_path / "events.sqlite")
    tracer = Tracer(store, "build", "run")
    client = EmbeddingClient(OpenAiEmbeddingModel(settings(), httpx.MockTransport(handler)), tracer)
    batch = await client.embed(["甲", "乙", "丙"])
    assert batch.vectors == [[1, 0, 1], [2, 0, 1], [1, 0, 1]]
    assert [r["input"] for r in requests] == [["甲", "乙"], ["丙"]]
    events = store.read("build")
    assert [e["type"] for e in events] == [
        "embedding.request",
        "embedding.raw",
        "embedding.response",
    ] * 2
    assert "secret-value" not in json.dumps(events)
    assert (
        require_object(require_list(require_object(events[2]["payload"]["raw"])["data"])[0])[
            "index"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "response",
    [
        {"data": []},
        {"data": [{"index": 0, "embedding": [1, 2]}]},
        {"data": [{"index": 0, "embedding": [0, 0, 0]}]},
        {"data": [{"index": 0, "embedding": [True, 1, 2]}]},
        {"data": [{"index": 1, "embedding": [1, 2, 3]}]},
    ],
)
async def test_invalid_embedding_never_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: JsonObject
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    store = EventStore(tmp_path / "events.sqlite")
    adapter = OpenAiEmbeddingModel(
        settings(), httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    )
    with pytest.raises(ValueError):
        await EmbeddingClient(adapter, Tracer(store, "s", "r")).embed(["text"])
    assert store.read("s")[-1]["type"] == "embedding.error"
    assert not any(e["type"] == "embedding.response" for e in store.read("s"))


async def test_embedding_http_error_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    store = EventStore(tmp_path / "events.sqlite")
    adapter = OpenAiEmbeddingModel(
        settings(), httpx.MockTransport(lambda _: httpx.Response(429, json={"error": "rate limit"}))
    )
    with pytest.raises(RuntimeError, match="429"):
        await EmbeddingClient(adapter, Tracer(store, "s", "r")).embed(["text"])
    assert store.read("s")[1]["payload"]["status"] == 429


def test_embedding_configuration_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from flowpay.core.config import load_settings

    config = tmp_path / "config.toml"
    config.write_text(
        'data_dir="."\ndataset_path="."\n[embedding]\n'
        + 'name="fixture"\nbase_url="https://embedding.test"\n'
        + 'dimensions=3\napi_key_env="TEST_EMBEDDING_KEY"\n'
    )
    monkeypatch.setenv("FLOWPAY_EMBEDDING_BATCH_SIZE", "4")
    result = load_settings(config)
    assert result.embedding is not None
    assert result.embedding.batch_size == 4
    assert require_object(result.snapshot()["embedding"])["dimensions"] == 3


async def test_canceled_embedding_has_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("Canceled handler must not complete")

    store = EventStore(tmp_path / "trace.sqlite")
    client = EmbeddingClient(
        OpenAiEmbeddingModel(settings(), httpx.MockTransport(handler)), Tracer(store, "s", "r")
    )
    task = asyncio.create_task(client.embed(["text"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["type"] for e in store.read("s")] == ["embedding.request", "embedding.canceled"]


async def test_sdk_embedding_request_and_no_hidden_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(500, json={"error": "temporary failure"})

    adapter = OpenAiEmbeddingModel(
        settings(request_options={"dimensions": 3, "vendor_option": "kept"}),
        httpx.MockTransport(handler),
    )
    recorded = adapter.prepare(["文本"])
    raw: list[JsonObject] = []

    async def emit(event: JsonObject) -> None:
        raw.append(event)

    with pytest.raises(RuntimeError, match="Embedding HTTP 500"):
        await adapter.invoke(recorded, emit)
    assert len(attempts) == 1
    assert str(attempts[0].url) == recorded["url"]
    assert parse_json(attempts[0].content) == recorded["body"]
    assert raw == [{"status": 500, "body": '{"error":"temporary failure"}'}]


def test_local_ollama_endpoints_share_one_vector_space() -> None:
    """同一台机器上的 Ollama，换个入口名不该被判成换了部署。

    宿主机写 127.0.0.1，容器里只能写 host.docker.internal，compose 的
    ollama profile 又叫 ollama。模型版本由 revision 的摘要钉死，端点主机名
    再参与比较，同一份集合换个入口读就要重建。
    """

    def settings(host: str) -> EmbeddingSettings:
        return EmbeddingSettings(
            provider="openai-compatible",
            name="qwen3-embedding:0.6b",
            base_url=f"http://{host}:11434/v1",
            api_key_env="OLLAMA_API_KEY",
            dimensions=1024,
            revision="digest",
        )

    current = settings("host.docker.internal").identity()
    # 历史集合里存的是未规范化的旧写法，比较时两边都要过一遍。
    assert same_vector_space({**current, "base_url": "http://127.0.0.1:11434/v1"}, current)
    assert same_vector_space(settings("localhost").identity(), current)
    assert same_vector_space(settings("ollama").identity(), current)
    # 端口和外部主机仍然算不同部署。
    assert not same_vector_space({**current, "base_url": "http://127.0.0.1:9999/v1"}, current)
    assert not same_vector_space(settings("10.0.0.5").identity(), current)
