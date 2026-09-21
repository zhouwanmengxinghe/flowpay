"""公开模型访问能力；消息、工具调用和响应类型直接从 openai.types.chat 导入。"""

from .contracts import ChatModel, ChunkSink

__all__ = ["ChunkSink", "ChatModel"]
