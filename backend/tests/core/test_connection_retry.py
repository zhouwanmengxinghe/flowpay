"""用真实 SDK 与受控传输复现连接错误，保证可见、有限且可取消的重试边界。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import override

import httpx
import pytest
from openai import APIConnectionError

from flowpay.core.agent.trace import Tracer
from flowpay.core.config import ModelSettings
from flowpay.core.event_log import EventStore
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.json import JsonObject
from flowpay.core.llm.chat.client import ChatClient
from flowpay.core.llm.chat.openai_chat import OpenAiChatModel

COMPLETE = (
    'data: {"choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":"stop"}]}'
    + "\n\ndata: [DONE]\n\n"
)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
@pytest.mark.parametrize("recovers", [True, False])
async def test_connection_failure_has_one_visible_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[httpx.RequestError],
    recovers: bool,
) -> None:
    """仅连接失败重试；成功与持续失败都保留同一个调用 ID 和准确尝试次数。"""
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    attempts: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """首次模拟握手故障，第二次可选择恢复或继续故障。"""
        attempts.append(request)
        if len(attempts) == 1 or not recovers:
            raise failure("private-proxy-detail", request=request)
        return httpx.Response(200, text=COMPLETE)

    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"), httpx.MockTransport(respond)
    )
    store = EventStore(tmp_path / "retry.sqlite")
    runtime = AgentRuntime(Tracer(store, "s", "r"))
    if recovers:
        reply, _ = await runtime.chat(adapter).call([])
        assert reply.choices[0].message.content == "你好"
    else:
        with pytest.raises(RuntimeError, match="after 2 attempts.*proxy and TLS"):
            await runtime.chat(adapter).call([])
    events = store.read("s")
    assert len(attempts) == 2 and attempts[0].content == attempts[1].content
    assert all(r.headers["x-stainless-retry-count"] == "0" for r in attempts)
    assert len({e["call_id"] for e in events}) == 1
    assert events[0]["type"] == "model.request"
    assert events[1]["type"] == "model.retry"
    assert events[1]["payload"] == {
        "kind": "retry",
        "attempt": 2,
        "max_attempts": 2,
        "delay_ms": 250,
        "error_type": failure.__name__,
    }
    assert events[-1]["type"] == ("model.response" if recovers else "model.error")
    assert "private-proxy-detail" not in str(events) and "secret" not in str(events)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.WriteError])
async def test_errors_after_sending_are_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[httpx.RequestError],
) -> None:
    """尚无响应头也可能已发送生成请求，读取超时和写入错误不能重放。"""
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    attempts: list[httpx.Request] = []
    chunks: list[JsonObject] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """在收到响应头前模拟非连接错误。"""
        attempts.append(request)
        raise failure("transport failed", request=request)

    async def emit(chunk: JsonObject) -> None:
        """收集适配器事件，检查没有产生重试。"""
        chunks.append(chunk)

    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"), httpx.MockTransport(respond)
    )
    with pytest.raises(APIConnectionError):
        await adapter.invoke(adapter.prepare([], []), emit)
    assert len(attempts) == 1 and not chunks


class BrokenStream(httpx.AsyncByteStream):
    """响应头已经返回，正文输出一段后中断。"""

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        """故意使用 ConnectError 验证捕获范围仅限建立连接阶段。"""
        yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ConnectError("stream interrupted")


async def test_partial_stream_is_never_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保留部分输出和失败事件，不能发起第二次请求或把它当作完整回答。"""
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    attempts: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """返回会在正文中断开的 HTTP 200 响应。"""
        attempts.append(request)
        return httpx.Response(200, stream=BrokenStream())

    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"), httpx.MockTransport(respond)
    )
    store = EventStore(tmp_path / "stream.sqlite")
    with pytest.raises(httpx.ConnectError):
        await ChatClient(adapter, Tracer(store, "s", "r")).call([])
    kinds = [e["type"] for e in store.read("s")]
    assert len(attempts) == 1
    assert "model.delta" in kinds and kinds[-1] == "model.error"
    assert "model.retry" not in kinds and "model.response" not in kinds


@pytest.mark.parametrize("cancel", [True, False])
async def test_retry_wait_can_be_canceled_and_trace_failure_stops_sending(
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    """重试事件持久化失败或等待期间取消时，都只发生第一次网络尝试。"""
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    attempts: list[httpx.Request] = []
    entered = asyncio.Event()

    def respond(request: httpx.Request) -> httpx.Response:
        """建立连接失败，尚未发生模型生成。"""
        attempts.append(request)
        raise httpx.ConnectError("offline", request=request)

    async def emit(chunk: JsonObject) -> None:
        """在重试边界通知测试，或模拟事件存储故障。"""
        assert chunk["kind"] == "retry"
        entered.set()
        if not cancel:
            raise OSError("event storage failed")

    adapter = OpenAiChatModel(
        ModelSettings(api_key_env="TEST_MODEL_KEY"), httpx.MockTransport(respond)
    )
    task = asyncio.create_task(adapter.invoke(adapter.prepare([], []), emit))
    await asyncio.wait_for(entered.wait(), timeout=3)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(OSError, match="event storage failed"):
            await task
    assert len(attempts) == 1
