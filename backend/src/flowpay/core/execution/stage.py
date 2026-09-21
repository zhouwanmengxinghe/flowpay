"""阶段在界面上的说明文字，与执行契约分开。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StageCard:
    """阶段的展示信息：标题、一句话说明、示例问题和回复提示。

    改这些文字不影响运行，也不改变会话兼容性；执行契约在 Workflow，
    预算与策略在阶段自己的文件里。
    """

    label: str
    description: str
    examples: list[str] = field(default_factory=list)
    reply_hint: str | None = None
