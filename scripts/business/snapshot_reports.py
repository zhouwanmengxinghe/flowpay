"""导出可随代码查看的测试摘要，不复制知识库正文、凭证或完整模型上下文。"""
from __future__ import annotations
import json
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parents[2]


def main() -> None:
    source=ROOT/'var/business'
    destination=ROOT/'docs/verification/iteration2'
    destination.mkdir(parents=True,exist_ok=True)
    for pattern,name in [('verification-*.json','business.json'),('advanced_*.json','advanced.json'),('concurrency-*.json','concurrency.json'),('benchmark-*.json','benchmark.json')]:
        latest=max(source.glob(pattern),key=lambda path:path.stat().st_mtime)
        shutil.copyfile(latest,destination/name)
    real=[]
    files=['real-smoke-s4-document.json','real-smoke-langchain-investigation-before-fix.json',
           'real-smoke-langchain-investigation-tool-budget-failure.json','real-smoke-langchain-investigation.json','real-smoke-langchain-followup.json']
    for number,name in enumerate(files,1):
        report=json.loads((source/name).read_text(encoding='utf-8'))
        last=report['detail']['runs'][-1]
        answer=last.get('answer') or ''
        final=[event['payload'] for event in report['events'] if event['type']=='run.finished' and event['run_id']==report['run']]
        real.append({'submission':number,'engine':report['engine'],'kind':report['kind'],'task_status':report['detail']['status'],
                     'elapsed_seconds':report['elapsed_seconds'],'evidence_entries':len(final[-1]['evidence']) if final else 0,
                     'citations':sorted(set(re.findall(r'doc:[A-Za-z0-9._-]+#c\d+',answer))),
                     'contains_protocol_marker':any(marker in answer for marker in ['<｜DSML｜','<|DSML|']),
                     'interpretation':{1:'s4 transport/retrieval smoke only; not quality scoring',2:'failed graph step bound',3:'failed tool budget bound',4:'transport completed but protocol defect; subsequent fix verified offline only',5:'provider 429; real multi-turn acceptance remains pending'}[number]})
    (destination/'real-smoke-summary.json').write_text(json.dumps({'submissions':real,'total_submitted':5,'no_calls_after_provider_429':True,'followup_acceptance':'pending'},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Reports saved:',destination)


if __name__=='__main__':
    main()
