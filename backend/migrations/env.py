# 运行知识库迁移。会话与轨迹存在本地 SQLite，没有第二个需要迁移的库。
import os

from alembic import context
from sqlalchemy import create_engine

# 连接密钥不写入 alembic.ini 或日志。
target = context.get_x_argument(as_dictionary=True).get("database", "knowledge")
if target != "knowledge":
    raise ValueError("database must be knowledge")
dsn = os.environ["FLOWPAY_KNOWLEDGE_ADMIN_DSN"]
engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg://", 1))
with engine.connect() as connection:
    context.configure(connection=connection, version_table="alembic_version")
    with context.begin_transaction():
        context.run_migrations(database=target)
engine.dispose()
