# 只访问应用配置登记的数据源；校验文件版本，并限制 SQL 表列和日志可见字段。
"""Read-only, registered sources. Policies are supplied by application configuration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import tomllib
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Literal, TypedDict, cast

from pydantic import TypeAdapter

from flowpay.core.event_log.contracts import EventSink
from flowpay.core.event_log.store import new_id
from flowpay.core.json import JsonObject, parse_json, require_object, require_text

from .policy import LogSource, SourcePolicy, SqlSource


class SourceFile(TypedDict):
    """查询所依据的单个文件及已校验摘要。"""

    path: str
    sha256: str


class SourceVersion(TypedDict):
    """业务快照的发布身份、访问策略及文件摘要。"""

    dataset_release_id: str
    dataset_manifest_sha256: str
    source_policy_sha256: str
    files: dict[str, str]


class SqlQuery(TypedDict):
    """已登记的查询操作及绑定参数。"""

    operation: str
    parameters: JsonObject


class LogQuery(TypedDict):
    """日志查询的来源、过滤和时间窗口。"""

    source: str
    filters: JsonObject
    start: str | None
    end: str | None
    contains: str | None


class SourceResult(TypedDict):
    """只读业务查询结果，包含返回行、截断标记、来源版本和实际查询条件。"""

    rows: list[JsonObject]
    truncated: bool
    source: SourceFile
    version: SourceVersion
    query: SqlQuery | LogQuery


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


# 解析符号链接后的路径仍需在数据根目录内，避免相对路径绕过边界。
def within(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Source path must be relative to the configured root")
    resolved = (root / path).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("Source escapes configured root")
    return resolved


def check_values(declared: dict[str, list[str]], given: JsonObject) -> None:
    """取值封闭的字段只接受登记集合内的值。

    工具的 JSON Schema 已经用 enum 拦过一遍；这里再拦一次，让绕过工具的调用
    也得到明确错误，而不是一个会被误读成"没有记录"的空结果。
    """
    for name, allowed in declared.items():
        if name in given and given[name] not in allowed:
            raise ValueError(f"{name} must be one of: {', '.join(allowed)}")


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result


class DataSources:
    """按应用提供的登记策略访问固定业务数据快照。

    policy 声明允许的 SQL 操作、表列和日志来源；它不是由模型自由生成的配置。
    构造时校验发布清单，查询前后核对文件摘要。这里只读原始 SQLite 和日志，
    不会把业务数据导入知识向量库。
    """

    def __init__(self, root: Path, policy: object):
        """解析业务根目录并复制登记策略，立即核对清单及文件摘要，拒绝漂移或越界来源。"""
        self.root: Path = root.resolve(strict=True)
        self.policy: SourcePolicy = TypeAdapter(SourcePolicy).validate_python(policy, strict=True)
        self.policy_hash: str = hashlib.sha256(
            json.dumps(policy, sort_keys=True).encode()
        ).hexdigest()
        self.manifest: Path = within(self.root, self.policy["manifest"])
        self.files: dict[str, str] = {}
        self.verify()

    @classmethod
    def from_config(cls, path: Path, root: Path) -> DataSources:
        """从指定 TOML 读取访问策略，在 root 下创建并校验只读业务来源集合。"""
        with path.open("rb") as handle:
            return cls(root, tomllib.load(handle))

    # 发布清单和登记文件都要匹配哈希，不能仅凭文件名判断版本。
    def verify(self) -> SourceVersion:
        """核对清单及所有登记文件的哈希，拒绝 SQLite 活动日志，返回已校验版本快照。"""
        if digest(self.manifest) != self.policy["manifest_sha256"]:
            raise ValueError("Dataset manifest hash mismatch")
        manifest = require_object(parse_json(self.manifest.read_text()))
        manifest_files = require_object(manifest["files"])
        files: dict[str, str] = {}
        for entry in [*self.policy.get("sql", {}).values(), *self.policy.get("logs", {}).values()]:
            relative = entry["path"]
            path = within(self.manifest.parent, relative)
            expected = require_object(manifest_files.get(relative, {}))
            if not expected or digest(path) != expected["sha256"]:
                raise ValueError(f"Source hash mismatch: {relative}")
            # 旁路日志可能改变看似固定的 SQLite 快照，因此拒绝活动日志文件。
            if entry in self.policy.get("sql", {}).values():
                if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
                    raise ValueError("SQLite snapshot has active journal sidecars")
            files[relative] = require_text(expected["sha256"])
        self.files = files
        return self.snapshot()

    def snapshot(self) -> SourceVersion:
        """返回最近已验证的版本和文件摘要副本；本方法自身不重新读取文件。"""
        return {
            "dataset_release_id": self.policy["release_id"],
            "dataset_manifest_sha256": self.policy["manifest_sha256"],
            "source_policy_sha256": self.policy_hash,
            "files": dict(self.files),
        }

    def _path(self, entry: SqlSource | LogSource) -> Path:
        """解析登记文件并再次核对摘要；文件越界或自上次校验后改变则拒绝。"""
        path = within(self.manifest.parent, entry["path"])
        if digest(path) != self.files[entry["path"]]:
            raise ValueError("Source changed since verification")
        return path

    # 执行登记好的参数化语句；只读连接和 SQLite 授权器共同限制访问。
    def sql(
        self,
        source: str,
        scope: JsonObject,
        filters: JsonObject | None = None,
        limit: int = 50,
    ) -> SourceResult:
        """按登记关系的白名单条件查询，条件值以绑定参数传入。

        语句由登记的关系、select 列和 WHERE 条件拼成；列表达式来自策略文件，
        条件的值始终是绑定参数，调用方给不进 SQL 文本。scope 必须给全，
        filters 只能是登记字段的子集。只读连接、query_only 和授权器分别约束
        文件模式、语句写入及表列访问；进度回调用于限制长查询。
        多取一行判断 truncated，并在返回时附带查询条件、来源摘要与数据版本。
        """
        spec = self.policy.get("sql", {}).get(source)
        if spec is None:
            raise ValueError("SQL source is not registered")
        conditions = require_object(scope)
        if set(conditions) != set(spec["scope"]):
            raise ValueError("Scope must give exactly the registered columns")
        chosen = require_object(filters) if filters is not None else {}
        if set(chosen) - set(spec["filters"]):
            raise ValueError("Unsupported SQL filter")
        check_values(spec.get("values", {}), {**conditions, **chosen})
        parameters: JsonObject = {**conditions, **chosen}
        where = [f"{spec['scope'][name]} = :{name}" for name in conditions]
        where += [f"{spec['filters'][name]} = :{name}" for name in chosen]
        statement = (
            f"SELECT {', '.join(spec['select'])} FROM {spec['relation']} "
            f"WHERE {' AND '.join(where)} ORDER BY {spec['order_by']}"
        )
        if type(limit) is not int or not 1 <= limit <= spec.get("max_rows", 100):
            raise ValueError("Invalid SQL row limit")
        path = self._path(spec)
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise ValueError("SQLite snapshot has active journal sidecars")
        allowed = {
            (table.lower(), col.lower()) for table, cols in spec["columns"].items() for col in cols
        }

        # 默认拒绝，只放行 SELECT 和登记的表列读取；只读文件模式本身并不能阻止
        # 查询内部审计表，因此还需 SQLite 授权器对实际访问逐项检查。
        def authorize(
            action: int,
            arg1: str | None,
            arg2: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if (
                action == sqlite3.SQLITE_READ
                and ((arg1 or "").lower(), (arg2 or "").lower()) in allowed
            ):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.enable_load_extension(False)
            db.set_authorizer(authorize)
            deadline = time.monotonic() + spec.get("timeout_seconds", 2)
            # 在执行虚拟机指令期间检查截止时间，避免复杂查询长期占用工作线程。
            # 该限制是查询保护，不是整个文件校验流程的端到端超时。
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = cast(list[sqlite3.Row], db.execute(statement, parameters).fetchmany(limit + 1))
            result = [require_object(dict(row)) for row in rows[:limit]]
        finally:
            db.close()
        if digest(path) != self.files[spec["path"]] or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")
        ):
            raise ValueError("SQLite snapshot changed during query")
        return {
            "rows": result,
            "truncated": len(rows) > limit,
            "source": {"path": spec["path"], "sha256": self.files[spec["path"]]},
            "version": self.snapshot(),
            "query": {"operation": source, "parameters": parameters},
        }

    # 先裁剪公开字段再筛选，避免通过匹配结果间接暴露隐藏字段。
    def logs(
        self,
        source: str,
        filters: JsonObject | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
        contains: str | None = None,
    ) -> SourceResult:
        """查询登记的 JSONL 或纯文本日志，并返回原文件行号。

        结构化过滤只允许配置字段；时间范围为 [start, end)，时间值必须带时区。
        contains 是字面子串，只在公开结果字段上匹配；纯文本来源只支持该匹配方式。
        扫描按行限制大小，多取一个匹配结果判断截断，查询结束后再次核对文件哈希。
        """
        spec = self.policy.get("logs", {}).get(source)
        if spec is None:
            raise ValueError("Log source is not registered")
        if contains is not None:
            contains = require_text(contains)
        filters = filters if filters is not None else {}
        if set(require_object(filters)) - set(spec.get("filter_fields", [])):
            raise ValueError("Unsupported log filter")
        check_values(spec.get("values", {}), filters)
        if type(limit) is not int or not 1 <= limit <= spec.get("max_rows", 100):
            raise ValueError("Invalid log row limit")
        lower, upper = timestamp(start) if start else None, timestamp(end) if end else None
        if lower and upper and lower >= upper:
            raise ValueError("Invalid time range")
        if (lower or upper) and not spec.get("time_field"):
            raise ValueError("This log has no configured timestamp field")
        path = self._path(spec)
        found: list[JsonObject] = []
        with path.open("rb") as handle:
            line_no = 0
            while True:
                # 限制的是读取字节数，而不是解码后的字符数；额外读取一个字节可发现超限。
                # 按行号保留来源，使返回记录能在原始日志中直接定位。
                raw = handle.readline(spec.get("max_line_bytes", 1048576) + 1)
                if not raw:
                    break
                line_no += 1
                if len(raw) > spec.get("max_line_bytes", 1048576):
                    raise ValueError(f"Log line exceeds configured size at line {line_no}")
                text = raw.decode("utf-8").rstrip("\r\n")
                if not text:
                    continue
                if spec.get("format", "jsonl") == "text":
                    if filters or lower or upper:
                        raise ValueError("Text logs support literal contains only")
                    row: JsonObject = {"text": text}
                else:
                    row = require_object(parse_json(text))
                    if any(row.get(key) != value for key, value in filters.items()):
                        continue
                    if lower or upper:
                        at = timestamp(require_text(row[require_text(spec.get("time_field"))]))
                        if (lower and at < lower) or (upper and at >= upper):
                            continue
                    # 结构化过滤已经过 filter_fields 白名单校验；自由文本 contains
                    # 必须在这一步裁剪之后进行，不能拿隐藏审计内容参与全文命中。
                    row = {key: row[key] for key in spec.get("result_fields", []) if key in row}
                # 只匹配公开投影内容，避免通过命中与否泄露被排除的字段。
                searchable = (
                    require_text(row["text"])
                    if spec.get("format") == "text"
                    else json.dumps(row, ensure_ascii=False)
                )
                if contains is not None and contains not in searchable:
                    continue
                found.append({"line": line_no, "record": row})
                if len(found) > limit:
                    break
        if digest(path) != self.files[spec["path"]]:
            raise ValueError("Log file changed during query")
        return {
            "rows": found[:limit],
            "truncated": len(found) > limit,
            "source": {"path": spec["path"], "sha256": self.files[spec["path"]]},
            "version": self.snapshot(),
            "query": {
                "source": source,
                "filters": filters,
                "start": start,
                "end": end,
                "contains": contains,
            },
        }


class SourceClient:
    """查询能力的追踪入口；调用点明确给出 SQL 或日志查询，不使用动态方法名。"""

    def __init__(self, sources: DataSources):
        """接收已登记的数据源；每次查询再通过本轮 EventSink 记录条件与结果。"""
        self.sources: DataSources = sources

    def field_values(self, kind: Literal["sql", "logs"], source: str) -> dict[str, list[str]]:
        """返回某个登记来源上取值封闭的字段及其合法取值；未声明的字段不出现。"""
        sources = cast(dict[str, JsonObject], self.sources.policy.get(kind, {}))
        spec = sources.get(source, {})
        return cast(dict[str, list[str]], spec.get("values", {}))

    async def query_sql(
        self,
        source: str,
        scope: JsonObject,
        context: EventSink,
        *,
        filters: JsonObject | None = None,
        limit: int = 50,
    ) -> SourceResult:
        """记录并执行一个登记关系上的查询。

        scope 给全登记的隔离列，filters 从登记字段里挑子集；两者的值都是绑定参数，
        调用方给不进 SQL 文本，也不能提供任意语句。
        """
        query = partial(self.sources.sql, source, scope, filters, limit)
        return await self._execute_recorded_query(
            query,
            context,
            {
                "kind": "sql",
                "name": source,
                "parameters": {**scope, **(filters or {})},
                "limit": limit,
            },
        )

    async def query_logs(
        self,
        source: str,
        context: EventSink,
        *,
        filters: JsonObject | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
        contains: str | None = None,
    ) -> SourceResult:
        """记录日志来源、过滤条件和时间范围，再返回带文件行号的结果；错误不转为空命中。"""
        options = {
            "filters": filters,
            "start": start,
            "end": end,
            "limit": limit,
            "contains": contains,
        }
        query = partial(
            self.sources.logs,
            source,
            filters=filters,
            start=start,
            end=end,
            limit=limit,
            contains=contains,
        )
        return await self._execute_recorded_query(
            query, context, {"kind": "logs", "name": source, **options}
        )

    async def _execute_recorded_query(
        self, query: Callable[[], SourceResult], context: EventSink, request: JsonObject
    ) -> SourceResult:
        # 线程中的查询无法被协程强行取消，所以底层只读查询必须有自身限制。
        """先写请求，再在线程执行只读查询；记录成功、取消或异常后返回或继续上抛。"""
        call_id = new_id("source")
        await context.emit(
            "source.request", {**request, "version": self.sources.snapshot()}, call_id
        )
        try:
            result = await asyncio.to_thread(query)
            await context.emit("source.response", dict(result), call_id)
            return result
        except asyncio.CancelledError:
            await context.emit("source.canceled", {}, call_id)
            raise
        except Exception as error:
            await context.emit(
                "source.error", {"error_type": type(error).__name__, "message": str(error)}, call_id
            )
            raise
