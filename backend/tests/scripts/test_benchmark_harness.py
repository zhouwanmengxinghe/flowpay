"""benchmark harness：证据对齐、追问判定与六种偏离分类。

这些判断决定报表数字的含义，且都不依赖模型，可以用构造数据钉死。
"""

import json

import pytest
from scripts.benchmark import conversation, evidence
from scripts.benchmark.run import Row, overview, supported, violated

from flowpay.core.event_log.types import Event
from flowpay.dataset import dataset_root

# 证据对齐要读数据集里的原文；判分与追问判定是纯函数，但为保持一份跳过口径，整份一起跳。
pytestmark = pytest.mark.skipif(
    dataset_root() is None, reason="FLOWPAY_DATASET_ROOT is not configured"
)


def event(kind: str, payload: dict[str, object], run: str = "run_1") -> Event:
    """构造一条最小事件；harness 只读 type、run_id 与 payload。"""
    return {
        "schema_version": 1,
        "event_id": f"evt_{kind}",
        "session_id": "s",
        "seq": 1,
        "run_id": run,
        "call_id": None,
        "parent_call_id": None,
        "timestamp": "2026-09-17T00:00:00Z",
        "type": kind,
        "payload": payload,
    }


def ask_event(missing: list[str], run: str = "run_1") -> Event:
    """构造一次 ask_user 工具调用。"""
    return event(
        "tool.started",
        {"name": "ask_user", "arguments": json.dumps({"missing": missing, "question": "？"})},
        run,
    )


def test_chunk_alignment_skips_the_title_only_block() -> None:
    """benchmark 把纯标题节算一块，课程分块器跳过它，因此编号整体差一。

    对齐靠小节标题而不是编号：纯标题块映射到空串，调用方据此知道课程侧没有对应物。
    """
    alignment = evidence.chunk_alignment()
    assert alignment["doc:KB-BR-001#c001"] == ""
    assert alignment["doc:KB-BR-001#c002"] == "适用范围"
    assert alignment["doc:KB-BR-001#c003"] == "Account 是租户边界"


def test_evidence_matches_by_heading_not_by_number() -> None:
    """课程取到某小节时，应命中 benchmark 里同标题的那一块。"""
    alignment = evidence.chunk_alignment()
    events = [
        event(
            "knowledge.response",
            {
                "results": [
                    {
                        "id": "doc:KB-BR-001#c002",
                        "source": {"locator": {"heading": "文档标题 / Account 是租户边界"}},
                    }
                ]
            },
        )
    ]
    assert "doc:KB-BR-001#c003" in evidence.evidence_ids(events, alignment)


def test_business_identifiers_come_from_tool_results_only() -> None:
    """只有工具真的返回过的编号才算证据；回答里提到的不算。"""
    events = [event("tool.result", {"rows": [{"payment_id": "pay_bg_001_0003"}]})]
    assert "record:pay_bg_001_0003" in evidence.evidence_ids(events, {})


def test_waiting_user_is_the_precise_clarification_signal() -> None:
    """s4 以 waiting_user 结束本轮时，缺项由 ask_user 的 missing 字段直接给出。"""
    events = [ask_event(["account_id", "payment_id"])]
    signal, categories = conversation.detect("waiting_user", "请问账户？", events, "run_1", set())
    assert signal == "waiting_user"
    assert categories == ["account_context", "identifiers"]


def test_text_fallback_requires_a_real_question_mark() -> None:
    """关键词会误匹配告别语和能力介绍，因此第三级信号必须同时有问号。"""
    closing = "好的，有需要随时找我。以后排查请带上账户 ID 和环境。"
    signal, _ = conversation.detect("completed", closing, [], "run_1", {"account_context"})
    assert signal == "none"
    asking, categories = conversation.detect(
        "completed", "请问是哪个账户？", [], "run_1", {"account_context"}
    )
    assert asking == "text"
    assert categories == ["account_context"]


def test_transcript_records_each_deviation_once() -> None:
    """同种偏离只记一次，报表按题计数。"""
    script = conversation.Transcript()
    script.note("repeated_category")
    script.note("repeated_category")
    script.note("unexpected_category")
    assert script.deviations == ["repeated_category", "unexpected_category"]


def test_assertion_matching_is_specific_enough() -> None:
    """断言判定要能认出改写，也不能对无关正文误判。"""
    assertion = "业务对象只能由所属 Account 访问；跨账户访问统一返回资源不存在或不可见语义。"
    assert supported(
        "业务对象只能由所属 Account 访问；跨账户访问统一返回资源不存在或不可见语义。", assertion
    )
    assert not supported("端点连续失败达到阈值后会被自动停用，需要人工恢复。", assertion)


def test_forbidden_conclusions_use_substring_matching() -> None:
    """禁止结论只认完整子串。

    它与正确结论共享同一批词，二元组重叠会把标准答案误判为违规（实测 17.3%）。
    代价是改写过的违规说法判不出来，属于漏判，方向上比误伤安全。
    """
    forbidden = "把 FP-4041 直接解释为 Payment ID 拼写错误"
    assert violated(f"我们认为{forbidden}，建议核对。", forbidden)
    assert not violated("FP-4041 同时表示不存在与不可见，不能据此判断拼写。", forbidden)


def row(identifier: str, *, multi: bool, passed: bool) -> Row:
    """构造一条最小结果行；总览只读 multi_turn、task_pass 与 evidence_pass。"""
    return {
        "question_id": identifier,
        "question_type": "simple_retrieval",
        "multi_turn": multi,
        "doc_hit": 0,
        "doc_total": 1,
        "record_hit": 0,
        "record_total": 0,
        "assertion_hit": 0,
        "assertion_total": 1,
        "negative_hit": 0,
        "negative_total": 1,
        "evidence_pass": False,
        "task_pass": passed,
        "seconds": 0.0,
        "deviations": [],
        "signals": [],
    }


def test_overview_separates_single_turn_from_multi_turn() -> None:
    """单轮与多轮必须分开统计。

    多轮题全部要求实例诊断，奖励的能力与单轮文档题不同；合并计分会把"某一级擅长
    哪类题"这个唯一有意义的结论抹平——实测里 s2 的强项在单轮、s4 在多轮，
    合计分数却看不出差别。
    """
    rows = [
        row("a", multi=False, passed=True),
        row("b", multi=False, passed=False),
        row("c", multi=True, passed=True),
        row("d", multi=True, passed=True),
    ]
    table = "\n".join(overview({"s4": rows}))
    assert "| s4 | 1/2 | 2/2 | 3/4 | 0/4 |" in table
