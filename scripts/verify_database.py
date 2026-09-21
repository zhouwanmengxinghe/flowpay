# 在独立测试数据库运行后端与 HTTP/SSE 回归，避免使用网页会话做测试。
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
from psycopg import sql


def target(uri: str, name: str) -> str:
    p = urlsplit(uri)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


with psycopg.connect(os.environ["FLOWPAY_KNOWLEDGE_ADMIN_DSN"], autocommit=True) as db:
    name = "flowpay_knowledge_test"
    if not db.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
os.environ["FLOWPAY_KNOWLEDGE_ADMIN_DSN"] = target(
    os.environ["FLOWPAY_KNOWLEDGE_ADMIN_DSN"], "flowpay_knowledge_test"
)
for variable in ("FLOWPAY_KNOWLEDGE_DSN", "FLOWPAY_KNOWLEDGE_BUILDER_DSN"):
    os.environ[variable] = target(os.environ[variable], "flowpay_knowledge_test")
subprocess.run([sys.executable, "scripts/database.py", "setup"], check=True)
os.environ["FLOWPAY_TEST_POSTGRES"] = "1"
subprocess.run([sys.executable, "-m", "pytest", "backend/tests", "-q"], check=True)

# HTTP/SSE 验收使用独立后端，会话存在临时 SQLite，不调用真实模型。
os.environ["FLOWPAY_PORT"] = "8001"
server = subprocess.Popen(
    [
        sys.executable,
        "scripts/demo.py",
        "--config",
        "backend/configs/local.toml",
    ],
    stdout=subprocess.DEVNULL,
)
try:
    for _ in range(100):
        if server.poll() is not None:
            raise RuntimeError("Smoke backend exited")
        try:
            if httpx.get("http://127.0.0.1:8001/api/health").is_success:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("Smoke backend startup timeout")
    subprocess.run(
        [sys.executable, "scripts/smoke_runtime.py", "--url", "http://127.0.0.1:8001"],
        check=True,
    )
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
