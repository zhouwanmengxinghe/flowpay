"""模拟服务迁出运行包之后，仍保持离线与测试存储隔离。"""

from pathlib import Path

import pytest
from scripts.demo import Arguments, configured_runtime
from scripts.runtime_demo.workflow import DemoWorkflow

CONFIG = Path(__file__).resolve().parents[3] / "backend/configs/local.toml"


def test_demo_needs_no_external_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """日常配置和环境不能让模拟器调用真实模型或绑定知识集合。"""
    monkeypatch.setenv("FLOWPAY_WORKFLOW", "s1")
    monkeypatch.delenv("FLOWPAY_MODEL_API_KEY", raising=False)
    settings = configured_runtime(Arguments(config=CONFIG))
    assert settings.workflow == "demo"
    assert settings.model.provider == "scripted"
    assert settings.data_dir == CONFIG.parents[2] / "var/demo"
    assert settings.knowledge_collection_id is None
    assert settings.embedding is None
    assert settings.sources_config is None
    assert DemoWorkflow(settings).id == "foundation-demo"
