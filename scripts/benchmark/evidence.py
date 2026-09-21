"""把 benchmark 的 evidence ID 与课程实际取到的片段、记录对齐。

benchmark 和课程各自分块，编号对不上：benchmark 把文档开头的纯标题节也算一块，
课程的分块器跳过它；课程还会按字符上限再切长节。因此不能按固定偏移换算，
这里按**小节标题**对齐——两边都以 Markdown 的 `## ` 分节，标题是共同的锚点。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from flowpay.core.event_log.types import Event
from flowpay.core.json import (
    JsonObject,
    parse_json,
    require_list,
    require_object,
    require_text,
)
from flowpay.dataset import require_dataset

# 业务记录编号：与 s3/s4 工具返回的行对应，用于匹配 sql: / runtime: 两类 evidence。
IDENTIFIER = re.compile(
    r"\b(?:acct|pay|evt|del|we|req|inc|att|cus|sub|inv|ref)_[A-Za-z0-9_]+\b"
)


def identifiers_in(text: str) -> list[str]:
    """文本里出现的业务记录编号；用 finditer 以保留字符串类型。"""
    return [match.group(0) for match in IDENTIFIER.finditer(text)]


def dataset_sections(path: Path) -> list[str]:
    """按 benchmark 的规则分节，返回每节的标题；编号即列表下标加一。

    第一节通常只有文档标题，没有正文。它在 benchmark 里占用 c001，在课程分块里不存在，
    因此返回的标题为空串，调用方据此判定"这一块课程侧没有对应物"。
    """
    text = path.read_text(encoding="utf-8")
    body = text.split("---", 2)[2] if text.startswith("---") else text
    sections: list[list[str]] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("## ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    titles: list[str] = []
    for lines in sections:
        if not "\n".join(lines).strip():
            continue
        first = lines[0].strip()
        titles.append(first[3:].strip() if first.startswith("## ") else "")
    return titles


def chunk_alignment() -> dict[str, str]:
    """benchmark 的 `doc:X#cNNN` → 课程侧的小节标题；纯标题块映射到空串。"""
    mapping: dict[str, str] = {}
    for path in sorted((require_dataset() / "data/knowledge").rglob("*.md")):
        doc_id = path.stem.split("_")[0]
        for index, heading in enumerate(dataset_sections(path), start=1):
            mapping[f"doc:{doc_id}#c{index:03d}"] = heading
    return mapping


def section_of(passage_id: str, locator: JsonObject) -> str:
    """课程片段所属的小节标题；locator 里存的是"文档标题 / 小节标题"。"""
    _ = passage_id
    heading = locator.get("heading")
    text = heading if isinstance(heading, str) else ""
    return text.split(" / ", 1)[1] if " / " in text else text


def observed(events: Sequence[Event]) -> tuple[set[str], set[str]]:
    """从事件里读出本轮实际看到的文档小节与业务记录编号。

    返回 (已取到的 `文档编号#小节标题` 集合, 已出现的业务记录编号集合)。
    工具返回的行原样进入第二个集合，回答里提到但工具没返回过的编号不算数。
    """
    sections: set[str] = set()
    records: set[str] = set()
    for event in events:
        payload = require_object(event["payload"])
        if event["type"] in {"knowledge.response", "document.response"}:
            items = payload.get("results") or payload.get("passages")
            for item in require_list(items if items is not None else []):
                entry = require_object(item)
                identifier = require_text(entry["id"])
                source = require_object(entry.get("source") or {})
                locator = require_object(source.get("locator") or {})
                doc_id = identifier.split("#")[0].removeprefix("doc:")
                sections.add(f"{doc_id}#{section_of(identifier, locator)}")
        if event["type"] == "tool.result":
            records.update(identifiers_in(json.dumps(payload, ensure_ascii=False)))
    return sections, records


def evidence_ids(events: Sequence[Event], alignment: dict[str, str]) -> list[str]:
    """把本轮观察翻译成 benchmark 词汇的 evidence ID，供评分器比对。

    只翻译能对上的部分：文档块按小节标题反查编号；业务记录按编号出现与否匹配
    `sql:` / `runtime:` 两类 selector。对不上的不猜，交由报表记为未覆盖。
    """
    sections, records = observed(events)
    found = [
        key
        for key, heading in alignment.items()
        if heading and f"{key.split('#')[0].removeprefix('doc:')}#{heading}" in sections
    ]
    found += [f"record:{name}" for name in sorted(records)]
    return sorted(found)


def expected_ids(question_id: str) -> tuple[list[str], list[str]]:
    """题目要求的证据，拆成文档块与业务记录两类。

    `negative:` 前缀表示"这条记录本不该存在"，属于结论而不是需要取到的证据，这里排除。
    """
    resolution = require_dataset() / "benchmark/resolved/evidence_resolution.jsonl"
    for line in resolution.read_text(encoding="utf-8").splitlines():
        row = require_object(parse_json(line))
        if require_text(row["question_id"]) != question_id:
            continue
        documents: list[str] = []
        records: list[str] = []
        for value in require_list(row.get("resolved_evidence_ids") or []):
            identifier = require_text(value)
            if identifier.startswith("doc:"):
                documents.append(identifier)
            elif not identifier.startswith("negative:"):
                records.extend(f"record:{name}" for name in identifiers_in(identifier))
        return documents, sorted(set(records))
    return [], []


def coverage(expected: Iterable[str], found: Iterable[str]) -> tuple[int, int]:
    """命中数与应命中数；应命中为 0 时返回 (0, 0)，由调用方决定是否计入。"""
    wanted = list(expected)
    hit = set(found)
    return sum(item in hit for item in wanted), len(wanted)
