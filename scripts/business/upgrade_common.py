"""升级验收共用的有界本地 HTTP 客户端与报告工具。"""

from __future__ import annotations

import http.cookiejar
import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "docs/verification/distributed/reports"


def load_support_env() -> None:
    for line in (ROOT / ".env.support").read_text(encoding="utf-8-sig").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


class LocalClient:
    def __init__(self, base: str = "http://127.0.0.1:28080") -> None:
        self.base = base
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )

    def call(self, path: str, body=None, expected: int = 200):
        request = urllib.request.Request(
            self.base + "/api" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        if status != expected:
            raise AssertionError(
                f"{path}: expected {expected}, got {status}: {raw[:300]!r}"
            )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode()


def environment(name: str, mode: str = "mock") -> dict:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ).strip()
    )
    return {
        "schema_version": 1,
        "suite": name,
        "run_id": uuid4().hex[:12],
        "started_at": datetime.now(UTC).isoformat(),
        "git": head + ("-dirty" if dirty else ""),
        "mode": mode,
        "host": platform.platform(),
        "ports": {
            "gateway": 28080,
            "agent": 28010,
            "postgres": 25433,
            "traffic_redis": 26379,
            "cache_redis": 26380,
        },
        "limits": {
            "global_slots": 2,
            "java_cpus_each": 0.5,
            "java_memory_mb_each": 384,
            "model_calls": 0,
        },
        "results": [],
    }


def check(report: dict, name: str, action) -> None:
    started = time.perf_counter()
    try:
        detail = action()
        report["results"].append(
            {
                "name": name,
                "passed": True,
                "seconds": round(time.perf_counter() - started, 4),
                "detail": detail,
            }
        )
        print("PASS", name, flush=True)
    except Exception as error:  # noqa: BLE001 - verification must preserve each failed invariant.
        report["results"].append(
            {
                "name": name,
                "passed": False,
                "seconds": round(time.perf_counter() - started, 4),
                "error": f"{type(error).__name__}: {error}",
            }
        )
        print("FAIL", name, error, flush=True)


def save(report: dict, filename: str) -> Path:
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["passed"] = sum(bool(row["passed"]) for row in report["results"])
    report["tests"] = len(report["results"])
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / filename
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def require_mock(agent_url: str = "http://127.0.0.1:28010") -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(agent_url + "/health", timeout=3) as response:
        health = json.load(response)
    if health.get("mode") != "mock":
        raise SystemExit("Refusing verification because the running Agent is not mock")
    return health


def wait_gateway(timeout: float = 90.0) -> dict:
    """等待 Spring 完成迁移和启动；容器 running 不代表 HTTP 已就绪。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with opener.open(
                "http://127.0.0.1:28080/api/health", timeout=3
            ) as response:
                payload = json.load(response)
            if payload.get("status") == "ok":
                return payload
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError(f"gateway did not become ready in {timeout}s: {last_error}")
