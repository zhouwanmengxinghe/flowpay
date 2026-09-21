# 回归测试：基座测试所需的独立模拟对象。测试中的固定输入用于复现边界，不作为业务答案来源。
from __future__ import annotations

import asyncio
from collections.abc import Sequence

from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, ChatCompletionToolParam

from flowpay.core.config import Settings
from flowpay.core.execution.contracts import TurnInput, TurnResult
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard
from flowpay.core.json import JsonObject
from flowpay.core.llm.chat import ChunkSink
from flowpay.core.tools import Tool


class FixtureAdapter:
    name: str = "test-model"
    capabilities: dict[str, bool] = {"tools": True, "streaming": True, "reasoning": False}

    def __init__(self, delay: float, fail: bool) -> None:
        self.delay: float = delay
        self.fail: bool = fail

    def prepare(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
    ) -> JsonObject:
        return {
            "provider": "test",
            "body": {"model": self.name, "messages": messages, "tools": tools},
        }

    async def invoke(self, request: JsonObject, emit: ChunkSink) -> ChatCompletion:
        _ = request
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("test model failure")
        await emit({"kind": "content_delta", "text": "fixture"})
        return ChatCompletion.model_validate(
            {
                "id": "fixture",
                "object": "chat.completion",
                "model": self.name,
                "created": 0,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "fixture"},
                    }
                ],
            }
        )


class FixtureWorkflow:
    id: str = "fixture"
    version: str = "1"
    card: StageCard = StageCard(label="Fixture", description="Isolated contract test")

    def __init__(self, settings: Settings) -> None:
        self.settings: Settings = settings

    async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
        async def lookup(arguments: JsonObject) -> JsonObject:
            if "/tool-error" in turn.user_message and not turn.resumed:
                raise RuntimeError("test tool failure")
            return {"input": arguments["input"]}

        toolbox = runtime.toolbox(
            [
                Tool(
                    "lookup",
                    "Generic fixture lookup",
                    {
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                        "required": ["input"],
                        "additionalProperties": False,
                    },
                    lookup,
                )
            ]
        )
        model = FixtureAdapter(
            self.settings.mock_delay_ms / 1000,
            "/model-error" in turn.user_message and not turn.resumed,
        )
        _, call_id = await runtime.chat(model).call(
            [
                {"role": "system", "content": "test"},
                {"role": "user", "content": turn.user_message},
            ],
            toolbox.definitions(),
        )
        await toolbox.execute("lookup", {"input": turn.user_message}, call_id=call_id)
        return TurnResult(
            "fixture reply",
            {"version": 1},
            "waiting_user" if turn.user_message == "question" else "completed",
        )
