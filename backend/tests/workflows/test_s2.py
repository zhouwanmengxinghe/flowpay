"""真实会话管理器驱动 s2，受控模型与检索用于验证决策循环及故障边界。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from flowpay.core.config import Settings
from flowpay.core.event_log import EventStore
from flowpay.core.execution.errors import Conflict
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.resources import RuntimeResources
from flowpay.core.json import parse_json, require_list, require_object, require_text
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.router import create_stage

from .fixtures import (
    ANSWER,
    Model,
    Search,
    completion,
    conversation,
    grounded,
    insufficient,
    read_call,
    search_call,
)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[SessionManager, Model, Search]]:
    """构建真实管理器和 SQLite 日志；检索与模型替换为可控依赖。"""
    model, search = Model(), Search()
    settings = Settings(tmp_path, tmp_path, workflow="s2", knowledge_collection_id="fixture")
    resources = RuntimeResources(settings, retriever=search.for_run)
    store = EventStore(tmp_path / "sessions.sqlite")
    manager = SessionManager(store, create_stage("s2", model, retrieval_input), settings, resources)
    try:
        yield manager, model, search
    finally:
        await manager.shutdown()
        resources.close()


async def submit(manager: SessionManager, content: str = "回调说地址没了，还会发吗？") -> str:
    """新建会话并等待这一轮完成，返回可用于读取事件的会话编号。"""
    sid = (await manager.create_session())["id"]
    await manager.submit_message(sid, "first", content)
    await manager.tasks[sid]
    return sid


async def test_observe_search_results_then_search_again(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """第一次空命中后模型改查 HTTP 410；结果和调用 ID 原样回填，引用可追踪。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[search_call("a", "missing 回调")]),
        completion(calls=[search_call("b", "HTTP 410 Endpoint", 4)]),
        grounded(),
    ]
    sid = await submit(manager)
    view = await manager.get_session(sid)
    assert view["status"] == "completed" and view["messages"][-1]["content"] == ANSWER
    assert search.queries == [
        (retrieval_input("missing 回调"), 10),
        (retrieval_input("HTTP 410 Endpoint"), 4),
    ]
    assert view["messages"][-1].get("evidence", [])[0]["id"] == "doc:RULE-1#c001"
    assert view["state"]["model_calls"] == 3
    messages = require_list(model.requests[2]["messages"])
    tools = [require_object(m) for m in messages if require_object(m)["role"] == "tool"]
    assert [t["tool_call_id"] for t in tools] == ["a", "b"]
    assert require_object(parse_json(str(tools[0]["content"]))) == {
        "query": "missing 回调",
        "passages": [],
        "notice": "未检索到片段。",
    }
    assert "公开的测试推理字段" in str(messages)
    events = manager.store.read(sid)
    starts = [e for e in events if e["type"] == "tool.started"]
    models = [e for e in events if e["type"] == "model.request"]
    assert [e["parent_call_id"] for e in starts] == [e["call_id"] for e in models[:2]]
    assert any(e["type"] == "model.delta" for e in events)


@pytest.mark.parametrize(
    "question, answer",
    [
        ("你好呀，今天又来麻烦你啦", "欢迎回来，有什么可以帮你？"),
        ("先别查资料，你能帮我做些什么？", "我可以搜索 FlowPay 文档，并根据文档回答问题。"),
        ("帮我检查刚刚那一笔支付的实时状态", "我只能检索文档，无法查询当前支付的实时状态。"),
    ],
)
async def test_model_can_answer_without_search(
    runtime: tuple[SessionManager, Model, Search], question: str, answer: str
) -> None:
    """不按用户措辞分流；模型选择直接回答时，保留正文且不强制检索或替换文案。"""
    manager, model, search = runtime
    model.responses = [conversation(answer)]
    sid = await submit(manager, question)
    view = await manager.get_session(sid)
    assert view["status"] == "completed"
    assert view["messages"][-1]["content"] == answer
    assert view["messages"][-1].get("evidence", []) == []
    assert view["state"]["stop_reason"] == "answered"
    assert not search.queries
    assert model.requests[0]["tools"]


