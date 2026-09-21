"""dataset 模拟用户的门面：按需接上它的源码目录，并把动态事实收窄成 fact_id。

复用 `flowpay_simulator.simulated_user` 的判定逻辑，不复制它。模拟器随数据集分发，
因此 sys.path 与导入都推迟到第一次真正用到，导入本模块不要求配置数据集。

导入进来的模块没有类型信息，这里用 Protocol 把实际用到的那几项声明清楚，
边界之外不再出现 Any。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping, Sequence
from functools import cache
from typing import Protocol, cast

from flowpay.core.json import JsonObject, require_object, require_text
from flowpay.dataset import require_dataset


class User(Protocol):
    """模拟用户实例：回答一次追问，返回正文与本次披露的事实。"""

    def answer(self, assistant_question: str) -> tuple[str, list[JsonObject]]: ...


class UserFactory(Protocol):
    """`DeterministicSimulatedUser` 的构造签名。"""

    def __call__(self, *, conversation: dict[str, object]) -> User: ...


class Module(Protocol):
    """本文件用到的全部模块成员。"""

    CATEGORY_KEYWORDS: Mapping[str, Sequence[str]]
    DeterministicSimulatedUser: UserFactory


@cache
def simulator() -> Module:
    """导入数据集自带的模拟用户模块；路径只登记一次。"""
    source = str(require_dataset() / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    # ModuleType 与 Protocol 不重叠，按类型检查器的要求先退回 object 再转。
    module = cast(object, importlib.import_module("flowpay_simulator.simulated_user"))
    return cast(Module, module)


def keyword_hits(text: str) -> set[str]:
    """正文命中的类别；仅用于第三级回退判定，单独使用会误判（告别语也能命中）。"""
    lowered = text.lower()
    return {
        name
        for name, words in simulator().CATEGORY_KEYWORDS.items()
        if any(word.lower() in lowered for word in words)
    }


class SimulatedUser:
    """按预设剧本回答追问：只披露助手明确问过的类别，同一事实不重复给。"""

    def __init__(self, conversation: dict[str, object]) -> None:
        """绑定本题的对话剧本；每道题独立一个实例。"""
        self.inner: User = simulator().DeterministicSimulatedUser(
            conversation=conversation
        )

    def answer(self, question: str) -> tuple[str, list[str]]:
        """回答一次追问，返回回复正文与本次披露的 fact_id 列表。

        问到的类别没有对应事实时，回复是一句"还没整理出来"，fact_id 列表为空，
        调用方据此记为 unexpected_category。
        """
        reply, facts = self.inner.answer(question)
        return reply, [require_text(require_object(fact)["fact_id"]) for fact in facts]
