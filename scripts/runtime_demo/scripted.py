# 确定性模拟适配器，用于复现流式、工具与错误场景，不调用外部模型。
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from copy import deepcopy

from flowpay.core.json import JsonObject, require_text
from flowpay.core.llm.chat import ChunkSink
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from openai.types.chat.chat_completion import Choice


class ScriptedAdapter:
    """用预设原生消息模拟模型，提供可重复的基座验收输入。

    文本每八个字符发一个片段，delay_ms 可放大取消和断线的观察窗口。
    fail 用于主动触发错误；不会请求网络，也不生成真实 token 用量。
    模拟能力只验证运行协议，不能用于宣称业务回答质量。
    """

    name: str = "foundation-demo-v1"

    def __init__(self, message: JsonObject, delay_ms: int = 0, fail: bool = False):
        """保存预设 assistant 消息、分片延迟和故障开关；不创建网络客户端。"""
        self.message: JsonObject = message
        self.delay_ms: int = delay_ms
        self.fail: bool = fail
        self.capabilities: dict[str, bool] = {
            "tools": True,
            "streaming": True,
            "reasoning": False,
        }

    def prepare(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
    ) -> JsonObject:
        """使用标准 messages 和 tools 构造带 simulation 标记的测试请求，供运行日志检查。"""
        return {
            "provider": "scripted",
            "simulation": True,
            "body": {
                "model": self.name,
                "messages": messages,
                "tools": tools,
                "stream": True,
            },
        }

    async def invoke(self, request: JsonObject, emit: ChunkSink) -> ChatCompletion:
        """按预设延迟输出文本或工具片段，返回 SDK ChatCompletion；fail 为真时主动抛错。"""
        _ = request
        if self.fail:
            raise RuntimeError("模拟模型错误：用于验证失败记录和显式继续")
        native = deepcopy(self.message)
        text = require_text(native.get("content") or "")
        for offset in range(0, len(text), 8):
            await asyncio.sleep(self.delay_ms / 1000)
            await emit(
                {
                    "kind": "content_delta",
                    "text": text[offset : offset + 8],
                    "simulation": True,
                }
            )
        if calls := native.get("tool_calls"):
            await asyncio.sleep(self.delay_ms / 1000)
            await emit({"kind": "tool_calls", "tool_calls": calls, "simulation": True})
        return ChatCompletion.model_construct(
            object="chat.completion",
            model=self.name,
            choices=[
                Choice.model_validate(
                    {
                        "index": 0,
                        "message": native,
                        "finish_reason": "tool_calls"
                        if native.get("tool_calls")
                        else "stop",
                    }
                )
            ],
        )
