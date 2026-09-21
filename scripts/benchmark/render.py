"""把 benchmark 的题目渲染成可读的 Markdown，按题型分文件，外加一份索引。

默认读 resolved/resolved_blueprints.jsonl。它是 design 那份的超集：题面、标准答案、
对话脚本两边逐字相同，只多出快照解析后的时间线、轨迹断言与最终证据 ID。

只用标准库，不导入 flowpay，因此不需要 backend/.venv，系统 python3 就能跑。
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import cast

# 题库是动态输入，在 load 一次收窄成这个形状，其余函数只读已收窄的值。
JsonObject = dict[str, object]

ROOT = Path(__file__).resolve().parents[2]

SOURCES = {
    "resolved": "benchmark/resolved/resolved_blueprints.jsonl",
    "design": "benchmark/design/benchmark_blueprints.jsonl",
}

# 六种题型的渲染顺序与中文标题，与 run.py 的 TYPES 一致。
TYPES: tuple[str, ...] = (
    "simple_retrieval",
    "multi_document",
    "multi_hop",
    "temporal_versioned",
    "tool_use_diagnostic",
    "multi_source",
)
TYPE_LABEL: dict[str, str] = {
    "simple_retrieval": "单点检索",
    "multi_document": "跨文档",
    "multi_hop": "多跳",
    "temporal_versioned": "时效版本",
    "tool_use_diagnostic": "工具诊断",
    "multi_source": "跨数据源",
}


def dataset_root(explicit: str | None) -> Path:
    """数据集位置：命令行 > 环境变量 > 项目根的 .env。"""
    if explicit:
        return Path(explicit).expanduser()
    value = os.environ.get("FLOWPAY_DATASET_ROOT")
    if not value:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                key, sep, raw = line.partition("=")
                if sep and key.strip() == "FLOWPAY_DATASET_ROOT":
                    value = raw.strip().strip("\"'")
                    break
    if not value:
        raise SystemExit("找不到数据集：设置 FLOWPAY_DATASET_ROOT 或传 --dataset")
    return Path(value).expanduser()


def load(path: Path) -> list[JsonObject]:
    """一行一题；JSON 是动态输入，在这里收窄，后面的函数不再面对 Any。"""
    if not path.exists():
        raise SystemExit(f"读不到题库：{path}")
    rows: list[JsonObject] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = cast(object, json.loads(line))  # 隔离标准库解码器的 Any
        if not isinstance(value, dict):
            raise SystemExit(f"题库每行都应该是一个对象：{path}")
        rows.append(cast(JsonObject, value))
    return rows


def cell(value: object) -> str:
    """表格单元：竖线和换行会破表，先换掉。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", "<br>")


def items(row: JsonObject, key: str) -> list[object]:
    """取一个列表字段；缺失或类型不符时当空列表，渲染不因个别题缺字段中断。"""
    value = row.get(key)
    return cast(list[object], value) if isinstance(value, list) else []


def mapping(row: JsonObject, key: str) -> JsonObject:
    """取一个对象字段；缺失或类型不符时当空对象。"""
    value = row.get(key)
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def objects(row: JsonObject, key: str) -> list[JsonObject]:
    """取一个由对象组成的列表；非对象元素跳过，不让个别脏数据中断渲染。"""
    return [cast(JsonObject, v) for v in items(row, key) if isinstance(v, dict)]


def code_list(values: list[object]) -> str:
    return "、".join(f"`{v}`" for v in values) if values else "—"


