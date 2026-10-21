from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.knowledge.ranking import (
    deduplicate_mmr,
    lexical_order,
    reciprocal_rank_fusion,
    rerank_hybrid,
)


def passage(chunk: int, text: str, score: float) -> Passage:
    return Passage(
        id=f"doc:KB-1#c{chunk:03}",
        title="Webhook 重试规则",
        text=text,
        score=score,
        source=PassageSource(
            collection_id="test",
            doc_id="KB-1",
            chunk_no=chunk,
            path="knowledge/webhook.md",
            sha256="source-hash",
            locator={"heading": "Webhook / 重试"},
            metadata={"doc_type": "business_rule"},
        ),
    )


def test_hybrid_retrieval_recall_can_add_keyword_only_result() -> None:
    dense = [passage(1, "网络回调投递失败后延迟重试。", 0.91)]
    corpus = [
        *dense,
        passage(2, "HTTP 410 会禁用 webhook endpoint。", 0.2),
        passage(3, "退款审批需要商户确认。", 0.1),
    ]
    lexical = lexical_order("webhook HTTP 410 endpoint", corpus)
    fused = reciprocal_rank_fusion([dense, lexical], candidate_limit=10)
    reranked = rerank_hybrid("webhook HTTP 410 endpoint", dense, lexical, candidate_limit=10)
    assert fused[0].id == "doc:KB-1#c001"
    assert reranked[0].id == "doc:KB-1#c002"


def test_mmr_suppresses_exact_duplicate_chunks_and_keeps_relevant_evidence() -> None:
    passages = [
        passage(1, "HTTP 410 会禁用 webhook endpoint。", 0.9),
        passage(2, "HTTP 410 会禁用 webhook endpoint。", 0.85),
        passage(3, "HTTP 429 会延迟重试 webhook。", 0.6),
    ]
    result = deduplicate_mmr("HTTP 410 webhook endpoint", passages, limit=3)
    assert [item.id for item in result] == ["doc:KB-1#c001", "doc:KB-1#c003"]
