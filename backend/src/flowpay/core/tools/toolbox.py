"""模型可见的工具集合与带追踪的执行入口。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import perf_counter
from typing import cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from openai.types.chat import ChatCompletionMessageFunctionToolCall, ChatCompletionToolParam

from flowpay.core.event_log.contracts import EventSink
from flowpay.core.json import JsonObject, parse_json, require_object

from .contracts import Tool, ToolInputError


class Toolbox:
    """一轮运行可以使用的工具，以及它们的执行与记录。

    definitions 给模型看名称、说明和参数结构；run 执行模型给出的调用，并把工具名、
    参数、结果或失败关联到发起它的那次模型响应。名称未知或参数不符抛 ToolInputError，
    阶段可以把它当作一次可纠正的观察回传给模型；工具内部故障继续上抛，不伪装成正常返回。
    """

    def __init__(self, tools: list[Tool], tracer: EventSink) -> None:
        """建立名称到工具的映射并绑定本轮追踪；名称重复时拒绝构造。"""
        self.tools: dict[str, Tool] = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("Duplicate tool names")
        self.tracer: EventSink = tracer

    def definitions(self) -> list[ChatCompletionToolParam]:
        """按注册顺序返回模型可见的工具定义，不执行任何工具。"""
        return [tool.definition() for tool in self.tools.values()]

    async def run(
        self, tool_call: ChatCompletionMessageFunctionToolCall, *, parent_call_id: str
    ) -> JsonObject:
        """执行 SDK 返回的一次 function 调用，返回工具结果。

        沿用模型给出的 tool_call.id 作为 call_id，之后的 tool 消息才能与
        assistant.tool_calls 对上。function.arguments 按协议是 JSON 字符串，
        在这里解码并校验，阶段无需重复解析协议。
        """
        return await self.execute(
            tool_call.function.name,
            tool_call.function.arguments,
            call_id=tool_call.id,
            parent_call_id=parent_call_id,
        )

    # 先记录工具名和参数；执行失败也保留关联事件，便于定位模型与工具的关系。
    async def execute(
        self,
        name: str,
        arguments: JsonObject | str,
        call_id: str,
        parent_call_id: str | None = None,
    ) -> JsonObject:
        """按名称执行工具；参数解码与结构校验都在记录之后进行。"""
        start = perf_counter()
        await self.tracer.emit(
            "tool.started", {"name": name, "arguments": arguments}, call_id, parent_call_id
        )
        try:
            result = await self._dispatch(name, arguments)
            await self.tracer.emit(
                "tool.result",
                {
                    "name": name,
                    "result": result,
                    "duration_ms": round((perf_counter() - start) * 1000),
                },
                call_id,
            )
            return result
        except asyncio.CancelledError:
            await self.tracer.emit("tool.canceled", {"name": name}, call_id)
            raise
        except Exception as error:
            await self.tracer.emit(
                "tool.error",
                {"name": name, "error_type": type(error).__name__, "message": str(error)},
                call_id,
            )
            raise

    async def _dispatch(self, name: str, arguments: JsonObject | str) -> JsonObject:
        """查找工具、校验模型给出的参数，再调用异步处理函数。"""
        tool = self.tools.get(name)
        if tool is None:
            raise ToolInputError(f"Unknown tool: {name}")
        try:
            parsed = (
                require_object(parse_json(arguments)) if isinstance(arguments, str) else arguments
            )
        except (ValueError, TypeError) as error:
            raise ToolInputError("工具参数必须是合法的 JSON 对象。") from error
        # jsonschema 的动态验证器未完整声明方法参数；仅在库边界标明其调用签名。
        validate = cast(Callable[[object], None], Draft202012Validator(tool.parameters).validate)
        try:
            validate(parsed)
        except ValidationError as error:
            raise ToolInputError(error.message) from error
        return await tool.execute(parsed)
