"""有界本地 HTTP 读接口负载，不执行 Agent，不推断模型问答容量。"""
from __future__ import annotations
import concurrent.futures
import argparse
import json
import math
import platform
import statistics
import time
from uuid import uuid4
from verify import Client, ROOT


def main() -> None:
    env=dict(line.split("=",1) for line in (ROOT/".env.support").read_text().splitlines() if line and not line.startswith("#") and "=" in line)
    client=Client("http://127.0.0.1:8080")
    client.call("/auth/login",{"username":"merchant_a","password":env["SUPPORT_DEMO_PASSWORD"]})
    client.call("/sessions")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=5,choices=range(1,31))
    args=parser.parse_args()
    rows=[]
    for concurrency in [1,4,8]:
        deadline=time.perf_counter()+args.seconds
        def request():
            start=time.perf_counter()
            try:
                client.call("/sessions")
                return (time.perf_counter()-start)*1000,True
            except Exception:
                return (time.perf_counter()-start)*1000,False
        def worker(_):
            samples=[]
            while time.perf_counter()<deadline:
                samples.append(request())
            return samples
        start=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples=[item for batch in pool.map(worker,range(concurrency)) for item in batch]
        seconds=time.perf_counter()-start
        values=sorted(value for value,success in samples)
        rows.append({"concurrency":concurrency,"requests":len(samples),"successes":sum(ok for _,ok in samples),
                     "mean_ms":round(statistics.mean(values),2),"p95_ms":round(values[math.ceil(len(values)*.95)-1],2),
                     "requests_per_second":round(len(samples)/seconds,2),"elapsed_seconds":round(seconds,2)})
        print(json.dumps(rows[-1]),flush=True)
    report={"target":"GET /api/sessions via 127.0.0.1","machine":platform.platform(),"warmup":1,"seconds_per_level":args.seconds,"results":rows,
            "limits":["Short fixed-duration test on one local machine","Java CPU count 2, max heap 512MB","Not a saturation or soak test","No model/embedding calls","No before-after baseline: no speedup claim"]}
    path=ROOT/"var/business"/("benchmark-"+uuid4().hex[:10]+".json")
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Report:",path)


if __name__=="__main__":
    main()
