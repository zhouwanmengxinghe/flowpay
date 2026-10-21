"""验证文档准备的来源边界和引用定位，不依赖网络或数据库。"""

import hashlib
import json
from pathlib import Path

import pytest
from scripts.ingestion.corpus import CorpusSettings, checked_bytes, prepare_corpus

from flowpay.core.json import JsonObject, require_int, require_text
from flowpay.core.knowledge.documents import parse_document, split_document
from flowpay.core.llm.embedding import EmbeddingSettings


def document_text(doc_id: str = "DOC-1") -> str:
    return (
        f"---\ndoc_id: {doc_id}\ntitle: 通知规则\ndoc_type: business_rule\n"
        + "indexing: include\neffective_from: 2026-01-01\n---\n"
        + "# 通知规则\n\n## 重试\n\nHTTP 410 会禁用 Endpoint。\n"
        + "```text\n# 这不是章节标题\n```\n"
        + "重复说明。" * 120
        + "\n"
    )


def test_chunks_preserve_source_locations_and_ignore_code_headings() -> None:
    document, start = parse_document("knowledge/rules.md", document_text())
    chunks = split_document(document, start, 160)
    assert len(chunks) > 1
    assert document.metadata["effective_from"] == "2026-01-01"
    assert [chunk.chunk_no for chunk in chunks] == list(range(1, len(chunks) + 1))
    for chunk in chunks:
        where = chunk.source_locator
        assert where["heading"] == "通知规则 / 重试"
        assert (
            chunk.content
            == require_text(where["prefix"])
            + document.content[require_int(where["start_char"]) : require_int(where["end_char"])]
        )
        assert len(chunk.content) <= 160
        assert "indexing:" not in chunk.content
    assert "# 这不是章节标题" in "".join(
        document.content[
            require_int(c.source_locator["start_char"]) : require_int(c.source_locator["end_char"])
        ]
        for c in chunks
    )


def test_structure_splitter_overlaps_and_prefers_paragraph_boundaries() -> None:
    from flowpay.core.knowledge.models import SplitterConfig

    text = document_text().replace("重复说明。" * 120, ("第一段内容。\n\n第二段内容。\n\n" * 45))
    document, start = parse_document("knowledge/rules.md", text)
    config = SplitterConfig(
        max_chunk_chars=220,
        name="markdown-structure-character-v2",
        overlap_chars=30,
    )
    chunks = split_document(document, start, config)
    assert len(chunks) > 1
    locators = [chunk.source_locator for chunk in chunks]
    assert all(
        int(left["end_char"]) > int(right["start_char"])
        for left, right in zip(locators, locators[1:], strict=False)
    )
    assert all(len(chunk.content) <= config.max_chunk_chars for chunk in chunks)
    assert all(
        chunk.content
        == locators[index]["prefix"]
        + document.content[int(locators[index]["start_char"]) : int(locators[index]["end_char"])]
        for index, chunk in enumerate(chunks)
    )


def test_non_indexable_and_empty_documents_fail_before_embedding(tmp_path: Path) -> None:
    config = write_release(
        tmp_path, {"knowledge/rules.md": document_text().replace("include", "exclude")}
    )
    with pytest.raises(ValueError, match="not indexable"):
        prepare_corpus(tmp_path, config, embedding())
    document, start = parse_document("knowledge/rules.md", document_text().split("## 重试")[0])
    with pytest.raises(ValueError, match="no indexable body"):
        split_document(document, start, 1000)


