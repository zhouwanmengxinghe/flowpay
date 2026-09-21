"""用 dataset 的 300 题跑课程阶段，产出按能力分层的对照报表。

不修改课程代码：只通过 SessionManager 的公开接口提交消息，其余全部从事件日志投影。
多轮题由 dataset 自带的确定性模拟用户驱动，追问判定见 conversation.py。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from flowpay.bootstrap import create_workflow
from flowpay.core.config import load_settings
from flowpay.core.event_log.runtime import open_store
from flowpay.core.event_log.types import Event
from flowpay.core.execution.manager import SessionManager
from flowpay.core.json import (
    JsonObject,
    parse_json,
    require_list,
    require_object,
    require_text,
)
from flowpay.core.llm.embedding.ollama import ollama_identity
from typing_extensions import TypedDict

ROOT = Path(__file__).resolve().parents[2]
# 同时支持 python scripts/benchmark/run.py 与 python -m scripts.benchmark.run。
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from flowpay.dataset import require_dataset

from scripts.benchmark import conversation, evidence
from scripts.benchmark.simulated import SimulatedUser, keyword_hits

TYPES = (
    "simple_retrieval",
    "multi_document",
    "multi_hop",
    "temporal_versioned",
    "multi_source",
    "tool_use_diagnostic",
)

ReadEvents = Callable[[str], list[Event]]


class Row(TypedDict):
    """一道题在一个阶段上的结果；逐题写进 <阶段>.jsonl，报表由它汇总。"""

    question_id: str
    question_type: str
    multi_turn: bool
    doc_hit: int
    doc_total: int
    record_hit: int
    record_total: int
    assertion_hit: int
    assertion_total: int
    negative_hit: int
    negative_total: int
    evidence_pass: bool
    task_pass: bool
    seconds: float
    deviations: list[str]
    signals: list[str]


class Arguments(argparse.Namespace):
    """命令行参数。"""

    stages: str = "s1,s2,s3,s4"
    per_type: int = 2
    config: Path = ROOT / "backend/configs/local.toml"


def blueprints() -> list[JsonObject]:
    """读取 300 题；不区分 dev/test，课程不调参，没有过拟合风险。"""
    source = require_dataset() / "benchmark/design/benchmark_blueprints.jsonl"
    lines = source.read_text(encoding="utf-8").splitlines()
    return [require_object(parse_json(line)) for line in lines]


def sample(rows: list[JsonObject], per_type: int) -> list[JsonObject]:
    """每种题型取前若干道，保证六类都有；per_type 为 0 表示全量。"""
    if not per_type:
        return rows
    picked: list[JsonObject] = []
    for kind in TYPES:
        matching = [r for r in rows if require_text(r["question_type"]) == kind]
        picked.extend(matching[:per_type])
    return picked


def turn_limit(talk: JsonObject) -> int:
    """本题允许的澄清轮数；缺省按 0 处理，即只提交一次。"""
    value = talk.get("max_clarification_turns")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def latest_run(events: list[Event]) -> str | None:
    """最后一次 run.started 的 run_id。"""
    started = [e for e in events if e["type"] == "run.started"]
    return started[-1]["run_id"] if started else None


async def play(
    manager: SessionManager, case: JsonObject, read: ReadEvents
) -> tuple[str, list[Event], conversation.Transcript]:
    """跑完一道题，返回最终回答、全部事件与多轮过程记录。

    单轮题只提交一次。多轮题按澄清预算循环：每轮先判定助手是不是在追问，
    是则由模拟用户披露对应类别的事实再提交下一轮，否则本题结束。
    """
    talk = require_object(case["conversation"])
    multi = require_text(talk["mode"]) == "progressive_disclosure"
    question = require_text(talk["initial_user_message"] if multi else case["question"])
    limit = turn_limit(talk) if multi else 0
    user = SimulatedUser(dict(talk))
    script = conversation.Transcript()
    session = require_text((await manager.create_session())["id"])
    identifier = require_text(case["question_id"])
    message = question
    answer = ""
    events: list[Event] = []
    for turn in range(limit + 1):
        await manager.submit_message(session, f"{identifier}-{turn}", message)
        await manager.tasks[session]
        view = await manager.get_session(session)
        events = read(session)
        messages = view["messages"]
        answer = require_text(messages[-1]["content"]) if messages else ""
        if not multi:
            break
        signal, categories = conversation.detect(
            view["status"], answer, events, latest_run(events), keyword_hits(answer)
        )
        if signal == "none":
            # 一次都没追问就作答，和追问后正常收尾是两回事：前者是跳过澄清，
            # 后者是标准路径。只有"看着像在问却认不出问的是什么"才算判定失败。
            if not script.exchanges:
                script.note("answered_without_clarification")
            elif conversation.looks_like_question(answer):
                script.note("clarification_not_recognized")
            break
        if turn == limit:
            script.note("max_clarifications_exceeded")
            break
        seen = {c for exchange in script.exchanges for c in exchange.categories}
        if categories and set(categories) <= seen:
            script.note("repeated_category")
        reply, disclosed = user.answer(answer)
        if not disclosed:
            script.note("unexpected_category")
        script.exchanges.append(
            conversation.Exchange(signal, categories, disclosed, reply[:200])
        )
        message = reply
    return answer, events, script


def violated(answer: str, forbidden: str) -> bool:
    """回答是否说出了被禁止的结论。

    禁止结论与正确结论共享同一批词（同一主题、相反判断），二元组重叠分不开两者：
    实测 300 条禁止结论里有 17.3% 会把标准答案误判为违规。因此这里只认完整子串，
    该口径在标准答案上误判为 0。代价是改写过的违规说法判不出来，属于漏判而非误判。
    """
    return "".join(forbidden.split()) in "".join(answer.split())


def supported(answer: str, assertion: str) -> bool:
    """断言是否被回答覆盖：子串命中，或二元组重叠过半，与 dataset 评分器同口径。

    实测 530 条必需结论：对本题标准答案判为支持 100%，对随机别题答案误判 1%。
    """
    text = "".join(answer.split())
    target = "".join(assertion.split())
    if not target:
        return False
    if target in text:
        return True
    grams = {target[index : index + 2] for index in range(len(target) - 1)}
    return bool(grams) and sum(gram in text for gram in grams) / len(grams) >= 0.5


def score(
    case: JsonObject, answer: str, events: list[Event], alignment: dict[str, str]
) -> Row:
    """按证据覆盖与断言支持打分；不报 strict，两边工具词汇不同。"""
    identifier = require_text(case["question_id"])
    documents, records = evidence.expected_ids(identifier)
    found = evidence.evidence_ids(events, alignment)
    doc_hit, doc_total = evidence.coverage(documents, found)
    record_hit, record_total = evidence.coverage(records, found)
    truth = require_object(case["ground_truth"])
    assertions = [
        require_object(item) for item in require_list(truth["required_assertions"])
    ]
    # 禁止结论的文本形如"不得认定：X"，判定时去掉前缀，只比对结论本身。
    forbidden = [
        require_text(item).split("：", 1)[-1]
        for item in require_list(truth.get("negative_assertions") or [])
    ]
    supported_count = sum(
        supported(answer, require_text(a["text"])) for a in assertions
    )
    breaches = sum(violated(answer, text) for text in forbidden)
    return {
        "question_id": identifier,
        "question_type": require_text(case["question_type"]),
        "multi_turn": require_text(require_object(case["conversation"])["mode"])
        == "progressive_disclosure",
        "doc_hit": doc_hit,
        "doc_total": doc_total,
        "record_hit": record_hit,
        "record_total": record_total,
        "assertion_hit": supported_count,
        "assertion_total": len(assertions),
        "negative_hit": breaches,
        "negative_total": len(forbidden),
        # 两条独立的轴：证据命中衡量"有没有走到指定证据"，任务通过衡量"结论对不对"。
        # 用别的文档答对时证据不通过而任务通过，两者不能互相替代。
        "evidence_pass": doc_total > 0 and doc_hit == doc_total,
        "task_pass": supported_count == len(assertions) and breaches == 0,
        "seconds": 0.0,
        "deviations": [],
        "signals": [],
    }


def render(rows: list[Row], stage: str) -> list[str]:
    """一个阶段的分层汇总；证据全命中才算通过，断言另算覆盖率。"""
    lines = [
        f"## {stage}",
        "",
        "| 题型 | 题数 | 任务通过 | 证据全命中 | 断言覆盖 | 违反禁止 | 其中多轮 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for kind in TYPES:
        group = [row for row in rows if row["question_type"] == kind]
        if not group:
            continue
        task = sum(row["task_pass"] for row in group)
        passed = sum(row["evidence_pass"] for row in group)
        hit = sum(row["assertion_hit"] for row in group)
        total = sum(row["assertion_total"] for row in group)
        bad = sum(row["negative_hit"] for row in group)
        multi = sum(row["multi_turn"] for row in group)
        lines.append(
            f"| {kind} | {len(group)} | {task} | {passed} | {hit}/{total} | {bad} | {multi} |"
        )
    counts = Counter(name for row in rows for name in row["deviations"])
    signals = Counter(name for row in rows for name in row["signals"])
    if signals:
        lines += ["", "澄清信号：" + "、".join(f"{k} {v}" for k, v in signals.items())]
    if counts:
        lines += ["多轮偏离：" + "、".join(f"{k} {v}" for k, v in counts.items())]
    return [*lines, ""]


def overview(results: dict[str, list[Row]]) -> list[str]:
    """跨阶段总表，按单轮与多轮分开。

    合在一起看会把结论抹平：单轮是文档题，多轮全部要求实例诊断，两类题奖励的是
    不同能力。后续阶段不是前面阶段的超集，混算出来的总分没有意义。
    """
    lines = [
        "## 分层总览",
        "",
        "| 阶段 | 单轮任务通过 | 多轮任务通过 | 合计 | 证据全命中 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for stage, rows in results.items():
        single = [row for row in rows if not row["multi_turn"]]
        multi = [row for row in rows if row["multi_turn"]]
        one = f"{sum(r['task_pass'] for r in single)}/{len(single)}"
        many = f"{sum(r['task_pass'] for r in multi)}/{len(multi)}"
        both = f"{sum(row['task_pass'] for row in rows)}/{len(rows)}"
        seen = f"{sum(row['evidence_pass'] for row in rows)}/{len(rows)}"
        lines.append(f"| {stage} | {one} | {many} | {both} | {seen} |")
    return [*lines, ""]


async def run_stage(
    stage: str,
    cases: list[JsonObject],
    alignment: dict[str, str],
    args: Arguments,
    output: Path,
) -> list[Row]:
    """在一个阶段上跑完全部题目；每题独立会话，事件写进该阶段自己的目录。"""
    settings = replace(
        load_settings(args.config),
        data_dir=output / stage,
        workflow=stage,
    )
    if settings.embedding is None:
        raise ValueError("Embedding settings required")
    (output / stage).mkdir(parents=True, exist_ok=True)
    await ollama_identity(settings.embedding)
    rows: list[Row] = []
    with open_store(settings) as store:
        manager = SessionManager(store, create_workflow(settings), settings)
        try:
            await manager.recover()
            for case in cases:
                started = perf_counter()
                answer, events, script = await play(manager, case, store.read)
                row = score(case, answer, events, alignment)
                row["seconds"] = round(perf_counter() - started, 1)
                row["deviations"] = list(script.deviations)
                row["signals"] = [exchange.signal for exchange in script.exchanges]
                rows.append(row)
                evidence_text = f"{row['doc_hit']}/{row['doc_total']}"
                assertion_text = f"{row['assertion_hit']}/{row['assertion_total']}"
                label = row["question_id"]
                verdict = "通过" if row["task_pass"] else "未过"
                print(
                    f"  {stage} {label} {verdict} 证据 {evidence_text} 断言 {assertion_text}",
                    flush=True,
                )
        finally:
            await manager.shutdown()
    return rows


async def main() -> None:
    """按阶段依次跑批，逐阶段落盘，最后渲染一张分层报表。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stages", default=Arguments.stages)
    parser.add_argument("--per-type", type=int, default=Arguments.per_type)
    parser.add_argument("--config", type=Path, default=Arguments.config)
    args = parser.parse_args(namespace=Arguments())

    output = (
        ROOT / "var/benchmark" / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
    )
    output.mkdir(parents=True)
    alignment = evidence.chunk_alignment()
    cases = sample(blueprints(), args.per_type)
    model = load_settings(args.config).model.name
    print(f"题目 {len(cases)} 道，阶段 {args.stages}，输出 {output}")

    report = [
        "# Benchmark 对照报表",
        "",
        (
            f"题目 {len(cases)} 道，模型 {model}。单次运行，阶段之间几道题的差距"
            "不足以区分策略差异与模型波动；方向性结论（某一级在哪类题上更强）才可读。"
        ),
        "",
        "两条独立的判据，不能互相替代：",
        "",
        "- **任务通过**：必需结论全部被支持，且没有说出禁止结论。与用了哪些文档无关。",
        "- **证据全命中**：检索是否走到 benchmark 指定的那几块原文。用别的文档答对时不通过。",
        "",
        (
            "不报 strict 路由：benchmark 按实体登记工具，课程按关系登记，两套词汇对不上，"
            "这是 s3 实测得出的设计而非缺陷。"
        ),
        "",
    ]
    results: dict[str, list[Row]] = {}
    for stage in args.stages.split(","):
        rows = await run_stage(stage, cases, alignment, args, output)
        (output / f"{stage}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        results[stage] = rows
    report += overview(results)
    for stage, rows in results.items():
        report += render(rows, stage)
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"报表：{output / 'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
