"""真实 SDK + MockTransport 验证协议；不访问公网、不使用用户密钥。"""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anthropic
import httpx2 as httpx
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, ToolMessage
from fastapi.testclient import TestClient

from flowpay.business.app import AuthorizedSources, EvidenceSink, ExecuteBody, app_factory
from flowpay.business.bootstrap import create_langchain_workflow
from flowpay.core.config import ModelSettings, Settings
from flowpay.core.data_sources.reader import SourceClient
from flowpay.core.execution.contracts import TurnInput
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.workflows.langchain_support import LangChainSupportWorkflow, answer_text


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase):
    def model(self, provider="anthropic-compatible"):
        settings = Settings(Path("."), Path("."), model=ModelSettings(
            provider=provider, name="deepseek-flash",
            base_url="https://api.deepseek.com/anthropic", api_key_env="TEST_MODEL_KEY",
            request_options={"max_tokens": 256, "thinking": {"type": "disabled"}}))
        with patch.dict(os.environ, {"TEST_MODEL_KEY": "fake-unit-test-key"}):
            return create_langchain_workflow(settings).model

    async def test_messages_wire_auth_tools_and_text_blocks(self):
        requests = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                content = [{"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"query": "test"}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "查询完成"}]
                stop = "end_turn"
            return httpx.Response(200, json={"id": "msg_test", "type": "message", "role": "assistant",
                "model": "deepseek-flash", "content": content, "stop_reason": stop,
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})
        model = self.model()
        self.assertIsInstance(model, ChatAnthropic)
        self.assertEqual(model.max_retries, 0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            sdk = anthropic.AsyncAnthropic(api_key="fake-unit-test-key",
                base_url="https://api.deepseek.com/anthropic", max_retries=0, http_client=transport)
            model.__dict__["_async_client"] = sdk
            bound = model.bind_tools([{"name": "lookup", "description": "Lookup",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}}])
            first = await bound.ainvoke([HumanMessage("查询")])
            self.assertEqual(first.tool_calls[0]["name"], "lookup")
            second = await bound.ainvoke([HumanMessage("查询"), first, ToolMessage("found", tool_call_id="call_1")])
            self.assertEqual(answer_text(second.content), "查询完成")
        self.assertEqual(str(requests[0].url), "https://api.deepseek.com/anthropic/v1/messages")
        self.assertEqual(requests[0].headers["x-api-key"], "fake-unit-test-key")
        self.assertNotIn("api-key", requests[0].headers)
        payload = json.loads(requests[1].content)
        self.assertEqual(payload["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_openai_provider_still_available(self):
        self.assertIsInstance(self.model("openai-compatible"), ChatOpenAI)

    async def test_real_langchain_loop_closes_anthropic_tools_at_budget(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            number = len(requests)
            content = ([{"type": "tool_use", "id": f"call_{number}", "name": "query_payments",
                "input": {"account_id": "acct_a", "environment": "production", "payment_id": "pay_test_001"}}]
                if number < 6 else [{"type": "text", "text": "未查询到记录，当前信息不足。"}])
            return httpx.Response(200, json={"id": f"msg_{number}", "type": "message", "role": "assistant",
                "model": "deepseek-flash", "content": content,
                "stop_reason": "tool_use" if number < 6 else "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5}})
        model = self.model()
        body = ExecuteBody(tenant_id="merchant-a", session_id="session", run_id="run",
            account_ids=["acct_a"], environment="production", content="test")
        runtime = AgentRuntime(EvidenceSink(), AuthorizedSources(SourceClient(None), body))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            model.__dict__["_async_client"] = anthropic.AsyncAnthropic(api_key="fake-unit-test-key",
                base_url="https://api.deepseek.com/anthropic", max_retries=0, http_client=transport)
            with patch.object(SourceClient, "field_values", return_value={}), \
                 patch.object(SourceClient, "query_sql", new_callable=AsyncMock, return_value={"rows": [], "truncated": False}):
                result = await LangChainSupportWorkflow(model, lambda text: text).run(TurnInput("查询 pay_test_001", [], {}), runtime)
        self.assertEqual(result.text, "未查询到记录，当前信息不足。")
        self.assertEqual(len(requests), 6)
        self.assertFalse(requests[-1].get("tools"))
        # ChatAnthropic 在没有工具定义时省略 tool_choice；此时没有可执行工具。
        self.assertEqual(requests[-1].get("tool_choice", {"type": "none"}), {"type": "none"})

    def test_only_text_blocks_are_displayed(self):
        self.assertEqual(answer_text([{"type": "thinking", "thinking": "private"},
            {"type": "text", "text": "可见回答"},
            {"type": "tool_use", "name": "lookup", "input": {}}]), "可见回答")

    def test_anthropic_provider_error_maps_without_leaking_body(self):
        from unittest.mock import AsyncMock
        key = "unit-test-internal-key-at-least-24-characters"
        for upstream, expected in [(429, 429), (400, 502)]:
            response = httpx.Response(upstream, request=httpx.Request("POST", "https://api.deepseek.com/anthropic/v1/messages"))
            error = anthropic.APIStatusError("sensitive provider body", response=response, body={})
            with patch.dict(os.environ, {"SUPPORT_AGENT_KEY": key, "SUPPORT_AGENT_MODE": "mock"}), \
                 patch("flowpay.business.execution.ExecutionRegistry.run", new_callable=AsyncMock, side_effect=error):
                with TestClient(app_factory()) as client:
                    result = client.post("/internal/execute", headers={"X-Agent-Key": key}, json={
                        "tenant_id": "merchant-a", "session_id": "session", "run_id": "run",
                        "account_ids": ["acct_a"], "environment": "production", "content": "test"})
                    self.assertEqual(result.status_code, expected)
                    self.assertNotIn("sensitive", result.text)
