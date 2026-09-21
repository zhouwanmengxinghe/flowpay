"""通用能力只检查消息协议和引用身份，阶段决定如何使用这些能力。"""

from pathlib import Path

import pytest

from flowpay.core.agent import AgentTurn, Budget
from flowpay.core.agent.reply import read_reply
from flowpay.core.agent.trace import Tracer
from flowpay.core.chat.context import chat_messages
from flowpay.core.chat.types import ChatMessage
from flowpay.core.event_log import EventStore
from flowpay.core.execution.contracts import TurnInput
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.json import parse_json, require_list, require_object
from flowpay.core.knowledge.passages import PassageSet
from flowpay.core.llm.chat.messages import MessageLog, ModelOutputError

from ..workflows.fixtures import (
    ANSWER,
    Model,
    completion,
    grounded,
    hit,
    other_hit,
    search_call,
)


def test_chat_history_keeps_original_text_without_duplicate_resumed_question() -> None:
    """界面历史转换只移除事件元数据，原文和引用不被偷偷改写。"""
    history: list[ChatMessage] = [
        {"role": "user", "content": "HTTP 410？", "run_id": "old", "seq": 1},
        {"role": "assistant", "content": ANSWER, "run_id": "old", "seq": 2},
        {"role": "user", "content": "怎么恢复？", "run_id": "new", "seq": 3},
    ]
    result = chat_messages(TurnInput("怎么恢复？", history, {}, resumed=True), "system")
    assert result == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "HTTP 410？"},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "怎么恢复？"},
    ]


def test_passage_set_dedupes_and_survives_a_checkpoint() -> None:
    """登记真实文档 ID；重复命中只保留一份，检查点往返后仍是同一批片段。"""
    retrieved = PassageSet()
    retrieved.add([hit(), other_hit("doc:SECOND#c001", "SECOND")])
    retrieved.add([hit()])
    assert [passage.id for passage in retrieved.all()] == ["doc:RULE-1#c001", "doc:SECOND#c001"]
    restored = PassageSet.restore(retrieved.all(), "fixture")
    assert restored.all() == retrieved.all()
    retrieved.passages.clear()
    assert len(restored.all()) == 2
    with pytest.raises(ValueError, match="another collection"):
        PassageSet.restore(restored.all(), "changed")


def test_model_context_keeps_passages_and_leaves_provenance_to_evidence() -> None:
    """交给模型的片段只有编号、标题、正文和分数；溯源字段仍可通过引用展开取回。"""
    retrieved = PassageSet()
    retrieved.add([hit()])
    passages = require_list(parse_json(retrieved.serialize()))
    assert [sorted(require_object(p)) for p in passages] == [["id", "score", "text", "title"]]
    assert retrieved.all()[0].source.sha256 == "fixture"


@pytest.mark.parametrize(
    "body", ["普通对话", "当前资料不足，请确认版本。", '```json\n{"a": 1}\n```']
)
def test_text_needs_no_answer_kind_or_json_wrapper(body: str) -> None:
    """回答方式由阶段提示和模型判断；协议层允许直接 Markdown。"""
    assert read_reply(completion(body), "call", forced=False).text == body


@pytest.mark.parametrize("body,finish", [("", "stop"), ("   ", "stop"), ("半句", "length")])
def test_incomplete_text_is_rejected(body: str, finish: str) -> None:
    """截断和空回答明确暴露给调用者，不自动变成等待用户的业务结果。"""
    with pytest.raises(ModelOutputError):
        read_reply(completion(body, finish=finish), "call", forced=False)


def test_answer_text_is_never_rewritten_by_citation_checks() -> None:
    """正文原样提交；编造的引用编号留在文字里，由界面据实际片段标出无法定位的编号。"""
    reply = read_reply(
        grounded(sources=["doc:UNKNOWN#c001", "doc:RULE-1#c001"]), "call", forced=False
    )
    assert "[doc:UNKNOWN#c001]" in reply.text and "[doc:RULE-1#c001]" in reply.text


async def test_invalid_stream_is_not_committed_as_an_answer(tmp_path: Path) -> None:
    """Markdown 草稿直接流出，校验失败只记录拒绝，不替阶段生成业务结果。"""
    model = Model()
    model.responses = [completion("半句", finish="length")]
    store = EventStore(tmp_path / "preview.sqlite")
    runtime = AgentRuntime(Tracer(store, "s", "r"))
    agent = AgentTurn.start(
        TurnInput("半句", [], {}), runtime, "system", model, Budget(model_calls=1, tool_calls=0)
    )
    with pytest.raises(ModelOutputError):
        await agent.think()
    events = store.read("s")
    assert any(e["type"] == "model.delta" for e in events)
    assert events[-1]["type"] == "answer.rejected"
    assert not any(e["type"] in {"answer.delta", "message.assistant"} for e in events)


def test_messages_refuse_to_split_a_tool_round() -> None:
    """工具结果没回填完就插别的消息会拆散配对，供应商会拒绝整个请求。

    s4 的追问发生在工具执行中途，正是这个坑的形状；护栏让它变成明确错误。
    """
    log = MessageLog([{"role": "system", "content": "system"}])
    log.add_system_message("此时没有待回填的工具调用")
    log.add_assistant_message(
        completion(calls=[search_call("a", "q"), search_call("b", "q2")]).choices[0].message
    )
    with pytest.raises(ValueError, match="Tool results must be appended"):
        log.add_system_message("不该插在这里")
    log.add_tool_result("a", {"rows": []})
    with pytest.raises(ValueError, match="Tool results must be appended"):
        log.add_system_message("还差一个也不行")
    log.add_tool_result("b", {"rows": []})
    log.add_system_message("全部回填之后可以追加")
    assert [m["role"] for m in log.messages][-4:] == ["assistant", "tool", "tool", "system"]


def test_restored_native_messages_keep_extensions_and_own_their_copy() -> None:
    """供应商字段和工具关联都保留；未提交运行修改自己的副本。"""
    original = MessageLog([{"role": "system", "content": "system"}])
    original.add_assistant_message(completion("hello").choices[0].message)
    restored = MessageLog.restore(original.messages)
    restored.add_user_message("next")
    assert len(original.messages) == 2 and len(restored.messages) == 3
    assert "公开的测试推理字段" in str(restored.messages)
