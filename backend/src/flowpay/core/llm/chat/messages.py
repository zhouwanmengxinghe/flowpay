"""Chat Completions 请求消息的拼接与正文提取。"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import cast

from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
)
from pydantic import TypeAdapter

from flowpay.core.json import JsonObject


class MessageLog:
    """按 Chat Completions 协议保存一轮的请求消息。

    只做机械拼接：检索策略、循环次数与结束条件都由阶段决定。
    """

    def __init__(self, messages: list[ChatCompletionMessageParam]):
        """接管这份消息列表；从检查点恢复请改用 restore。"""
        self.messages: list[ChatCompletionMessageParam] = messages

    @classmethod
    def restore(cls, messages: object) -> MessageLog:
        """深拷贝检查点后接管，使未完成的一轮不会改到上一轮已提交的上下文。"""
        # 验证标准协议字段，保留原字典的供应商扩展，避免验证器裁剪。
        TypeAdapter(list[ChatCompletionMessageParam]).validate_python(messages, strict=True)
        return cls(copy.deepcopy(cast(list[ChatCompletionMessageParam], messages)))

    def add_user_message(self, content: str) -> None:
        """追加一条 user 消息；继续中断的一轮时，调用方负责不要重复加入原问题。"""
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, message: ChatCompletionMessage) -> None:
        """追加一条 assistant 消息，工具调用与供应商扩展字段原样保留。"""
        # 保留 reasoning_content 等扩展字段，下一次请求才能原样回传。
        self.messages.append(assistant_message(message))

    def add_system_message(self, content: str) -> None:
        """追加一条 system 消息。

        Raises:
            ValueError: 上一轮工具调用还没回填完。此时插入别的消息会拆散
                assistant.tool_calls 与 tool 消息的配对，供应商会拒绝整个请求。
        """
        if self.awaiting_tool_results():
            raise ValueError("Tool results must be appended before other messages")
        self.messages.append({"role": "system", "content": content})

    def awaiting_tool_results(self) -> bool:
        """最后一次 assistant 的工具调用是否还有没回填结果的。"""
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message["role"] != "assistant":
                continue
            calls = cast(Sequence[object], message.get("tool_calls") or ())
            filled = sum(later["role"] == "tool" for later in self.messages[index + 1 :])
            return len(calls) > filled
        return False

    def add_tool_result(self, tool_call_id: str, result: JsonObject) -> None:
        """追加一条 tool 消息，携带原 tool_call_id，结果序列化为 JSON 正文。"""
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )


def assistant_message(message: ChatCompletionMessage) -> ChatCompletionAssistantMessageParam:
    """把 SDK 响应消息转成请求消息，工具调用与供应商扩展原样保留。

    标准字段已由 ChatCompletionMessage 校验过，两种 SDK 类型共享消息结构，
    因此这里的 cast 只是标明序列化结果的协议类型。
    """
    return cast(
        ChatCompletionAssistantMessageParam,
        # 响应允许 tool_calls=null；请求类型要求有值时必须是调用列表。
        cast(object, message.model_dump(mode="json", exclude_unset=True, exclude_none=True)),
    )


class ModelOutputError(ValueError):
    """模型输出无法按请求使用，阶段可在剩余预算内让模型纠正。

    只表示输出本身的问题（截断、空正文、被拒绝）；外部服务故障保留原异常类型。
    """


def answer_text(completion: ChatCompletion) -> str:
    """提取可提交的完整正文。

    Raises:
        ModelOutputError: 多候选、截断、被拒绝、空正文，或仍带着未完成的工具调用。
    """
    if len(completion.choices) != 1:
        raise ModelOutputError("需要且只能返回一个完整回答。")
    choice = completion.choices[0]
    message = choice.message
    if (
        choice.finish_reason != "stop"
        or message.tool_calls
        or message.refusal
        or not message.content
        or not message.content.strip()
    ):
        raise ModelOutputError("回答未完整结束，请重新给出简洁、完整的正文。")
    return message.content
