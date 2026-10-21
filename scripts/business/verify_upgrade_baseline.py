"""T00 独立基线：真实 PG/Redis、mock Agent，拒绝 real 模式。"""

from __future__ import annotations

import concurrent.futures
import os
import time

from upgrade_common import (
    LocalClient,
    check,
    environment,
    load_support_env,
    require_mock,
    save,
    wait_gateway,
)


def require(value: bool):
    if not value:
        raise AssertionError("invariant did not hold")


def wait(client: LocalClient, sid: str, states: set[str], timeout: float = 35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.call("/sessions/" + sid)
        if detail["status"] in states:
            return detail
        time.sleep(0.15)
    raise AssertionError(f"session did not reach {states}")


def main() -> int:
    load_support_env()
    require_mock()
    wait_gateway()
    report = environment("upgrade-baseline")
    password = os.environ["SUPPORT_DEMO_PASSWORD"]
    a, b = LocalClient(), LocalClient()
    a.call("/auth/login", {"username": "merchant_a", "password": password})
    b.call("/auth/login", {"username": "merchant_b", "password": password})
    sid = a.call(
        "/sessions", {"accountId": "acct_bg_001", "environment": "production"}
    )["id"]
    check(report, "mock_health_hard_check", lambda: require_mock())
    check(
        report, "tenant_detail_hidden", lambda: b.call("/sessions/" + sid, expected=404)
    )

    def idem():
        body = {"requestId": report["run_id"] + "-idem", "content": "[slow] baseline"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(
                pool.map(
                    lambda _: a.call(f"/sessions/{sid}/messages", body, 202), range(8)
                )
            )
        require(len({row["id"] for row in rows}) == 1)
        return {"requests": 8, "run_id": rows[0]["id"]}

    check(report, "parallel_idempotency", idem)
    check(report, "completed_after_queue", lambda: wait(a, sid, {"completed"}))
    a.call(
        f"/sessions/{sid}/messages",
        {"requestId": report["run_id"] + "-fail", "content": "[fail]"},
        202,
    )
    check(report, "explicit_failure_terminal", lambda: wait(a, sid, {"failed"}))
    a.call(
        f"/sessions/{sid}/messages",
        {
            "requestId": report["run_id"] + "-continue",
            "content": "failure continuation",
        },
        202,
    )
    check(
        report,
        "same_session_continues_after_failure",
        lambda: wait(a, sid, {"completed"}),
    )
    a.call(
        f"/sessions/{sid}/messages",
        {"requestId": report["run_id"] + "-cancel", "content": "[cancel-probe]"},
        202,
    )
    wait(a, sid, {"running"})
    a.call(f"/sessions/{sid}/cancel", {})
    time.sleep(0.5)
    check(
        report,
        "cancel_is_durable",
        lambda: require(a.call("/sessions/" + sid)["status"] == "canceled"),
    )
    path = save(report, "baseline.json")
    print(path)
    return 0 if report["passed"] == report["tests"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
