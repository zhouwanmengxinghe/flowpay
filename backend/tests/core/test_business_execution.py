"""真实 asyncio 取消与同 run 合并；不调用外部模型。"""

import asyncio
import unittest

from fastapi import HTTPException

from flowpay.business.execution import ExecutionRegistry


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_propagates_into_running_coroutine(self):
        registry = ExecutionRegistry()
        started, stopped = asyncio.Event(), asyncio.Event()

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task = asyncio.create_task(registry.run("run", "hash", work))
        await started.wait()
        await registry.cancel("run")
        with self.assertRaises(HTTPException) as result:
            await task
        self.assertEqual(result.exception.status_code, 409)
        self.assertTrue(stopped.is_set())

    async def test_cancel_before_execute_never_starts_work(self):
        registry = ExecutionRegistry()
        await registry.cancel("run")

        async def work():
            self.fail("Canceled run started")

        with self.assertRaises(HTTPException):
            await registry.run("run", "hash", work)

    async def test_duplicate_call_shares_calculation_and_conflict_rejected(self):
        registry = ExecutionRegistry()
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def work():
            nonlocal count
            count += 1
            started.set()
            await release.wait()
            return {"answer": "one"}

        first = asyncio.create_task(registry.run("run", "hash", work))
        await started.wait()
        second = asyncio.create_task(registry.run("run", "hash", work))
        with self.assertRaises(HTTPException):
            await registry.run("run", "other", work)
        release.set()
        self.assertEqual(await first, await second)
        self.assertEqual(count, 1)

    async def test_new_execution_for_same_run_does_not_reuse_old_result(self):
        registry = ExecutionRegistry()
        calls = 0

        async def work():
            nonlocal calls
            calls += 1
            return {"attempt": calls}

        self.assertEqual((await registry.run("exec-1", "same", work))["attempt"], 1)
        self.assertEqual((await registry.run("exec-2", "same", work))["attempt"], 2)
        await registry.cancel("exec-1")
        self.assertEqual((await registry.run("exec-2", "same", work))["attempt"], 2)