async def test_empty_search_preserves_models_explanation(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """搜索为空时由模型决定继续还是解释缺口，工作流不覆盖它的最终回答。"""
    manager, model, search = runtime
    answer = "当前片段没有覆盖端点失效后的投递策略，暂时无法确认是否继续发送。"
    model.responses = [
        completion(calls=[search_call("empty", "missing endpoint")]),
        insufficient(answer),
    ]
    sid = await submit(manager)
    view = await manager.get_session(sid)
    assert view["status"] == "completed"
    assert answer in view["messages"][-1]["content"]
    assert view["messages"][-1].get("evidence", []) == []
    assert len(search.queries) == 1


async def test_model_chooses_between_searching_and_reading_a_whole_document(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """两个工具都交给模型：检索给零散片段，按编号整篇读回给完整原文。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[search_call("a", "HTTP 410")]),
        completion(calls=[read_call("b", "RULE-1")]),
        grounded(),
    ]
    sid = await submit(manager)
    assert search.queries and search.reads == ["RULE-1"]
    offered = [require_object(t)["function"] for t in require_list(model.requests[0]["tools"])]
    assert {require_object(f)["name"] for f in offered} == {"search_documents", "read_document"}
    actions = require_list((await manager.get_session(sid))["state"]["actions"])
    assert [require_object(a)["tool"] for a in actions] == ["search_documents", "read_document"]


async def test_read_document_tells_the_model_how_a_doc_id_is_shaped(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """引用编号带 doc: 前缀和 #c001 后缀，工具参数只要中间那段；说明写在 schema 里。

    实测里模型确实把整个引用编号填进过 doc_id，被 pattern 拦下后才自己改对。
    """
    manager, model, _ = runtime
    model.responses = [
        completion(calls=[read_call("a", "doc:RULE-1#c001")]),
        grounded(),
    ]
    await submit(manager)
    tools = {
        require_object(require_object(t)["function"])["name"]: require_object(
            require_object(require_object(t)["function"])["parameters"]
        )
        for t in require_list(model.requests[0]["tools"])
    }
    doc_id = require_object(require_object(tools["read_document"])["properties"])["doc_id"]
    assert "去掉 doc: 前缀" in str(require_object(doc_id)["description"])
    messages = require_list(model.requests[1]["messages"])
    observation = [require_object(m) for m in messages if require_object(m)["role"] == "tool"][0]
    assert "does not match" in str(observation["content"])


async def test_reading_and_searching_share_one_budget_and_one_dedup_record(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """换个工具再问同一件事同样被拦；预算由两个工具共用，不是各给三次。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[read_call("a", "RULE-1"), read_call("b", "rule-1")]),
        completion(
            calls=[read_call("c", "MISSING"), search_call("d", "q1"), search_call("e", "q2")]
        ),
        grounded(),
    ]
    await submit(manager)
    assert search.reads == ["RULE-1", "MISSING"]
    assert len(search.queries) == 1
    assert "已经取过" in str(model.requests[1]["messages"])
    assert "工具预算已耗尽" in str(model.requests[2]["messages"])


async def test_duplicate_queries_do_not_consume_searches(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """空格和大小写不同的重复请求不再次嵌入；工具结果明确提醒模型换查询。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[search_call("a", "HTTP   410"), search_call("b", "http 410")]),
        grounded(),
    ]
    sid = await submit(manager)
    assert len(search.queries) == 1
    assert "已经取过" in str(model.requests[1]["messages"])
    assert len((await manager.get_session(sid))["messages"][-1].get("evidence", [])) == 1


async def test_multiple_calls_cannot_exceed_search_budget(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """单条响应里请求四次搜索，只执行三次，下一次模型请求撤下工具并要求结束。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[search_call(str(i), f"query {i}") for i in range(4)]),
        grounded(),
    ]
    sid = await submit(manager)
    assert len(search.queries) == 3
    assert "工具预算已耗尽" in str(model.requests[1]["messages"])
    assert model.requests[1]["tools"] == []
    assert (await manager.get_session(sid))["state"]["stop_reason"] == "budget_exhausted"