def question(row: JsonObject) -> str:
    """渲染一道题。"""
    qid = row.get("question_id")
    talk = mapping(row, "conversation")
    multi = talk.get("mode") == "progressive_disclosure"
    out = [f"## {qid}", ""]

    meta = [
        f"**{TYPE_LABEL.get(str(row.get('question_type')), '')}**（`{row.get('question_type')}`）",
        f"难度 {row.get('difficulty')}",
        f"{'多轮' if multi else '单轮'}",
        f"`{row.get('split')}`",
    ]
    out += [" · ".join(meta), ""]
    out += [
        f"- 场景：`{row.get('scenario_id')}`",
        f"- 快照时点 `as_of`：{row.get('as_of')}　接口版本：{row.get('api_version_context')}",
    ]
    tags = items(row, "secondary_tags")
    if tags:
        out.append(f"- 标签：{code_list(tags)}")
    out.append("")

    # 多轮题的 question 就是开场白，逐字相同，只渲染一次。
    heading = "### 开场白" if multi else "### 题面"
    out += [heading, "", "> " + str(row.get("question", "")).replace("\n", "\n> "), ""]

    if multi:
        out += [
            f"### 对话脚本（最多追问 {talk.get('max_clarification_turns')} 轮）",
            "",
        ]
        out += ["开场只说表面现象，其余条件要助手自己问。", ""]
        facts = objects(talk, "available_user_facts")
        if facts:
            out += ["模拟用户手里的事实——**问到哪一类才说哪一类**：", ""]
            out += ["| 类别 | 助手问到这些词才披露 | 用户会说 |", "| --- | --- | --- |"]
            for fact in facts:
                asked = "、".join(
                    str(w) for w in items(fact, "disclose_when_asked_about")
                )
                out.append(
                    f"| `{fact.get('category')}` | {cell(asked)} | {cell(fact.get('user_value'))} |"
                )
            out.append("")
        plan = objects(talk, "clarification_plan")
        if plan:
            out += ["预设的追问节奏：", ""]
            for i, step in enumerate(plan, 1):
                cats = "、".join(f"`{c}`" for c in items(step, "categories"))
                out.append(
                    f"{i}. （`{step.get('stage')}`，取 {cats}）{step.get('assistant_question')}"
                )
            out.append("")
        required = items(talk, "required_clarification_categories")
        if required:
            out += [f"必须问全的类别：{code_list(required)}", ""]

    truth = mapping(row, "ground_truth")
    out += ["### 判分标准", ""]
    if truth.get("canonical_answer"):
        out += [
            "标准答案：",
            "",
            "> " + str(truth["canonical_answer"]).replace("\n", "\n> "),
            "",
        ]
    must = objects(truth, "required_assertions")
    if must:
        out += ["必需结论（全部要被证据支持）：", ""]
        out += [f"- `{a.get('assertion_id')}` {a.get('text')}" for a in must]
        out.append("")
    never = items(truth, "negative_assertions")
    if never:
        out += ["禁止结论（说了就算错）：", ""] + [f"- {n}" for n in never] + [""]
    if row.get("stop_condition"):
        out += [f"结束条件：{row['stop_condition']}", ""]

    out += ["### 该走到哪", ""]
    out += [
        f"- 数据源：{code_list(items(row, 'required_sources'))}",
        f"- 工具：{code_list(items(row, 'required_tools'))}",
        f"- 文档：{code_list(items(row, 'required_documents'))}",
        f"- 操作：{code_list(items(row, 'required_operations'))}",
        f"- 规则：{code_list(items(row, 'required_rule_ids'))}",
    ]
    optional = items(row, "optional_tools")
    if optional:
        out.append(f"- 可选工具：{code_list(optional)}")
    shortcuts = items(row, "forbidden_shortcuts")
    if shortcuts:
        out.append(f"- 禁用捷径：{code_list(shortcuts)}")
    out.append("")

    hops = objects(row, "expected_hops")
    if hops:
        out += ["预期步骤：", ""]
        for hop in hops:
            line = f"{hop.get('step')}. {hop.get('purpose')}"
            tools = items(hop, "tools") or items(hop, "operations")
            if tools:
                line += f"　→ {code_list(tools)}"
            out.append(line)
            if hop.get("expected"):
                out.append(f"   - 应得出：{hop['expected']}")
        out.append("")

    selectors = objects(row, "evidence_selectors")
    if selectors:
        out += [
            "证据取值口径：",
            "",
            "| 别名 | 来源 | 工具.操作 | 条件 | 条数 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for sel in selectors:
            where = "、".join(f"`{k}`={v}" for k, v in mapping(sel, "where").items())
            head = f"| `{sel.get('alias')}` | `{sel.get('source')}` |"
            call = f" `{sel.get('tool')}.{sel.get('operation')}` |"
            out.append(f"{head}{call} {cell(where)} | `{sel.get('cardinality')}` |")
        out.append("")

    timeline = objects(row, "resolved_timeline")
    trajectory = objects(row, "resolved_trajectory_assertions")
    evidence = items(row, "resolved_evidence_ids")
    if timeline or trajectory or evidence:
        out += ["### 快照里的实况", ""]
        if timeline:
            out += (
                ["时间线：", ""]
                + [f"- `{t.get('at')}` {t.get('description')}" for t in timeline]
                + [""]
            )
        if trajectory:
            out += ["查到的实例事实：", ""]
            out += [
                f"- `{t.get('selector_alias')}` {t.get('text')}" for t in trajectory
            ]
            out.append("")
        if evidence:
            out += [f"证据 ID：{code_list(evidence)}", ""]

    return "\n".join(out)


def index(rows: list[JsonObject], source: str, files: dict[str, str]) -> str:
    """索引页：统计 + 每题一行。"""
    by_type = Counter(str(r.get("question_type")) for r in rows)
    by_difficulty = Counter(str(r.get("difficulty")) for r in rows)
    by_split = Counter(str(r.get("split")) for r in rows)
    multi = sum(
        1
        for r in rows
        if mapping(r, "conversation").get("mode") == "progressive_disclosure"
    )

    out = [
        "# FlowPay benchmark 题库",
        "",
        f"共 **{len(rows)} 道**，渲染自 `{SOURCES[source]}`。",
        f"单轮 {len(rows) - multi} 道，多轮 {multi} 道；"
        + "难度 "
        + "／".join(f"{k}:{by_difficulty[k]}" for k in sorted(by_difficulty))
        + "；"
        + "／".join(f"{k} {v}" for k, v in sorted(by_split.items()))
        + "。",
        "",
        "| 题型 | 道数 | 文件 |",
        "| --- | --- | --- |",
    ]
    for kind in TYPES:
        if by_type.get(kind):
            out.append(
                f"| {TYPE_LABEL[kind]}（`{kind}`） | {by_type[kind]} | [{files[kind]}]({files[kind]}) |"
            )
    out += [
        "",
        "## 全部题目",
        "",
        "| ID | 题型 | 难度 | 轮次 | 题面 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        kind = str(row.get("question_type"))
        turns = (
            "多轮"
            if mapping(row, "conversation").get("mode") == "progressive_disclosure"
            else "单轮"
        )
        text = str(row.get("question", ""))
        brief = text if len(text) <= 42 else text[:42] + "…"
        link = f"[{row.get('question_id')}]({files[kind]}#{str(row.get('question_id')).lower()})"
        out.append(
            f"| {link} | {TYPE_LABEL.get(kind, kind)} | {row.get('difficulty')} | {turns} | {cell(brief)} |"
        )
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="把 benchmark 题库渲染成 Markdown")
    parser.add_argument(
        "--dataset", help="数据集根目录，默认读 FLOWPAY_DATASET_ROOT 或 .env"
    )
    parser.add_argument(
        "--source", choices=sorted(SOURCES), default="resolved", help="渲染哪一份题库"
    )
    parser.add_argument("--out", help="输出目录，默认写进数据集的 benchmark/readable/")
    parser.add_argument("--type", dest="only", choices=TYPES, help="只渲染一种题型")
    args = parser.parse_args()
    # argparse 的属性是 Any，在这里一次性收窄，后面按普通变量用。
    given_dataset = cast(str | None, args.dataset)
    source = cast(str, args.source)
    only = cast(str | None, args.only)
    given_out = cast(str | None, args.out)

    dataset = dataset_root(given_dataset)
    rows = load(dataset / SOURCES[source])
    rows.sort(
        key=lambda r: (TYPES.index(str(r["question_type"])), str(r["question_id"]))
    )
    if only:
        rows = [r for r in rows if r.get("question_type") == only]

    out_dir = (
        Path(given_out).expanduser() if given_out else dataset / "benchmark/readable"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {kind: f"{i:02d}-{kind}.md" for i, kind in enumerate(TYPES, 1)}

    written: list[Path] = []
    for kind in TYPES:
        group = [r for r in rows if r.get("question_type") == kind]
        if not group:
            continue
        body = [f"# {TYPE_LABEL[kind]}　`{kind}`　{len(group)} 道", ""]
        body += [question(row) for row in group]
        target = out_dir / files[kind]
        target.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
        written.append(target)

    readme = out_dir / "README.md"
    readme.write_text(index(rows, source, files), encoding="utf-8")
    written.insert(0, readme)

    for path in written:
        print(
            f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}  {path.stat().st_size // 1024} KB"
        )


if __name__ == "__main__":
    main()
