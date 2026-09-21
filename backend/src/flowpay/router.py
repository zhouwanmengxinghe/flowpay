"""课程阶段入口；只选择阶段，不执行检索策略或 Agent Loop。"""

from collections.abc import Callable

from flowpay.core.execution.contracts import Workflow
from flowpay.core.llm.chat import ChatModel
from flowpay.workflows.s1 import S1Workflow
from flowpay.workflows.s2 import S2Workflow
from flowpay.workflows.s3 import S3Workflow
from flowpay.workflows.s4 import S4Workflow

StageFactory = Callable[[ChatModel, Callable[[str], str]], Workflow]

# 主线四级按投入递增，每一级新增一层结构；不设与主线并列的变体入口。
STAGES: dict[str, StageFactory] = {
    "s1": S1Workflow,
    "s2": S2Workflow,
    "s3": S3Workflow,
    "s4": S4Workflow,
}


def resolve_stage(name: str) -> str:
    """检查阶段已实现；未知名称直接报错。"""
    if name in STAGES:
        return name
    raise ValueError(f"Unknown stage: {name}; available: {', '.join(STAGES)}")


def create_stage(name: str, model: ChatModel, prepare_query: Callable[[str], str]) -> Workflow:
    """将装配好的能力交给所选阶段；各阶段保持独立的 run 实现和会话身份。"""
    return STAGES[resolve_stage(name)](model, prepare_query)
