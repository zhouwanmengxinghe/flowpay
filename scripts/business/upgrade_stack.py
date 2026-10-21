"""启动或清理隔离升级验收栈；只操作 flowpay-support-upgrade 项目。"""

from __future__ import annotations

import argparse
import os
import subprocess

from upgrade_common import ROOT, load_support_env, require_mock, wait_gateway

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    load_support_env()
    env = dict(os.environ)
    if args.action == "start":
        if env.get("SUPPORT_AGENT_MODE", "mock") != "mock":
            raise SystemExit("Upgrade stack refuses real mode")
        subprocess.run(
            [*COMPOSE, "up", "-d", "--build", "--wait"], cwd=ROOT, env=env, check=True
        )
        require_mock()
        wait_gateway()
        print("Upgrade stack ready: http://127.0.0.1:28080")
    elif args.action == "stop":
        subprocess.run(
            [*COMPOSE, "down", "-v", "--remove-orphans"], cwd=ROOT, env=env, check=True
        )
        print(
            "Only flowpay-support-upgrade containers, network, and volumes were removed."
        )
    else:
        subprocess.run([*COMPOSE, "ps"], cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    main()
