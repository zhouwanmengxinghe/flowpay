"""累积本轮检索到的片段。"""

from __future__ import annotations

import json

from pydantic import TypeAdapter

from flowpay.core.knowledge.contracts import Passage, for_model


class PassageSet:
    """一轮 agent 累积的片段，按 Passage.id 去重。

    检索工具每次把命中加进来，回答随附的证据清单和离线复核都从这里取原文。
    片段不可变，因此登记后直接共享同一个对象；调用方决定它能跨多少轮保留。
    """

    def __init__(self) -> None:
        """新建空集合；恢复检查点请使用 restore。"""
        self.passages: dict[str, Passage] = {}

    @classmethod
    def restore(cls, value: object, collection_id: str | None) -> PassageSet:
        """从检查点恢复片段。

        这是阶段状态与当前数据版本之间的唯一一道边界：受理本轮时已核对过数据版本，
        这里只在真的读到别的集合的片段时拒绝，避免引用指向另一个版本的原文。
        """
        # 检查点里的片段是 JSON 对象，按 Passage 的字段类型还原；strict 会拒绝 dict 输入。
        passages = TypeAdapter(list[Passage]).validate_python(value)
        if any(passage.source.collection_id != collection_id for passage in passages):
            raise ValueError("Saved evidence belongs to another collection")
        restored = cls()
        restored.add(passages)
        return restored

    def add(self, passages: list[Passage]) -> list[Passage]:
        """登记去重后返回本次命中对应的片段，顺序与传入一致。"""
        result: dict[str, Passage] = {}
        for passage in passages:
            result[passage.id] = self.passages.setdefault(passage.id, passage)
        return list(result.values())

    def all(self) -> list[Passage]:
        """按登记顺序返回全部片段，用于检查点和引用关联。"""
        return list(self.passages.values())

    def serialize(self) -> str:
        """序列化交给模型的片段；溯源信息留在本对象内，供引用展开和事件日志使用。"""
        return json.dumps(for_model(self.all()), ensure_ascii=False)
