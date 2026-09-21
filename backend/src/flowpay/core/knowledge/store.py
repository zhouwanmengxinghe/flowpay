# 保存知识集合、原文和分块向量；集合就绪后不可修改，引用可回查原文位置。
"""Storage primitives; parsing, chunking and retrieval strategy belong to workflows."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import PurePosixPath

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import TypeAdapter

from flowpay.core.json import JsonObject, require_int, require_number, require_object, require_text
from flowpay.core.llm.embedding import EmbeddedBatch, text_hash, validate_vector
from flowpay.core.llm.embedding.settings import same_vector_space

from .contracts import (
    Chunk,
    Collection,
    Document,
    Passage,
    PassageLookup,
    PassageSource,
    PendingChunk,
)


class PostgresVectorStore:
    """PostgreSQL + pgvector 的同步存取实现，连接权限由所用 DSN 决定。

    builder 用于构建，reader 用于检索；这里不扫描目录、不决定切块策略。
    调用方须显式提供集合 ID，将数据版本和向量模型身份固定在同一集合中。
    异步工作流通过 KnowledgeClient 在线程中调用，避免阻塞事件循环。
    """

    def __init__(self, dsn: str):
        """按 DSN 创建同步连接池；读写权限由数据库角色决定，连接失败直接上抛。"""
        self.pool: ConnectionPool[Connection[JsonObject]] = ConnectionPool[Connection[JsonObject]](
            dsn, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, timeout=10, open=True
        )
        self.pool.wait(timeout=10)

    def close(self) -> None:
        """关闭知识库连接池；须在依赖该存储的查询和构建任务结束后调用。"""
        self.pool.close()

    def get_collection(self, collection_id: str) -> Collection:
        """按 collection_id 查询集合快照；未知 ID 抛 KeyError。"""
        with self.pool.connection() as db:
            return self._read_collection(db, collection_id)

    @staticmethod
    def _read_collection(
        db: Connection[JsonObject], collection_id: str, *, lock_for_update: bool = False
    ) -> Collection:
        """复用当前连接读取集合，可选行锁供写事务使用；未知集合抛 KeyError。"""
        row = db.execute(
            "SELECT * FROM collections WHERE collection_id=%s"
            + (" FOR UPDATE" if lock_for_update else ""),
            (collection_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown knowledge collection: {collection_id}")
        return TypeAdapter(Collection).validate_python(row)

    # 集合绑定数据发布版本和完整嵌入配置，不以维度相同作为模型兼容依据。
    def create_collection(
        self,
        collection_id: str,
        dataset_release_id: str,
        dataset_manifest_sha256: str,
        build_config: JsonObject,
        dimensions: int,
    ) -> Collection:
        """创建 building 集合并返回快照。

        相同 ID 和配置可重复调用；已存在但数据版本、模型或维度不同则拒绝。
        插入与一致性核对在同一事务内完成，不会覆盖旧集合。
        """
        spec = require_object(build_config.get("embedding", {}))
        if spec.get("dimensions") != dimensions or not spec.get("model"):
            raise ValueError("build_config.embedding must identify the model and dimensions")
        with self.pool.connection() as db:
            db.execute(
                "INSERT INTO collections(collection_id,dataset_release_id,"
                + "dataset_manifest_sha256,build_config,dimensions) VALUES(%s,%s,%s,%s,%s) "
                + "ON CONFLICT DO NOTHING",
                (
                    collection_id,
                    dataset_release_id,
                    dataset_manifest_sha256,
                    Jsonb(build_config),
                    dimensions,
                ),
            )
            row = self._read_collection(db, collection_id, lock_for_update=True)
            wanted: JsonObject = {
                "dataset_release_id": dataset_release_id,
                "dataset_manifest_sha256": dataset_manifest_sha256,
                "build_config": build_config,
                "dimensions": dimensions,
            }
            if any(dict(row)[k] != v for k, v in wanted.items()):
                raise ValueError("Collection already exists with different configuration")
            return row

    # 原文与全部分块一起写入；同标识重试必须保持内容一致。
    def save_document(self, collection_id: str, document: Document, chunks: list[Chunk]) -> None:
        """原子保存一个文档快照及调用方提供的完整分块列表。

        分块编号需连续且唯一，原文和分块内容用于生成校验信息。重复导入只接受
        内容完全一致的文档；不同内容需创建新集合，不能原地改变已引用的证据。
        """
        path = PurePosixPath(document.source_path)
        if path.is_absolute() or ".." in path.parts or "\\" in document.source_path:
            raise ValueError("source_path must be a relative source path")
        if not document.doc_id or not document.content or not chunks:
            raise ValueError("Document and chunks must not be empty")
        if [c.chunk_no for c in chunks] != list(range(1, len(chunks) + 1)):
            raise ValueError("Chunk numbers must be contiguous and start at 1")
        for chunk in chunks:
            require_object(chunk.source_locator)
        if any(not c.content.strip() for c in chunks):
            raise ValueError("Invalid chunk content or locator")
        with self.pool.connection() as db:
            collection = self._read_collection(db, collection_id, lock_for_update=True)
            existing = db.execute(
                "SELECT * FROM documents WHERE collection_id=%s AND doc_id=%s",
                (collection_id, document.doc_id),
            ).fetchone()
            # 相同文档 ID 不代表内容相同；连分块和定位信息也要比较。
            # 允许原样重试，但拒绝覆盖，保证历史引用不会悄悄指向新内容。
            if existing:
                old_chunks = db.execute(
                    "SELECT chunk_no,content,source_locator FROM chunks "
                    + "WHERE collection_id=%s AND doc_id=%s ORDER BY chunk_no",
                    (collection_id, document.doc_id),
                ).fetchall()
                if any(
                    existing[k] != v for k, v in require_object(asdict(document)).items()
                ) or old_chunks != [asdict(c) for c in chunks]:
                    raise ValueError("Existing document or chunks differ; create a new collection")
                return
            if collection["status"] != "building":
                raise ValueError("Collection is ready and immutable")
            db.execute(
                "INSERT INTO documents VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    collection_id,
                    document.doc_id,
                    document.title,
                    document.source_path,
                    document.content,
                    text_hash(document.content),
                    Jsonb(document.metadata),
                ),
            )
            for chunk in chunks:
                db.execute(
                    "INSERT INTO chunks VALUES(%s,%s,%s,%s,%s,NULL)",
                    (
                        collection_id,
                        document.doc_id,
                        chunk.chunk_no,
                        chunk.content,
                        Jsonb(chunk.source_locator),
                    ),
                )

    # 只读取尚未填充向量的分块，让中断后的构建可以继续。
    def missing_embeddings(self, collection_id: str, limit: int = 32) -> list[PendingChunk]:
        """返回指定集合内缺少向量的片段，按稳定顺序限制数量，供构建续跑。"""
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid batch limit")
        with self.pool.connection() as db:
            self._read_collection(db, collection_id)
            rows = db.execute(
                "SELECT doc_id,chunk_no,content FROM chunks "
                + "WHERE collection_id=%s AND embedding IS NULL "
                + "ORDER BY doc_id,chunk_no LIMIT %s",
                (collection_id, limit),
            ).fetchall()
            return TypeAdapter(list[PendingChunk]).validate_python(rows)

    # 逐项比对输入摘要和分块内容，防止把向量写到错误的文本上。
    def save_embeddings(
        self, collection_id: str, keys: list[tuple[str, int]], batch: EmbeddedBatch
    ) -> None:
        """把一批向量绑定到集合内指定的文档分块。

        keys、vectors 和 input_hashes 一一对应，先检查重复键与数量，再锁定集合。
        逐项核对模型身份、维度和文本摘要；已经填充的项跳过，未填充项只在 building
        状态下写入。单项失败整批回滚，避免部分绑定到错误的输入。
        """
        if (
            not keys
            or len(keys) != len(set(keys))
            or not (len(keys) == len(batch.vectors) == len(batch.input_hashes))
        ):
            raise ValueError("Embedding batch keys/count mismatch")
        with self.pool.connection() as db:
            collection = self._read_collection(db, collection_id, lock_for_update=True)
            if not same_vector_space(collection["build_config"]["embedding"], batch.spec):
                raise ValueError("Embedding model configuration differs from collection")
            for (doc_id, number), vector, digest in zip(
                keys, batch.vectors, batch.input_hashes, strict=True
            ):
                validate_vector(vector, collection["dimensions"])
                row = db.execute(
                    "SELECT content,embedding IS NOT NULL AS filled FROM chunks "
                    + "WHERE collection_id=%s AND doc_id=%s AND chunk_no=%s",
                    (collection_id, doc_id, number),
                ).fetchone()
                if row is None or text_hash(require_text(row["content"])) != digest:
                    raise ValueError("Embedding input differs from stored chunk")
                # 内容摘要和模型身份已核对，已填充项不再覆盖。
                # 构建重试可以补缺，但不承诺供应商对相同文本每次返回逐位相同的向量。
                if row["filled"]:
                    continue
                if collection["status"] != "building":
                    raise ValueError("Collection is immutable")
                db.execute(
                    "UPDATE chunks SET embedding=%s::vector WHERE collection_id=%s "
                    + "AND doc_id=%s AND chunk_no=%s",
                    (json.dumps(vector, allow_nan=False), collection_id, doc_id, number),
                )

    # 调用方给出预期文档清单；数量核对与数据库约束共同保证集合完整。
    def mark_ready(self, collection_id: str, expected_documents: dict[str, int]) -> None:
        """核对完整文档清单后发布集合，使其可供检索。

        expected_documents 是 doc_id 到分块数量的映射；只检查总数不足以发现漏文档。
        数据库还会检查向量是否齐全。ready 是不可变快照，策略或模型变化要新建集合。
        """
        if not expected_documents or any(
            type(n) is not int or n <= 0 for n in expected_documents.values()
        ):
            raise ValueError("Expected document/chunk counts required")
        with self.pool.connection() as db:
            collection = self._read_collection(db, collection_id, lock_for_update=True)
            actual = {
                r["doc_id"]: r["count"]
                for r in db.execute(
                    "SELECT doc_id,count(*) FROM chunks WHERE collection_id=%s GROUP BY doc_id",
                    (collection_id,),
                )
            }
            # 精确比较每个文档的块数：集合非空或总块数相等，都不能证明没有漏导文档。
            if actual != expected_documents:
                raise ValueError("Collection differs from expected document manifest")
            if collection["status"] == "ready":
                return
            db.execute(
                "UPDATE collections SET status='ready',ready_at=now() WHERE collection_id=%s",
                (collection_id,),
            )

    # 先限定集合与元数据候选，再计算余弦相似度，避免混入不同向量空间。
    def search(
        self,
        collection_id: str,
        query: EmbeddedBatch,
        limit: int = 5,
        filters: JsonObject | None = None,
    ) -> list[Passage]:
        """在指定就绪集合内返回带来源信息的相似分块。

        query 必须只有一个向量且模型身份与集合一致；filters 对文档 metadata 做包含匹配。
        score 为 1 减余弦距离，表示相似度而非答案置信度；同分按文档与块号稳定排序。
        返回结果只提供检索证据，不自动推断业务结论。
        """
        if not 1 <= limit <= 100 or len(query.vectors) != 1:
            raise ValueError("Search needs one query vector and limit 1..100")
        if filters is not None:
            require_object(filters)
        with self.pool.connection() as db:
            collection = self._read_collection(db, collection_id)
            if collection["status"] != "ready":
                raise ValueError("Collection is not ready")
            if not same_vector_space(collection["build_config"]["embedding"], query.spec):
                raise ValueError("Query embedding configuration differs from collection")
            validate_vector(query.vectors[0], collection["dimensions"])
            rows = db.execute(
                """
                WITH candidates AS MATERIALIZED (
                  SELECT c.collection_id,c.doc_id,c.chunk_no,c.content,c.source_locator,c.embedding,
                         d.title,d.source_path,d.source_sha256,d.metadata
                  FROM chunks c JOIN documents d USING(collection_id,doc_id)
                  WHERE c.collection_id=%s AND d.metadata @> %s::jsonb
                )
                SELECT collection_id,doc_id,chunk_no,content,source_locator,title,source_path,
                       source_sha256,metadata,1-(embedding <=> %s::vector) AS score
                FROM candidates ORDER BY score DESC,doc_id,chunk_no LIMIT %s
                """,
                (collection_id, Jsonb(filters or {}), json.dumps(query.vectors[0]), limit),
            ).fetchall()
            return [_passage(row) for row in rows]

    # 整篇读回只按文档编号排序，不重新计算相似度：这是"读完这一篇"，不是检索。
    def read_document(self, collection_id: str, doc_id: str) -> list[Passage]:
        """按文档编号返回它的全部分块，按块号排序；文档不存在时返回空列表。

        分块上限 1000 字符，单篇文档最多几个分块，因此整篇读回的代价是可预期的。
        返回 Passage 而不是一整段文本，模型据此仍能按块号精确引用。
        """
        with self.pool.connection() as db:
            collection = self._read_collection(db, collection_id)
            if collection["status"] != "ready":
                raise ValueError("Collection is not ready")
            rows = db.execute(
                "SELECT c.collection_id,c.doc_id,c.chunk_no,c.content,c.source_locator,"
                + "d.title,d.source_path,d.source_sha256,d.metadata "
                + "FROM chunks c JOIN documents d USING(collection_id,doc_id) "
                + "WHERE c.collection_id=%s AND c.doc_id=%s ORDER BY c.chunk_no",
                (collection_id, doc_id),
            ).fetchall()
            return [_passage(row, score=0.0) for row in rows]

    # 引用必须携带集合上下文；相同文档编号在不同集合中可能对应不同版本。
    def read_passage(self, collection_id: str, doc_id: str, chunk_no: int) -> PassageLookup:
        """读取集合、文档、块号所指向的片段及其所属文档全文。

        不重新嵌入或检索，避免引用点击后因排序改变而得到另一块。
        不存在的块抛 KeyError；集合未就绪则拒绝回查。
        """
        with self.pool.connection() as db:
            collection = self._read_collection(db, collection_id)
            if collection["status"] != "ready":
                raise ValueError("Collection is not ready")
            row = db.execute(
                "SELECT c.content,c.source_locator,d.source_path,d.source_sha256,d.title,"
                + "d.metadata,d.content AS document_content "
                + "FROM chunks c JOIN documents d USING(collection_id,doc_id) "
                + "WHERE c.collection_id=%s AND c.doc_id=%s AND c.chunk_no=%s",
                (collection_id, doc_id, chunk_no),
            ).fetchone()
            if row is None:
                raise KeyError("Passage not found")
            # 回查不重新计算相似度，score 为 0 表示"本次不是检索得到的"。
            passage = _passage(
                {**row, "collection_id": collection_id, "doc_id": doc_id, "chunk_no": chunk_no},
                score=0.0,
            )
            return PassageLookup(passage, require_text(row["document_content"]))


# 检索与回查共用同一个转换点，保证两条路径给出的片段身份完全一致。
def _passage(row: JsonObject, score: float | None = None) -> Passage:
    """把一行连表结果转换为 Passage；id 由文档编号和块号生成，不从数据库读取。"""
    doc_id, chunk_no = require_text(row["doc_id"]), require_int(row["chunk_no"])
    return Passage(
        id=f"doc:{doc_id}#c{chunk_no:03}",
        title=require_text(row["title"]),
        text=require_text(row["content"]),
        score=score if score is not None else require_number(row["score"]),
        source=PassageSource(
            collection_id=require_text(row["collection_id"]),
            doc_id=doc_id,
            chunk_no=chunk_no,
            path=require_text(row["source_path"]),
            sha256=require_text(row["source_sha256"]),
            locator=require_object(row["source_locator"]),
            metadata=require_object(row["metadata"]),
        ),
    )
