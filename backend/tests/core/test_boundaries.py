# 回归测试：基座依赖边界。测试中的固定输入用于复现边界，不作为业务答案来源。
import ast
from pathlib import Path


def test_core_does_not_import_workflow_or_adapter() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "src/flowpay/core").is_dir()
    assert (root / "tests/core").is_dir()
    files = list((root / "src/flowpay/core").rglob("*.py"))
    files += list((root / "tests/core").rglob("*.py"))
    for path in files:
        forbidden = ("flowpay.workflows", "flowpay.adapters", "flowpay.router", "flowpay.bootstrap")
        if "src" in path.parts:
            forbidden += ("scripts",)
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(forbidden), path
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith(forbidden) for alias in node.names), path


def test_workflows_use_capability_interfaces_without_infrastructure() -> None:
    """保护课堂入口：执行机制和业务适配必须从装配层注入。"""
    root = Path(__file__).resolve().parents[2] / "src/flowpay/workflows"
    assert root.is_dir()
    forbidden = (
        "openai",
        "httpx",
        "psycopg",
        "sqlite3",
        "os",
        "json",
        "flowpay.adapters",
        "flowpay.bootstrap",
        "flowpay.router",
        "scripts",
        "flowpay.core.llm.chat.openai_chat",
        "flowpay.core.llm.embedding.openai_embedding",
        "flowpay.core.knowledge.store",
        "flowpay.core.event_log.store",
    )
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                # SDK 的协议类型允许用于注解；客户端和执行实现仍禁止进入 workflows。
                if (node.module or "").startswith("openai.types."):
                    continue
                modules = [node.module or ""]
                assert not {alias.name for alias in node.names} & {
                    "Toolbox",
                    "PostgresVectorStore",
                }, path
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in modules
                for prefix in forbidden
            ), path
