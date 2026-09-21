# 创建知识库结构。
from pathlib import Path

import psycopg
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade(database: str) -> None:
    _ = database
    sql = (Path(__file__).parents[1] / "knowledge.sql").read_text()
    driver = op.get_bind().connection.driver_connection
    if not isinstance(driver, psycopg.Connection):
        raise RuntimeError("Migrations require a PostgreSQL psycopg connection")
    # SQL 来自仓库内固定迁移文件；字节查询不经过 SQLAlchemy 的参数解析。
    driver.execute(sql.encode("utf-8"))


def downgrade(database: str) -> None:
    _ = database
    raise RuntimeError("Destructive downgrade is intentionally unsupported; restore a backup")
