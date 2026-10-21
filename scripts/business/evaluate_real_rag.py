"""Run up to five bounded, real DeepSeek-backed FlowPay RAG questions once each."""
from __future__ import annotations

import json
import argparse
import re
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from verify import Client, ROOT, require


CASES = [
    {
        "name": "production-boundaries",
        "account": "acct_bg_001",
        "environment": "production",
        "session": "new",
        "question": "请检索 KB-API-005，用中文列出 Production 启用与暂停的三条常见边界，并逐条引用实际返回的 doc:片段编号。",
        "expected": ["doc:KB-API-005#c"],
    },
    {
        "name": "payment-webhook-investigation",
        "account": "acct_bg_001",
        "environment": "production",
        "session": "investigation",
        "question": "生产环境，账户 acct_bg_001，支付 pay_bg_001_0003 已成功，但没有收到 Webhook 通知。请查询该支付、相关事件和投递记录，并检索有关文档，说明已核实的原因、证据和下一步。",
        "expected": ["doc:"],
    },
    {
        "name": "investigation-followup",
        "account": "acct_bg_001",
        "environment": "production",
        "session": "investigation",
        "question": "根据刚才查询到的记录，下一步该如何处理？请沿用已有证据并引用来源，不要重新索要已知编号。",
        "expected": [],
    },
    {
        "name": "rate-limit-guidance",
        "account": "acct_bg_001",
        "environment": "production",
        "session": "new",
        "question": "生产环境 API 返回 HTTP 429。请检索 FlowPay 文档，说明如何检查限流信息、正确退避重试，以及哪些做法会加重问题；引用实际返回的文档片段。",
        "expected": ["doc:KB-API-034#c"],
    },
    {
        "name": "missing-payment-identifier",
        "account": "acct_bg_001",
        "environment": "production",
        "session": "new",
        "question": "生产环境里有一笔支付失败了，请帮我查原因并告诉我下一步怎么处理。",
        "expected": [],
        # The fixture has many matching failures, so listing a scoped summary
        # and then asking for a payment/request ID is a valid completed turn.
        "expected_status": "completed",
        "expected_answer_markers": ["payment_id", "request_id"],
    },
]


