import json,shutil,sys,time,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from audit_medium_task import AuditedService,Recorder,AuditToolbox,TASK
from services.agent_team import run_team
if '--smoke' in sys.argv:
 TASK='В services/project_search.py и services/agent_tools.py проверь наличие параметров case_sensitive и output_mode=files и их согласованность. Существующие целевые тесты: tests/test_project_search.py. Специалист формулирует критерии и пути; исполнитель запускает целевые тесты и исправляет только воспроизводимый дефект; проверяющий независимо повторяет целевые проверки. Не обходи весь проект: пути уже указаны. Итог каждого этапа передай finish_stage.'
out=ROOT/'scratch'/('three_stage_live_'+time.strftime('%Y%m%d_%H%M%S'));project=out/'project';project.mkdir(parents=True)
for folder in ['app','core','services','utils','widgets','extensions','tests','design']:
 shutil.copytree(ROOT/folder,project/folder,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','test_codelane_offline*'))
for name in ['USER_GUIDE.md','pyproject.toml','requirements.txt']:shutil.copy2(ROOT/name,project/name)
if len(sys.argv)>1:
 source=Path(sys.argv[1])
 for name in ['services/project_search.py','services/agent_tools.py','tests/test_project_search.py','USER_GUIDE.md']:
  shutil.copy2(source/name,project/name)
cfg=json.loads((ROOT/'config/settings.json').read_text(encoding='utf-8'));service=AuditedService(cfg,timeout=45);service.recorder=Recorder(out)
box=AuditToolbox(str(project),access_mode='full');box.background=True
print('OUTPUT',out,flush=True)
result=run_team(service,box,TASK,lambda n,r,g,s:print('STAGE',n,r,s,flush=True),lambda:False,strategy='three_stage')
verification=subprocess.run([sys.executable,'-m','pytest','tests/test_project_search.py','tests/test_agent_tools.py','tests/test_ai_context.py','-q'],cwd=project,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
report={'complete':result.complete,'answer':result.answer,'usage':result.usage,'stages':result.optimization,'changed_files':result.changed_files,'verification_exit':verification.returncode,'verification_output':verification.stdout+verification.stderr}
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False),flush=True)
