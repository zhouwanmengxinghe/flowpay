"""通用 Markdown 能力不要求 FlowPay 的发布范围或文档类型标签。"""

from flowpay.core.knowledge.documents import parse_document, split_document


def test_document_parser_preserves_metadata_without_imposing_release_policy() -> None:
    """发布政策由导入脚本控制，核心解析仍可处理其他来源的文档元数据。"""
    content = "---\ndoc_id: note\ntitle: 笔记\nindexing: exclude\n---\n## 内容\n可解析的原文。\n"
    document, start = parse_document("notes/example.md", content)
    chunks = split_document(document, start, 1000)
    assert document.metadata["indexing"] == "exclude"
    assert "doc_type" not in document.metadata
    assert chunks[0].content.endswith("## 内容\n可解析的原文。\n")
