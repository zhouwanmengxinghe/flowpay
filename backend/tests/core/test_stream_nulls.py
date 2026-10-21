"""回归 HKU 流式响应中的显式 null；所有请求均由内存 transport 返回。"""

import json
import unittest
from unittest.mock import patch

import httpx

from flowpay.core.config import ModelSettings
from flowpay.core.json import JsonObject
from flowpay.core.llm.chat.openai_chat import OpenAiChatModel
from flowpay.core.llm.chat.stream import ChatStreamAccumulator


class NullableStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_hku_greeting_preserves_role_text_and_wire(self) -> None:
        frames: list[JsonObject] = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
            {
                "choices": [{"index": 0, "delta": {
                    "role": None, "content": "你好", "tool_calls": None,
                    "reasoning_content": None,
                }}],
                "usage": None,
            },
            {"choices": [{"index": 0, "delta": {
                "role": None, "content": None, "tool_calls": None,
            }, "finish_reason": "stop"}]},
        ]

        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["api-key"], "fixture-key")
            wire = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
            return httpx.Response(200, text=wire + "data: [DONE]\n\n")

        model = OpenAiChatModel(
            ModelSettings(
                base_url="https://api.hku.hk/foundry/student/models",
                api_key_env="TEST_MODEL_KEY",
            ),
            httpx.MockTransport(respond),
        )
        chunks: list[JsonObject] = []

        async def emit(chunk: JsonObject) -> None:
            chunks.append(chunk)

        with patch.dict("os.environ", {"TEST_MODEL_KEY": "fixture-key"}):
            reply = await model.invoke(model.prepare([], []), emit)
        message = reply.choices[0].message
        self.assertEqual(message.role, "assistant")
        self.assertEqual(message.content, "你好")
        self.assertIsNone(message.tool_calls)
        self.assertEqual(reply.choices[0].finish_reason, "stop")
        self.assertIn({"kind": "content_delta", "text": "你好"}, chunks)
        self.assertEqual(
            [chunk["data"] for chunk in chunks if chunk.get("kind") == "wire"],
            [*(json.dumps(frame) for frame in frames), "[DONE]"],
        )

    def test_null_does_not_erase_fragmented_tool_calls(self) -> None:
        stream = ChatStreamAccumulator()
        for delta in [
            {"role": "assistant", "tool_calls": []},
            {"tool_calls": [{"index": 0, "id": "call_one", "type": "function",
                             "function": {"name": "lookup", "arguments": '{"id":'}}]},
            {"role": None, "tool_calls": None, "content": None},
            {"tool_calls": [{"index": 0, "id": None, "type": None,
                             "function": {"name": None, "arguments": '"pay_1"}'}}]},
            {"tool_calls": [{"index": 0, "function": None}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": None}}]},
        ]:
            stream.consume({"choices": [{"index": 0, "delta": delta}]})
        stream.consume({"choices": [{"index": 0, "delta": {},
                                     "finish_reason": "tool_calls"}]})
        message = stream.finish().choices[0].message
        self.assertEqual(message.role, "assistant")
        calls = message.tool_calls
        assert calls and calls[0].type == "function"
        self.assertEqual(calls[0].function.name, "lookup")
        self.assertEqual(calls[0].function.arguments, '{"id":"pay_1"}')

    def test_non_null_invalid_tool_calls_still_fail(self) -> None:
        for value in [False, {}, ""]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Expected a list"):
                ChatStreamAccumulator().consume({"choices": [{"delta": {"tool_calls": value}}]})

    def test_non_null_invalid_function_fields_still_fail(self) -> None:
        for function in [False, "", [], {"name": False}, {"arguments": {}}]:
            with self.subTest(function=function), self.assertRaises(ValueError):
                ChatStreamAccumulator().consume({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": function}
                ]}}]})
