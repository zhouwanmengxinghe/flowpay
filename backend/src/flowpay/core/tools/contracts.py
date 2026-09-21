"""工具的模型可见协议与可纠正的输入错误。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from openai.types.chat import ChatCompletionToolParam

from flowpay.core.json import JsonObject


@dataclass(frozen=True)
class Tool:
    """一个工具的模型可见协议与实际异步处理函数。

    parameters 是 JSON Schema，构造时检查 Schema 本身是否合法。
    execute 不会发送给模型，definition 只暴露名称、说明和参数结构。
    """

    name: str
    description: str
    parameters: JsonObject
    execute: Callable[[JsonObject], Awaitable[JsonObject]]

    def __post_init__(self) -> None:
        """在注册前校验 parameters 本身是合法 JSON Schema，错误直接上抛。"""
        Draft202012Validator.check_schema(self.parameters)

    def definition(self) -> ChatCompletionToolParam:
        """返回 SDK 的 function 工具定义；只含模型可见字段，不含 Python 处理函数。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolInputError(ValueError):
    """工具名称或输入参数不合法；可反馈给模型纠正，工具内部故障继续抛出原异常。"""

    def as_result(self) -> JsonObject:
        """生成明确的失败观察；保留原工具调用 ID 的工作由消息容器完成。"""
        return {"error": str(self)}
