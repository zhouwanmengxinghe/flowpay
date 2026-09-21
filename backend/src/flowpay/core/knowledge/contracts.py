"""知识库的公开数据结构与能力接口。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from typing_extensions import TypedDict

from flowpay.core.json import JsonObject
from flowpay.core.llm.embedding import EmbeddedBatch


# 保留完整原文与来源元数据，分块引用最终可回查到这里。
@dataclass
class Document:
    """一篇完整的知识文档快照。

    doc_id 在集合内标识文档；content 保存原文，source_path 与 metadata 保留
    来源信息。分块单独传入存储，原文用于历史引用回查。
    """

    doc_id: str
    title: str
    source_path: str
    content: str
    metadata: JsonObject = field(default_factory=dict)


# 分块编号在文档内定位；source_locator 由切块方提供原文位置。
@dataclass
class Chunk:
    """调用方生成的一个文档片段；chunk_no 从 1 连续编号，source_locator 定位原文。"""

    chunk_no: int
    content: str
    source_locator: JsonObject = field(default_factory=dict)


class Collection(TypedDict):
    """固定数据版本、构建配置和向量维度的一组知识快照；ready 后内容不可改写。"""

    collection_id: str
    dataset_release_id: str
    dataset_manifest_sha256: str
    build_config: JsonObject
    dimensions: int
    status: Literal["building", "ready"]
    created_at: datetime
    ready_at: datetime | None


class PendingChunk(TypedDict):
    """尚需生成向量的片段，携带集合内文档 ID、块编号和待嵌入正文。"""

    doc_id: str
    chunk_no: int
    content: str


@dataclass(frozen=True)
class PassageSource:
    """片段的溯源信息：定位原文、校验数据版本。不进入模型上下文。"""

    collection_id: str
    doc_id: str
    chunk_no: int
    path: str
    sha256: str
    locator: JsonObject
    metadata: JsonObject


@dataclass(frozen=True)
class Passage:
    """一个可引用的文档片段——检索、引用和回查的共同单位。

    id 形如 doc:KB-TS-012#c002，用于去重和回查原文；text 是模型判断的依据；
    score 是相似度而非答案置信度；source 保留回查和版本校验需要的全部信息。
    """

    id: str
    title: str
    text: str
    score: float
    source: PassageSource


class ModelPassage(TypedDict):
    """交给模型阅读的片段：编号用于引用，正文用于判断，分数用于权衡可信度。"""

    id: str
    title: str
    text: str
    score: float


def for_model(passages: list[Passage]) -> list[ModelPassage]:
    """把片段投影成模型真正会用到的四个字段。

    source 留在 PassageSet、事件日志和前端，不随工具结果进入模型上下文。模型无法
    使用这些字段，却要为它们付出注意力和预算：改造前实测它们占检索载荷的 84%。
    """
    return [
        {
            "id": passage.id,
            "title": passage.title,
            "text": passage.text,
            "score": round(passage.score, 3),
        }
        for passage in passages
    ]


@dataclass(frozen=True)
class PassageLookup:
    """按编号回查到的片段及其所属文档全文；用于引用展开和导入校验，不生成新回答。"""

    passage: Passage
    document_text: str


class VectorStore(Protocol):
    """向量与集合的存取契约；不含查询文本构造策略，也不记录调用轨迹。"""

    def get_collection(self, collection_id: str) -> Collection:
        """按 ID 返回集合快照；集合不存在时抛 KeyError。"""
        ...

    def missing_embeddings(self, collection_id: str, limit: int = 32) -> list[PendingChunk]:
        """取出仍缺向量的片段，最多 limit 条；已保存的向量不重复选出。"""
        ...

    def save_embeddings(
        self, collection_id: str, keys: list[tuple[str, int]], batch: EmbeddedBatch
    ) -> None:
        """按文档和块键写入带身份的向量批次；身份、文本绑定或数量不符时拒绝写入。"""
        ...

    def search(
        self,
        collection_id: str,
        query: EmbeddedBatch,
        limit: int = 5,
        filters: JsonObject | None = None,
    ) -> list[Passage]:
        """在指定集合内用查询向量排序并过滤元数据，返回最多 limit 条带来源的候选。"""
        ...

    def read_document(self, collection_id: str, doc_id: str) -> list[Passage]:
        """按文档编号返回它的全部分块，按块号排序；文档不存在时返回空列表。"""
        ...

    def read_passage(self, collection_id: str, doc_id: str, chunk_no: int) -> PassageLookup:
        """按集合、文档和块编号回查片段与所属文档全文；不存在时抛 KeyError。"""
        ...


class DocumentRetriever(Protocol):
    """运行期的文档检索能力，也是替换检索实现的接缝。

    KnowledgeClient 是正式实现；测试和演示可以提供别的实现，而不必接一个真实向量库。
    调用方决定查询文本、数量和过滤条件；本接口不构造查询，也不解释业务含义。
    """

    async def search(
        self,
        collection_id: str,
        text: str,
        limit: int = 5,
        filters: JsonObject | None = None,
    ) -> list[Passage]:
        """检索固定集合并记录过程；失败和取消向上传播。"""
        ...

    async def read_document(self, collection_id: str, doc_id: str) -> list[Passage]:
        """整篇取回一份文档的全部分块并记录过程；文档不存在时返回空列表。"""
        ...
