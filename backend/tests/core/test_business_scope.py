"""内部执行授权回归，不访问真实业务文件或模型 API。"""
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from flowpay.business.app import AuthorizedSources, ExecuteBody, app_factory
from flowpay.core.data_sources.reader import SourceClient
from flowpay.core.tools import ToolInputError
from flowpay.core.config import Settings
from flowpay.core.execution.contracts import TurnResult


def request_body() -> ExecuteBody:
    return ExecuteBody(tenant_id="tenant-a", session_id="session-a", run_id="run-a",
                       account_ids=["acct_a"], environment="production", content="test")


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    def client(self) -> AuthorizedSources:
        return AuthorizedSources(SourceClient(None), request_body())

    async def test_cross_account_and_environment_never_reach_database(self):
        for scope in [{"account_id": "acct_b", "environment": "production"},
                      {"account_id": "acct_a", "environment": "sandbox"}, {}]:
            with self.subTest(scope=scope), patch.object(SourceClient, "query_sql", new_callable=AsyncMock) as query:
                with self.assertRaises(ToolInputError):
                    await self.client().query_sql("payments", scope, None)
                query.assert_not_called()

    async def test_delivery_logs_require_a_returned_delivery(self):
        client = self.client()
        with self.assertRaises(ToolInputError):
            await client.query_logs("delivery_attempts", None, filters={"delivery_id": "del_b"})
        result = {"rows": [{"delivery_id": "del_a"}]}
        with patch.object(SourceClient, "query_sql", new_callable=AsyncMock, return_value=result):
            await client.query_sql("webhook_deliveries", {"account_id": "acct_a", "environment": "production"}, None)
        with patch.object(SourceClient, "query_logs", new_callable=AsyncMock, return_value={"rows": []}) as query:
            await client.query_logs("delivery_attempts", None, filters={"delivery_id": "del_a"})
            query.assert_awaited_once()

    async def test_request_logs_cannot_omit_account_or_environment(self):
        with self.assertRaises(ToolInputError):
            await self.client().query_logs("api_requests", None, filters={"request_id": "req_b"})

    async def test_unregistered_log_is_denied(self):
        with self.assertRaises(ToolInputError):
            await self.client().query_logs("audit", None, filters={"account_id": "acct_a"})


class ExecutionHttpTests(unittest.TestCase):
    def test_real_execution_contract_uses_authorized_adapter_and_history(self):
        key = "unit-test-internal-key-at-least-24-characters"
        resources = Mock()
        resources.snapshot = AsyncMock(return_value={})
        resources.source_client = SourceClient(None)
        resources.document_retriever.return_value = None
        owner = self

        class Workflow:
            async def run(self, turn, runtime):
                owner.assertIsInstance(runtime.sources, AuthorizedSources)
                owner.assertIn("account_id=acct_a", turn.user_message)
                owner.assertEqual(turn.chat_history[0]["content"], "earlier question")
                await runtime.tracer.emit("source.response", {"rows": [{"status": "fixture"}]})
                return TurnResult("fixture answer", {"messages": [], "evidence": []})

        with patch.dict("os.environ", {"SUPPORT_AGENT_KEY": key, "SUPPORT_AGENT_MODE": "real", "SUPPORT_AGENT_ENGINE": "s4"}), \
             patch("flowpay.business.app.load_settings", return_value=Settings(Path("."), Path("."))), \
             patch("flowpay.business.app.RuntimeResources.from_settings", return_value=resources), \
             patch("flowpay.business.app.create_workflow", return_value=Workflow()):
            with TestClient(app_factory()) as client:
                body = request_body().model_dump()
                body["history"] = [{"role": "user", "content": "earlier question"}]
                result = client.post("/internal/execute", json=body, headers={"X-Agent-Key": key})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["checkpoint"]["_execution_mode"], "real")
                self.assertEqual(result.json()["evidence"][0]["kind"], "source.response")
        resources.close.assert_called_once()

    def test_internal_auth_and_explicit_simulation(self):
        key = "unit-test-internal-key-at-least-24-characters"
        with patch.dict("os.environ", {"SUPPORT_AGENT_KEY": key, "SUPPORT_AGENT_MODE": "mock"}):
            with TestClient(app_factory()) as client:
                body = request_body().model_dump()
                self.assertEqual(client.post("/internal/execute", json=body, headers={"X-Agent-Key": "wrong"}).status_code, 401)
                response = client.post("/internal/execute", json=body, headers={"X-Agent-Key": key})
                self.assertEqual(response.status_code, 200)
                self.assertIn("模拟验收", response.json()["answer"])
                body["content"] = "[fail]"
                body["run_id"] = "run-failure"
                self.assertEqual(client.post("/internal/execute", json=body, headers={"X-Agent-Key": key}).status_code, 503)

    def test_real_mode_is_not_implicitly_mocked(self):
        with patch.dict("os.environ", {"SUPPORT_AGENT_KEY": "short", "SUPPORT_AGENT_MODE": "mock"}):
            with self.assertRaises(ValueError):
                app_factory()
