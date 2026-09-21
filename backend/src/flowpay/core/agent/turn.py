"""一轮 agent 的状态与动作：想、做、看，然后回答。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from openai.types.chat import ChatCompletionMessageParam

from flowpay.core.chat.context import chat_messages
from flowpay.core.execution.contracts import TurnInput, TurnResult
from flowpay.core.json import JsonObject, to_json_object
from flowpay.core.knowledge.contracts import Passage
from flowpay.core.knowledge.passages import PassageSet
from flowpay.core.llm.chat import ChatModel
from flowpay.core.llm.chat.client import ChatClient
from flowpay.core.llm.chat.messages import MessageLog, ModelOutputError
from flowpay.core.tools import Tool, Toolbox, ToolInputError

from .budget import Budget, StopReason
from .reply import Action, Observation, Reply, correction_messages, read_reply

# execution.runtime 反向依赖 agent.trace，运行时导入会成环；注解在 __future__ 下是字符串。
if TYPE_CHECKING:
    from flowpay.core.execution.runtime import AgentRuntime

CLOSING = "本轮工具预算已关闭，请根据已有信息直接回答并说明限制。"

# 业务记录编号形如 pay_bg_001_0003、req_bm_030_01：小写前缀加下划线，且带数字。
# 用它区分"真的引用了一条记录"和"提到了 FP-4021 这类错误码或字段名"。
RECORD_ID = re.compile(r"\b[a-z]{2,6}_[A-Za-z0-9_]*\d[A-Za-z0-9_]*\b")


def record_ids(text: str) -> list[str]:
    """文本里出现的记录编号；用 finditer 以保留字符串类型。"""
    return [match.group(0) for match in RECORD_ID.finditer(text)]


def given_record_ids(messages: MessageLog) -> list[str]:
    """会话里用户自己给出的记录编号，跨轮累计。

    多轮排查中编号通常只在给出的那一轮出现，之后几轮都在讨论同一条记录。只认本轮
    就会把用户给过的编号判成模型编的，而工具查空时它又永远进不了 seen，于是模型
    每次改写都再次被拒，直到预算耗尽。assistant 的历史回答不计入：那是模型自己说的。
    """
    found: list[str] = []
    for message in messages.messages:
        content = message.get("content")
        if message["role"] == "user" and isinstance(content, str):
            found.extend(record_ids(content))
    return found


DISPUTE = (
    "回答里出现了这些编号，但本轮没有任何工具返回过它们：{names}。"
    "请改写回答：只引用工具实际返回的记录，或说明这一项无法确认。"
)


@dataclass
class AgentTurn:
    """一轮 agent 运行的状态，以及它能做的四件事。

    think 请求模型并把响应读成 Reply；perform 执行模型选择的动作；observe 把结果
    回填为工具消息；accept 提交回答。阶段负责写循环、预算数字、提示词和结束条件，
    本类只保证协议细节正确：完成原因校验、tool_call_id 配对、参数解码、输出纠正。

    用 start 构造。messages 是发给模型的原生消息，retrieved 是累计的片段，
    两者一起构成检查点；remember=False 的阶段两者都不跨轮保留。
    """

    question: str
    messages: MessageLog
    retrieved: PassageSet
    budget: Budget
    runtime: AgentRuntime
    chat: ChatClient
    remember: bool = True
    toolbox: Toolbox = field(init=False)
    # 工具真的返回过的记录编号，加上用户在本会话里给出的——后者是调查输入，不是模型编的。
    seen: set[str] = field(init=False)

    def __post_init__(self) -> None:
        """先给一个空工具箱；阶段调用 offer 之后才有工具交给模型。"""
        self.toolbox = self.runtime.toolbox([])
        self.seen = set(record_ids(self.question)) | set(given_record_ids(self.messages))

    @classmethod
    def start(
        cls,
        turn: TurnInput,
        runtime: AgentRuntime,
        system: str,
        model: ChatModel,
        budget: Budget,
        remember: bool = True,
    ) -> AgentTurn:
        """开始新一轮：有检查点就接着上一轮的消息与片段，否则从系统提示词重新开始。

        remember=False 表示本阶段不跨轮保留消息与片段，每轮都从头开始，也不写检查点。
        数据版本在受理本轮时已核对；这里只在恢复已登记片段时守住集合边界。
        """
        if remember and turn.saved_state:
            messages = MessageLog.restore(turn.saved_state["messages"])
            messages.add_user_message(turn.user_message)
            retrieved = PassageSet.restore(turn.saved_state["evidence"], runtime.collection_id)
        else:
            messages = MessageLog(chat_messages(turn, system))
            retrieved = PassageSet()
        return cls(
            turn.user_message, messages, retrieved, budget, runtime, runtime.chat(model), remember
        )

    def offer(self, *tools: Tool) -> None:
        """把本阶段的工具交给模型；处理函数由阶段提供，执行与记录由 Toolbox 负责。"""
        self.toolbox = self.runtime.toolbox(list(tools))

    def may_think(self) -> bool:
        """模型调用预算是否还有剩余。"""
        return self.budget.may_think()

    async def think(self) -> Reply:
        """请求模型，返回它选择的动作或给出的回答。

        输出不符合协议时（截断的工具调用、空正文、被拒绝）在剩余预算内把待纠正正文
        和纠正要求一起回传重试，纠正草稿不写入检查点。预算耗尽前的最后一次请求不再
        提供工具并要求直接作答；结果通过 Reply.forced 区分"模型认为够了"和"被要求收尾"。
        网络、数据库异常与取消继续上抛。
        """
        correction: list[ChatCompletionMessageParam] = []
        while self.budget.may_think():
            forced = self.budget.must_answer()
            closing: list[ChatCompletionMessageParam] = (
                [{"role": "system", "content": CLOSING}] if forced else []
            )
            self.budget.spend_model_call()
            completion, call_id = await self.chat.call(
                [*self.messages.messages, *correction, *closing],
                [] if forced else self.toolbox.definitions(),
            )
            try:
                reply = read_reply(completion, call_id, forced)
            except ModelOutputError as error:
                await self.runtime.tracer.emit("answer.rejected", {"message": str(error)}, call_id)
                correction = correction_messages(completion, error)
                continue
            self.messages.add_assistant_message(completion.choices[0].message)
            return reply
        raise ModelOutputError("模型调用预算内没有得到可用的输出")

    async def perform(self, action: Action) -> Observation:
        """执行模型选择的动作；参数错误作为可纠正的观察回传，工具内部故障继续上抛。"""
        try:
            payload = await self.toolbox.run(action.call, parent_call_id=action.from_call)
        except ToolInputError as error:
            payload = error.as_result()
        return Observation(action, payload)

    def observe(self, observation: Observation) -> None:
        """把观察回填为 tool 消息，并记下这次返回过哪些记录编号。

        原 tool_call_id 与 assistant.tool_calls 成对保留。编号在这里收集，
        供提交前核对回答是否引用了没查到过的记录。
        """
        content = observation.content()
        self.seen.update(record_ids(json.dumps(content, ensure_ascii=False)))
        self.messages.add_tool_result(observation.action.id, content)

    async def disputed(self, reply: Reply) -> bool:
        """提交前核对：回答引用的记录编号是否都被工具返回过，或由用户给出。

        对不上就把这些编号回传给模型，让它在剩余预算内改写，返回 True 表示本轮继续。
        这是纯文本比对，不额外调用模型；预算只剩最后一次时不再要求改写，
        否则模型没有机会交出任何回答。
        """
        invented = sorted({name for name in record_ids(reply.text) if name not in self.seen})
        if not invented or not self.budget.may_think() or self.budget.must_answer():
            return False
        await self.runtime.tracer.emit("answer.disputed", {"identifiers": invented}, reply.call_id)
        self.messages.add_system_message(DISPUTE.format(names="、".join(invented)))
        return True

    async def search(self, text: str, limit: int = 5) -> list[Passage]:
        """检索并登记片段；返回已登记的对象，跨轮引用指向同一段原文。"""
        return self.retrieved.add(await self.runtime.search_knowledge(text, limit=limit))

    async def read(self, doc_id: str) -> list[Passage]:
        """整篇读回一份文档并登记它的分块；返回空列表表示集合里没有这篇文档。"""
        return self.retrieved.add(await self.runtime.read_document(doc_id))

    def accept(self, reply: Reply, **state: object) -> TurnResult:
        """提交模型给出的回答。

        本轮检索到的片段全部作为可展开引用交给界面，不只挑回答里引用过的那几条：
        读者据此能同时看到"检索到什么"和"回答用了什么"。state 是阶段自有的检查点字段。
        """
        reason = StopReason.BUDGET_EXHAUSTED if reply.forced else StopReason.ANSWERED
        return TurnResult(
            reply.text,
            self.checkpoint(stop_reason=reason, **state),
            evidence=self.retrieved.all(),
        )

    def pause(self, question: str, **state: object) -> TurnResult:
        """提交一次追问并结束本轮，等用户补充后再继续。

        与 accept 的差别只在状态：检查点照常推进，已检索到的片段照常交给界面，
        因此下一轮接着已有的消息和资料走。调用前必须把本轮所有工具结果回填完，
        否则检查点里会留下没有配对的 tool_calls，下一轮请求会被供应商拒绝。
        """
        return TurnResult(
            question,
            self.checkpoint(stop_reason=StopReason.NEED_USER_INPUT, **state),
            status="waiting_user",
            evidence=self.retrieved.all(),
        )

    def give_up(self, text: str) -> TurnResult:
        """没有资料可用时直接结束本轮，不调用模型，也不推进检查点。"""
        return TurnResult(text, {})

    def checkpoint(self, **state: object) -> JsonObject:
        """本轮可提交的状态：原生消息、累计片段、预算花销与阶段自有字段。

        不保留跨轮状态的阶段返回空检查点，下一轮因此从系统提示词重新开始。
        """
        if not self.remember:
            return {}
        return {
            "messages": self.messages.messages,
            "evidence": as_json(self.retrieved.all()),
            "collection_id": self.runtime.collection_id,
            "model_calls": self.budget.spent_model_calls,
            "tool_calls": self.budget.spent_tool_calls,
            **{key: as_json(value) for key, value in state.items()},
        }


def as_json(value: object) -> object:
    """把阶段状态里的自有对象转成 JSON；已经是 JSON 的值原样保留。"""
    if isinstance(value, list):
        return [as_json(item) for item in cast(list[object], value)]
    if hasattr(value, "__dataclass_fields__"):
        return to_json_object(value)
    return value
