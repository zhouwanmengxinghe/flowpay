"""解析已发布 Markdown，保留原文及章节位置，提供可复现的基础分块策略。"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import cast

import yaml

from flowpay.core.json import parse_json, require_object, require_text, to_json_object
from flowpay.core.knowledge import Chunk, Document

from .models import MarkdownLocator


def metadata_scalar(value: object) -> str:
    """YAML 的日期标量转成 ISO 字符串，其他非 JSON 类型明确拒绝。"""
    if isinstance(value, date):
        return value.isoformat()
    raise ValueError(f"Unsupported metadata type: {type(value).__name__}")


def parse_document(source_path: str, content: str) -> tuple[Document, int]:
    """返回完整原文快照与正文起始字符位置；front matter 只进入 metadata。"""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Missing front matter: {source_path}")
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        raise ValueError(f"Unclosed front matter: {source_path}")
    raw_metadata: object = cast(object, yaml.safe_load("".join(lines[1:closing])))
    metadata = require_object(parse_json(json.dumps(raw_metadata, default=metadata_scalar)))
    for key in ("doc_id", "title"):
        if not require_text(metadata.get(key)).strip():
            raise ValueError(f"Missing {key}: {source_path}")
    document = Document(
        require_text(metadata["doc_id"]),
        require_text(metadata["title"]),
        source_path,
        content,
        metadata,
    )
    return document, sum(map(len, lines[: closing + 1]))


def section_ranges(content: str, start: int) -> list[tuple[int, int, str]]:
    """按围栏外的 ATX 标题分节；标题路径用于片段上下文，不重写原文。"""
    sections: list[tuple[int, int, str]] = []
    headings: list[tuple[int, str]] = []
    offset = section_start = start
    section_title = ""
    fence_character = ""
    fence_length = 0
    for line in content[start:].splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*$", line)
        if fence:
            marker = fence.group(1)
            if not fence_character:
                fence_character, fence_length = marker[0], len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                if not line[fence.end() :].strip():
                    fence_character = ""
        elif heading and not fence_character:
            if offset > section_start:
                sections.append((section_start, offset, section_title))
            level, title = len(heading.group(1)), heading.group(2)
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            section_start, section_title = offset, " / ".join(text for _, text in headings)
        offset += len(line)
    if offset > section_start:
        sections.append((section_start, offset, section_title))
    return sections


def split_document(document: Document, body_start: int, max_chars: int) -> list[Chunk]:
    """每块保留原始字符窗口和标题前缀；字符上限包含前缀，编号从 1 开始。"""
    if max_chars < 128:
        raise ValueError("max_chunk_chars must be at least 128")
    chunks: list[Chunk] = []
    for start, end, heading in section_ranges(document.content, body_start):
        section = document.content[start:end]
        # 独立的文档标题无检索正文，标题会作为后续片段的前缀保存。
        meaningful = [line for line in section.splitlines() if line.strip()]
        if not meaningful or (len(meaningful) == 1 and meaningful[0].lstrip().startswith("#")):
            continue
        prefix = f"{document.title}\n{heading}\n\n" if heading else f"{document.title}\n\n"
        window = max_chars - len(prefix)
        if window < 32:
            raise ValueError("Heading prefix leaves insufficient chunk space")
        for position in range(start, end, window):
            stop = min(position + window, end)
            excerpt = document.content[position:stop]
            if not excerpt.strip():
                continue
            locator = MarkdownLocator(
                heading=heading,
                start_char=position,
                end_char=stop,
                start_line=document.content.count("\n", 0, position) + 1,
                end_line=document.content.count("\n", 0, stop - 1) + 1,
                prefix=prefix,
            )
            chunks.append(Chunk(len(chunks) + 1, prefix + excerpt, to_json_object(locator)))
    if not chunks:
        raise ValueError(f"Document has no indexable body: {document.doc_id}")
    return chunks
