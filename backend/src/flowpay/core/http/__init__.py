"""HTTP 应用与请求模型公共入口。"""

from .app import ContinueBody, MessageBody, create_app

__all__ = ["MessageBody", "ContinueBody", "create_app"]
