"""聚合 Chat Completions 流；不负责网络、重试或日志写入。"""

from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage

from flowpay.core.json import (
    JsonObject,
    require_int,
    require_list,
    require_object,
    require_text,
)


class ChatStreamAccumulator:
    """把首个候选的流式 delta 合并成 SDK ChatCompletion；原始帧由调用日志保留。"""

    def __init__(self) -> None:
        """初始化第一个候选的消息、工具片段和响应元信息；原始帧由外层日志入口保存。"""
        self.message: JsonObject = {"role": "assistant", "content": ""}
        self.tool_calls: dict[int, JsonObject] = {}
        self.usage: JsonObject = {}
        self.finish_reason: str = ""
        self.response_fields: JsonObject = {}

    def consume(self, chunk: JsonObject) -> str:
        """合并一帧并返回公开文本增量，工具和推理字段不混入展示文本。"""
        self.response_fields.update(
            {
                key: value
                for key, value in chunk.items()
                if key not in {"choices", "usage", "object"}
            }
        )
        self.usage = require_object(chunk.get("usage") or self.usage)
        text_parts: list[str] = []
        for raw_choice in require_list(chunk.get("choices", [])):
            choice = require_object(raw_choice)
            if choice.get("index", 0) != 0:
                continue
            self.finish_reason = require_text(choice.get("finish_reason") or self.finish_reason)
            delta = require_object(choice.get("delta", {}))
            content = delta.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            self._merge_message_delta(delta)
        return "".join(text_parts)

    def _merge_message_delta(self, delta: JsonObject) -> None:
        """合并内容、角色与供应商扩展增量；工具片段按 index 分别累积。"""
        for field, value in delta.items():
            # null 表示本帧没有该字段的增量，不能清空已有角色或当工具列表解析。
            if value is None:
                continue
            if field == "tool_calls":
                for part in require_list(value):
                    self._merge_tool_call(require_object(part))
            elif field == "role":
                self.message[field] = value
            elif isinstance(value, str):
                # reasoning_content 等公开扩展字段同样需要拼接并回传。
                self.message[field] = require_text(self.message.get(field, "")) + value
            elif value is not None:
                self.message[field] = value

    def _merge_tool_call(self, part: JsonObject) -> None:
        """按协议 index 拼接 function.name 和 arguments；流未结束时不尝试解析 JSON。"""
        if part.get("type") not in {None, "function"}:
            raise ValueError("Only function tool streams are supported")
        tool_call = self.tool_calls.setdefault(
            require_int(part["index"]),
            {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if part.get("id"):
            tool_call["id"] = part["id"]
        # 参数 JSON 在流结束前可能不完整；此处只拼字符串，执行前再解码。
        function = require_object(tool_call["function"])
        delta = part.get("function")
        if delta is None:
            return
        for field, text in require_object(delta).items():
            if text is None:
                continue
            function[field] = require_text(function.get(field, "")) + require_text(text)

    def finish(self) -> ChatCompletion:
        """返回包含首个候选的 SDK ChatCompletion；缺少 finish_reason 时拒绝返回成功。

        用量和响应元信息只保留实际字段，原始其他候选仍可从 wire 事件回查。
        """
        if not self.finish_reason:
            raise RuntimeError("Model stream ended without finish_reason")
        if self.tool_calls:
            self.message["tool_calls"] = [
                self.tool_calls[index] for index in sorted(self.tool_calls)
            ]
        # 兼容服务可能省略元信息或部分用量字段。沿用 SDK 的宽松构造方式，
        # 保留实际返回值，不伪造 completion id、时间或 token 用量。
        return ChatCompletion.model_construct(
            **self.response_fields,
            _fields_set=None,
            object="chat.completion",
            choices=[
                Choice.model_validate(
                    {
                        "index": 0,
                        "finish_reason": self.finish_reason,
                        "message": self.message,
                    }
                )
            ],
            usage=CompletionUsage.model_construct(**self.usage, _fields_set=None)
            if self.usage
            else None,
        )
