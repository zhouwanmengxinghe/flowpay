"""模型跨 JSON 边界时保留供应商扩展，拒绝无法序列化的对象。"""

from dataclasses import dataclass

import pytest
from openai.types.chat import ChatCompletionMessageParam
from pydantic_core import PydanticSerializationError

from flowpay.core.json import JsonObject, to_json_object
from flowpay.core.llm.chat.messages import MessageLog


@dataclass
class MessageCheckpoint:
    """用于验证嵌套 SDK 消息及任意阶段字段的测试检查点。"""

    messages: list[ChatCompletionMessageParam]
    state: JsonObject


def test_serialization_keeps_nested_provider_extensions() -> None:
    """不能因为 SDK 标准字段中没有推理内容就将其从下一轮请求删除。"""
    raw = [{"role": "assistant", "content": "答案", "reasoning_content": "推理"}]
    messages = MessageLog.restore(raw).messages
    model = MessageCheckpoint(messages, {"extension": {"future": [1, True, None]}})
    assert to_json_object(model) == {"messages": raw, "state": model.state}


def test_serialization_rejects_non_json_values() -> None:
    """动态扩展也必须能写入 JSON，不能把任意 Python 对象送到日志出口。"""
    with pytest.raises(PydanticSerializationError):
        to_json_object({"invalid": object()})
