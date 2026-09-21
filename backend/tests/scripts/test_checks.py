"""知识准备入口不重建已有集合，身份冲突时停止；复用构建器继续未完成工作。"""

from pathlib import Path
from typing import cast

import pytest
from scripts.development import checks
from scripts.development.environment import ROOT, PreparationError
from scripts.ingestion.corpus import CorpusSettings, prepare_corpus

from flowpay.core.config import Settings, load_settings
from flowpay.core.json import JsonObject, to_json_object
from flowpay.core.knowledge.contracts import Collection
from flowpay.dataset import snapshot_ready

pytestmark = pytest.mark.skipif(
    not snapshot_ready(), reason="business dataset snapshot is not available"
)


@pytest.mark.parametrize("state", ["ready", "building", "missing", "mismatch"])
async def test_collection_preparation(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """相同就绪集合跳过；缺失/未完成集合续建；不匹配集合拒绝覆盖。"""
    settings = load_settings(ROOT / "backend/configs/local.toml")
    assert settings.embedding is not None
    corpus = prepare_corpus(
        settings.dataset_path,
        CorpusSettings.load(ROOT / "backend/configs/knowledge.toml"),
        settings.embedding,
    )
    collection: JsonObject = {
        "build_config": to_json_object(corpus.build_config) if state != "mismatch" else {},
        "dataset_release_id": corpus.release_id,
        "dataset_manifest_sha256": corpus.canonical_manifest_sha256,
        "dimensions": settings.embedding.dimensions,
        "status": state,
    }
    calls: list[str] = []

    class Store:
        """记录关闭行为的只读知识连接。"""

        def __init__(self, dsn: str) -> None:
            """不连接真实数据库。"""
            _ = dsn

        def get_collection(self, identifier: str) -> Collection:
            """按场景返回集合身份。"""
            _ = identifier
            if state == "missing":
                raise KeyError("missing")
            return cast(Collection, cast(object, collection))

        def close(self) -> None:
            """记录资源释放。"""
            calls.append("close")

    async def build(
        settings: Settings, corpus: CorpusSettings, operation: str, output: Path
    ) -> JsonObject:
        """代替实际嵌入调用。"""
        _ = settings, corpus, output
        calls.append(operation)
        return {}

    monkeypatch.setenv(settings.knowledge_url_env, "test")
    monkeypatch.setattr(checks, "PostgresVectorStore", Store)
    monkeypatch.setattr(checks, "run_preparation", build)
    if state == "mismatch":
        with pytest.raises(PreparationError, match="不一致"):
            await checks.prepare_knowledge(settings, True)
        assert calls == ["close"]
    else:
        await checks.prepare_knowledge(settings, True)
        assert calls == (["close"] if state == "ready" else ["close", "build"])
        calls.clear()
        if state != "ready":
            with pytest.raises(PreparationError, match="尚未完成"):
                await checks.prepare_knowledge(settings, False)
            assert calls == ["close"]
