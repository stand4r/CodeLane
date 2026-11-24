"""Run a real CodeLane task against a disposable source copy; log every API request."""
from __future__ import annotations
import collections
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.ai_service import AIService
from services.agent_tools import LocalToolbox, ToolError, TOOL_DEFINITIONS
from services.agent_team import run_team

TASK = '''Доработай поиск проекта CodeLane. Добавь два необязательных параметра инструмента search_in_project:
case_sensitive (по умолчанию false) — учитывать регистр для обычного и regex-поиска;
output_mode (content по умолчанию или files) — в режиме files возвращать только уникальные пути файлов с совпадениями.
Сохрани совместимость query/queries/glob/context_lines/max_results и отмену. В files лимит должен ограничивать число файлов, а не число строк совпадений.
Обнови реализацию, схему инструмента, автоматические тесты и USER_GUIDE.md. Проверь оба режима с буквальным и regex-поиском, группой queries, лимитами и отменой.
Выполни изменения и запусти целевые тесты. Простого описания плана недостаточно. Работай только внутри текущего проекта.'''

def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def size(value):
    return len((value if isinstance(value, str) else dump(value)).encode('utf-8'))

class Recorder:
    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.Lock()
        self.requests = []
        self.seen = set()
        self.started = time.perf_counter()
    def begin(self, actor, payload):
        with self.lock:
            number = len(self.requests) + 1
            categories = collections.Counter()
            repeated = collections.Counter()
            ids = {c['id']:c['function']['name'] for m in payload.get('messages', []) for c in m.get('tool_calls', [])}
            pieces = [('tool_schemas', payload.get('tools', []))]
            if payload.get('system'):
                pieces.append(('system', payload['system']))
            for m in payload.get('messages', []):
                role = m.get('role', '')
                if role == 'tool':
                    category = 'tool_result:' + ids.get(m.get('tool_call_id'), 'unknown')
                else:
                    category = {'system':'system','user':'task_or_dependency','assistant':'assistant_text'}.get(role,role)
                if m.get('content'):
                    pieces.append((category,m['content']))
                if m.get('tool_calls'):
                    pieces.append(('assistant_tool_arguments',m['tool_calls']))
            for category, value in pieces:
                count = size(value)
                categories[category] += count
                key = actor, category, hashlib.sha256(dump(value).encode()).hexdigest()
                if key in self.seen:
                    repeated[category] += count
                self.seen.add(key)
            record = {'id':number,'actor':actor,'started_seconds':round(time.perf_counter()-self.started,3),
                      'payload_bytes':size(payload),'component_bytes':dict(categories),
                      'repeated_component_bytes':dict(repeated),'tool_count':len(payload.get('tools',[])),
                      'message_count':len(payload.get('messages',[])), 'usage':{}, 'tool_calls':[]}
            self.requests.append(record)
            (self.directory/f'request_{number:03}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
            print(dump({'event':'request','id':number,'actor':actor,'bytes':record['payload_bytes'],'messages':record['message_count']}),flush=True)
            return record
    def finish(self, record, started, text, calls, error):
        with self.lock:
            record['seconds'] = round(time.perf_counter()-started,3)
            record['response_text'] = text
            record['tool_calls'] = calls
            if error:
                record['error'] = error
            (self.directory/'requests.json').write_text(json.dumps(self.requests,ensure_ascii=False,indent=2),encoding='utf-8')
            print(dump({'event':'response','id':record['id'],'seconds':record['seconds'],'usage':record['usage'],'calls':[c.get('name') for c in calls],'error':error}),flush=True)

class AuditedService(AIService):
    def _stream_json_once(self, path, payload):
        actor = getattr(self,'_agent_system_prompt','supervisor')
        if actor != 'supervisor':
            actor = actor.split('роль: ',1)[-1].split('.',1)[0]
        record = self.recorder.begin(actor,payload)
        started = time.perf_counter(); text=[]; slots={}; error=''
        try:
            for event in super()._stream_json_once(path,payload):
                if isinstance(event.get('usage'),dict):
                    record['usage'].update(event['usage'])
                for choice in event.get('choices') or []:
                    delta=choice.get('delta') or {}
                    if delta.get('content'):
                        text.append(delta['content'])
                    for index,call in enumerate(delta.get('tool_calls') or []):
                        slot=slots.setdefault(call.get('index',index),{'name':'','arguments':''})
                        function=call.get('function') or {}
                        if function.get('name'): slot['name']=function['name']
                        slot['arguments']+=function.get('arguments') or ''
                yield event
        except Exception as exc:
            error=type(exc).__name__+': '+str(exc)
            raise
        finally:
            self.recorder.finish(record,started,''.join(text),list(slots.values()),error)

class AuditToolbox(LocalToolbox):
    def _resolve(self, raw):
        result=super()._resolve(raw)
        if not result.is_relative_to(self.root):
            raise ToolError('Аудит ограничен копией проекта.')
        return result
    def run(self,name,arguments):
        if name in {'run_admin_command','open_application','run_executable','browser_navigate','capture_screen'}:
            raise ToolError('Инструмент не относится к задаче аудита.')
        return super().run(name,arguments)

def main():
    audit = ROOT/'scratch'/('token_audit_'+time.strftime('%Y%m%d_%H%M%S'))
    project = audit/'project'; project.mkdir(parents=True)
    listed=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT,capture_output=True,check=True).stdout.decode('utf-8').split('\0')
    before={}
    for rel in listed:
        path=Path(rel)
        if not rel or path.parts[0] not in {'app','core','services','widgets','utils','design','tests','config','extensions'} and len(path.parts)>1:
            continue
        if path.suffix.lower() not in {'.py','.toml','.txt','.md','.json','.qss','.yaml','.yml','.ini','.cfg'}:
            continue
        if rel.lower() in {'config/settings.json','config/github_auth.json'} or path.name.startswith('.env'):
            continue
        source=ROOT/path
        if not source.is_file() or source.stat().st_size>2*1024*1024:
            continue
        target=project/path;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        before[rel]=hashlib.sha256(source.read_bytes()).hexdigest()
    config=json.loads((ROOT/'config/settings.json').read_text(encoding='utf-8'))
    # Active profile overrides follow the same selection as the UI.
    active=next((p for p in config.get('ai_profiles',[]) if p.get('id')==config.get('ai_active_profile')),None)
    if active:
        config.update({k:v for k,v in {'ai_provider':active.get('provider'),'ai_model':active.get('model'),'ai_base_url':active.get('base_url'),'ai_protocol':active.get('protocol'),'ai_api_key':active.get('api_key')}.items() if v})
    recorder=Recorder(audit)
    service=AuditedService(config,timeout=90);service.recorder=recorder
    deadline=time.monotonic()+900
    stop=lambda:time.monotonic()>deadline
    box=AuditToolbox(str(project),access_mode='full',should_stop=stop)
    box.background=True
    # Present the IDE tool catalogue; adapt only relevant UI operations to the copy.
    def ui(name,args):
        if name=='read_buffer':return '__NO_OPEN_EDITOR_BUFFER__'
        if name in {'open_file','editor_save','explorer_open','explorer_reveal'}:return 'Файл доступен на диске копии проекта.'
        if name=='workspace_state':return dump({'project_root':str(project),'open_files':[],'note':'Аудит в отдельной копии; редакторных буферов нет.'})
        if name=='editor_state':return dump({'open_files':[],'project_root':str(project)})
        if name=='terminal_execute':return box.run_terminal_command(args['command'],timeout_sec=max(1,args.get('wait_seconds',30)))
        if name=='edit_file':
            original=box.ui_action;box.ui_action=None
            try:return box.edit_file(args['path'],args['old_text'],args['new_text'],expected_source=args.get('expected'))
            finally:box.ui_action=original
        if name=='git_read':return 'Копия для аудита не содержит Git-истории; используй чтение файлов.'
        if name=='test_results':return 'В копии нет панели тестов; запусти run_project_tests.'
        raise ToolError('UI-инструмент недоступен в копии: '+name)
    box.ui_action=ui
    (audit/'task.txt').write_text(TASK,encoding='utf-8')
    print(dump({'audit':str(audit),'model':service.model,'provider':service.provider_key,'tool_count':len(box.definitions()),'files_copied':len(before)}),flush=True)
    def trace(actor,text):
        with recorder.lock:
            with (audit/'tools.jsonl').open('a',encoding='utf-8') as out:out.write(dump({'actor':actor,'text':text})+'\n')
    def emit(number,role,goal,status):
        print(dump({'event':'agent','id':number,'role':role,'status':status}),flush=True)
    result=run_team(service,box,TASK,emit,stop,trace=trace)
    # Apply prepared changes only inside the copy, as the UI does on acceptance.
    applied=[]
    for edit in result.edits:
        try:applied.append({'path':edit.path,'result':edit.apply(box)})
        except Exception as exc:applied.append({'path':edit.path,'error':str(exc)})
    changes=[]
    for file in project.rglob('*'):
        if not file.is_file() or '__pycache__' in file.parts or '.pytest_cache' in file.parts:continue
        rel=file.relative_to(project).as_posix()
        if hashlib.sha256(file.read_bytes()).hexdigest()!=before.get(rel):changes.append(rel)
    verification=subprocess.run([sys.executable,'-m','pytest','tests/test_project_search.py','tests/test_agent_tools.py','tests/test_ai_context.py','-q'],cwd=project,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
    report={'model':service.model,'provider':service.provider_key,'task':TASK,'complete':result.complete,
            'answer':result.answer,'usage':result.usage,'failed_agents':result.failed_agents,
            'changed_files':changes,'prepared_edits':applied,'verification':{'exit_code':verification.returncode,'output':verification.stdout+verification.stderr},
            'requests':recorder.requests,'note':'API token totals are exact provider reports. Component sizes are UTF-8 bytes, not token counts. No GUI/MCP servers or prior history were attached.'}
    (audit/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(dump({'event':'finished','audit':str(audit),'complete':result.complete,'usage':result.usage,'changed_files':changes,'verification_exit':verification.returncode}),flush=True)

if __name__=='__main__':main()
