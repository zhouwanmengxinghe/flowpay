"""s3 多源调查：真实读取 dataset，检验依赖参数、账户隔离与无效调用。

这里连的是数据集里真实的 SQLite 与日志快照，不用固定模拟证据冒充查询；
数据集位置由 FLOWPAY_DATASET_PATH 指定，未配置时整份跳过。
数据缺失时整组跳过，避免把"没数据"报成"实现有问题"。
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
from flowpay.core.json import JsonObject, parse_json, require_list, require_object
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.dataset import dataset_path, snapshot_ready
from flowpay.router import create_stage

from .fixtures import Model, Search, completion, grounded

ROOT = Path(__file__).resolve().parents[3]
DATASET = dataset_path() or Path("/nonexistent")
POLICY = ROOT / "backend/policies/sources.toml"

# 取自 dataset 的一条真实链路：支付成功，但事件的 Webhook 投递仍在重试。
ACCOUNT, ENVIRONMENT = "acct_bg_001", "production"
PAYMENT, EVENT, DELIVERY = "pay_bg_001_0003", "evt_bg_001_0003", "del_bg_001_0003"

pytestmark = pytest.mark.skipif(
    not snapshot_ready(), reason="business dataset snapshot is not available"
)


def call(identifier: str, name: str, arguments: JsonObject) -> JsonObject:
    """构造一次 SDK function 调用；arguments 仍是协议要求的 JSON 字符串。"""
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def tool_results(model: Model, index: int) -> list[JsonObject]:
    """读出第 index 次模型请求里全部工具观察，按出现顺序返回。"""
    messages = require_list(model.requests[index]["messages"])
    return [
        require_object(parse_json(str(require_object(m)["content"])))
        for m in messages
        if require_object(m)["role"] == "tool"
    ]


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[SessionManager, Model]]:
    """真实业务数据源 + 可控模型；知识检索仍用夹具，本组只关心业务工具。"""
    model, search = Model(), Search()
    settings = Settings(tmp_path, tmp_path, workflow="s3", knowledge_collection_id="fixture")
    resources = RuntimeResources(
        settings,
        sources=DataSources.from_config(POLICY, DATASET),
        retriever=search.for_run,
    )
    store = EventStore(tmp_path / "sessions.sqlite")
    manager = SessionManager(store, create_stage("s3", model, retrieval_input), settings, resources)
    try:
        yield manager, model
    finally:
        await manager.shutdown()
        resources.close()


async def submit(manager: SessionManager, text: str) -> str:
    """新建会话并等待这一轮结束。"""
    sid = (await manager.create_session())["id"]
    await manager.submit_message(sid, "first", text)
    await manager.tasks[sid]
    return sid


async def test_later_queries_use_ids_returned_by_earlier_ones(
    runtime: tuple[SessionManager, Model],
) -> None:
    """支付 → 事件 → 投递：每一步的参数来自上一步真实返回的编号。"""
    manager, model = runtime
    scope = {"account_id": ACCOUNT, "environment": ENVIRONMENT}
    model.responses = [
        completion(calls=[call("a", "query_payments", {**scope, "payment_id": PAYMENT})]),
        completion(calls=[call("b", "query_events", {**scope, "object_id": PAYMENT})]),
        completion(calls=[call("c", "query_deliveries", {**scope, "event_id": EVENT})]),
        grounded(),
    ]
    sid = await submit(manager, f"{PAYMENT} 成功了但没收到通知")

    payment = tool_results(model, 1)[0]["rows"]
    assert require_object(require_list(payment)[0])["status"] == "succeeded"
    event = require_object(require_list(tool_results(model, 2)[1]["rows"])[0])
    assert event["event_id"] == EVENT and event["event_type"] == "payment.completed"
    delivery = require_object(require_list(tool_results(model, 3)[2]["rows"])[0])
    assert delivery["delivery_id"] == DELIVERY and delivery["status"] == "retrying"

    actions = require_list((await manager.get_session(sid))["state"]["actions"])
    assert [require_object(a)["tool"] for a in actions] == [
        "payments",
        "events",
        "webhook_deliveries",
    ]


async def test_another_account_cannot_read_these_records(
    runtime: tuple[SessionManager, Model],
) -> None:
    """账户与环境隔离由登记 SQL 保证：换个账户查同一个事件只会得到空行。"""
    manager, model = runtime
    model.responses = [
        completion(
            calls=[
                call(
                    "a",
                    "query_deliveries",
                    {
                        "account_id": "acct_someone_else",
                        "environment": ENVIRONMENT,
                        "event_id": EVENT,
                    },
                ),
                call(
                    "b",
                    "query_deliveries",
                    {"account_id": ACCOUNT, "environment": "sandbox", "event_id": EVENT},
                ),
            ]
        ),
        grounded(),
    ]
    await submit(manager, "换个账户查查看")
    assert [result["rows"] for result in tool_results(model, 1)] == [[], []]


async def test_invalid_tool_calls_come_back_as_correctable_observations(
    runtime: tuple[SessionManager, Model],
) -> None:
    """缺参数、未知工具和重复查询都回传为可纠正的观察，不中断本轮。"""
    manager, model = runtime
    scope = {"account_id": ACCOUNT, "environment": ENVIRONMENT}
    model.responses = [
        completion(
            calls=[
                call("a", "query_payments", {"payment_id": PAYMENT}),
                call("b", "no_such_tool", {}),
                call("c", "query_payments", {**scope, "payment_id": PAYMENT}),
                call("d", "query_payments", {**scope, "payment_id": PAYMENT}),
            ]
        ),
        grounded(),
    ]
    sid = await submit(manager, "查一下这笔支付")
    missing, unknown, ok, repeated = tool_results(model, 1)
    assert "account_id" in str(missing["error"])
    assert "no_such_tool" in str(unknown["error"])
    assert require_list(ok["rows"])
    assert "已经执行过" in str(repeated["error"])
    # 三次失败调用都不计入预算，只有真正打库的那次记账。
    assert len(require_list((await manager.get_session(sid))["state"]["actions"])) == 1


async def test_one_relation_answers_different_questions(
    runtime: tuple[SessionManager, Model],
) -> None:
    """登记的是关系不是查询：同一个 query_payments 既按支付编号查，也按请求编号反查。

    按请求编号反查是评测里暴露过的断链——当时没有登记这条查询形状，模型走不过去。
    """
    manager, model = runtime
    model.responses = [
        completion(
            calls=[
                call(
                    "a",
                    "query_payments",
                    {"account_id": ACCOUNT, "environment": ENVIRONMENT, "payment_id": PAYMENT},
                ),
                call(
                    "b",
                    "query_payments",
                    {
                        "account_id": "acct_bm_030",
                        "environment": ENVIRONMENT,
                        "created_by_request_id": "req_bm_030_01",
                    },
                ),
            ]
        ),
        grounded(),
    ]
    await submit(manager, "这两笔支付分别怎么样")
    by_id, by_request = tool_results(model, 1)
    assert require_object(require_list(by_id["rows"])[0])["payment_id"] == PAYMENT
    found = require_object(require_list(by_request["rows"])[0])
    assert found["payment_id"] == "pay_bm_030" and found["status"] == "failed"


async def test_scope_only_query_is_refused_unless_the_relation_allows_it(
    runtime: tuple[SessionManager, Model],
) -> None:
    """只给账户与环境会被要求补条件；端点这类行数很少的关系例外。"""
    manager, model = runtime
    scope: JsonObject = {"account_id": ACCOUNT, "environment": ENVIRONMENT}
    model.responses = [
        completion(
            calls=[
                call("a", "query_payments", scope),
                call("b", "query_endpoints", scope),
            ]
        ),
        grounded(),
    ]
    await submit(manager, "这个账户的情况")
    payments, endpoints = tool_results(model, 1)
    assert payments["rows"] == [] and "至少再给一个条件" in str(payments["notice"])
    assert require_list(endpoints["rows"])


async def test_closed_fields_reach_the_model_as_enums(
    runtime: tuple[SessionManager, Model],
) -> None:
    """取值封闭的字段写进 JSON Schema：模型看得到合法取值，而不是自己猜。"""
    manager, model = runtime
    model.responses = [grounded()]
    await submit(manager, "随便问点什么")
    tools = {
        require_object(require_object(t)["function"])["name"]: require_object(
            require_object(require_object(t)["function"])["parameters"]
        )
        for t in require_list(model.requests[0]["tools"])
    }
    incidents = require_object(require_object(tools["list_incidents"])["properties"])
    assert require_object(incidents["region"])["enum"] == ["NA", "EU", "APAC"]
    assert require_object(incidents["service"])["enum"] == ["api", "payments", "webhooks"]
    assert "时区" in str(require_object(incidents["start"])["description"])
    payments = require_object(require_object(tools["query_payments"])["properties"])
    assert require_object(payments["environment"])["enum"] == ["production", "sandbox"]
    # 不封闭的字段不写 enum，否则会拦掉合法编号。
    assert "enum" not in require_object(payments["payment_id"])


async def test_values_outside_the_declared_set_are_refused_before_querying(
    runtime: tuple[SessionManager, Model],
) -> None:
    """猜错的取值在执行前被拦下，模型拿到的是纠正提示，不是会被误读的空结果。"""
    manager, model = runtime
    model.responses = [
        completion(
            calls=[call("a", "list_incidents", {"service": "payments", "region": "us-east-1"})]
        ),
        grounded(),
    ]
    await submit(manager, "是不是平台故障")
    refused = tool_results(model, 1)[0]
    assert "us-east-1" in str(refused["error"]) or "enum" in str(refused["error"])


async def test_log_sources_need_at_least_one_filter(
    runtime: tuple[SessionManager, Model],
) -> None:
    """日志工具至少要一个过滤条件，避免整份文件被读进模型上下文。"""
    manager, model = runtime
    model.responses = [
        completion(calls=[call("a", "list_delivery_attempts", {"delivery_id": DELIVERY})]),
        grounded(),
    ]
    await submit(manager, "看看投递尝试")
    attempts = require_list(tool_results(model, 1)[0]["rows"])
    assert attempts and require_object(attempts[0])["delivery_id"] == DELIVERY


async def test_identifiers_from_an_earlier_turn_are_not_treated_as_invented(
    runtime: tuple[SessionManager, Model],
) -> None:
    """编号在上一轮给出、本轮没有复述时，引用它同样不算编造。

    业务工具查空是常态（跨租户、条件不足、记录确实不存在），空结果里没有编号。
    只认本轮消息的话，模型想说清"这条查不到"就必须提编号，于是被反复驳回。
    """
    manager, model = runtime
    model.responses = [completion(f"{PAYMENT} 的状态还需要继续查证。")]
    sid = await submit(manager, f"支付 {PAYMENT} 一直没收到通知")
    model.responses = [completion(f"{PAYMENT} 在这些条件下查不到记录。")]
    await manager.submit_message(sid, "second", "那还能怎么查？")
    await manager.tasks[sid]
    assert not [e for e in manager.store.read(sid) if e["type"] == "answer.disputed"]
    view = await manager.get_session(sid)
    assert PAYMENT in str(view["messages"][-1]["content"])
