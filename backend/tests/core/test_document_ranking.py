from unittest.mock import AsyncMock, Mock, patch

import pytest

from flowpay.core.knowledge.client import KnowledgeClient
from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.knowledge.document_ranking import (
    DocumentRankingConfig,
    document_lexical_order,
    infer_document_route,
    rank_documents,
)
from flowpay.core.knowledge.ranking import lexical_order, reciprocal_rank_fusion
from flowpay.core.llm.embedding import EmbeddingSettings
from flowpay.core.llm.embedding.ollama import retrieval_input, retrieval_question


def chunk(doc: str, number: int, text: str, kind: str = "api_doc") -> Passage:
    return Passage(
        id=f"doc:{doc}#c{number:03}",
        title=doc,
        text=text,
        score=0.0,
        source=PassageSource(
            collection_id="test",
            doc_id=doc,
            chunk_no=number,
            path=f"{doc}.md",
            sha256="hash",
            locator={},
            metadata={"doc_type": kind},
        ),
    )


def test_collapse_frees_slots_used_by_duplicate_source_documents() -> None:
    repeated = [chunk("A", number, "同一文档的不同片段") for number in range(1, 6)]
    target = chunk("B", 1, "退款完成后到账时间")
    ranking = [*repeated, target]
    baseline = reciprocal_rank_fusion([ranking, ranking])[:5]
    result = rank_documents("退款到账", ranking, ranking, DocumentRankingConfig())
    assert all(item.source.doc_id == "A" for item in baseline)
    assert [item.source.doc_id for item in result] == ["A", "B"]
    assert result[1].text == target.text
    assert result[1].source == target.source


def test_whole_document_bm25_returns_original_passage_not_merged_document() -> None:
    corpus = [chunk("A", 1, "签名校验"), chunk("A", 2, "Webhook 重试"), chunk("B", 1, "退款")]
    documents = document_lexical_order("Webhook 重试", corpus)
    result = rank_documents(
        "Webhook 重试",
        corpus[::-1],
        corpus,
        DocumentRankingConfig(method="whole-document-rrf"),
        whole_documents=documents,
    )
    original = {item.id: item for item in corpus}
    assert len(result) == 2
    assert all(item.text == original[item.id].text for item in result)
    assert corpus[0].text == "签名校验"


def test_route_is_soft_and_based_on_question_intent() -> None:
    api = chunk("A", 1, "参数", "api_doc")
    case = chunk("B", 1, "参数", "support_case")
    plain = rank_documents("请求参数", [case, api], [case, api], DocumentRankingConfig())
    routed = rank_documents(
        "请求参数", [case, api], [case, api], DocumentRankingConfig(route_boost=0.5)
    )
    assert infer_document_route("请求参数怎么填写") == "api_doc"
    assert plain[0].source.doc_id == "B"
    assert routed[0].source.doc_id == "A"
    assert len(routed) == 2


def test_invalid_config_and_missing_document_corpus_are_rejected() -> None:
    with pytest.raises(ValueError):
        DocumentRankingConfig(dense_depth=0)
    with pytest.raises(ValueError):
        DocumentRankingConfig(route_boost=-0.1)
    with pytest.raises(ValueError):
        rank_documents("问题", [], [], DocumentRankingConfig(method="whole-document-rrf"))


def test_lexical_query_removes_only_the_exact_embedding_instruction() -> None:
    question = "Query: 请求参数是什么？"
    assert retrieval_question(retrieval_input(question)) == question
    assert retrieval_question("Instruct: 用户自己的文本\nQuery: 保留") == (
        "Instruct: 用户自己的文本\nQuery: 保留"
    )


async def test_client_document_mode_preserves_filters_and_embedding_input() -> None:
    candidates = [chunk("A", 1, "参数"), chunk("A", 2, "重试"), chunk("B", 1, "退款")]
    identity = EmbeddingSettings("test", "http://localhost:11434/v1", 1, "TEST_KEY").identity()
    embeddings = Mock(identity=identity, embed=AsyncMock(return_value="vector-batch"))
    store = Mock()
    store.get_collection.return_value = {"status": "ready", "build_config": {"embedding": identity}}
    store.search.return_value = candidates
    store.list_passages.return_value = candidates
    tracer = Mock(emit=AsyncMock())
    client = KnowledgeClient(store, embeddings, tracer, "hybrid-document")
    prepared = retrieval_input("退款参数")
    with patch("flowpay.core.knowledge.client.lexical_order", wraps=lexical_order) as lexical:
        result = await client.search("test", prepared, filters={"tenant": "A"})
    embeddings.embed.assert_awaited_once_with([prepared])
    store.search.assert_called_once_with("test", "vector-batch", 50, {"tenant": "A"})
    store.list_passages.assert_called_once_with("test", {"tenant": "A"})
    assert lexical.call_args.args[0] == "退款参数"
    assert len({item.source.doc_id for item in result}) == len(result)
    assert all(item.text in {candidate.text for candidate in candidates} for item in result)
