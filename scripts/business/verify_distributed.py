"""D1-D10/O1/Q1：隔离双实例故障验收；只允许 mock Agent。"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import time
import urllib.error

from upgrade_common import (
    ROOT,
    LocalClient,
    check,
    environment,
    load_support_env,
    require_mock,
    save,
    wait_gateway,
)

COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    ".env.support",
    "-p",
    "flowpay-support-upgrade",
    "-f",
    "compose.business.yaml",
    "-f",
    "compose.business.distributed.yaml",
    "-f",
    "compose.business.test.yaml",
    "--profile",
    "mq",
]


def command(*args):
    return subprocess.check_output(
        [*COMPOSE, *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def sql(statement: str):
    return command(
        "exec",
        "-T",
        "support-db",
        "psql",
        "-U",
        "support",
        "-d",
        "support",
        "-v",
        "ON_ERROR_STOP=1",
        "-Atc",
        statement,
    )


def require(value: bool):
    if not value:
        raise AssertionError("invariant did not hold")


def wait(client: LocalClient, sid: str, states: set[str], timeout: float = 65):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            detail = client.call("/sessions/" + sid)
        except (AssertionError, ConnectionError, urllib.error.URLError):
            time.sleep(0.2)
            continue
        if detail["status"] in states:
            return detail
        time.sleep(0.2)
    raise AssertionError(f"session {sid} did not reach {states}")


def main() -> int:
    load_support_env()
    require_mock()
    wait_gateway()
    report = environment("distributed-correctness")
    password = os.environ["SUPPORT_DEMO_PASSWORD"]
    merchant = LocalClient()
    merchant.call("/auth/login", {"username": "merchant_a", "password": password})
    support = LocalClient()
    support.call("/auth/login", {"username": "support_a", "password": password})
    sql(
        "UPDATE tenant_limits SET requests_per_minute=600,daily_runs=10000,active_runs=30"
    )

    def deployment_guards():
        require(int(sql("SELECT count(*) FROM execution_slots")) == 2)
        require(
            int(
                sql(
                    "SELECT count(*) FROM scheduler_instances "
                    "WHERE scheduler_mode='distributed'"
                )
            )
            == 2
        )
        conflict = subprocess.run(
            [
                *COMPOSE,
                "run",
                "--rm",
                "--no-deps",
                "-e",
                "SUPPORT_SCHEDULER_MODE=single",
                "support-api-a",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        require(conflict.returncode != 0)
        return {
            "global_slots": 2,
            "active_distributed_instances": 2,
            "mixed_mode_rejected": True,
        }

    check(report, "T04_T08_schema_restart_and_mode_guard", deployment_guards)

    def d1():
        sid = merchant.call(
            "/sessions", {"accountId": "acct_bg_001", "environment": "production"}
        )["id"]
        body = {
            "requestId": report["run_id"] + "-d1",
            "content": "[slow] distributed idempotency",
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            rows = list(
                pool.map(
                    lambda _: merchant.call(f"/sessions/{sid}/messages", body, 202),
                    range(12),
                )
            )
        require(len({r["id"] for r in rows}) == 1)
        wait(merchant, sid, {"completed"})
        count = int(sql(f"SELECT count(*) FROM runs WHERE session_id='{sid}'"))
        require(count == 1)
        return {"http_requests": 12, "runs": count, "run_id": rows[0]["id"]}

    check(report, "D1_cross_instance_idempotency", d1)

    batch = []

    def d2():
        for i in range(10):
            sid = merchant.call(
                "/sessions", {"accountId": "acct_bg_001", "environment": "production"}
            )["id"]
            merchant.call(
                f"/sessions/{sid}/messages",
                {
                    "requestId": f"{report['run_id']}-d2-{i}",
                    "content": "[slow] slot probe",
                },
                202,
            )
            batch.append((merchant, sid))
        max_slots = 0
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            active = int(
                sql(
                    "SELECT count(*) FROM execution_slots WHERE execution_id IS NOT NULL"
                )
            )
            max_slots = max(max_slots, active)
            if all(
                c.call("/sessions/" + sid)["status"] == "completed" for c, sid in batch
            ):
                break
            time.sleep(0.15)
        require(
            max_slots <= 2
            and all(
                c.call("/sessions/" + sid)["status"] == "completed" for c, sid in batch
            )
        )
        attempts = int(
            sql(
                "SELECT count(*) FROM run_attempts WHERE run_id IN "
                f"(SELECT id FROM runs WHERE request_id LIKE '{report['run_id']}-d2-%')"
            )
        )
        require(attempts == 10)
        return {"runs": 10, "attempts": attempts, "max_observed_slots": max_slots}

    check(report, "D2_global_two_slots_and_single_claim", d2)

    recovery = {}

    def d3_d5_d9():
        client = merchant
        sid = client.call(
            "/sessions", {"accountId": "acct_bg_001", "environment": "production"}
        )["id"]
        client.call(
            f"/sessions/{sid}/messages",
            {
                "requestId": report["run_id"] + "-recovery",
                "content": "[cancel-probe] owner loss",
            },
            202,
        )
        wait(client, sid, {"running"})
        row = sql(
            f"SELECT owner_instance||'|'||current_execution_id FROM runs WHERE session_id='{sid}' ORDER BY created_at DESC LIMIT 1"
        )
        owner, old_execution = row.split("|", 1)
        service = "support-api-a" if owner.startswith("api-a-") else "support-api-b"
        command("kill", "-s", "SIGKILL", service)
        try:
            final = wait(client, sid, {"completed", "failed"}, 55)
            require(final["status"] == "completed")
            attempts = int(
                sql(
                    f"SELECT count(*) FROM run_attempts a JOIN runs r ON r.id=a.run_id WHERE r.session_id='{sid}'"
                )
            )
            recovered = int(
                sql(
                    f"SELECT count(*) FROM session_events WHERE session_id='{sid}' AND type='run.recovered'"
                )
            )
            current = sql(
                f"SELECT current_execution_id FROM runs WHERE session_id='{sid}' ORDER BY created_at DESC LIMIT 1"
            )
            require(attempts == 2 and recovered == 1 and current != old_execution)
            recovery.update(
                session=sid,
                old_execution=old_execution,
                new_execution=current,
                stopped=service,
            )
            return {
                "attempts": attempts,
                "recovery_events": recovered,
                "old_result_fenced": current != old_execution,
            }
        finally:
            command("up", "-d", "--no-build", "--wait", service)
            wait_gateway()

    check(report, "D3_D5_D9_owner_loss_recovery_and_fencing", d3_d5_d9)

    def d6():
        client = merchant
        sid = client.call(
            "/sessions", {"accountId": "acct_bg_001", "environment": "production"}
        )["id"]
        client.call(
            f"/sessions/{sid}/messages",
            {
                "requestId": report["run_id"] + "-cancel",
                "content": "[cancel-probe] cross instance cancel",
            },
            202,
        )
        client.call(f"/sessions/{sid}/cancel", {})
        time.sleep(1)
        require(client.call("/sessions/" + sid)["status"] == "canceled")
        pending = int(
            sql(
                f"SELECT count(*) FROM run_attempts a JOIN runs r ON r.id=a.run_id WHERE r.session_id='{sid}' AND a.cancel_ack_at IS NULL"
            )
        )
        deadline = time.monotonic() + 12
        while pending and time.monotonic() < deadline:
            time.sleep(0.3)
            pending = int(
                sql(
                    f"SELECT count(*) FROM run_attempts a JOIN runs r ON r.id=a.run_id WHERE r.session_id='{sid}' AND a.cancel_ack_at IS NULL"
                )
            )
        require(pending == 0)
        return {"terminal": "canceled", "pending_cancel": pending}

    check(report, "D6_persistent_cross_instance_cancel", d6)

    def d7():
        username = "temp_" + report["run_id"]
        member_password = "Distributed-Test-2026!"
        member = merchant.call(
            "/members",
            {
                "username": username,
                "password": member_password,
                "administrator": False,
            },
        )
        member_client = LocalClient()
        member_client.call(
            "/auth/login", {"username": username, "password": member_password}
        )
        sid = member_client.call(
            "/sessions", {"accountId": "acct_bg_001", "environment": "sandbox"}
        )["id"]
        member_client.call(
            f"/sessions/{sid}/messages",
            {
                "requestId": report["run_id"] + "-disabled-member",
                "content": "[cancel-probe] disable member",
            },
            202,
        )
        wait(member_client, sid, {"running"})
        merchant.call(
            "/members/" + member["id"], {"active": False, "administrator": False}
        )
        merchant_detail = merchant.call("/sessions/" + sid)
        require(merchant_detail["status"] == "canceled")
        member_client.call("/auth/me", expected=401)
        audit = merchant.call("/admin/audit")
        require(
            any(
                row["target_id"] == member["id"] and row["action"] == "member.updated"
                for row in audit
            )
        )
        return {
            "disabled_member": member["id"],
            "running_task_terminal": "canceled",
            "token_revoked": True,
            "audit_present": True,
        }

    check(report, "D7_member_disable_audited", d7)

    def d8():
        command("stop", "support-cache")
        require_mock()
        command("up", "-d", "--no-build", "--wait", "support-cache")
        client = LocalClient()
        client.call("/auth/login", {"username": "merchant_b", "password": password})
        sid = client.call(
            "/sessions", {"accountId": "acct_bm_030", "environment": "production"}
        )["id"]
        command("stop", "support-redis")
        try:
            client.call(
                f"/sessions/{sid}/messages",
                {"requestId": report["run_id"] + "-redis-down", "content": "x"},
                503,
            )
        finally:
            command("up", "-d", "--no-build", "--wait", "support-redis")
        return {"cache_failure": "agent healthy", "traffic_failure_http": 503}

    check(report, "D8_cache_degrades_traffic_redis_fails_closed", d8)

    def d10():
        sql(
            "UPDATE tenant_limits SET requests_per_minute=3,active_runs=30 WHERE tenant_id='merchant-b'"
        )
        accepted = rejected = 0
        for i in range(6):
            client = LocalClient()
            client.call("/auth/login", {"username": "merchant_b", "password": password})
            sid = client.call(
                "/sessions", {"accountId": "acct_bm_030", "environment": "production"}
            )["id"]
            try:
                client.call(
                    f"/sessions/{sid}/messages",
                    {"requestId": f"{report['run_id']}-rate-{i}", "content": "rate"},
                    202,
                )
                accepted += 1
            except AssertionError as error:
                if "got 429" not in str(error):
                    raise
                rejected += 1
        require(accepted == 3 and rejected == 3)
        return {"accepted": accepted, "rejected": rejected, "shared_limit": 3}

    check(report, "D10_shared_rate_window", d10)

    def q1_broker_outage():
        sql(
            "UPDATE tenant_limits SET requests_per_minute=600,active_runs=30 WHERE tenant_id='merchant-a'"
        )
        command("stop", "support-rabbit")
        client = merchant
        try:
            sid = client.call(
                "/sessions", {"accountId": "acct_bg_001", "environment": "sandbox"}
            )["id"]
            client.call(
                f"/sessions/{sid}/messages",
                {
                    "requestId": report["run_id"] + "-broker-down",
                    "content": "broker fallback",
                },
                202,
            )
            final = wait(client, sid, {"completed"}, 20)
            run_id = final["runs"][-1]["id"]
            pending = int(
                sql(
                    f"SELECT count(*) FROM outbox_events WHERE run_id='{run_id}' AND published_at IS NULL"
                )
            )
            require(pending == 1)
            attempts_before = int(
                sql(f"SELECT count(*) FROM run_attempts WHERE run_id='{run_id}'")
            )
        finally:
            command("up", "-d", "--no-build", "--wait", "support-rabbit")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and int(
            sql(
                f"SELECT count(*) FROM outbox_events WHERE run_id='{run_id}' AND published_at IS NULL"
            )
        ):
            time.sleep(0.25)
        require(
            int(
                sql(
                    f"SELECT count(*) FROM outbox_events WHERE run_id='{run_id}' AND published_at IS NULL"
                )
            )
            == 0
        )
        # 重新发布同一个轻量通知；终态 run 不可再次领取。
        sql(
            f"UPDATE outbox_events SET published_at=NULL,available_at=now() WHERE run_id='{run_id}'"
        )
        time.sleep(3)
        attempts_after = int(
            sql(f"SELECT count(*) FROM run_attempts WHERE run_id='{run_id}'")
        )
        require(attempts_after == attempts_before)
        return {
            "broker_down_terminal": "completed",
            "db_fallback": True,
            "duplicate_notification_attempts": attempts_after,
        }

    check(report, "Q1_broker_outage_and_duplicate_notification", q1_broker_outage)

    def o1_q1():
        operations = support.call("/operations")
        metrics = support.call("/operations/prometheus")
        serialized = json.dumps(operations, ensure_ascii=False) + str(metrics)
        require(
            "acct_bg_001" not in serialized
            and "distributed idempotency" not in serialized
        )
        deadline = time.monotonic() + 8
        pending = 1
        while pending and time.monotonic() < deadline:
            pending = int(
                sql("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
            )
            if pending:
                time.sleep(0.2)
        require(pending == 0)
        return {
            "scheduler": operations["scheduler"],
            "outbox_pending": pending,
            "bounded_labels": True,
            "mq_role": "notification_only",
        }

    check(report, "O1_Q1_metrics_and_outbox_delivery", o1_q1)
    report["scope_notes"] = [
        "D3/D5/D9 use one explicit Java owner kill with a 15s test lease.",
        "The old external mock execution may physically finish; the database CAS fences its result.",
        "RabbitMQ carries wake-up notifications only; PostgreSQL remains the task state machine.",
    ]
    path = save(report, "distributed-correctness.json")
    print(path)
    return 0 if report["passed"] == report["tests"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