async def test_repeated_requests_stop_at_model_budget(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """重复搜索不能让模型无限循环；第五次调用只允许结束。"""
    manager, model, search = runtime
    model.responses = [completion(calls=[search_call(str(i), "HTTP 410")]) for i in range(4)]
    model.responses.append(grounded())
    sid = await submit(manager)
    assert len(model.requests) == 5 and len(search.queries) == 1
    assert model.requests[-1]["tools"] == []
    assert (await manager.get_session(sid))["status"] == "completed"


@pytest.mark.parametrize(
    "failure",
    [
        "unknown_tool",
        "bad_json",
        "limit",
        "truncated",
        "empty_text",
        "search_error",
    ],
)
async def test_invalid_protocol_and_dependencies_fail(
    runtime: tuple[SessionManager, Model, Search], failure: str
) -> None:
    """协议、工具参数或外部依赖出错时保留失败事件，不推进检查点。"""
    manager, model, search = runtime
    call = search_call("a", "HTTP 410", 11 if failure == "limit" else 10)
    function = require_object(call["function"])
    if failure == "unknown_tool":
        function["name"] = "query_payment"
    if failure == "bad_json":
        function["arguments"] = "{broken"
    if failure in {"unknown_tool", "bad_json", "limit", "search_error"}:
        model.responses = [completion(calls=[call])]
        search.fail = failure == "search_error"
    else:
        model.responses = [
            completion("未完成", finish="length") if failure == "truncated" else completion("")
        ]
    if failure != "search_error":
        model.responses *= 5
    sid = await submit(manager)
    view = await manager.get_session(sid)
    assert view["status"] == "failed" and view["state"] == {}
    assert manager.store.read(sid)[-1]["type"] == "run.failed"
    if failure in {"unknown_tool", "bad_json", "limit", "search_error"}:
        assert any(e["type"] == "tool.error" for e in manager.store.read(sid))
    else:
        assert any(e["type"] == "answer.rejected" for e in manager.store.read(sid))
    if failure != "search_error":
        assert not search.queries


async def test_failed_turn_restarts_without_reusing_partial_evidence(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """恢复重跑原问题；前一次失败中的工具结果不会冒充本次成功搜索。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[search_call("a", "HTTP 410")]),
        RuntimeError("model offline"),
    ]
    sid = await submit(manager)
    assert (await manager.get_session(sid))["status"] == "failed"
    model.responses = [completion(calls=[search_call("b", "HTTP 410")]), grounded()]
    await manager.resume_run(sid, "resume")
    await manager.tasks[sid]
    view = await manager.get_session(sid)
    assert view["status"] == "completed" and len(search.queries) == 2
    assert len([m for m in view["messages"] if m["role"] == "user"]) == 1
    assert model.requests[0]["messages"] == model.requests[2]["messages"]


async def test_cancel_during_search_does_not_commit_state(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """取消穿过工具和工作流，没有最终回答或新的摘要提交。"""
    manager, model, search = runtime
    search.block = True
    model.responses = [completion(calls=[search_call("a", "HTTP 410")])]
    sid = (await manager.create_session())["id"]
    await manager.submit_message(sid, "cancel", "HTTP 410")
    await asyncio.wait_for(search.started.wait(), timeout=3)
    await manager.cancel_run(sid)
    view = await manager.get_session(sid)
    assert view["status"] == "canceled" and view["state"] == {}
    assert any(e["type"] == "tool.canceled" for e in manager.store.read(sid))


async def test_new_turn_keeps_tool_history_without_changing_stage_identity(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """下一轮保留聊天和工具历史；旧 s1 会话不能被 s2 静默接管。"""
    manager, model, _ = runtime
    model.responses = [
        completion(calls=[search_call("a", "HTTP 410")]),
        grounded(),
        conversation("你好"),
    ]
    sid = await submit(manager)
    await manager.submit_message(sid, "second", "你好")
    await manager.tasks[sid]
    messages = require_list(model.requests[-1]["messages"])
    assert [require_object(m)["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert "HTTP 410 后禁用端点。" in str(messages)
    assert "doc:RULE-1#c001" in str(messages) and "source_sha256" not in str(messages)
    # 片段跨轮累积：追问时引用上一轮查到的文档，界面仍能定位，不会误判为无法定位。
    last = (await manager.get_session(sid))["messages"][-1]
    assert [e["id"] for e in last.get("evidence", [])] == ["doc:RULE-1#c001"]
    old_sid = "session_old_s1"
    await manager.store.aappend(
        old_sid, [("session.created", {"workflow": {"id": "flowpay-document-rag", "version": "3"}})]
    )
    with pytest.raises(Conflict, match="Workflow version"):
        await manager.submit_message(old_sid, "wrong_stage", "HTTP 410")


async def test_followup_history_survives_restart_and_resume(tmp_path: Path) -> None:
    """重启后仍带入前文，模型可据此生成完整查询；失败后继续不重复问题或复用工具结果。"""
    settings = Settings(tmp_path, tmp_path, workflow="s2", knowledge_collection_id="fixture")
    path = tmp_path / "sessions.sqlite"
    first_model = Model()
    first_model.responses = [
        completion(calls=[search_call("first", "HTTP 410")]),
        grounded(),
    ]
    first_resources = RuntimeResources(settings, retriever=Search().for_run)
    first = SessionManager(
        EventStore(path),
        create_stage("s2", first_model, retrieval_input),
        settings,
        first_resources,
    )
    try:
        sid = await submit(first, "HTTP 410 会怎样？")
    finally:
        await first.shutdown()
        first_resources.close()

    model, search = Model(), Search()
    resources = RuntimeResources(settings, retriever=search.for_run)
    manager = SessionManager(
        EventStore(path), create_stage("s2", model, retrieval_input), settings, resources
    )
    query = "HTTP 410 已禁用的 Endpoint 如何恢复"
    question = "那还能恢复吗？"
    try:
        await manager.recover()
        model.responses = [
            completion(calls=[search_call("followup", query)]),
            RuntimeError("offline"),
        ]
        await manager.submit_message(sid, "second", question)
        await manager.tasks[sid]
        assert (await manager.get_session(sid))["status"] == "failed"
        before = require_list(model.requests[0]["messages"])
        assert [require_object(m)["role"] for m in before] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "user",
        ]
        assert require_object(before[-1])["content"] == question
        assert "公开的测试推理字段" in str(before)
        model.responses = [completion(calls=[search_call("retry", query)]), grounded()]
        await manager.resume_run(sid, "retry")
        await manager.tasks[sid]
        view = await manager.get_session(sid)
        assert view["status"] == "completed"
        assert model.requests[0]["messages"] == model.requests[2]["messages"]
        assert search.queries == [(retrieval_input(query), 10), (retrieval_input(query), 10)]
        assert [m["content"] for m in view["messages"] if m["role"] == "user"] == [
            "HTTP 410 会怎样？",
            question,
        ]
        # 新一轮允许再次查询同一主题，上一轮的去重记录和预算不应限制本轮。
        model.responses = [completion(calls=[search_call("again", query)]), grounded()]
        await manager.submit_message(sid, "same_question", question)
        await manager.tasks[sid]
        assert len(search.queries) == 3
        assert (await manager.get_session(sid))["status"] == "completed"
        user_messages = [
            require_object(m)["content"]
            for m in require_list(model.requests[4]["messages"])
            if require_object(m)["role"] == "user"
        ]
        assert user_messages == ["HTTP 410 会怎样？", question, question]
        model.responses = [conversation("我可以搜索文档。")]
        other_sid = await submit(manager, "你能做什么？")
        assert require_list(model.requests[-1]["messages"])[1:] == [
            {"role": "user", "content": "你能做什么？"},
        ]
        assert (await manager.get_session(other_sid))["status"] == "completed"
    finally:
        await manager.shutdown()
        resources.close()


async def test_history_evidence_can_be_reused_without_search(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """上一轮原文与来源在同一集合中可直接复用，无需强制再次搜索。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[search_call("a", "HTTP 410")]),
        grounded(),
        grounded(),
    ]
    sid = await submit(manager)
    await manager.submit_message(sid, "second", "再确认一下呢？")
    await manager.tasks[sid]
    assert "[doc:RULE-1#c001]" in str(model.requests[-1]["messages"])
    view = await manager.get_session(sid)
    assert view["status"] == "completed"
    assert len(search.queries) == 1
    assert len(view["messages"][-1].get("evidence", [])) == 1


