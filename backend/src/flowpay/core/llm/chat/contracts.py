"""模型访问接口；请求消息与响应直接使用 OpenAI SDK 的 Chat Completions 类型。"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, ChatCompletionToolParam

from flowpay.core.json import JsonObject

ChunkSink = Callable[[JsonObject], Awaitable[None]]


class ChatModel(Protocol):
    """提供可记录的 Chat Completions 请求与执行能力。

    prepare 只准备请求，invoke 才访问模型；AgentRuntime 在两者之间保存请求日志。
    响应直接返回 SDK 的 ChatCompletion，不另造 text、native_message 等同义字段。
    capabilities 声明实现支持的功能，不代表所有兼容供应商均已验收。
    """

    name: str
    capabilities: dict[str, bool]

    def prepare(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
    ) -> JsonObject:
        """构造包含地址和实际请求体的日志对象；不得发送请求或包含认证密钥。"""
        ...

    async def invoke(self, request: JsonObject, emit: ChunkSink) -> ChatCompletion:
        """执行已记录请求并逐段等待 emit；失败向上传播，成功返回 SDK 响应对象。"""
        ...
