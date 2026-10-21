"""T04：在临时 PostgreSQL 数据库验证 V1/V2 存量升级到 V5。"""

from __future__ import annotations

import subprocess

from upgrade_common import ROOT, environment, save

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


def main() -> int:
    report = environment("migration-compatibility")
    run_id = report["run_id"]
    database = "support_migration_" + run_id
    container = subprocess.check_output(
        [*COMPOSE, "ps", "-q", "support-db"], cwd=ROOT, text=True
    ).strip()
    target = "/tmp/migration-" + run_id

    def docker(*args: str) -> str:
        return subprocess.check_output(["docker", *args], cwd=ROOT, text=True).strip()

    try:
        subprocess.run(
            [
                "docker",
                "cp",
                str(ROOT / "business/src/main/resources/db/migration") + "/.",
                f"{container}:{target}",
            ],
            cwd=ROOT,
            check=True,
        )
        docker("exec", container, "createdb", "-U", "support", database)
        for version in (1, 2):
            name = next(
                (ROOT / "business/src/main/resources/db/migration").glob(
                    f"V{version}__*.sql"
                )
            ).name
            docker(
                "exec",
                container,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "support",
                "-d",
                database,
                "-f",
                f"{target}/{name}",
            )
        seed = (
            "INSERT INTO tenants VALUES ('old-tenant','Old');"
            "INSERT INTO app_users(id,username,password_hash,tenant_id,role) VALUES ('old-user','old-user','x','old-tenant','MERCHANT');"
            "INSERT INTO tenant_accounts VALUES ('old-tenant','old-account');"
            "INSERT INTO sessions(id,tenant_id,owner_id,account_id,environment) VALUES ('old-session','old-tenant','old-user','old-account','sandbox');"
            "INSERT INTO runs(id,session_id,request_id,input,status) VALUES ('old-run','old-session','old-request','old input','completed');"
        )
        docker(
            "exec",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "support",
            "-d",
            database,
            "-c",
            seed,
        )
        for version in (3, 4, 5):
            name = next(
                (ROOT / "business/src/main/resources/db/migration").glob(
                    f"V{version}__*.sql"
                )
            ).name
            docker(
                "exec",
                container,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "support",
                "-d",
                database,
                "-f",
                f"{target}/{name}",
            )
        result = docker(
            "exec",
            container,
            "psql",
            "-U",
            "support",
            "-d",
            database,
            "-Atc",
            "SELECT id||'|'||status FROM runs WHERE id='old-run';"
            "SELECT count(*) FROM execution_slots;"
            "SELECT count(*) FROM information_schema.columns WHERE table_name='run_attempts' AND column_name IN ('cancel_attempt','cancel_available_at','cancel_deadline_at');",
        ).splitlines()
        main_slots = docker(
            "exec",
            container,
            "psql",
            "-U",
            "support",
            "-d",
            "support",
            "-Atc",
            "SELECT count(*) FROM execution_slots;",
        )
        passed = result == ["old-run|completed", "2", "3"] and main_slots == "2"
        report["results"] = [
            {
                "name": "V1_V2_to_V5_preserves_history_and_slots",
                "passed": passed,
                "detail": {
                    "old_run": result[0],
                    "temporary_slots": int(result[1]),
                    "cancel_columns": int(result[2]),
                    "restart_slots": int(main_slots),
                },
            }
        ]
    finally:
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "dropdb",
                "-U",
                "support",
                "--if-exists",
                database,
            ],
            check=False,
        )
        subprocess.run(["docker", "exec", container, "rm", "-rf", target], check=False)
    path = save(report, "migration-compatibility.json")
    print(path)
    return 0 if report["passed"] == report["tests"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
