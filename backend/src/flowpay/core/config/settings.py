# 统一解析配置与环境覆盖；运行快照只保留配置值和密钥变量名，不保存密钥。
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from flowpay.core.json import JsonObject, require_object
from flowpay.core.llm.embedding import EmbeddingSettings


@dataclass(frozen=True)
class ModelSettings:
    """聊天模型连接配置。

    name 对应请求的 model，base_url 指向兼容服务；api_key_env 是密钥变量名，
    默认那个变量名不绑定供应商，换模型只改 .env，不改代码。
    request_options 保存供应商扩展参数，不得覆盖应用构造的消息和工具。
    """

    provider: str = "scripted"
    name: str = "foundation-demo-v1"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "FLOWPAY_MODEL_API_KEY"
    timeout_seconds: float = 60
    request_options: JsonObject = field(default_factory=dict)

    def __post_init__(self):
        """拒绝非对象扩展参数以及对 messages、tools、model、stream 等保留字段的覆盖。"""
        require_object(self.request_options)
        reserved = {"model", "messages", "tools", "stream", "stream_options"}
        if reserved & self.request_options.keys():
            raise ValueError("Model request_options cannot override reserved request fields")


@dataclass(frozen=True)
class Settings:
    """一次服务启动使用的项目配置，不是发给模型的请求参数。

    data_dir 用于本地运行产物，dataset_path 是业务数据根目录；model 和 embedding
    分别配置聊天与嵌入。路径由 loader 解析，凭据字段只保存环境变量名。
    """

    data_dir: Path
    dataset_path: Path
    workflow: str = "s1"
    app_env: str = "development"
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    mock_delay_ms: int = 70
    model: ModelSettings = ModelSettings()
    knowledge_url_env: str = "FLOWPAY_KNOWLEDGE_DSN"
    knowledge_builder_url_env: str = "FLOWPAY_KNOWLEDGE_BUILDER_DSN"
    embedding: EmbeddingSettings | None = None
    sources_config: Path | None = None
    knowledge_collection_id: str | None = None

    # 保留复现所需的运行设置；认证环境变量的值不属于快照内容。
    def snapshot(self) -> JsonObject:
        """返回可序列化的运行配置，供 run.started 固定本轮设置。

        Path 转成字符串；数据库与模型凭据保存的是环境变量名，不读取其值。
        调用者仍不得把密钥放进 request_options 等普通配置字段，这里不是通用脱敏器。
        """
        value = asdict(self)
        value["data_dir"] = str(self.data_dir)
        value["dataset_path"] = str(self.dataset_path)
        value["sources_config"] = str(self.sources_config) if self.sources_config else None
        return value
