# 将嵌入请求转换为兼容 HTTP 协议；按输入索引还原向量顺序并保留原始响应。
from __future__ import annotations

import os

import httpx
from openai import APIStatusError, AsyncOpenAI
from pydantic import TypeAdapter

from flowpay.core.json import (
    JsonObject,
    require_int,
    require_list,
    require_object,
    require_text,
)
from flowpay.core.llm.chat import ChunkSink
from flowpay.core.llm.embedding import EmbeddingReply, EmbeddingSettings


class OpenAiEmbeddingModel:
    """使用官方 OpenAI SDK 访问兼容的 /embeddings 服务。

    在解析前记录响应原文，按响应 index 还原输入顺序；不参与知识集合写入。
    """

    def __init__(
        self, settings: EmbeddingSettings, transport: httpx.AsyncBaseTransport | None = None
    ):
        """校验供应商并保存连接设置；transport 可注入测试 HTTP 实现，不在构造时请求网络。"""
        if settings.provider != "openai-compatible":
            raise ValueError("Unsupported embedding provider")
        self.settings: EmbeddingSettings = settings
        self.transport: httpx.AsyncBaseTransport | None = transport

    def prepare(self, texts: list[str]) -> JsonObject:
        """构造 float 编码的嵌入请求和目标 URL；附加供应商参数但不附加认证头。"""
        return {
            "provider": self.settings.provider,
            "url": self.settings.base_url.rstrip("/") + "/embeddings",
            "body": {
                **self.settings.request_options,
                "model": self.settings.name,
                "input": texts,
                "encoding_format": "float",
            },
        }

    async def invoke(self, request: JsonObject, emit: ChunkSink) -> EmbeddingReply:
        """通过 SDK 请求 float 格式向量，并保留完整响应正文用于追踪。

        供应商可按不同顺序返回 data，因此先验证 index 恰好覆盖所有输入再排序。
        排序不改动 raw，以便把解析结果与供应商原文逐项核对。
        这里只检查响应索引；数量、数值和维度还会由 EmbeddingClient 验证。
        """
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise ValueError(f"Missing environment variable: {self.settings.api_key_env}")
        # 客户端的关闭同时释放底层 HTTP 连接；每个记录对应一次请求。
        async with AsyncOpenAI(
            api_key=key,
            base_url=self.settings.base_url,
            timeout=self.settings.timeout_seconds,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=self.transport),
        ) as client:
            body = require_object(request["body"])
            texts = TypeAdapter(list[str]).validate_python(body["input"], strict=True)
            try:
                async with client.embeddings.with_streaming_response.create(
                    model=require_text(body["model"]),
                    input=texts,
                    encoding_format="float",
                    extra_body={
                        k: v
                        for k, v in body.items()
                        if k not in {"model", "input", "encoding_format"}
                    },
                ) as response:
                    # 先落原始正文，再解析向量；解析失败也能回查供应商实际返回。
                    await emit({"status": response.status_code, "body": await response.text()})
                    raw = require_object(await response.json())
            except APIStatusError as error:
                await emit(
                    {
                        "status": error.status_code,
                        "body": (await error.response.aread()).decode(errors="replace"),
                    }
                )
                raise RuntimeError(f"Embedding HTTP {error.status_code}") from None
        rows = [require_object(row) for row in require_list(raw.get("data", []))]
        expected = list(range(len(texts)))
        indices = [require_int(row.get("index")) for row in rows]
        if any(type(i) is not int for i in indices) or sorted(indices) != expected:
            raise ValueError("Embedding indices missing or duplicated")
        rows = sorted(rows, key=lambda row: require_int(row["index"]))
        vectors = TypeAdapter(list[list[float]]).validate_python(
            [row["embedding"] for row in rows], strict=True
        )
        return EmbeddingReply(vectors, require_object(raw.get("usage") or {}), raw)
