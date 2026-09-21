"""嵌入配置和向量空间身份；凭据值不进入身份信息。"""

from dataclasses import dataclass, field
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

from flowpay.core.json import JsonObject, require_object, require_text


class EmbeddingIdentity(TypedDict):
    """确定向量空间的版本信息，用于拒绝不同模型或配置的向量混用；不包含密钥。"""

    provider: str
    model: str
    revision: str
    base_url: str
    dimensions: int
    request_options: JsonObject


# 同一台机器上的那个 Ollama，宿主机叫 127.0.0.1，容器里叫 host.docker.internal，
# 用 compose 的 ollama profile 时叫 ollama。三个名字是同一个部署的三个入口，
# 而模型版本已由 revision 的摘要钉死，所以主机名不参与向量空间身份比较——
# 否则同一份集合换个入口读就会被判成"配置不一致"，必须重建。
# 端口、协议和路径仍然参与：它们真的可能指向另一个服务。
LOCAL_ENDPOINTS = frozenset({"127.0.0.1", "localhost", "host.docker.internal", "ollama"})
LOCAL_ALIAS = "local"


def canonical_endpoint(url: str) -> str:
    """把本机 Ollama 的几种写法归一；其余端点原样返回（只去掉尾部斜杠）。"""
    parts = urlsplit(url.rstrip("/"))
    if parts.hostname not in LOCAL_ENDPOINTS:
        return urlunsplit(parts)
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(parts._replace(netloc=f"{LOCAL_ALIAS}{port}"))


def same_vector_space(stored: object, current: EmbeddingIdentity) -> bool:
    """集合的向量空间身份是否与当前配置一致。

    比较前把已入库那一份的端点也规范化：集合可能是在另一个入口下建的
    （宿主机 127.0.0.1 / 容器 host.docker.internal），那不算换了部署。
    构建、补向量和检索三处都用它，口径只有一份。
    """
    recorded = dict(require_object(stored))
    if "base_url" in recorded:
        recorded["base_url"] = canonical_endpoint(require_text(recorded["base_url"]))
    return recorded == dict(current)


@dataclass(frozen=True)
class EmbeddingSettings:
    """嵌入服务配置及批处理限制；dimensions 表示每个向量长度，revision 固定模型版本。"""

    name: str
    base_url: str
    dimensions: int
    api_key_env: str
    provider: str = "openai-compatible"
    revision: str = "unpinned"
    timeout_seconds: float = 60
    batch_size: int = 32
    request_options: JsonObject = field(default_factory=dict)

    def __post_init__(self):
        """校验维度、批大小与超时为正，并拒绝覆盖保留字段或请求不一致的维度。"""
        if self.dimensions <= 0 or self.batch_size <= 0 or self.timeout_seconds <= 0:
            raise ValueError("Embedding dimensions, batch size and timeout must be positive")
        require_object(self.request_options)
        if {"model", "input", "encoding_format"} & self.request_options.keys():
            raise ValueError("Embedding options cannot override reserved fields")
        if (
            "dimensions" in self.request_options
            and self.request_options["dimensions"] != self.dimensions
        ):
            raise ValueError("Requested dimensions differ from configured dimensions")

    # 包含影响向量空间的配置，不包含认证密钥；集合使用该身份判断兼容性。
    def identity(self) -> EmbeddingIdentity:
        """返回会影响向量表示的配置；端点也参与比较，不仅比较模型名称或维度。

        端点先经 canonical_endpoint 规范化：本机 Ollama 换个入口名不算换部署。
        """
        return {
            "provider": self.provider,
            "model": self.name,
            "revision": self.revision,
            "base_url": canonical_endpoint(self.base_url),
            "dimensions": self.dimensions,
            "request_options": self.request_options,
        }
