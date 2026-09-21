"""一次模型响应的领域视图：它要么给出动作，要么给出回答。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)

from flowpay.core.json import JsonObject
from flowpay.core.llm.chat.messages import ModelOutputError, answer_text, assistant_message


@dataclass(frozen=True)
class Action:
    """模型选择的一次工具调用。

    id 来自供应商，回填工具结果时必须原样带回，assistant.tool_calls 与 tool 消息才能配对。
    arguments 按协议是 JSON 字符串，解码和校验由 Toolbox 完成，阶段不必解析协议。
    """

    id: str
    name: str
    call: ChatCompletionMessageFunctionToolCall
    from_call: str


@dataclass(frozen=True)
class Observation:
    """一次动作的结果，也是回填给模型的内容。

    payload 是工具的原始返回值，包括可纠正的参数错误；note 是阶段附加的说明，
    例如 s3 的信息缺口反馈。两者一起交回模型，由模型决定下一步。
    """

    action: Action
    payload: JsonObject
    note: str | None = None

    def annotate(self, note: str) -> Observation:
        """附加一段说明并返回新的观察；原观察不可变，便于对照原始工具结果。"""
        return replace(self, note=note)

    def content(self) -> JsonObject:
        """组合成回填给模型的工具结果正文。"""
        return self.payload if self.note is None else {**self.payload, "feedback": self.note}


@dataclass(frozen=True)
class Reply:
    """一次模型响应：actions 非空表示要执行动作，否则 text 是可提交的回答。

    completion 保留供应商原始响应，供追踪和下一轮原样回传；call_id 是本地追踪 ID。
    forced 表示这次请求已经收走工具、要求模型直接作答，用于区分"模型认为够了"
    和"预算耗尽被要求收尾"。
    """

    completion: ChatCompletion
    call_id: str
    actions: list[Action]
    text: str
    forced: bool

    @property
    def is_answer(self) -> bool:
        """没有动作就是回答；本类型构造时已保证回答正文完整。"""
        return not self.actions


def read_reply(completion: ChatCompletion, call_id: str, forced: bool) -> Reply:
    """把供应商响应读成领域视图；不符合协议的输出抛 ModelOutputError 交给调用方纠正。"""
    if len(completion.choices) != 1:
        raise ModelOutputError("需要且只能返回一个完整回答。")
    message = completion.choices[0].message
    if not message.tool_calls:
        return Reply(completion, call_id, [], answer_text(completion), forced)
    if completion.choices[0].finish_reason != "tool_calls":
        raise ModelOutputError("工具调用被截断，请重新提供完整参数。")
    actions: list[Action] = []
    for call in message.tool_calls:
        if call.type != "function":
            raise ModelOutputError("只支持 function 工具。")
        actions.append(Action(call.id, call.function.name, call, call_id))
    return Reply(completion, call_id, actions, "", forced)


def correction_messages(
    completion: ChatCompletion, error: ModelOutputError
) -> list[ChatCompletionMessageParam]:
    """把待修正正文和纠正要求组成下一次请求的附加消息；它们不进入成功检查点。"""
    message = completion.choices[0].message if len(completion.choices) == 1 else None
    draft = (
        [assistant_message(message)]
        if message is not None and message.content and not message.tool_calls
        else []
    )
    return [*draft, {"role": "system", "content": f"上次输出无法使用：{error} 请纠正后重试。"}]
