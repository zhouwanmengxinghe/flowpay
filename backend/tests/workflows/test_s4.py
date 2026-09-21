"""s4 多轮协作：条件跨轮累积、缺项追问、补齐后继续、改口后按新条件重查。

这些是 s4 相对 s3 的全部新行为，都不依赖模型的判断质量：条件登记、追问拦截、
去重和检查点由代码保证，因此可以用受控模型逐条钉住。
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from flowpay.core.config import Settings
from flowpay.core.data_sources import DataSources
from flowpay.core.event_log import EventStore
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.resources import RuntimeResources
from flowpay.core.json import JsonObject, require_list, require_object, require_text
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.dataset import dataset_path, snapshot_ready
from flowpay.router import create_stage
from flowpay.workflows.s4 import Case, conditions_in

from .fixtures import Model, Search, completion

ROOT = Path(__file__).resolve().parents[3]
DATASET = dataset_path() or Path("/nonexistent")
POLICY = ROOT / "backend/policies/sources.toml"

pytestmark = pytest.mark.skipif(
    not snapshot_ready(), reason="business dataset snapshot is not available"
)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[SessionManager, Model, Search]]:
    """真实业务数据源 + 可控模型；检索仍用夹具。"""
    model, search = Model(), Search()
    settings = Settings(tmp_path, tmp_path, workflow="s4", knowledge_collection_id="fixture")
    resources = RuntimeResources(
        settings, sources=DataSources.from_config(POLICY, DATASET), retriever=search.for_run
    )
    store = EventStore(tmp_path / "sessions.sqlite")
    manager = SessionManager(store, create_stage("s4", model, retrieval_input), settings, resources)
    try:
        yield manager, model, search
    finally:
        await manager.shutdown()
        resources.close()


def ask_call(identifier: str, missing: list[str], question: str) -> JsonObject:
    """构造一次 ask_user 调用；参数仍是协议要求的 JSON 字符串。"""
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "ask_user",
            "arguments": json.dumps({"missing": missing, "question": question}),
        },
    }


def search_document(identifier: str, query: str) -> JsonObject:
    """构造一次 search_documents 调用。"""
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "search_documents",
            "arguments": json.dumps({"query": query, "limit": 10}),
        },
    }


def payments_call(identifier: str, account: str, environment: str, payment: str) -> JsonObject:
    """构造一次 query_payments 调用，条件取自当前已知的账户与环境。"""
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": "query_payments",
            "arguments": json.dumps(
                {
                    "account_id": account,
                    "environment": environment,
                    "filters": {"payment_id": payment},
                }
            ),
        },
    }


async def say(manager: SessionManager, sid: str, request_id: str, text: str) -> None:
    """提交一轮用户消息并等它结束。"""
    await manager.submit_message(sid, request_id, text)
    await manager.tasks[sid]


def state_of(manager: SessionManager, sid: str) -> JsonObject:
    """读取会话当前的检查点。"""
    events = manager.store.read(sid)
    finished = [e for e in events if e["type"] == "run.finished"]
    assert finished, "本轮没有正常提交"
    return require_object(require_object(finished[-1]["payload"])["state"])


def request_text(model: Model, index: int) -> str:
    """第 index 次模型请求里的全部消息正文，用于检查追加的说明。"""
    return str(model.requests[index]["messages"])


async def test_identifier_prefixes_and_environment_are_read_from_one_message() -> None:
    """编号按前缀归位；两种环境同时出现时都不取，交给模型去问。"""
    found = conditions_in("生产环境 acct_bg_001 的 pay_bg_001_0003 没收到通知")
    assert found == {
        "account_id": "acct_bg_001",
        "payment_id": "pay_bg_001_0003",
        "environment": "production",
    }
    assert "environment" not in conditions_in("生产和沙箱都试过了")
    assert conditions_in("帮我看看昨天的支付") == {}


async def test_changing_a_value_is_recorded_as_a_correction_not_an_addition() -> None:
    """同一字段换了值即视为改口，Revision 留下旧值供提示词说明作废范围。"""
    case = Case()
    first = case.apply(conditions_in("acct_bg_001 生产环境"))
    assert first.added == {"account_id": "acct_bg_001", "environment": "production"}
    assert first.changed == {}
    second = case.apply(conditions_in("说错了，是 acct_bm_030"))
    assert second.changed == {"account_id": "acct_bg_001"}
    assert case.known["account_id"] == "acct_bm_030"
    assert case.known["environment"] == "production"


async def test_changing_the_account_drops_identifiers_that_belonged_to_it() -> None:
    """账户或环境一改，属于旧集合的编号就作废；本条消息里刚给的编号保留。

    这是真跑一遍才发现的：用户改口换账户后，旧账户的 payment_id 仍留在已知条件里，
    下一轮会被当成仍然可用的条件交给模型。
    """
    case = Case()
    case.apply(conditions_in("生产环境 acct_bg_001 的 pay_bg_001_0003"))
    revision = case.apply(conditions_in("账户说错了，是 acct_bm_030，请求编号 req_bm_030_01"))
    assert revision.dropped == {"payment_id": "pay_bg_001_0003"}
    assert case.known == {
        "account_id": "acct_bm_030",
        "environment": "production",
        "request_id": "req_bm_030_01",
    }


async def test_adding_a_condition_keeps_earlier_identifiers() -> None:
    """只是补齐条件时不动已有编号：作用域没变，旧编号仍然指向同一批记录。"""
    case = Case()
    case.apply(conditions_in("acct_bg_001 的 pay_bg_001_0003"))
    revision = case.apply(conditions_in("是生产环境"))
    assert revision.dropped == {} and case.known["payment_id"] == "pay_bg_001_0003"


async def test_asking_for_missing_fields_ends_the_turn_waiting_for_the_user(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """条件不足时模型调 ask_user；本轮就此结束，等用户补充，缺项写进检查点。"""
    manager, model, _ = runtime
    model.responses = [
        completion(calls=[ask_call("a", ["account_id", "environment"], "请问是哪个账户和环境？")])
    ]
    sid = (await manager.create_session())["id"]
    await say(manager, sid, "one", "有笔支付成功了但没收到通知")
    session = await manager.get_session(sid)
    assert session["status"] == "waiting_user"
    assert "哪个账户和环境" in require_text(session["messages"][-1]["content"])
    state = state_of(manager, sid)
    assert require_list(state["waiting_for"]) == ["account_id", "environment"]
    assert state["stop_reason"] == "need_user_input"


async def test_a_paused_turn_leaves_every_tool_call_answered(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """追问结束本轮之前，同一回复里的工具结果必须全部回填。

    检查点里留下没有配对的 tool_calls，下一轮请求会被供应商以 400 拒绝——
    专题 T2 踩过一次，这里用下一轮真的跑起来作为证据。
    """
    manager, model, _ = runtime
    model.responses = [
        completion(calls=[ask_call("a", ["account_id"], "哪个账户？")]),
        completion("已确认这笔支付成功。"),
    ]
    sid = (await manager.create_session())["id"]
    await say(manager, sid, "one", "支付没收到通知")
    messages = require_list(state_of(manager, sid)["messages"])
    assistant = [require_object(m) for m in messages if require_object(m)["role"] == "assistant"]
    filled = [require_object(m) for m in messages if require_object(m)["role"] == "tool"]
    assert len(require_list(assistant[-1]["tool_calls"])) == len(filled)
    await say(manager, sid, "two", "生产环境 acct_bg_001")
    assert (await manager.get_session(sid))["status"] == "completed"


async def test_supplied_conditions_are_handed_back_and_not_asked_again(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """补充过的条件进入下一轮的消息，模型再问同一项会被退回，本轮不结束。"""
    manager, model, _ = runtime
    model.responses = [
        completion(calls=[ask_call("a", ["account_id", "environment"], "哪个账户和环境？")]),
        # 第二轮明知故问：条件已经在检查点里，这次调用必须被退回而不是发给用户。
        completion(calls=[ask_call("b", ["account_id"], "再问一次账户？")]),
        completion(calls=[payments_call("c", "acct_bg_001", "production", "pay_bg_001_0003")]),
        completion("支付 pay_bg_001_0003 状态为 succeeded。"),
    ]
    sid = (await manager.create_session())["id"]
    await say(manager, sid, "one", "支付没收到通知")
    await say(manager, sid, "two", "生产环境 acct_bg_001 的 pay_bg_001_0003")
    session = await manager.get_session(sid)
    assert session["status"] == "completed"
    assert "acct_bg_001" in request_text(model, 1) and "不要再次索要" in request_text(model, 1)
    # 被退回的追问把已知值交还给模型，并说明不必重复索要。
    assert "这些条件用户已经提供" in request_text(model, 2)
    assert require_object(state_of(manager, sid)["case"])["known"] == {
        "account_id": "acct_bg_001",
        "environment": "production",
        "payment_id": "pay_bg_001_0003",
    }


async def test_a_corrected_condition_is_announced_as_invalidating_old_results(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """用户改口时，下一次请求里要写明改了什么、旧结果不再适用。"""
    manager, model, _ = runtime
    model.responses = [
        completion(calls=[payments_call("a", "acct_bg_001", "production", "pay_bg_001_0003")]),
        completion("这笔支付成功了。"),
        completion("账户已更正，需要重新查询。"),
    ]
    sid = (await manager.create_session())["id"]
    await say(manager, sid, "one", "生产环境 acct_bg_001 的 pay_bg_001_0003 怎么样")
    await say(manager, sid, "two", "搞错了，账户是 acct_bm_030")
    briefed = request_text(model, 2)
    assert "account_id 由 acct_bg_001 改为 acct_bm_030" in briefed
    assert "旧值查到的结果不再适用" in briefed
    known = require_object(require_object(state_of(manager, sid)["case"])["known"])
    assert known["account_id"] == "acct_bm_030"


async def test_the_same_query_is_not_run_twice_across_turns(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """同一条件的同一查询跨轮只打一次库；结果还在消息历史里，模型被要求复用。"""
    manager, model, search = runtime
    model.responses = [
        completion(calls=[search_document("a", "端点禁用规则")]),
        completion("按规则会禁用端点。"),
        completion(calls=[search_document("b", "端点禁用规则")]),
        completion("上一轮已经查过，结论不变。"),
    ]
    sid = (await manager.create_session())["id"]
    await say(manager, sid, "one", "端点被禁用之后还能恢复吗")
    await say(manager, sid, "two", "再确认一下这条规则")
    assert len(search.queries) == 1
    assert "已经执行过" in request_text(model, 3)


async def test_identifiers_from_an_earlier_turn_are_not_treated_as_invented(
    runtime: tuple[SessionManager, Model, Search],
) -> None:
    """编号在追问那一轮给出、之后没有复述时，引用它同样不算编造。

    s4 的条件本来就靠 ask_user 分轮收集，Case 跨轮记着它们；若引用核对只认本轮消息，
    模型一提这些编号就被驳回，而工具查空时它们永远进不了 seen，改写注定失败。
    """
    manager, model, _ = runtime
    model.responses = [
        completion(calls=[ask_call("a", ["account_id", "payment_id"], "请问是哪个账户和支付？")]),
        completion("acct_bm_006 的 pay_bm_006 目前查不到记录。"),
        completion("没有请求编号时，acct_bm_006 的 pay_bm_006 只能按账户和环境列全部记录。"),
    ]
    sid = (await manager.create_session())["id"]
    await say(manager, sid, "one", "有两笔金额很特殊的支付怎么都建不出来")
    await say(manager, sid, "two", "这个账户的编号是 acct_bm_006，那笔支付的编号是 pay_bm_006")
    await say(manager, sid, "three", "没有 request id 就无法查询了吗？")
    assert not [e for e in manager.store.read(sid) if e["type"] == "answer.disputed"]
    view = await manager.get_session(sid)
    assert require_text(view["status"]) == "completed"
    assert "pay_bm_006" in require_text(view["messages"][-1]["content"])
