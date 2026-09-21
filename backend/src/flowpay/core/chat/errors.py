"""会话读取异常，供 HTTP 层映射为 404。"""


class SessionNotFound(Exception):
    """指定会话不存在；不用于包装程序内部的字段或配置错误。"""
