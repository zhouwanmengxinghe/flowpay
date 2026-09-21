# 补充集合插入约束，防止绕过构建校验直接创建就绪集合。
import psycopg
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade(database: str) -> None:
    if database == "knowledge":
        driver = op.get_bind().connection.driver_connection
        if not isinstance(driver, psycopg.Connection):
            raise RuntimeError("Migrations require a PostgreSQL psycopg connection")
        driver.execute("""
        CREATE FUNCTION require_building_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.status <> 'building' THEN
            RAISE EXCEPTION 'New collection must be building';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER collection_insert BEFORE INSERT ON collections
          FOR EACH ROW EXECUTE FUNCTION require_building_insert();
        """)


def downgrade(database: str) -> None:
    _ = database
    raise RuntimeError("Restore from backup instead of destructive downgrade")
