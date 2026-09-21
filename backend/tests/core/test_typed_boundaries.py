"""验证动态输入的类型边界，不丢失原始消息、事件扩展或配置错误。"""

import json
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletionMessage

from flowpay.core.config import load_settings
from flowpay.core.event_log import EventStore
from flowpay.core.event_log.types import require_event
from flowpay.core.json import JsonObject, parse_json, require_object
from flowpay.core.llm.chat.messages import MessageLog


def test_message_checkpoint_preserves_extensions_and_is_copied() -> None:
    message = ChatCompletionMessage.model_validate(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "保留的公开推理字段",
            "tool_calls": [
                {
                    "id": "tool-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        }
    )
    conversation = MessageLog([{"role": "system", "content": "system"}])
    conversation.add_assistant_message(message)
    conversation.add_tool_result("tool-1", {"found": True})
    checkpoint = parse_json(json.dumps(conversation.messages))
    resumed = MessageLog.restore(checkpoint)
    assert resumed.messages == conversation.messages
    # 断言序列化后的完整结构，包含 SDK 请求类型未声明的供应商扩展。
    assert json.dumps(resumed.messages, ensure_ascii=False).count("保留的公开推理字段") == 1
    resumed.add_user_message("继续")
    assert len(conversation.messages) == 3
    assert len(resumed.messages) == 4


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "wrong", "content": "text"}],
        [{"role": "user", "content": 123}],
        {"role": "user", "content": "text"},
    ],
)
def test_malformed_checkpoint_is_rejected(messages: object) -> None:
    with pytest.raises(ValueError):
        MessageLog.restore(messages)


def test_event_validation_keeps_extensions_and_rejects_coercion(tmp_path: Path) -> None:
    event = EventStore(tmp_path / "trace.sqlite").append("s", [("custom", {"nested": {"a": 1}})])[0]
    extended: JsonObject = {**event, "future_extension": {"keep": True}}
    assert require_event(extended) == extended
    invalid: JsonObject = {**event, "seq": "1"}
    with pytest.raises(ValueError):
        require_event(invalid)
    assert require_object(extended["payload"])["nested"] == {"a": 1}


@pytest.mark.parametrize("table", ["model", "embedding"])
def test_unknown_nested_configuration_is_not_silently_ignored(tmp_path: Path, table: str) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(f'data_dir="."\ndataset_path="."\n[{table}]\ntypo=true\n')
    with pytest.raises(ValueError, match="Unknown"):
        load_settings(path)
