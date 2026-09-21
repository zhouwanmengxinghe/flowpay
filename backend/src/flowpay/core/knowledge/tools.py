"""文档检索与整篇读回两个工具的参数和协议适配；策略与处理函数由调用方提供。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from flowpay.core.json import JsonObject
from flowpay.core.knowledge.contracts import Passage, for_model
from flowpay.core.tools import Tool
from flowpay.core.tools.contracts import ToolInputError


class DocumentSearchArguments(BaseModel):
    """模型提供的搜索参数；只接受查询文本和数量，不接受预设文档类型。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    limit: int = Field(default=10, strict=True, ge=1, le=10)


@dataclass(frozen=True)
class DocumentSearchResult:
    """回填给模型的查询与原始命中；notice 用来说明空结果或调用方限制。"""

    query: str
    passages: list[Passage]
    notice: str | None = None


DocumentSearchHandler = Callable[[DocumentSearchArguments], Awaitable[DocumentSearchResult]]


def document_search_tool(handler: DocumentSearchHandler) -> Tool:
    """把已绑定本轮的搜索函数包成模型可见的工具；只做协议校验与序列化。"""

    async def search(arguments: JsonObject) -> JsonObject:
        """验证参数后调用处理函数，只把模型可见字段回填为工具结果。

        完整来源已由调用方登记到 PassageSet，并随 knowledge.response 事件落库；
        工具结果不重复携带，避免溯源字段挤占模型上下文。
        """
        try:
            validated = DocumentSearchArguments.model_validate(arguments)
        except ValidationError as error:
            raise ToolInputError(str(error)) from error
        result = await handler(validated)
        payload: JsonObject = {"query": result.query, "passages": for_model(result.passages)}
        if result.notice:
            payload["notice"] = result.notice
        return payload

    return Tool(
        name="search_documents",
        description="检索本轮绑定的文档集合，返回原文片段与引用 ID。",
        parameters=DocumentSearchArguments.model_json_schema(),
        execute=search,
    )


class DocumentReadArguments(BaseModel):
    """模型提供的文档编号；来自它已经看到的引用编号，不接受自由文本查询。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    doc_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description=(
            "文档编号本身，例如 KB-TS-012。"
            "从片段编号 doc:KB-TS-012#c001 取用时，去掉 doc: 前缀和 #c001 后缀。"
        ),
    )


@dataclass(frozen=True)
class DocumentReadResult:
    """整篇读回的分块；notice 用来说明文档不存在或调用方的限制。"""

    doc_id: str
    passages: list[Passage]
    notice: str | None = None


DocumentReadHandler = Callable[[DocumentReadArguments], Awaitable[DocumentReadResult]]


def document_read_tool(handler: DocumentReadHandler) -> Tool:
    """把已绑定本轮的整篇读回函数包成模型可见的工具；只做协议校验与序列化。"""

    async def read(arguments: JsonObject) -> JsonObject:
        """验证参数后调用处理函数，只把模型可见字段回填为工具结果。"""
        try:
            validated = DocumentReadArguments.model_validate(arguments)
        except ValidationError as error:
            raise ToolInputError(str(error)) from error
        result = await handler(validated)
        payload: JsonObject = {"doc_id": result.doc_id, "passages": for_model(result.passages)}
        if result.notice:
            payload["notice"] = result.notice
        return payload

    return Tool(
        name="read_document",
        description=(
            "按文档编号整篇读回它的全部片段，按顺序排列。"
            "已经知道某份文档相关、但检索只给出零散片段时使用。"
        ),
        parameters=DocumentReadArguments.model_json_schema(),
        execute=read,
    )
