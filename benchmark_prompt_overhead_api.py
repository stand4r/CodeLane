"""Manual same-model API check for system/team instruction overhead."""
import copy
import json
import time
from pathlib import Path
from services.ai_service import AIService
from services.agent_tools import LocalToolbox
from services.agent_team import run_team

root = Path(__file__).resolve().parent
config = json.loads((root/'config/settings.json').read_text(encoding='utf-8'))
before = json.loads((root/'prompt_overhead_before.json').read_text(encoding='utf-8'))
previous = 'Предыдущая задача: проверь модуль авторизации.\nИтог: ' + 'Проверки завершены, проблем не найдено. ' * 180
results = []
for case in ('before', 'after'):
    service = AIService(copy.deepcopy(config), timeout=45)
    calls = []
    start = time.perf_counter()
    if case == 'before':
        service._agent_system_prompt = before['system']
        answer = service.converse(before['team'] + '\n\nПредыдущая задача и её итог:\n' + previous,
            tools=before['tools'], tool_runner=lambda name,args:'Не требуется для вычисления.',
            on_tool=lambda name,args:calls.append(name),should_stop=lambda:time.perf_counter()-start>60)
        usage = service.last_usage
    else:
        result = run_team(service, LocalToolbox(str(root)), 'Сколько будет 2 + 2? Ответ только число.',
            lambda *_: None, lambda:time.perf_counter()-start>60, previous_result=previous,
            trace=lambda _id,text:calls.append(text) if text.startswith('read_previous_result') else None)
        answer, usage = result.answer, result.usage
    item = {'case':case,'model':service.model,'seconds':round(time.perf_counter()-start,2),
            'answer':answer,'correct':answer.strip()=='4','usage':usage,'tools':calls}
    results.append(item)
    print(json.dumps(item,ensure_ascii=False),flush=True)
(root/'prompt_overhead_benchmark.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
