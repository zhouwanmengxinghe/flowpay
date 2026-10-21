"""多轮并发受理/调度竞争回归；使用模拟 Agent，不测最大系统吞吐。"""
import concurrent.futures
import json
import time
import urllib.request
from uuid import uuid4
from verify import Client,ROOT,require


def main() -> None:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8010/health') as response:
        require(json.load(response)['mode']=='mock')
    env=dict(line.split('=',1) for line in (ROOT/'.env.support').read_text().splitlines() if line and not line.startswith('#') and '=' in line)
    a=Client('http://127.0.0.1:8080')
    a.call('/auth/login',{'username':'merchant_a','password':env['SUPPORT_DEMO_PASSWORD']})
    results=[]
    for index in range(10):
        sid=a.call('/sessions',{'accountId':'acct_bg_001','environment':'production'})['id']
        body={'requestId':uuid4().hex,'content':'并发调度竞争回归 '+str(index)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            replies=list(pool.map(lambda _:a.call('/sessions/'+sid+'/messages',body,202),range(12)))
        require(len({reply['id'] for reply in replies})==1)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            detail=a.call('/sessions/'+sid)
            if detail['status']=='completed':break
            time.sleep(.05)
        require(detail['status']=='completed' and len(detail['runs'])==1)
        results.append({'round':index+1,'parallel_requests':12,'runs':1,'status':'completed'})
    path=ROOT/'var/business'/('concurrency-'+uuid4().hex[:10]+'.json')
    path.write_text(json.dumps({'rounds':10,'parallel_requests_per_round':12,'results':results,'limits':'Mock Agent; correctness under admission/dispatch contention, not a capacity benchmark'},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS 10 rounds x 12 parallel submissions:',path)


if __name__=='__main__':
    main()
