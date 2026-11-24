"""Compare line-only search with grouped contextual search through the configured API."""
import copy
import json
import tempfile
import time
from pathlib import Path
from services.ai_service import AIService
from services.agent_tools import LocalToolbox, TOOL_DEFINITIONS
from services.ai_context import ContextCache, render_project_context

root = Path(__file__).resolve().parent
config = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
results = []
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory, 'limits.py')
    path.write_text('# route=west-Q73\nALPHA_LIMIT = 120\n' + '\n'.join(f'# padding {i}' for i in range(500)) + '\n# route=east-M82\nBETA_LIMIT = 240\n', encoding='utf-8')
    box = LocalToolbox(directory)
    catalog = [copy.deepcopy(t) for t in TOOL_DEFINITIONS if t['function']['name'] in {'read_file', 'search_in_project'}]
    for mode in ('line_only_before', 'contextual_after'):
        service = AIService(config, timeout=45)
        definitions = copy.deepcopy(catalog)
        if mode == 'line_only_before':
            search = next(t['function'] for t in definitions if t['function']['name']=='search_in_project')
            search['description'] = 'Найти текст в проекте. Возвращает совпадения с путями и номерами строк. regex включает регулярные выражения.'
            search['parameters']['properties'] = {k:v for k,v in search['parameters']['properties'].items() if k in {'query','glob','regex'}}
            search['parameters']['required'] = ['query']
        calls = []
        def runner(name, args):
            args = dict(args)
            if name == 'search_in_project' and mode == 'line_only_before':
                args['context_lines'] = 0
            return box.run(name, json.dumps(args))
        start = time.perf_counter()
        answer = service.converse('В limits.py найди ALPHA_LIMIT и BETA_LIMIT. Для каждой константы назови значение и route из комментария перед ней. Ответ две строки.',
            context=render_project_context(Path(directory), cache=ContextCache()),
            tools=definitions, tool_runner=runner, on_tool=lambda name,args:calls.append({'name':name,'args':args}),
            should_stop=lambda:time.perf_counter()-start>90)
        result={'case':mode,'model':service.model,'seconds':round(time.perf_counter()-start,2),
                'correct':all(value in answer for value in ('120','240','west-Q73','east-M82')),
                'answer':answer,'tools':calls,'usage':service.last_usage}
        results.append(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
(root/'context_search_benchmark.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
