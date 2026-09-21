"""嵌入接口输入输出，独立于供应商 SDK 和知识库。"""

from dataclasses import dataclass
from typing import Protocol

from flowpay.core.json import JsonObject
from flowpay.core.llm.chat import ChunkSink

from .settings import EmbeddingIdentity, EmbeddingSettings


@dataclass
class EmbeddingReply:
    """嵌入适配结果，vectors 按输入顺序排列，usage 保留实际用量，raw 保留原始正文。

    这是嵌入批处理的内部结构，不属于 Chat Completions 响应，也不作为模型聊天消息。
    """

    vectors: list[list[float]]
    usage: JsonObject
    raw: JsonObject


@dataclass
class EmbeddedBatch:
    """携带向量及其生成身份的批次结果。

    spec 标识模型空间，input_hashes 按输入顺序绑定原文本，vectors 顺序与之相同。
    usage_batches 保留各次供应商用量，不推测缺失字段，也不承担费用估算。
    """

    spec: EmbeddingIdentity
    input_hashes: list[str]
    vectors: list[list[float]]
    usage_batches: list[JsonObject]


class EmbeddingModel(Protocol):
    """嵌入供应商能力接口；将可记录的请求准备与实际网络调用分开。"""

    settings: EmbeddingSettings

    def prepare(self, texts: list[str]) -> JsonObject:
        """将文本列表组织为可记录请求，不访问模型，不把认证凭据写入结果。"""
        ...

    async def invoke(self, request: JsonObject, emit: ChunkSink) -> EmbeddingReply:
        """执行已准备请求并通过 emit 记录原文；返回按输入顺序排列的向量，失败抛异常。"""
        ...
