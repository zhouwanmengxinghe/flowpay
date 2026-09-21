# 通过 HTTP 和 SSE 验证会话、追问、断线回放、取消与继续。
"""Exercise a running backend, including SSE replay after a simulated disconnect."""

from __future__ import annotations

import argparse
import json
import time

import httpx
from flowpay.core.chat.types import SessionView
from flowpay.core.event_log.types import Event
from flowpay.core.execution.types import TurnRecord
from pydantic import TypeAdapter


class Arguments(argparse.Namespace):
    """本命令解析后的参数类型，供调用入口读取。"""

    url: str = "http://127.0.0.1:8000"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args(namespace=Arguments())
    with httpx.Client(base_url=args.url, timeout=30) as client:
        client.get("/api/health").raise_for_status()
        created = client.post("/api/sessions", json={})
        created.raise_for_status()
        sid = TypeAdapter(SessionView).validate_json(created.content)["id"]
        base = f"/api/sessions/{sid}"

        def send(request_id: str, content: str | None = None) -> TurnRecord:
            body = {"request_id": request_id}
            if content is not None:
                body["content"] = content
            response = client.post(
                base + ("/messages" if content is not None else "/continue"), json=body
            )
            response.raise_for_status()
            return TypeAdapter(TurnRecord).validate_json(response.content)

        def wait() -> SessionView:
            for _ in range(600):
                result = TypeAdapter(SessionView).validate_json(
                    client.get(base).content
                )
                if result["status"] != "running":
                    return result
                time.sleep(0.05)
            raise AssertionError("run timeout")

        question = "订单成功了，但一直没有收到支付通知，是 Webhook 出问题了吗？"
        run = send("smoke-initial", question)
        assert send("smoke-initial", question)["id"] == run["id"]
        # 读取一个 SSE 事件后断开连接，验证独立后台任务仍继续执行。
        cursor: int | None = None
        with client.stream("GET", base + "/stream?after=0") as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    cursor = TypeAdapter(Event).validate_json(line[6:])["seq"]
                    break
        assert cursor is not None, "SSE ended before its first event"
        waiting = wait()
        assert waiting["status"] == "waiting_user"
        with client.stream(
            "GET", base + "/stream", headers={"Last-Event-ID": str(cursor)}
        ) as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    assert (
                        TypeAdapter(Event).validate_json(line[6:])["seq"] == cursor + 1
                    )
                    break
        send("smoke-answer", "补充信息")
        final = wait()
        assert final["status"] == "completed"
        assert len(final["messages"]) == 4
        events = TypeAdapter(list[Event]).validate_json(
            client.get(base + "/events").content
        )
        exported = [
            TypeAdapter(Event).validate_json(line)
            for line in client.get(base + "/export").text.splitlines()
        ]
        assert events == exported
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        started = [e for e in events if e["type"] == "tool.started"]
        assert all(e["parent_call_id"] for e in started)
        assert [e["payload"]["name"] for e in started] == [
            "probe_rules",
            "probe_rules",
            "probe_record",
        ]
        send("smoke-slow", "/slow")
        client.post(base + "/cancel", json={}).raise_for_status()
        assert wait()["status"] == "canceled"
        send("smoke-continue")
        assert wait()["status"] == "completed"
        print(
            json.dumps(
                {
                    "status": "passed",
                    "session_id": sid,
                    "checks": [
                        "SSE disconnect",
                        "cursor replay",
                        "idempotent submit",
                        "clarification",
                        "cross-tool trace",
                        "JSONL export",
                        "cancel and continue",
                    ],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
