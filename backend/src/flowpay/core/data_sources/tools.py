"""把已登记的只读数据源包装成模型可见的工具。

本模块不知道 FlowPay 有哪些表和字段：操作名、参数名与说明都由阶段在 spec 里给出，
实际可读的表列仍由 backend/policies/sources.toml 的白名单决定。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from flowpay.core.json import JsonObject, require_object, require_text
from flowpay.core.tools import Tool

from .reader import SourceResult

SqlQuery = Callable[[str, JsonObject, JsonObject], Awaitable[SourceResult]]


class LogQuery(Protocol):
    """日志查询入口；阶段传入 AgentRuntime.query_logs。"""

    async def __call__(
        self,
        source: str,
        *,
        filters: JsonObject | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> SourceResult:
        """按来源和过滤条件读取日志行。"""
        ...


@dataclass(frozen=True)
class SqlToolSpec:
    """一个关系查询工具：模型看到的名字与说明，以及它能给哪些条件。

    登记的是关系，不是某一条查询：scope 每次必填（账户与环境这类隔离条件），
    filters 由模型按需组合，因此新增一种问法不需要新增工具。
    require_filter 为假时允许只给 scope，用于本身行数就很少的关系。
    """

    name: str
    description: str
    source: str
    scope: tuple[str, ...]
    filters: tuple[str, ...]
    require_filter: bool = True


@dataclass(frozen=True)
class LogToolSpec:
    """一个日志工具；window 为真时额外接受 start / end 时间窗口。"""

    name: str
    description: str
    source: str
    filters: tuple[str, ...]
    window: bool = False


def sql_tool(
    spec: SqlToolSpec, query: SqlQuery, values: Mapping[str, Sequence[str]] | None = None
) -> Tool:
    """把一个登记关系变成工具；条件名取自登记字段，值始终作为绑定参数传入。"""

    async def run(arguments: JsonObject) -> JsonObject:
        scope: JsonObject = {n: require_text(arguments[n]) for n in spec.scope}
        filters: JsonObject = {
            n: require_text(arguments[n]) for n in spec.filters if n in arguments
        }
        if spec.require_filter and not filters:
            return {
                "rows": [],
                "truncated": False,
                "notice": f"除账户与环境外至少再给一个条件：{'、'.join(spec.filters)}。",
            }
        return rows_for_model(await query(spec.source, scope, filters))

    return Tool(spec.name, spec.description, schema(spec.scope, spec.filters, values), run)


def log_tool(
    spec: LogToolSpec, query: LogQuery, values: Mapping[str, Sequence[str]] | None = None
) -> Tool:
    """把一个登记的日志来源变成工具；过滤条件可以只给一部分。"""

    async def run(arguments: JsonObject) -> JsonObject:
        filters: JsonObject = {
            k: require_text(v) for k, v in arguments.items() if k in spec.filters
        }
        if not filters:
            return {"rows": [], "truncated": False, "notice": "至少给出一个过滤条件。"}
        window = {k: require_text(arguments[k]) for k in ("start", "end") if k in arguments}
        return log_rows_for_model(await query(spec.source, filters=filters, **window))

    optional = (*spec.filters[1:], *(("start", "end") if spec.window else ()))
    return Tool(spec.name, spec.description, schema(spec.filters[:1], optional, values), run)


WINDOW_HINT = "ISO 8601 时间戳，必须带时区，例如 2026-08-19T11:00:00Z"


def schema(
    required: Sequence[str],
    optional: Sequence[str] = (),
    values: Mapping[str, Sequence[str]] | None = None,
) -> JsonObject:
    """生成工具参数的 JSON Schema。

    取值封闭的字段写成 enum，模型因此填不出登记之外的值，Toolbox 在执行前就会拒绝。
    这比让它拿到一个空结果重要：空结果既可能是"真的没有"，也可能是"问法不对"。
    """
    declared = values or {}
    properties: JsonObject = {}
    for name in (*required, *optional):
        field: JsonObject = {"type": "string", "minLength": 1}
        if name in declared:
            field["enum"] = list(declared[name])
        elif name in ("start", "end"):
            field["description"] = WINDOW_HINT
        properties[name] = field
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def rows_for_model(result: SourceResult) -> JsonObject:
    """把 SQL 查到的行交给模型。

    来源文件与数据版本已随 source.response 事件落库，界面和评测从那里读取；
    重复放进模型上下文只会挤占预算。
    """
    return {"rows": result["rows"], "truncated": result["truncated"]}


def log_rows_for_model(result: SourceResult) -> JsonObject:
    """把日志行交给模型，取出 record 本身。

    日志行是 {line, record}；行号属于溯源，留在事件里，模型看到的字段形状与 SQL 一致。
    """
    rows = [require_object(require_object(row)["record"]) for row in result["rows"]]
    return {"rows": rows, "truncated": result["truncated"]}
