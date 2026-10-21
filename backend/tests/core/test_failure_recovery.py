"""失败后同会话新消息与原问题重试；只用本地工作流，不访问模型。"""
import tempfile
import unittest
from pathlib import Path

from flowpay.core.config import Settings
from flowpay.core.event_log import EventStore
from flowpay.core.execution.contracts import TurnInput, TurnResult
from flowpay.core.execution.manager import SessionManager
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard


class RecoveryWorkflow:
    id = "recovery-test"
    version = "1"
    card = StageCard(label="Recovery", description="offline fixture")

    def __init__(self):
        self.inputs: list[TurnInput] = []
        self.fail_once = True

    async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
        self.inputs.append(turn)
        if turn.user_message == "fail" and self.fail_once:
            self.fail_once = False
            raise ValueError("fixture failure")
        return TurnResult("ok", {"saved": turn.user_message})


class FailureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_message_and_explicit_retry_after_failure(self):
        for resume in [False, True]:
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workflow = RecoveryWorkflow()
                runtime = SessionManager(EventStore(root / "events.sqlite"), workflow,
                                         Settings(root, root / "dataset"))
                try:
                    sid = (await runtime.create_session())["id"]
                    await runtime.submit_message(sid, "initial", "good")
                    await runtime.tasks[sid]
                    await runtime.submit_message(sid, "failure", "fail")
                    await runtime.tasks[sid]
                    self.assertEqual((await runtime.get_session(sid))["status"], "failed")
                    if resume:
                        await runtime.resume_run(sid, "retry")
                    else:
                        await runtime.submit_message(sid, "new", "new question")
                    await runtime.tasks[sid]
                    final = await runtime.get_session(sid)
                    self.assertEqual(final["status"], "completed")
                    self.assertEqual(workflow.inputs[-1].saved_state, {"saved": "good"})
                    self.assertEqual(workflow.inputs[-1].resumed, resume)
                    self.assertEqual(workflow.inputs[-1].user_message,
                                     "fail" if resume else "new question")
                    self.assertEqual(len(final["messages"]), 4 if resume else 5)
                    self.assertTrue(any(e["type"] == "run.failed" for e in runtime.store.read(sid)))
                finally:
                    await runtime.shutdown()
