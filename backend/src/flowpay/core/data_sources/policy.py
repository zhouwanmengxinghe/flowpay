"""只读数据源登记格式；在文件边界验证，查询代码使用明确字段。"""

from typing import NotRequired

from typing_extensions import TypedDict


class SqlSource(TypedDict):
    """一个已登记的只读关系：可以按哪些条件查、能读回哪些列。

    登记的是关系而不是某一条查询：调用方从 filters 里挑条件组合，语句由 WHERE 拼出来，
    因此新增一种问法不需要新增登记项。scope 是必填条件，用于强制账户与环境隔离；
    关系本身可以是连表，这样没有账户列的表也能按账户过滤。

    Attributes:
        relation: FROM 之后的关系表达式，可以带别名和 JOIN。
        scope: 参数名到列表达式；每次查询都必须给全，永远参与 WHERE。
        filters: 参数名到列表达式；调用方按需给出子集。
        select: 读回的列表达式，按登记顺序返回。
        order_by: 排序表达式，保证同条件下结果稳定。
        columns: 授权器白名单，表名到允许读取的列；未列出的表列一律拒绝。
        values: 取值封闭的字段到它的合法取值；声明后查询只接受集合内的值，
            工具也据此给模型 enum。取值来自产品定义，不是某一份数据快照的现状。
    """

    path: str
    relation: str
    scope: dict[str, str]
    filters: dict[str, str]
    select: list[str]
    order_by: str
    columns: dict[str, list[str]]
    values: NotRequired[dict[str, list[str]]]
    max_rows: NotRequired[int]
    timeout_seconds: NotRequired[float]


class LogSource(TypedDict):
    """一个日志文件的格式、公开字段和允许的过滤条件。"""

    path: str
    format: NotRequired[str]
    time_field: NotRequired[str]
    filter_fields: NotRequired[list[str]]
    result_fields: NotRequired[list[str]]
    values: NotRequired[dict[str, list[str]]]
    max_rows: NotRequired[int]
    max_line_bytes: NotRequired[int]


class SourcePolicy(TypedDict):
    """数据快照身份及允许访问的 SQL、日志来源，不包含业务调查策略。"""

    release_id: str
    manifest: str
    manifest_sha256: str
    sql: NotRequired[dict[str, SqlSource]]
    logs: NotRequired[dict[str, LogSource]]
