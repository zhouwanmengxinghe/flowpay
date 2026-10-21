"""本地模拟验收：成员、授权、配额、取消传播；--faults 包含 Redis 中断与 Java 重启。"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
from pathlib import Path
import subprocess
import time
import urllib.request
from uuid import uuid4

from verify import Client, ROOT, require


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults", action="store_true")
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def agent():
        with opener.open("http://127.0.0.1:8010/health", timeout=5) as response:
            return json.load(response)
    require(agent()["mode"] == "mock")
    env = dict(line.split("=", 1) for line in (ROOT/".env.support").read_text().splitlines() if line and not line.startswith("#") and "=" in line)
    def login(username, password=None):
        client = Client("http://127.0.0.1:8080")
        client.call("/auth/login", {"username": username, "password": password or env["SUPPORT_DEMO_PASSWORD"]})
        return client
    a,b,sa,sb = [login(name) for name in ["merchant_a","merchant_b","support_a","support_b"]]
    prefix = "advanced_" + uuid4().hex[:10]
    results = []
    def check(name, action):
        start = time.monotonic()
        action()
        results.append({"name":name,"passed":True,"seconds":round(time.monotonic()-start,3)})
        print("PASS", name, flush=True)
    def wait(action, timeout=45):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=action()
            if value: return value
            time.sleep(.15)
        raise AssertionError("Timed out waiting for condition")
    def session():
        return a.call("/sessions",{"accountId":"acct_bg_001","environment":"production"})["id"]
    def finished(sid):
        detail=a.call("/sessions/"+sid)
        return detail if detail["status"]=="completed" else None
    member=a.call("/members",{"username":prefix,"password":"Advanced-Test-Only-2026!","administrator":False})
    user=login(prefix,"Advanced-Test-Only-2026!")
    check("member_has_no_admin_or_observer_privilege",lambda:[user.call(path,expected=403) for path in ["/members","/operations","/operations/prometheus","/admin/grants"]])
    check("foreign_admin_cannot_modify_member",lambda:b.call("/members/"+member["id"],{"active":False,"administrator":False},404))
    check("last_active_admin_cannot_be_disabled",lambda:b.call("/members/merchant-b",{"active":False,"administrator":False},409))
    a.call("/members/"+member["id"],{"active":True,"administrator":True})
    check("promotion_applies_to_existing_login",lambda:user.call("/members"))
    a.call("/members/"+member["id"],{"active":True,"administrator":False})
    check("demotion_applies_to_existing_login",lambda:user.call("/members",expected=403))
    a.call("/members/"+member["id"],{"active":False,"administrator":False})
    check("disable_revokes_existing_cookie",lambda:user.call("/auth/me",expected=401))
    check("disabled_member_cannot_login",lambda:user.call("/auth/login",{"username":prefix,"password":"Advanced-Test-Only-2026!"},401))
    for _ in range(7):
        user.call("/auth/login",{"username":prefix,"password":"wrong"},401)
    def rate_boundary():
        def attempt(_):
            try:
                user.call("/auth/login",{"username":prefix,"password":"wrong"},401)
                return 401
            except AssertionError as error:
                if "got 429" not in str(error): raise
                return 429
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            require(sorted(pool.map(attempt,range(2)))==[401,429])
    check("redis_atomic_login_rate_boundary",rate_boundary)
    check("member_changes_are_audited",lambda:require(sum(row["target_id"]==member["id"] for row in a.call("/admin/audit"))==4))
    sid=session()
    sb.call("/sessions/"+sid)
    try:
        sa.call("/admin/grants",{"userId":"support-b","tenantId":"merchant-a","enabled":False})
        check("grant_revocation_applies_without_relogin",lambda:sb.call("/sessions/"+sid,expected=404))
        check("merchant_cannot_grant_support_access",lambda:a.call("/admin/grants",{"userId":"support-b","tenantId":"merchant-b","enabled":True},403))
    finally:
        sa.call("/admin/grants",{"userId":"support-b","tenantId":"merchant-a","enabled":True})
    check("observer_metrics_are_available",lambda:require("runs" in sa.call("/operations")))
    check("prometheus_requires_observer",lambda:require("jvm_memory" in sa.call("/operations/prometheus")))
    usage=a.call("/usage"); policy=usage["policy"]
    def limits(daily=10000,active=2,rpm=30):
        return sa.call("/admin/limits/merchant-a",{"dailyRuns":daily,"activeRuns":active,"requestsPerMinute":rpm})
    try:
        limits(active=1)
        sid=session(); other=session(); before=agent()["canceled"]
        a.call("/sessions/"+sid+"/messages",{"requestId":prefix+"slow","content":"[cancel-probe]"},202)
        wait(lambda:agent()["active"]>0)
        check("tenant_active_run_limit",lambda:a.call("/sessions/"+other+"/messages",{"requestId":prefix+"busy","content":"new"},429))
        a.call("/sessions/"+sid+"/cancel",{})
        check("cancel_reaches_python_coroutine",lambda:wait(lambda:agent()["canceled"]>before and agent()["active"]==0))
        a.call("/sessions/"+sid+"/messages",{"requestId":prefix+"done","content":"继续"},202)
        wait(lambda:finished(sid))
        used=a.call("/usage")["today"]
        limits(daily=used)
        check("daily_quota_rejects_new_run",lambda:a.call("/sessions/"+sid+"/messages",{"requestId":prefix+"quota","content":"new"},429))
        check("idempotent_retry_does_not_charge_again",lambda:a.call("/sessions/"+sid+"/messages",{"requestId":prefix+"done","content":"继续"},202))
        limits()
        if args.faults:
            compose=["docker","compose","--env-file",".env.support","-f","compose.business.yaml"]
            try:
                subprocess.run([*compose,"stop","support-redis"],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
                check("redis_failure_fails_closed",lambda:a.call("/sessions/"+sid+"/messages",{"requestId":prefix+"redis","content":"new"},503))
            finally:
                subprocess.run([*compose,"start","support-redis"],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
            sid=session()
            def submit_after_redis_recovery():
                try:
                    return a.call("/sessions/"+sid+"/messages",{"requestId":prefix+"restart","content":"[cancel-probe]"},202)
                except AssertionError as error:
                    if "got 503" not in str(error): raise
                    return None
            # 容器 start 不代表 Lettuce 已重连；固定请求编号等待应用恢复，避免重复受理。
            wait(submit_after_redis_recovery,10)
            wait(lambda:agent()["active"]>0)
            subprocess.run(["python","scripts/business/stop.py"],cwd=ROOT,check=True)
            subprocess.run(["python","scripts/business/start.py","--skip-build","--mode","mock"],cwd=ROOT,check=True)
            check("java_restart_recovers_persisted_run",lambda:wait(lambda:finished(sid),60))
    finally:
        limits(daily=policy["daily_runs"],active=policy["active_runs"],rpm=policy["requests_per_minute"])
    report={"mode":"mock-agent-real-services","tests":len(results),"passed":len(results),"results":results,"faults":args.faults,
            "limitations":["No real model calls","Single scheduler","Read-only computation may repeat after Python process loss"]}
    path=ROOT/"var/business"/(prefix+".json")
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"tests":len(results),"report":str(path)}),flush=True)


if __name__=="__main__":
    main()