def write_release(root: Path, sources: dict[str, str]) -> CorpusSettings:
    artifacts: list[JsonObject] = []
    for relative, content in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = content.encode()
        path.write_bytes(raw)
        artifacts.append(
            {
                "path": "data/" + relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
    canonical = root / "generated/canonical/manifest.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}")
    release: JsonObject = {
        "status": "frozen",
        "release_id": "test-release",
        "artifact_groups": [
            {
                "group_id": "published_knowledge",
                "visibility": "agent_knowledge",
                "file_count": len(artifacts),
                "artifacts": artifacts,
            },
            {
                "group_id": "canonical_agent_data",
                "visibility": "agent_via_typed_tools",
                "file_count": 1,
                "artifacts": [
                    {
                        "path": "data/generated/canonical/manifest.json",
                        "sha256": hashlib.sha256(b"{}").hexdigest(),
                        "bytes": 2,
                    }
                ],
            },
        ],
    }
    raw_release = json.dumps(release).encode()
    (root / "release.json").write_bytes(raw_release)
    return CorpusSettings("test", "release.json", hashlib.sha256(raw_release).hexdigest())


def embedding() -> EmbeddingSettings:
    return EmbeddingSettings("fixture", "http://localhost/v1", 3, "UNUSED")


def test_plan_is_repeatable_and_rejects_changed_source(tmp_path: Path) -> None:
    settings = write_release(tmp_path, {"knowledge/rules.md": document_text()})
    first = prepare_corpus(tmp_path, settings, embedding())
    assert first.summary() == prepare_corpus(tmp_path, settings, embedding()).summary()
    assert first.expected_documents == {"DOC-1": 1}
    (tmp_path / "knowledge/rules.md").write_text(document_text() + "changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        prepare_corpus(tmp_path, settings, embedding())


def test_hidden_path_in_published_group_is_rejected(tmp_path: Path) -> None:
    settings = write_release(tmp_path, {"audit/answers.md": document_text()})
    with pytest.raises(ValueError, match="not a knowledge Markdown"):
        prepare_corpus(tmp_path, settings, embedding())


def test_duplicate_document_ids_are_rejected(tmp_path: Path) -> None:
    settings = write_release(
        tmp_path, {"knowledge/a.md": document_text(), "knowledge/b.md": document_text()}
    )
    with pytest.raises(ValueError, match="Duplicate doc_id"):
        prepare_corpus(tmp_path, settings, embedding())


def test_source_symlink_cannot_escape_dataset(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    root.mkdir()
    external = tmp_path / "secret.md"
    external.write_text("outside")
    try:
        (root / "link.md").symlink_to(external)
    except OSError as error:
        pytest.skip(f"The current Windows account cannot create symlinks: {error}")
    with pytest.raises(ValueError, match="escapes dataset root"):
        checked_bytes(root, "link.md", hashlib.sha256(b"outside").hexdigest())


def test_unknown_corpus_setting_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "corpus.toml"
    path.write_text(
        'collection_id="test"\nrelease_manifest="manifest.json"\nrelease_manifest_sha256="hash"\ntypo=true\n'
    )
    with pytest.raises(ValueError, match="Unknown"):
        CorpusSettings.load(path)


def test_build_identity_keeps_persisted_json_shape(tmp_path: Path) -> None:
    """类建模不能改变已有集合的构建身份，否则会误报冲突并要求重建向量。"""
    from pydantic import TypeAdapter
    from scripts.ingestion.models import KnowledgeBuildConfig

    from flowpay.core.json import to_json_object
    from flowpay.core.llm.embedding.ollama import QUERY_INSTRUCTION

    config = write_release(tmp_path, {"knowledge/rules.md": document_text()})
    corpus = prepare_corpus(tmp_path, config, embedding())
    expected = {
        "importer_version": "s0-markdown-v1",
        "release_manifest": config.release_manifest,
        "release_manifest_sha256": config.release_manifest_sha256,
        "document_sources": {
            "knowledge/rules.md": hashlib.sha256(document_text().encode()).hexdigest()
        },
        "splitter": {
            "name": "markdown-heading-character-v1",
            "max_chunk_chars": 1000,
            "overlap_chars": 0,
            "prefix": "title-and-heading-v1",
        },
        "embedding": embedding().identity(),
        "query_instruction": QUERY_INSTRUCTION,
    }
    assert to_json_object(corpus.build_config) == expected
    assert TypeAdapter(KnowledgeBuildConfig).validate_python(expected) == corpus.build_config
    assert to_json_object({"plan": corpus.summary()})["plan"] == to_json_object(corpus.summary())
    assert to_json_object(corpus.summary())["build_config"] == expected


@pytest.mark.parametrize("invalid", [True, "1", 1.5])
def test_locator_rejects_non_integer_offsets(invalid: object) -> None:
    """恢复定位时不能将布尔值、文本或小数悄悄转换成字符偏移。"""
    from pydantic import TypeAdapter, ValidationError

    from flowpay.core.knowledge.models import MarkdownLocator

    with pytest.raises(ValidationError):
        TypeAdapter(MarkdownLocator).validate_python(
            {
                "heading": "",
                "prefix": "",
                "start_char": invalid,
                "end_char": 10,
                "start_line": 1,
                "end_line": 2,
            }
        )
