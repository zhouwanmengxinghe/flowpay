"""Markdown 分块配置与原文位置；可供各阶段的知识构建复用。"""

from dataclasses import dataclass

from pydantic import StrictInt


@dataclass(frozen=True)
class SplitterConfig:
    """确定分块方式；这些字段参与已有向量集合的兼容性判断。"""

    max_chunk_chars: int
    name: str = "markdown-heading-character-v1"
    overlap_chars: int = 0
    prefix: str = "title-and-heading-v1"


@dataclass(frozen=True)
class MarkdownLocator:
    """原文字符窗口及展示前缀，用于从文档重建检索片段。"""

    heading: str
    start_char: StrictInt
    end_char: StrictInt
    start_line: StrictInt
    end_line: StrictInt
    prefix: str
