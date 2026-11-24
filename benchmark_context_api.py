import json,time
from pathlib import Path
from services.ai_service import AIService
from services.ai_context import ContextCache,render_project_context
cfg=json.loads(Path('config/settings.json').read_text(encoding='utf-8'))
service=AIService(cfg,timeout=45)
print(json.dumps({'provider':service.provider_key,'model':service.model,'key_available':bool(service.api_key)}),flush=True)
if not service.api_key:
    raise SystemExit('Benchmark skipped: no API key; no tokens consumed.')
print(json.dumps({'provider':service.provider_key,'model':service.model,'key_available':bool(service.api_key)}),flush=True)
for name,ctx in [('control',None),('project',render_project_context(Path.cwd(),cache=ContextCache()))]:
    # Never transmit local credentials in the benchmark.
    for value in [cfg.get('ai_api_key',''),*list((cfg.get('ai_provider_keys') or {}).values())]:
        if value and ctx: ctx=ctx.replace(str(value),'[REDACTED]')
    start=time.perf_counter(); first=[]
    try:
        answer=service.converse('Назови значение PROJECT_CONTEXT_BYTES и файл, где оно определено. Если контекст не приложен, ответь только NO_CONTEXT. Ответ одной строкой.',context=ctx,on_text=lambda s:first.append(time.perf_counter()-start) if not first else None,should_stop=lambda:time.perf_counter()-start>60)
        print(json.dumps({'case':name,'seconds':round(time.perf_counter()-start,2),'first_text':first[:1],'answer':answer,'usage':service.last_usage},ensure_ascii=False),flush=True)
    except Exception as e: print(json.dumps({'case':name,'seconds':round(time.perf_counter()-start,2),'error_type':type(e).__name__,'error':str(e)[:300]},ensure_ascii=False),flush=True)

# Exercise the actual file tool and harness, with the answer beyond the former preview.
import tempfile
from services.agent_tools import LocalToolbox,TOOL_DEFINITIONS
with tempfile.TemporaryDirectory() as directory:
    marker='FAST-CONTEXT-739184'
    Path(directory,'large.txt').write_text(('padding line\n'*7000)+'verification_code='+marker+'\n'+('tail line\n'*7000),encoding='utf-8')
    box=LocalToolbox(directory)
    definitions=[d for d in TOOL_DEFINITIONS if d['function']['name']=='read_file']
    start=time.perf_counter(); calls=[]
    answer=service.converse('Прочитай large.txt целиком одним вызовом read_file без offset и limit. Найди verification_code в середине файла и ответь только его значением.',tools=definitions,tool_runner=lambda name,args:box.read_file(**args),on_tool=lambda name,args:calls.append(name),should_stop=lambda:time.perf_counter()-start>60)
    print(json.dumps({'case':'large_file_tool','seconds':round(time.perf_counter()-start,2),'correct':marker in answer,'tools':calls,'answer':answer,'usage':service.last_usage},ensure_ascii=False),flush=True)
