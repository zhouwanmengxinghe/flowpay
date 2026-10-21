# 解析兼容接口的流式响应；保留原始帧，拼接文本及工具参数，拒绝不完整响应。
from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import TypeAdapter

from flowpay.core.config import ModelSettings
from flowpay.core.json import JsonObject, parse_json, require_object, require_text, to_json_object
from flowpay.core.llm.chat import ChunkSink

from .stream import ChatStreamAccumulator


@dataclass(frozen=True)
class ConnectionRetry:
    """下一次连接尝试的追踪数据，不包含认证头、代理地址或底层异常正文。"""

    error_type: str
    kind: Literal["retry"] = "retry"
    attempt: int = 2
    max_attempts: int = 2
    delay_ms: int = 250


class OpenAiChatModel:
    """基于官方 SDK 的聊天适配器，保留原始 SSE 数据与供应商扩展字段。"""

    capabilities: dict[str, bool] = {"tools": True, "streaming": True, "reasoning": True}

    def __init__(self, settings: ModelSettings, transport: httpx.AsyncBaseTransport | None = None):
        """保存模型配置和可选测试 transport；SDK 客户端在实际调用时创建并关闭。"""
        self.settings: ModelSettings = settings
        self.name: str = settings.name
        self.transport: httpx.AsyncBaseTransport | None = transport

    # 构造可记录的请求体；密钥仅在实际发送时用于认证头。
    def prepare(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
    ) -> JsonObject:
        """按 SDK 消息与工具类型构造实际请求体，返回 URL 与正文供追踪；不访问网络。"""
        body = {
            **self.settings.request_options,
            "model": self.name,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
        return {
            "provider": "openai-compatible",
            "url": self.settings.base_url.rstrip("/") + "/chat/completions",
            "body": body,
        }

    # 流式返回可能拆开工具名和 JSON 参数，须累积后再交给调用方。
    async def invoke(self, request: JsonObject, emit: ChunkSink) -> ChatCompletion:
        """用官方 SDK 发送 prepare 得到的请求，返回 SDK 的 ChatCompletion。

        SDK 自动重试关闭，仅连接建立失败时记录事件并额外尝试一次。原始响应接口保留
        供应商的 SSE data 载荷；适配层自行合并工具参数和扩展字段，避免类型转换丢失
        reasoning_content 等信息。只聚合第一个候选，原始 chunks 仍完整保留。
        流结束但缺少 finish_reason 视为中断，不把已经收到部分文本当作成功。
        """
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise ValueError(f"Missing environment variable: {self.settings.api_key_env}")
        accumulator = ChatStreamAccumulator()
        # HKU 网关使用订阅密钥头；仅向指定主机发送，避免影响其他兼容服务。
        headers: dict[str, str] = {}
        if urlsplit(self.settings.base_url).hostname == "api.hku.hk":
            headers["api-key"] = key
        # SDK 负责认证、请求和 HTTP 错误；禁用内部重试，避免一次记录隐藏多次请求。
        async with AsyncOpenAI(
            api_key=key,
            base_url=self.settings.base_url,
            default_headers=headers,
            timeout=self.settings.timeout_seconds,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=self.transport),
        ) as client:
            body = require_object(request["body"])
            TypeAdapter(list[ChatCompletionMessageParam]).validate_python(
                body["messages"], strict=True
            )
            messages = cast(list[ChatCompletionMessageParam], body["messages"])
            try:
                # 原始响应接口保留精确 SSE 数据；消息拼接仍由薄适配层负责。
                async with AsyncExitStack() as stack:
                    attempt = 1
                    while True:
                        try:
                            response = await stack.enter_async_context(
                                client.chat.completions.with_streaming_response.create(
                                    model=require_text(body["model"]),
                                    messages=messages,
                                    stream=True,
                                    extra_body={
                                        k: v
                                        for k, v in body.items()
                                        if k not in {"model", "messages", "stream"}
                                    },
                                )
                            )
                            break
                        except APIConnectionError as error:
                            # 读取或写入失败可能已触发生成，不能重放；只处理连接阶段。
                            cause = error.__cause__
                            if not isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)):
                                raise
                            if attempt == 2:
                                raise RuntimeError(
                                    "Model connection failed after 2 attempts; "
                                    + "check provider reachability, proxy and TLS settings "
                                    + f"({type(cause).__name__})"
                                ) from None
                            retry = ConnectionRetry(error_type=type(cause).__name__)
                            # 重试先落日志再等待；日志失败或等待被取消都不继续发送。
                            await emit(to_json_object(retry))
                            await asyncio.sleep(retry.delay_ms / 1000)
                            attempt += 1
                    # 流读取在重试循环之外，收到响应头之后任何中断都不能重放请求。
                    async for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        # 先保留供应商 data 载荷，再做 JSON 解析：即使下游解析失败，
                        # 仍能在日志里看到触发错误的原文。这里只记录 data 行，不保存认证头。
                        await emit({"kind": "wire", "data": data})
                        if data == "[DONE]":
                            break
                        content = accumulator.consume(require_object(parse_json(data)))
                        if content:
                            await emit({"kind": "content_delta", "text": content})
            except APIStatusError as error:
                await emit(
                    {
                        "kind": "http_error",
                        "status": error.status_code,
                        "body": (await error.response.aread()).decode(errors="replace"),
                    }
                )
                # 不向上层传播携带认证请求对象的 SDK 异常。
                raise RuntimeError(f"Model HTTP {error.status_code}") from None
        return accumulator.finish()
