# 回归测试：知识库的不可变约束由数据库触发器强制，绕过应用层写入同样会被拒绝。
import os
from typing import LiteralString

import psycopg
import pytest

from flowpay.core.event_log.store import new_id

pytestmark = pytest.mark.skipif(not os.getenv("FLOWPAY_TEST_POSTGRES"), reason="Opt-in PostgreSQL")


def test_knowledge_guards() -> None:
    with psycopg.connect(os.environ["FLOWPAY_KNOWLEDGE_ADMIN_DSN"]) as db:
        cid = new_id("test_collection")
        db.execute(
            "INSERT INTO collections(collection_id,dataset_release_id,dataset_manifest_sha256,"
            + "build_config,dimensions) VALUES(%s,'fixture',%s,'{}',3)",
            (cid, "a" * 64),
        )
        db.execute(
            "INSERT INTO documents VALUES(%s,'doc','title','fixture.md','text',%s,'{}')",
            (cid, "b" * 64),
        )
        db.execute("INSERT INTO chunks VALUES(%s,'doc',1,'text','{}',NULL)", (cid,))
        queries: list[LiteralString] = [
            "UPDATE collections SET status='ready',ready_at=now() WHERE collection_id=%s",
            "UPDATE chunks SET embedding='[1,2]' WHERE collection_id=%s",
        ]
        for query in queries:
            with pytest.raises(psycopg.Error):
                with db.transaction():
                    db.execute(query, (cid,))
        db.execute("UPDATE chunks SET embedding='[1,2,3]' WHERE collection_id=%s", (cid,))
        db.execute(
            "UPDATE collections SET status='ready',ready_at=now() WHERE collection_id=%s", (cid,)
        )
        with pytest.raises(psycopg.Error):
            with db.transaction():
                db.execute("UPDATE chunks SET content='changed' WHERE collection_id=%s", (cid,))
        db.rollback()