async def test_answer_citing_a_record_no_tool_returned_is_sent_back(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """回答引用了工具没返回过的记录编号时，提交前退回让模型改写。

    这是纯文本比对，不额外调用模型：编造一个看起来合理的编号，是最该在交付前
    拦住的一类错误，而它在离线评测里才被发现就太晚了。
    """
    manager, model, _ = runtime
    model.responses = [
        completion("依据 pay_bg_001_0003，这笔支付已成功。"),
        completion("资料中没有对应的支付记录，无法确认这笔支付的状态。"),
    ]
    sid = await submit(manager)
    events = manager.store.read(sid)
    disputed = [e for e in events if e["type"] == "answer.disputed"]
    assert require_list(require_object(disputed[0]["payload"])["identifiers"]) == [
        "pay_bg_001_0003"
    ]
    view = await manager.get_session(sid)
    assert "无法确认" in str(view["messages"][-1]["content"])


async def test_identifiers_from_the_question_are_not_treated_as_invented(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """用户在问题里给出的编号是调查输入，回答引用它不算编造。"""
    manager, model, _ = runtime
    model.responses = [completion("请补充 req_demo_503 对应的账户与环境。")]
    sid = await submit(manager, "请求 req_demo_503 返回了 503，这是怎么回事？")
    assert not [e for e in manager.store.read(sid) if e["type"] == "answer.disputed"]
    view = await manager.get_session(sid)
    assert require_text(view["status"]) == "completed"


async def test_the_last_model_call_is_accepted_even_with_unknown_identifiers(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """预算只剩最后一次调用时不再要求改写，否则本轮交不出任何回答。"""
    manager, model, _ = runtime
    model.responses = [
        *[completion(calls=[search_call(str(i), f"query {i}")]) for i in range(3)],
        completion("依据 pay_unknown_001 给出结论。"),
    ]
    sid = await submit(manager)
    view = await manager.get_session(sid)
    assert require_text(view["status"]) == "completed"
    assert "pay_unknown_001" in str(view["messages"][-1]["content"])


async def test_identifiers_from_an_earlier_turn_are_not_treated_as_invented(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """编号在上一轮给出、本轮没有复述时，引用它同样不算编造。

    多轮排查里用户只会报一次编号，之后几轮都在讨论同一条记录。若只认本轮消息，
    工具查空时这个编号永远进不了 seen，模型每次改写都被驳回，直到预算耗尽。
    """
    manager, model, _ = runtime
    model.responses = [completion("pay_bg_001_0003 的状态还需要继续查证。")]
    sid = await submit(manager, "支付 pay_bg_001_0003 一直没收到通知")
    model.responses = [completion("pay_bg_001_0003 在这些条件下查不到记录。")]
    await manager.submit_message(sid, "second", "那还能怎么查？")
    await manager.tasks[sid]
    assert not [e for e in manager.store.read(sid) if e["type"] == "answer.disputed"]
    view = await manager.get_session(sid)
    assert require_text(view["status"]) == "completed"
    assert "pay_bg_001_0003" in str(view["messages"][-1]["content"])
