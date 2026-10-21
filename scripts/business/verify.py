"""真实 HTTP + PostgreSQL 验收；模拟 Agent，绝不调用真实模型。

先运行 scripts/business/start.py；本脚本会创建验收会话和工单，不删除用户数据。
审计回滚实验仅在显式 --rollback-probe 时执行，会短暂增加测试约束并在 finally 中移除。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


class Client:
    def __init__(self, base: str):
        self.base = base
        self.cookies = http.cookiejar.CookieJar()
        # 本地服务明确绕过公共代理，不触碰真实模型和公共互联网。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies))

    def call(self, path: str, body=None, expected=200, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(self.base + "/api" + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        if status != expected:
            raise AssertionError(f"{path}: expected HTTP {expected}, got {status}: {raw[:250]!r}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--rollback-probe", action="store_true")
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in (ROOT / ".env.support").read_text(encoding="utf-8-sig").splitlines()
               if line and not line.startswith("#") and "=" in line)
    if env.get("SUPPORT_AGENT_MODE") != "mock":
        raise SystemExit("Verification requires SUPPORT_AGENT_MODE=mock; refusing real-model calls")
    # 覆盖配置可能让实际 Agent 模式与 env 文件不同；提交任何问题前检查运行实例。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8010/health", timeout=5) as response:
        if json.load(response).get("mode") != "mock":
            raise SystemExit("Running Agent is not mock; refusing real-model calls")
    results = []
    prefix = uuid4().hex[:12]

    def check(name, action):
        started = time.perf_counter()
        action()
        results.append({"name": name, "passed": True, "seconds": round(time.perf_counter()-started, 4)})
        print("PASS", name, flush=True)

    def sql(statement):
        command = ["docker", "compose", "--env-file", ".env.support", "-f", "compose.business.yaml", "exec", "-T",
                   "support-db", "psql", "-U", "support", "-d", "support", "-v", "ON_ERROR_STOP=1", "-Atc", statement]
        return subprocess.check_output(command, cwd=ROOT, text=True, encoding="utf-8").strip()

    guest = Client(args.url)
    check("unauthenticated_session_access", lambda: guest.call("/sessions", expected=401))
    check("wrong_password_rejected", lambda: guest.call("/auth/login", {"username":"merchant_a","password":"not-the-password"},401))
    clients = {}
    for username in ["merchant_a", "merchant_b", "support_a", "support_b"]:
        client = Client(args.url)
        client.call("/auth/login", {"username":username,"password":env["SUPPORT_DEMO_PASSWORD"]})
        clients[username] = client
    a,b,sa,sb = (clients[x] for x in ["merchant_a","merchant_b","support_a","support_b"])
    check("cross_origin_mutation_rejected", lambda: a.call("/sessions", {"accountId":"acct_bg_001","environment":"production"},403,"https://attacker.invalid"))
    check("foreign_account_creation_rejected", lambda: a.call("/sessions", {"accountId":"acct_bm_030","environment":"production"},403))
    session = a.call("/sessions", {"accountId":"acct_bg_001","environment":"production"})["id"]
    for suffix in ["", "/events", "/export"]:
        check("tenant_isolation_" + (suffix or "detail"), lambda suffix=suffix: b.call("/sessions/"+session+suffix,expected=404))
    check("tenant_isolation_cancel", lambda: b.call("/sessions/"+session+"/cancel",{},404))
    check("tenant_isolation_submit", lambda: b.call("/sessions/"+session+"/messages",{"requestId":prefix+"foreign","content":"x"},404))
    check("session_list_excludes_foreign", lambda: require(all(row["id"]!=session for row in b.call("/sessions"))))

    def wait(sid, desired):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            detail = a.call("/sessions/"+sid)
            if detail["status"] == desired:
                return detail
            time.sleep(.1)
        raise AssertionError(f"Session did not reach {desired}")

    def parallel_submit():
        body = {"requestId":prefix+"message", "content":"[slow] Webhook 未收到通知"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(lambda _: a.call("/sessions/"+session+"/messages",body,202),range(12)))
        require(len({row["id"] for row in responses})==1)
        require(len(a.call("/sessions/"+session)["runs"])==1)
    check("12_parallel_message_requests_one_run", parallel_submit)
    check("idempotency_content_conflict", lambda: a.call("/sessions/"+session+"/messages",{"requestId":prefix+"message","content":"changed"},409))
    completed = wait(session,"completed")
    check("simulation_is_labeled", lambda: require("模拟验收" in completed["runs"][-1]["answer"]))
    check("support_cannot_submit_as_merchant", lambda: sa.call("/sessions/"+session+"/messages",{"requestId":prefix+"support","content":"x"},403))

    body = {"sessionId":session,"requestId":prefix+"ticket","title":"验收工单 "+prefix}
    def parallel_tickets():
        nonlocal ticket
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            rows=list(pool.map(lambda _:a.call("/tickets",body),range(12)))
        require(len({row["id"] for row in rows})==1)
        ticket=rows[0]
        require(len(ticket["history"])==1)
    ticket = None
    check("12_parallel_ticket_requests_one_ticket_one_audit",parallel_tickets)
    tid=ticket["id"]
    check("ticket_idempotency_conflict",lambda:a.call("/tickets",{**body,"title":"different"},409))
    check("foreign_ticket_hidden",lambda:b.call("/tickets/"+tid,expected=404))
    check("merchant_cannot_assign",lambda:a.call("/tickets/"+tid+"/transition",{"version":0,"status":"ASSIGNED","note":"no"},403))
    check("illegal_transition_rejected",lambda:sa.call("/tickets/"+tid+"/transition",{"version":0,"status":"CLOSED","note":"no"},409))

    def race_assign():
        def attempt(client):
            try:
                return client.call("/tickets/"+tid+"/transition",{"version":0,"status":"ASSIGNED","note":"并发接单"})
            except AssertionError as error:
                if "got 409" not in str(error): raise
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(attempt,[sa,sb]))
        require(sum(row is not None for row in rows)==1)
    check("two_support_agents_exactly_one_assignment",race_assign)
    assigned=a.call("/tickets/"+tid)
    owner=sa if assigned["assignee_id"]=="support-a" else sb
    other=sb if owner is sa else sa
    check("stale_version_rejected",lambda:owner.call("/tickets/"+tid+"/transition",{"version":0,"status":"IN_PROGRESS","note":"stale"},409))
    check("non_assignee_cannot_advance",lambda:other.call("/tickets/"+tid+"/transition",{"version":1,"status":"IN_PROGRESS","note":"not mine"},403))

    if args.rollback_probe:
        constraint="probe_"+prefix
        before=a.call("/tickets/"+tid)
        try:
            sql(f"ALTER TABLE ticket_history ADD CONSTRAINT {constraint} CHECK(note <> 'rollback-probe') NOT VALID")
            owner.call("/tickets/"+tid+"/transition",{"version":1,"status":"IN_PROGRESS","note":"rollback-probe"},500)
            after=a.call("/tickets/"+tid)
            check("audit_failure_rolls_back_status_version_and_history",lambda:require(
                before["status"]==after["status"] and before["version"]==after["version"] and before["history"]==after["history"]))
        finally:
            sql(f"ALTER TABLE ticket_history DROP CONSTRAINT IF EXISTS {constraint}")

    owner.call("/tickets/"+tid+"/transition",{"version":1,"status":"IN_PROGRESS","note":"调查中"})
    owner.call("/tickets/"+tid+"/transition",{"version":2,"status":"RESOLVED","note":"已提交解决方案"})
    check("merchant_confirmation_closes_ticket",lambda:require(a.call("/tickets/"+tid+"/transition",{"version":3,"status":"CLOSED","note":"确认解决"})["status"]=="CLOSED"))

    a.call("/sessions/"+session+"/messages",{"requestId":prefix+"fail","content":"[fail]"},202)
    wait(session,"failed")
    a.call("/sessions/"+session+"/messages",{"requestId":prefix+"after-fail","content":"失败后继续提问"},202)
    check("same_session_recovers_after_agent_failure",lambda:wait(session,"completed"))
    a.call("/sessions/"+session+"/messages",{"requestId":prefix+"cancel","content":"[cancel-probe]"},202)
    wait(session,"running")
    a.call("/sessions/"+session+"/cancel",{})
    time.sleep(10.5)
    check("late_agent_response_does_not_overwrite_cancel",lambda:require(a.call("/sessions/"+session)["status"]=="canceled"))
    a.call("/sessions/"+session+"/messages",{"requestId":prefix+"after-cancel","content":"停止后继续"},202)
    check("same_session_continues_after_cancel",lambda:wait(session,"completed"))
    a.call("/auth/logout",{})
    check("logout_revokes_token",lambda:a.call("/auth/me",expected=401))
    report={"mode":"mock-agent-real-java-real-postgresql", "run_id":prefix,
            "tests":len(results),"passed":sum(row["passed"] for row in results),"results":results,
            "limits":["No real model calls", "Not a capacity benchmark", "Single Java scheduler", "Test fixtures retained"]}
    directory=ROOT/"var/business";directory.mkdir(parents=True,exist_ok=True)
    (directory/("verification-"+prefix+".json")).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"tests":report["tests"],"passed":report["passed"],"report":str(directory/("verification-"+prefix+".json"))}),flush=True)


def require(condition):
    if not condition:
        raise AssertionError("Invariant did not hold")


if __name__=="__main__":
    main()
