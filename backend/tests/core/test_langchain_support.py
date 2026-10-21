"""使用 LangChain 真实图循环和脚本模型，验证多轮追问与工具授权。"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from flowpay.business.app import AuthorizedSources, EvidenceSink, ExecuteBody
from flowpay.core.data_sources.reader import SourceClient
from flowpay.core.execution.contracts import TurnInput
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.workflows.langchain_support import LangChainSupportWorkflow


class ScriptedChat(BaseChatModel):
    replies: list[AIMessage]
    calls: int = 0
    @property
    def _llm_type(self):
        return "scripted-langchain-verification"
    def bind_tools(self, tools, **kwargs):
        return self
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self.replies[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=reply)])


def call(name, args, identifier):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier, "type": "tool_call"}])


class LangChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_text_blocks_use_the_same_output_guard(self):
        model = ScriptedChat(replies=[AIMessage(content=[{"type": "text", "text": "<｜DSML｜tool_calls>"}]),
            AIMessage(content=[{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "目前尚未核实原因。"}])])
        with patch.object(SourceClient, "field_values", return_value={}):
            result = await LangChainSupportWorkflow(model, lambda text: text).run(TurnInput("排查通知", [], {}), self.runtime())
        self.assertEqual(result.text, "目前尚未核实原因。")
        self.assertEqual(model.calls, 2)

    def runtime(self):
        body = ExecuteBody(tenant_id="merchant-a", session_id="session", run_id="run", account_ids=["acct_a"], environment="production", content="x")
        return AgentRuntime(EvidenceSink(), AuthorizedSources(SourceClient(None), body))

    async def test_ask_then_resume_with_paired_tool_messages(self):
        model = ScriptedChat(replies=[call("ask_user", {"missing": ["payment_id"], "question": "请提供支付编号"}, "ask1"),
            call("query_payments", {"account_id": "acct_a", "environment": "production", "payment_id": "pay_test_001"}, "query1"),
            AIMessage(content="查到该支付已成功。")])
        workflow = LangChainSupportWorkflow(model, lambda text: text)
        with patch.object(SourceClient, "field_values", return_value={}), patch.object(SourceClient, "query_sql", new_callable=AsyncMock, return_value={"rows": [{"status": "succeeded"}], "truncated": False}) as query:
            first = await workflow.run(TurnInput("支付出了问题", [], {}), self.runtime())
            self.assertEqual(first.status, "waiting_user")
            self.assertEqual(model.calls, 1)
            stored = first.saved_state["langchain_messages"]
            self.assertEqual(stored[-1]["type"], "tool")
            second = await workflow.run(TurnInput("pay_test_001", [], first.saved_state), self.runtime())
            self.assertEqual(second.status, "completed")
            self.assertIn("成功", second.text)
            query.assert_awaited_once()

    async def test_framework_tool_call_cannot_bypass_scope(self):
        model = ScriptedChat(replies=[call("query_payments", {"account_id": "acct_b", "environment": "production", "payment_id": "pay_b"}, "bad"), AIMessage(content="无权访问其他账户。")])
        with patch.object(SourceClient, "field_values", return_value={}), patch.object(SourceClient, "query_sql", new_callable=AsyncMock) as query:
            result = await LangChainSupportWorkflow(model, lambda text: text).run(TurnInput("查其他商户", [], {}), self.runtime())
            query.assert_not_called()
            self.assertIn("超出", json.dumps(result.saved_state, ensure_ascii=False))

    async def test_fabricated_record_is_corrected_before_commit(self):
        model = ScriptedChat(replies=[AIMessage(content="支付 pay_fake_999 已成功。"), AIMessage(content="尚未核实支付记录，请提供支付编号。")])
        with patch.object(SourceClient, "field_values", return_value={}):
            result = await LangChainSupportWorkflow(model, lambda text: text).run(TurnInput("查支付", [], {}), self.runtime())
        self.assertEqual(model.calls, 2)
        self.assertNotIn("pay_fake_999", result.text)

    async def test_repeated_fabrication_stops_at_six_model_calls(self):
        model = ScriptedChat(replies=[AIMessage(content="支付 pay_fake_999 已成功。") for _ in range(8)])
        with patch.object(SourceClient, "field_values", return_value={}):
            with self.assertRaises(Exception) as result:
                await LangChainSupportWorkflow(model, lambda text: text).run(TurnInput("查支付", [], {}), self.runtime())
        self.assertEqual(model.calls, 6)
        self.assertIn("ModelCallLimit", type(result.exception).__name__)

    async def test_ninth_tool_is_blocked_but_partial_answer_is_kept(self):
        batch = AIMessage(content="", tool_calls=[{"name":"query_payments", "args":{"account_id":"acct_a","environment":"production","payment_id":f"pay_test_{number:03}"}, "id":f"call{number}","type":"tool_call"} for number in range(9)])
        model = ScriptedChat(replies=[batch, AIMessage(content="部分记录已核实，其余查询因工具预算不足尚未确认。")])
        with patch.object(SourceClient, "field_values", return_value={}), patch.object(SourceClient, "query_sql", new_callable=AsyncMock, return_value={"rows":[],"truncated":False}) as query:
            result = await LangChainSupportWorkflow(model,lambda text:text).run(TurnInput("调查支付",[],{}),self.runtime())
        self.assertEqual(query.await_count,8)
        self.assertEqual(result.status,"completed")
        self.assertIn("尚未确认",result.text)

    async def test_recorded_dsml_text_is_never_committed_as_an_answer(self):
        protocol="还需检查配置。<｜DSML｜tool_calls><｜DSML｜invoke name=\"get_endpoint_config\">"
        model=ScriptedChat(replies=[AIMessage(content=protocol),AIMessage(content="尚未核实配置，无法认定通知失败原因。")])
        with patch.object(SourceClient,"field_values",return_value={}):
            result=await LangChainSupportWorkflow(model,lambda text:text).run(TurnInput("排查通知",[],{}),self.runtime())
        self.assertNotIn("DSML",result.text)
        self.assertEqual(model.calls,2)

    async def test_protocol_at_model_budget_returns_explicit_partial_result(self):
        model=ScriptedChat(replies=[AIMessage(content="<｜DSML｜tool_calls>") for _ in range(6)])
        with patch.object(SourceClient,"field_values",return_value={}):
            result=await LangChainSupportWorkflow(model,lambda text:text).run(TurnInput("排查通知",[],{}),self.runtime())
        self.assertEqual(model.calls,6)
        self.assertEqual(result.saved_state["outcome"],"partial")
        self.assertNotIn("DSML",result.text)