def read_env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def run_metrics(run_id: str) -> dict[str, int]:
    sql = (
        "SELECT COALESCE(metrics->>'model_calls','0')||'|'||"
        "COALESCE(metrics->>'tool_calls','0')||'|'||"
        "COALESCE(metrics->>'input_tokens','0')||'|'||"
        "COALESCE(metrics->>'output_tokens','0') FROM runs WHERE id='"
        + run_id.replace("'", "")
        + "'"
    )
    command = [
        "docker", "compose", "--env-file", ".env.support", "-f", "compose.business.yaml",
        "exec", "-T", "support-db", "psql", "-U", "support", "-d", "support", "-Atc", sql,
    ]
    raw = subprocess.check_output(command, cwd=ROOT, text=True, encoding="utf-8").strip()
    if not raw:
        raise RuntimeError("Run metrics not found in business database")
    model_calls, tool_calls, input_tokens, output_tokens = map(int, raw.split("|"))
    return {
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-synthetic-data-egress",
        action="store_true",
        help="Confirm that synthetic FlowPay account/payment records and retrieved KB passages may be sent to DeepSeek.",
    )
    args = parser.parse_args()
    if not args.confirm_synthetic_data_egress:
        raise SystemExit(
            "No model request sent. Obtain explicit approval to send synthetic FlowPay records and retrieved KB passages to DeepSeek, then pass --confirm-synthetic-data-egress."
        )
    health_url = "http://127.0.0.1:8010/health"
    opener = __import__("urllib.request", fromlist=["build_opener"]).build_opener(
        __import__("urllib.request", fromlist=["ProxyHandler"]).ProxyHandler({})
    )
    with opener.open(health_url, timeout=5) as response:
        health = json.load(response)
    require(health.get("mode") == "real" and health.get("engine") == "langchain")
    env = read_env(ROOT / ".env.support")
    client = Client("http://127.0.0.1:8080")
    client.call("/auth/login", {"username": "merchant_a", "password": env["SUPPORT_DEMO_PASSWORD"]})

    run_id = uuid4().hex
    report = {
        "schema_version": 1,
        "suite": "paid-deepseek-real-rag-e2e",
        "evaluation_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "provider": "DeepSeek Anthropic-compatible",
        "model": "deepseek-flash",
        "agent_engine": "LangChain",
        "questions_submitted_limit": 5,
        "per_question_limits": {"model_calls": 6, "tool_calls": 8},
        "automatic_retries": 0,
        "retrieval_cache_enabled": False,
        "cases": [],
        "limitations": [
            "Five synthetic-data questions only; results do not establish general RAG quality.",
            "No separate LLM-as-judge call was made, to keep the paid-model budget bounded.",
            "Token counts are usage measurements; dollar cost is not estimated here.",
        ],
    }
    sessions: dict[str, str] = {}
    for index, case in enumerate(CASES, start=1):
        session_key = case["session"]
        if session_key == "new":
            sid = client.call("/sessions", {
                "accountId": case["account"], "environment": case["environment"]
            })["id"]
        else:
            sid = sessions.get(session_key)
            if sid is None:
                sid = client.call("/sessions", {
                    "accountId": case["account"], "environment": case["environment"]
                })["id"]
                sessions[session_key] = sid

        started = time.monotonic()
        submitted = client.call(
            f"/sessions/{sid}/messages",
            {"requestId": f"rag-eval-{run_id[:10]}-{index}", "content": case["question"]},
            expected=202,
        )
        deadline = started + 185
        detail = None
        while time.monotonic() < deadline:
            detail = client.call(f"/sessions/{sid}")
            if detail["status"] not in {"queued", "running"}:
                break
            time.sleep(0.5)
        if detail is None or detail["status"] in {"queued", "running"}:
            raise TimeoutError(f"Case {case['name']} did not finish; stopping without resubmission")
        row = next((item for item in detail.get("runs", []) if item.get("id") == submitted["id"]), None)
        if row is None:
            raise RuntimeError(f"Run row missing for case {case['name']}")
        if row.get("status") not in {"completed", "waiting_user"}:
            report["cases"].append({
                "name": case["name"], "status": row.get("status"),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": row.get("error"), "question_submitted": True,
            })
            print(f"STOP {case['name']}: run status {row.get('status')}; no automatic retry", flush=True)
            break

        metrics = run_metrics(submitted["id"])
        budget_respected = metrics["model_calls"] <= 6 and metrics["tool_calls"] <= 8
        answer = row.get("answer") or ""
        citations = sorted(set(re.findall(r"doc:[A-Za-z0-9._-]+#c\d+", answer)))
        events = client.call(f"/sessions/{sid}/events")
        finished = next((event for event in reversed(events)
                         if event.get("run_id") == submitted["id"] and event.get("type") == "run.finished"), {})
        evidence = finished.get("payload", {}).get("evidence", [])
        expected_status = case.get("expected_status", "completed")
        expected_markers = case["expected"]
        markers_present = all(any(marker in citation for citation in citations) for marker in expected_markers)
        answer_markers_present = all(
            marker.casefold() in answer.casefold()
            for marker in case.get("expected_answer_markers", [])
        )
        item = {
            "name": case["name"],
            "session_id": sid,
            "run_id": submitted["id"],
            "status": row.get("status"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "metrics": metrics,
            "budget_respected": budget_respected,
            "expected_status": expected_status,
            "status_matches_expectation": row.get("status") == expected_status,
            "expected_answer_markers": case.get("expected_answer_markers", []),
            "answer_markers_present": answer_markers_present,
            "expected_citation_markers": expected_markers,
            "citations_in_answer": citations,
            "citation_markers_present": markers_present,
            "evidence_items": len(evidence),
            "question": case["question"],
            "answer": answer,
        }
        report["cases"].append(item)
        print(
            f"PASS {index}/5 {case['name']} status={row.get('status')} "
            f"calls={metrics['model_calls']} tools={metrics['tool_calls']} "
            f"seconds={item['elapsed_seconds']}",
            flush=True,
        )
        if item["status"] not in {"completed", "waiting_user"} or not budget_respected:
            print("Stopping after unsuccessful run or budget violation; no resubmission.", flush=True)
            break

    report["finished_at"] = datetime.now(UTC).isoformat()
    report["summary"] = {
        "submitted_questions": len(report["cases"]),
        "succeeded_runs": sum(item.get("status") in {"completed", "waiting_user"} for item in report["cases"]),
        "citation_expectations_met": sum(
            bool(item.get("expected_citation_markers")) and item.get("citation_markers_present", False)
            for item in report["cases"]
        ),
        "answer_expectations_met": sum(
            bool(item.get("expected_answer_markers")) and item.get("answer_markers_present", False)
            for item in report["cases"]
        ),
        "total_model_calls": sum(item.get("metrics", {}).get("model_calls", 0) for item in report["cases"]),
        "total_tool_calls": sum(item.get("metrics", {}).get("tool_calls", 0) for item in report["cases"]),
        "total_input_tokens": sum(item.get("metrics", {}).get("input_tokens", 0) for item in report["cases"]),
        "total_output_tokens": sum(item.get("metrics", {}).get("output_tokens", 0) for item in report["cases"]),
        "mean_latency_seconds": round(statistics.mean(item["elapsed_seconds"] for item in report["cases"]), 3)
        if report["cases"] else 0,
        "median_latency_seconds": round(statistics.median(item["elapsed_seconds"] for item in report["cases"]), 3)
        if report["cases"] else 0,
        "all_per_question_budgets_respected": all(
            item.get("budget_respected", False)
            for item in report["cases"] if "metrics" in item
        ),
    }
    out = ROOT / "docs/verification/real-rag"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"deepseek-e2e-{run_id[:12]}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT {path.relative_to(ROOT).as_posix()}", flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return 0 if report["summary"]["submitted_questions"] == 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
