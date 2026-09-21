"""只验证运行契约的确定性工作流，不模拟业务调查。

冒烟脚本靠它检查 HTTP 与 SSE 这一层：提交幂等、waiting_user 状态、断线后按 seq 续传、
事件序号连续、工具调用带 parent_call_id、取消后继续。这些行为在课程阶段里也有，
但要用真实模型触发就不可复现，因此这里用固定剧本走一遍同样的协议路径。

`/slow`、`/tool-error`、`/model-error` 是给验收用的触发器，让对应分支可复现地发生。
"""

from __future__ import annotations

from flowpay.core.config import Settings
from flowpay.core.execution.contracts import TurnInput, TurnResult
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard
from flowpay.core.execution.types import TurnResultStatus
from flowpay.core.json import JsonObject, require_object
from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.tools import Tool
from openai.types.chat import ChatCompletion

from scripts.runtime_demo.scripted import ScriptedAdapter

SYSTEM = (
    "这是基座契约验收使用的确定性剧本，不是模型诊断，也不查询任何真实数据。"
    "工具结果为固定值，只用于验证运行协议。"
)

# 两个通用工具：一个不需要参数，一个需要一个编号。足够覆盖"有参数/无参数"两种调用形状。
ARGUMENTS: JsonObject = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
    "additionalProperties": False,
}


def tool_call(name: str) -> JsonObject:
    """构造一次 function 调用；arguments 按协议是 JSON 字符串。"""
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": '{"note": "contract"}'},
    }


def fixture_passage() -> Passage:
    """一条固定证据；来源标记为 simulation，与真实检索结果区分。"""
    return Passage(
        id="fixture:contract",
        title="契约验收固定证据",
        text="这条证据由验收剧本提供，不代表任何业务事实。",
        score=1.0,
        source=PassageSource(
            collection_id="simulation",
            doc_id="fixture:contract",
            chunk_no=1,
            path="scripts/runtime_demo/workflow.py",
            sha256="simulation",
            locator={},
            metadata={"simulation": True},
        ),
    )


class DemoWorkflow:
    """按固定剧本走完一次追问与一次作答，用于验证运行契约。

    不做实体识别，也不判断业务语义：第二轮只看用户是否回了内容，不看回了什么。
    """

    id: str = "foundation-demo"
    version: str = "5"
    card: StageCard = StageCard(
        label="契约验收",
        description="确定性剧本，用于验证会话、SSE、取消与恢复",
        examples=["订单成功了，但一直没有收到支付通知"],
        reply_hint="这是基座验收模式，不调用真实模型。",
    )

    def __init__(self, settings: Settings):
        """保存配置；模拟延迟用于放大取消与断线的观察窗口。"""
        self.settings: Settings = settings

    async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
        """第一轮追问并挂起，补充之后作答；检查点只记录是否已经问过。"""
        delay = (
            max(self.settings.mock_delay_ms, 400)
            if "/slow" in turn.user_message
            else (self.settings.mock_delay_ms)
        )
        state: JsonObject = dict(turn.saved_state)
        asked = bool(state.get("asked"))

        async def probe(arguments: JsonObject) -> JsonObject:
            """固定返回；/tool-error 时抛错，用于验证工具失败被记录且可继续。"""
            if "/tool-error" in turn.user_message and not turn.resumed:
                raise RuntimeError("模拟工具错误：用于验证失败记录和显式继续")
            return {"ok": True, "note": require_object(arguments).get("note")}

        toolbox = runtime.toolbox(
            [
                Tool("probe_rules", "读取固定规则（模拟）", ARGUMENTS, probe),
                Tool("probe_record", "读取固定记录（模拟）", ARGUMENTS, probe),
            ]
        )

        async def speak(message: JsonObject) -> tuple[ChatCompletion, str]:
            """发一次剧本模型调用；/model-error 时主动失败，验证错误被记录且可继续。"""
            adapter = ScriptedAdapter(
                message,
                delay_ms=delay,
                fail="/model-error" in turn.user_message and not turn.resumed,
            )
            return await runtime.chat(adapter).call(
                [{"role": "system", "content": SYSTEM}]
            )

        async def probe_with(name: str) -> None:
            """走一遍"模型请求工具、逐个执行"的真实协议形状，工具事件因此带父调用。"""
            completion, call_id = await speak(
                {"role": "assistant", "content": "", "tool_calls": [tool_call(name)]}
            )
            for requested in completion.choices[0].message.tool_calls or []:
                # 剧本只生成 function 调用；与 core 读取模型回复时的收窄方式一致。
                if requested.type == "function":
                    await toolbox.run(requested, parent_call_id=call_id)

        async def say(text: str, status: TurnResultStatus) -> TurnResult:
            """提交最终正文；界面据此收到与真实阶段相同的流式片段。"""
            completion, _ = await speak({"role": "assistant", "content": text})
            return TurnResult(
                completion.choices[0].message.content or "",
                state,
                status,
                [fixture_passage()],
            )

        await probe_with("probe_rules")
        if not asked:
            state["asked"] = True
            return await say(
                "需要补充一项信息才能继续，请回复任意内容以推进剧本。", "waiting_user"
            )
        await probe_with("probe_record")
        state["resolved"] = True
        return await say(
            "剧本已走完：追问、补充、工具调用与作答都已记录。", "completed"
        )
