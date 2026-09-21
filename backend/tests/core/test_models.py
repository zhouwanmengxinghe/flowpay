# 回归测试：流式模型适配。测试中的固定输入用于复现边界，不作为业务答案来源。
import json
from pathlib import Path

import httpx
import pytest

from flowpay.core.agent.trace import Tracer
from flowpay.core.config import ModelSettings
from flowpay.core.event_log import EventStore
from flowpay.core.json import (
    JsonObject,
    parse_json,
    require_list,
    require_object,
    require_text,
)
from flowpay.core.llm.chat.client import ChatClient
from flowpay.core.llm.chat.openai_chat import OpenAiChatModel


async def test_wire_stream_and_tool_argument_fragments_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")
    frames: list[JsonObject] = [
        {
            "id": "provider-completion",
            "created": 123,
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "reasoning_content": "公开摘要"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_one",
                                "function": {"name": "get_request", "arguments": '{"id":'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"req_1"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"total_tokens": 100}},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-secret"
        assert (
            require_object(
                require_object(
                    require_list(require_object(parse_json(request.content))["tools"])[0]
                )["function"]
            )["name"]
            == "get_request"
        )
        content = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(
            200, text=content + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    adapter = OpenAiChatModel(
        ModelSettings(
            provider="openai-compatible",
            name="fixture",
            api_key_env="TEST_MODEL_KEY",
        ),
        httpx.MockTransport(handler),
    )
    request = adapter.prepare(
        [{"role": "system", "content": "instructions"}],
        [
            {"type": "function", "function": {"name": "get_request"}},
        ],
    )
    assert "test-secret" not in json.dumps(request)
    chunks: list[JsonObject] = []

    async def emit(chunk: JsonObject) -> None:
        chunks.append(chunk)

    reply = await adapter.invoke(request, emit)
    assert reply.id == "provider-completion"
    assert reply.model == "fixture"
    assert reply.created == 123
    tool_calls = reply.choices[0].message.tool_calls
    assert tool_calls and tool_calls[0].type == "function"
    assert reply.choices[0].message.model_extra is not None
    assert reply.usage is not None
    assert tool_calls[0].function.arguments == '{"id":"req_1"}'
    assert reply.choices[0].message.model_extra["reasoning_content"] == "公开摘要"
    assert reply.usage.total_tokens == 100
    assert [
        parse_json(require_text(c["data"]))
        for c in chunks
        if c.get("kind") == "wire" and c["data"] != "[DONE]"
    ] == frames
    assert tool_calls[0].function.name == "get_request"
    assert chunks[-1]["data"] == "[DONE]"


async def test_missing_finish_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"),
        httpx.MockTransport(lambda _: httpx.Response(200, text="data: [DONE]\n\n")),
    )

    async def emit(chunk: JsonObject) -> None:
        _ = chunk

    with pytest.raises(RuntimeError, match="without finish_reason"):
        await adapter.invoke(adapter.prepare([], []), emit)


def test_provider_options_are_recorded_without_overriding_context() -> None:
    settings = ModelSettings(request_options={"max_tokens": 2048, "thinking": {"type": "disabled"}})
    request = OpenAiChatModel(settings).prepare([{"role": "user", "content": "Hi"}], [])
    assert require_object(request["body"])["thinking"] == {"type": "disabled"}
    assert require_object(request["body"])["max_tokens"] == 2048
    assert require_object(request["body"])["messages"] == [{"role": "user", "content": "Hi"}]
    for key in ("messages", "tools", "stream", "stream_options", "model"):
        with pytest.raises(ValueError, match="reserved"):
            ModelSettings(request_options={key: None})


async def test_http_error_keeps_status_and_body_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")
    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"),
        httpx.MockTransport(lambda _: httpx.Response(429, json={"error": "rate limited"})),
    )
    chunks: list[JsonObject] = []

    async def emit(chunk: JsonObject) -> None:
        chunks.append(chunk)

    with pytest.raises(RuntimeError, match="Model HTTP 429"):
        await adapter.invoke(adapter.prepare([], []), emit)
    assert chunks[0]["status"] == 429
    assert parse_json(require_text(chunks[0]["body"])) == {"error": "rate limited"}
    assert "test-secret" not in json.dumps(chunks)


