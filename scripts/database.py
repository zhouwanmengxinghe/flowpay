# 初始化知识库迁移及角色权限；会话存本地 SQLite，业务 SQLite 不参与迁移。
"""知识库的显式建库与权限配置；会话与轨迹存在本地 SQLite，不需要准备。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import psycopg
from flowpay.core.json import require_text
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def setup() -> None:
    """为知识库运行迁移并配置最小运行权限。

    使用管理员 DSN 做部署操作。reader 只读，builder 可新增文档并补缺向量，
    不能自由改写已就绪内容。此命令会改变数据库结构和权限，不能在普通聊天请求中调用。
    会话与轨迹存在本地 SQLite，不需要数据库准备。
    """
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "backend/alembic.ini"),
            "-x",
            "database=knowledge",
            "upgrade",
            "head",
        ],
        check=True,
    )
    with psycopg.connect(os.environ["FLOWPAY_KNOWLEDGE_ADMIN_DSN"]) as db:
        row = db.execute("SELECT current_database()").fetchone()
        if row is None:
            raise RuntimeError("Database identity was not returned")
        database = require_text(cast(object, row[0]))
        db.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database)
            )
        )
        # 口令固定：库只发布在本机回环，权限隔离靠下面的 GRANT，不靠口令强度。
        password = os.environ.get("FLOWPAY_DB_PASSWORD", "flowpay_local")
        for role in ("flowpay_knowledge_reader", "flowpay_knowledge_builder"):
            if not db.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone():
                db.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            db.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
            db.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            db.execute(
                sql.SQL("GRANT SELECT ON collections,documents,chunks TO {}").format(
                    sql.Identifier(role)
                )
            )
        db.execute(
            "GRANT INSERT ON collections,documents,chunks TO flowpay_knowledge_builder"
        )
        db.execute(
            "GRANT UPDATE(status,ready_at) ON collections TO flowpay_knowledge_builder"
        )
        db.execute("GRANT UPDATE(embedding) ON chunks TO flowpay_knowledge_builder")
    print("Knowledge database migrated; reader and builder roles granted.")


if __name__ == "__main__":
    # 唯一的子命令，但文档、Makefile 与 setup 流程都写成 `database.py setup`，
    # 这里显式接受它，省得三处各自记住"其实不用带参数"。
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", nargs="?", default="setup", choices=["setup"])
    _ = parser.parse_args()
    setup()
