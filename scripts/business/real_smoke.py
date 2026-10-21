"""有限真实模型验收：只提交一条问题，最长等待 180 秒，不自动重试。"""
from __future__ import annotations
import argparse
import json
import time
import urllib.request
from uuid import uuid4
from verify import Client, ROOT, require


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine",choices=["s4","langchain"],required=True)
    parser.add_argument("--kind",choices=["document","investigation","followup"],required=True)
    args=parser.parse_args()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8010/health",timeout=5) as response:
        health=json.load(response)
    require(health["mode"]=="real" and health["engine"]==args.engine)
    env=dict(line.split("=",1) for line in (ROOT/".env.support").read_text().splitlines() if line and not line.startswith("#") and "=" in line)
    client=Client("http://127.0.0.1:8080")
    client.call("/auth/login",{"username":"merchant_a","password":env["SUPPORT_DEMO_PASSWORD"]})
    state=ROOT/"var/business/real-smoke-session.json"
    if args.kind=="followup":
        saved=json.loads(state.read_text())
        require(saved["engine"]==args.engine)
        sid=saved["session"]
    else:
        sid=client.call("/sessions",{"accountId":"acct_bg_001","environment":"production"})["id"]
        state.write_text(json.dumps({"engine":args.engine,"session":sid}))
    questions={"document":"请检索知识库，用中文列出 KB-API-005 中 Production 启用与暂停的三条常见边界，给出原文片段引用。",
               "investigation":"生产环境，账户 acct_bg_001，支付 pay_bg_001_0003 已成功，但没有收到 Webhook 通知。请查询支付、相关事件和投递记录，并检索有关文档，说明原因和证据。",
               "followup":"根据刚才查询到的记录，下一步该如何处理？请引用已有证据，不要重新索要已知编号。"}
    start=time.monotonic()
    run=client.call("/sessions/"+sid+"/messages",{"requestId":"real-smoke-"+uuid4().hex,"content":questions[args.kind]},202)
    detail=None
    while time.monotonic()-start<190:
        detail=client.call("/sessions/"+sid)
        if detail["status"] not in {"running","queued"}: break
        time.sleep(.5)
    events=client.call("/sessions/"+sid+"/events")
    report={"engine":args.engine,"kind":args.kind,"session":sid,"run":run["id"],"elapsed_seconds":round(time.monotonic()-start,2),"detail":detail,"events":events,
            "limits":"One submitted question; business worker max six model calls and eight tools; not a quality benchmark"}
    path=ROOT/"var/business"/("real-smoke-"+args.engine+"-"+args.kind+".json")
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"status":detail["status"],"seconds":report["elapsed_seconds"],"report":str(path)},ensure_ascii=False))
    require(detail["status"] in {"completed","waiting_user"})


if __name__=="__main__":
    main()