@pytest.mark.parametrize("status", [429, 500])
async def test_sdk_request_matches_trace_and_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # SDK 不应改写扩展参数，也不能让一次 request 事件对应多次 HTTP 尝试。
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(status, json={"error": "temporary failure"})

    adapter = OpenAiChatModel(
        ModelSettings(
            name="compatible-model",
            base_url="https://provider.test/custom/v1",
            api_key_env="TEST_MODEL_KEY",
            request_options={"thinking": {"type": "enabled"}, "max_tokens": 99},
        ),
        httpx.MockTransport(handler),
    )
    recorded = adapter.prepare(
        [{"role": "user", "content": "hello"}],
        [{"type": "function", "function": {"name": "lookup"}}],
    )

    async def emit(chunk: JsonObject) -> None:
        _ = chunk

    with pytest.raises(RuntimeError, match=f"Model HTTP {status}"):
        await adapter.invoke(recorded, emit)
    assert len(attempts) == 1
    assert str(attempts[0].url) == recorded["url"]
    assert parse_json(attempts[0].content) == recorded["body"]
    assert attempts[0].headers["x-stainless-retry-count"] == "0"


async def test_sdk_timeout_records_failure_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"), httpx.MockTransport(handler)
    )
    store = EventStore(tmp_path / "timeout.sqlite")
    with pytest.raises(Exception, match="timed out"):
        await ChatClient(adapter, Tracer(store, "s", "r")).call([])
    assert len(attempts) == 1
    events = store.read("s")
    assert [e["type"] for e in events] == ["model.request", "model.error"]
    assert events[-1]["payload"]["error_type"] == "APITimeoutError"


async def test_model_text_events_are_normalized_without_losing_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    frames: list[JsonObject] = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "你好", "reasoning_content": "分析"},
                    "finish_reason": "stop",
                }
            ]
        }
    ]
    wire = "data: " + json.dumps(frames[0]) + "\n\ndata: [DONE]\n\n"
    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"),
        httpx.MockTransport(lambda _: httpx.Response(200, text=wire)),
    )
    store = EventStore(tmp_path / "events.sqlite")
    reply, call_id = await ChatClient(adapter, Tracer(store, "s", "r")).call([])
    events = store.read("s")
    assert [e["payload"] for e in events if e["type"] == "model.delta"] == [{"content": "你好"}]
    assert [
        parse_json(require_text(e["payload"]["data"]))
        for e in events
        if e["type"] == "model.chunk"
        and e["payload"].get("kind") == "wire"
        and e["payload"]["data"] != "[DONE]"
    ] == frames
    assert reply.choices[0].message.tool_calls is None
    assert all(e["call_id"] == call_id for e in events)
    assert (
        require_object(
            require_object(
                require_list(require_object(events[-1]["payload"]["completion"])["choices"])[0]
            )["message"]
        )["content"]
        == "你好"
    )
    assert reply.choices[0].message.model_extra is not None
    assert reply.choices[0].message.model_extra["reasoning_content"] == "分析"
    assert any(e["type"] == "model.chunk" and e["payload"]["data"] == "[DONE]" for e in events)


@pytest.mark.parametrize("arguments", ["{", "[]"])
async def test_protocol_tool_arguments_fail_with_a_record_before_execution(
    tmp_path: Path, arguments: str
) -> None:
    from openai.types.chat import ChatCompletionMessageFunctionToolCall

    from flowpay.core.tools import Tool, Toolbox

    executed: list[JsonObject] = []

    async def lookup(parameters: JsonObject) -> JsonObject:
        executed.append(parameters)
        return {"found": True}

    store = EventStore(tmp_path / "tools.sqlite")
    toolbox = Toolbox(
        [Tool("lookup", "查找", {"type": "object"}, lookup)], Tracer(store, "session", "run")
    )
    tool_call = ChatCompletionMessageFunctionToolCall.model_validate(
        {
            "id": "provider-tool",
            "type": "function",
            "function": {"name": "lookup", "arguments": arguments},
        }
    )
    with pytest.raises(ValueError):
        await toolbox.run(tool_call, parent_call_id="local-model")
    assert executed == []
    events = store.read("session")
    assert [event["type"] for event in events] == ["tool.started", "tool.error"]
    assert events[0]["payload"]["arguments"] == arguments
    assert events[0]["parent_call_id"] == "local-model"
    assert all(event["call_id"] == "provider-tool" for event in events)
