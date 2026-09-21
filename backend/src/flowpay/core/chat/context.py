"""将界面聊天记录投影为模型消息；原生工具历史由阶段检查点单独保存。"""

from openai.types.chat import ChatCompletionMessageParam

from flowpay.core.chat.types import ChatMessage
from flowpay.core.execution.contracts import TurnInput


def conversation_history(turn: TurnInput) -> list[ChatMessage]:
    """本轮之前的界面历史；继续中断的一轮时不把当前问题重复算进去。"""
    history = turn.chat_history
    if turn.resumed and history and history[-1]["role"] == "user":
        return history[:-1]
    return history


def chat_messages(turn: TurnInput, system: str) -> list[ChatCompletionMessageParam]:
    """用系统提示词、历史正文和当前问题组成一轮的初始消息。"""
    messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
    for message in conversation_history(turn):
        messages.append(
            {"role": "user", "content": message["content"]}
            if message["role"] == "user"
            else {"role": "assistant", "content": message["content"]}
        )
    messages.append({"role": "user", "content": turn.user_message})
    return messages
