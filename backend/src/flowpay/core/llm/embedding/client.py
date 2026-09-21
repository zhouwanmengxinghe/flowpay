"""分批调用嵌入接口，校验结果并保存调用轨迹。"""

import asyncio
from dataclasses import asdict

from flowpay.core.event_log.contracts import EventSink
from flowpay.core.event_log.store import new_id
from flowpay.core.json import JsonObject, require_list, require_text

from .contracts import EmbeddedBatch, EmbeddingModel
from .settings import EmbeddingIdentity
from .validation import text_hash, validate_vector


class EmbeddingClient:
    """生成带来源身份的向量批次；构建和查询都必须记录请求与结果。"""

    def __init__(self, adapter: EmbeddingModel, tracer: EventSink):
        """保存嵌入模型与本轮追踪；连接由模型负责，本类负责批处理、校验和记录。"""
        self.adapter: EmbeddingModel = adapter
        self.tracer: EventSink = tracer

    @property
    def identity(self) -> EmbeddingIdentity:
        """返回适配器的完整向量空间身份，供构建与查询比较，包含模型版本和维度。"""
        return self.adapter.settings.identity()

    @property
    def batch_size(self) -> int:
        """返回配置允许的每批文本数；embed 据此拆分一次调用的输入。"""
        return self.adapter.settings.batch_size

    # 每批请求均保留输入和用量；向量必须通过数量、维度与数值校验。
    async def embed(self, texts: list[str]) -> EmbeddedBatch:
        """将非空文本列表分批调用适配器，返回保持输入顺序的 EmbeddedBatch。

        每批先记录请求，再记录原始响应，校验通过后才记录成功事件。
        任一批失败会终止本次调用；先前批次的外部请求与事件不会被撤销。
        本方法只生成向量，知识库写入及构建续跑由 KnowledgeClient 负责。
        """
        if not require_list(texts) or any(not require_text(t).strip() for t in texts):
            raise ValueError("Embedding input must be a nonempty list of nonempty texts")
        vectors: list[list[float]] = []
        usages: list[JsonObject] = []
        size = self.batch_size
        # 分批限制单次请求大小，每批独立 call_id；向量只在当前批完整校验后并入结果。
        # 这不是数据库事务，多批外部调用的费用无法因后续失败而回滚。
        for offset in range(0, len(texts), size):
            batch = texts[offset : offset + size]
            call_id = new_id("embedding")
            request = self.adapter.prepare(batch)
            await self.tracer.emit(
                "embedding.request",
                {"request": request, "spec": self.identity},
                call_id,
            )

            # 用默认参数固定本批 ID，避免回调闭包将结果错误关联到之后的批次。
            async def emit(payload: JsonObject, call_id: str = call_id) -> None:
                await self.tracer.emit("embedding.raw", payload, call_id)

            try:
                reply = await self.adapter.invoke(request, emit)
                if len(reply.vectors) != len(batch):
                    raise ValueError("Embedding response count mismatch")
                for vector in reply.vectors:
                    validate_vector(vector, self.adapter.settings.dimensions)
                await self.tracer.emit("embedding.response", asdict(reply), call_id)
                vectors.extend(reply.vectors)
                usages.append(reply.usage)
            except asyncio.CancelledError:
                await self.tracer.emit("embedding.canceled", {}, call_id)
                raise
            except Exception as error:
                await self.tracer.emit(
                    "embedding.error",
                    {"error_type": type(error).__name__, "message": str(error)},
                    call_id,
                )
                raise
        return EmbeddedBatch(self.identity, [text_hash(t) for t in texts], vectors, usages)
