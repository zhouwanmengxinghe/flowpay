"""带追踪的聊天调用：请求先落库，再访问供应商。"""

import asyncio
from collections.abc import Sequence
from time import perf_counter
from typing import Literal

from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, ChatCompletionToolParam

from flowpay.core.event_log.contracts import EventSink
from flowpay.core.event_log.store import new_id
from flowpay.core.json import JsonObject

from .contracts import ChatModel


class ChatClient:
    """对一个聊天模型的有记录调用入口。

    prepare 只构造请求、不访问网络，因此请求可以先落库：事件里看到的就是供应商
    实际收到的内容，工具定义也在其中。请求落库失败时不调用模型；响应落库失败时
    不向调用方返回成功。取消与异常分别记录后继续上抛，本类不做自动重试。
    """

    def __init__(self, model: ChatModel, tracer: EventSink) -> None:
        """绑定模型与本轮追踪出口；连接由模型实现自己管理。"""
        self.model: ChatModel = model
        self.tracer: EventSink = tracer

    async def call(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam] | None = None,
        *,
        output: Literal["answer", "internal"] = "answer",
    ) -> tuple[ChatCompletion, str]:
        """执行一次模型请求，返回 SDK 响应与本地追踪 call_id。

        Args:
            messages: 本轮实际交给模型的全部上下文。
            tools: SDK 工具声明；None 表示这次不提供工具。
            output: internal 表示这次是内部判断，界面不把它当作回答展示。

        Returns:
            SDK 的 ChatCompletion（保留供应商扩展字段）与本地 call_id。
            call_id 不覆盖 completion.id，调用方用它关联后续工具事件。
        """
        call_id, start = new_id("model"), perf_counter()
        request = self.model.prepare(messages, tools or [])
        await self.tracer.emit(
            "model.request",
            {"request": request, "capabilities": self.model.capabilities, "output": output},
            call_id,
        )

        async def chunk(data: JsonObject) -> None:
            if data.get("kind") == "content_delta":
                await self.tracer.emit("model.delta", {"content": data["text"]}, call_id)
            elif data.get("kind") == "retry":
                await self.tracer.emit("model.retry", data, call_id)
            else:
                await self.tracer.emit("model.chunk", data, call_id)

        try:
            completion = await self.model.invoke(request, chunk)
            await self.tracer.emit(
                "model.response",
                {
                    "completion": completion.model_dump(mode="json", exclude_unset=True),
                    **({"simulation": True} if request.get("simulation") else {}),
                    "duration_ms": round((perf_counter() - start) * 1000),
                },
                call_id,
            )
            return completion, call_id
        # 取消与故障记为不同事件；两者都继续上抛，由管理器决定本轮终态。
        except asyncio.CancelledError:
            await self.tracer.emit("model.canceled", {}, call_id)
            raise
        except Exception as error:
            await self.tracer.emit(
                "model.error", {"error_type": type(error).__name__, "message": str(error)}, call_id
            )
            raise
