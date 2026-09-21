"""动态 JSON 边界的公共类型与校验；不定义会话或运行结构。"""

import json
from typing import cast

from pydantic import TypeAdapter

JsonObject = dict[str, object]


def require_object(value: object) -> JsonObject:
    """在供应商 JSON 解码边界验证顶层对象，不使用强转掩盖格式错误。"""
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object with string keys")
    # isinstance 只确认容器；先以 object 保留未知值，再逐个验证键。
    items = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in items):
        raise ValueError("Expected a JSON object with string keys")
    return cast(JsonObject, items)


def require_list(value: object) -> list[object]:
    """验证动态输入是列表；元素仍须由协议或业务边界检查。"""
    if not isinstance(value, list):
        raise ValueError("Expected a list")
    return cast(list[object], value)


def require_text(value: object) -> str:
    """要求实际字符串，拒绝把缺失值或其他对象强行转为文本。"""
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value


def require_int(value: object) -> int:
    """读取整数编号；bool 是 int 的子类，但不代表有效编号。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Expected an integer")
    return value


def require_number(value: object) -> float:
    """读取相似度、耗时一类的数值；bool 是 int 的子类，但不是有效数值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a number")
    return float(value)


def parse_json(text: str | bytes) -> object:
    """隔离标准库 JSON 解码器的 Any；解码不代表已验证协议结构。"""
    return cast(object, json.loads(text))


def to_json_object(value: object) -> JsonObject:
    """在事件、持久化或报告出口序列化有类型的对象；内部调用继续传递原模型。"""
    return require_object(
        cast(
            object, TypeAdapter(type(value)).dump_python(value, mode="json", serialize_as_any=True)
        )
    )
