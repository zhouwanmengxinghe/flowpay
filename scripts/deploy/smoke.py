"""Exercise a deployed mock stack without ever calling a chat model."""
from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.request
import uuid


def main() -> None:
    base = os.environ.get("DEPLOY_TEST_URL", "http://api:8080")
    agent = os.environ.get("DEPLOY_TEST_AGENT_URL", "http://agent:8010")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPCookieProcessor(jar))

    def request(path: str, body: dict | None = None) -> object:
        target = path if path.startswith("http") else base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(target, data=data, headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=10) as response:
            return json.load(response)

    assert request(agent + "/health")["mode"] == "mock", "Refusing to submit to a real Agent"
    request("/api/auth/login", {"username": "merchant_a", "password": os.environ["SUPPORT_DEMO_PASSWORD"]})
    if existing := os.environ.get("DEPLOY_TEST_SESSION_ID"):
        detail = request(f"/api/sessions/{existing}")
        assert detail["runs"][-1]["status"] == "completed"
        print(json.dumps({"restart_persistence": "passed", "session_id": existing}))
        return
    accounts = request("/api/accounts")
    session = request("/api/sessions", {"accountId": accounts[0]["account_id"], "environment": "sandbox"})
    sid = session["id"]
    request(f"/api/sessions/{sid}/messages", {"requestId": uuid.uuid4().hex, "content": "Check webhook delivery"})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        detail = request(f"/api/sessions/{sid}")
        runs = detail["runs"]
        if runs and runs[-1]["status"] in {"succeeded", "completed"}:
            print(json.dumps({"login": "passed", "mock_investigation": "passed", "session_id": sid}))
            return
        if runs and runs[-1]["status"] in {"failed", "cancelled"}:
            raise RuntimeError("Mock investigation failed: " + str(runs[-1].get("error")))
        time.sleep(0.5)
    raise TimeoutError("Mock investigation did not finish before its deadline")


if __name__ == "__main__":
    main()
