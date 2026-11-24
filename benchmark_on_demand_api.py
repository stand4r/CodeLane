"""Manual live API benchmark. Uses local settings without logging credentials."""
import json
import sys
import time
from pathlib import Path
from services.ai_context import ContextCache, render_project_context
from services.ai_service import AIService
from services.agent_tools import LocalToolbox, TOOL_DEFINITIONS
from core.ignore_rules import walk_files, BINARY_SUFFIXES

root = Path(__file__).resolve().parent
cfg = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
service = AIService(cfg, timeout=60)
box = LocalToolbox(str(root))
tools = [t for t in TOOL_DEFINITIONS if t['function']['name'] in {'read_file', 'search_in_project', 'list_directory'}]
print(json.dumps({'provider': service.provider_key, 'model': service.model}), flush=True)
# Reproduce the previous automatic snapshot for a same-model comparison.
blocks = []
remaining = 850_000
for path in sorted(walk_files(root, max_files=10000, max_bytes=4*1024*1024).files,
                   key=lambda p: (len(p.relative_to(root).parts), p.as_posix().lower())):
    rel = path.relative_to(root).as_posix()
    if path.suffix.lower() in BINARY_SUFFIXES or path.is_symlink() or path.name.lower().startswith('.env') or rel.lower() in {'config/settings.json', 'settings.json'}:
        continue
    if path.stat().st_size > remaining:
        continue
    text = path.read_text(encoding='utf-8', errors='replace')
    block = f'Файл проекта {rel}:\n```\n{text}\n```'
    size = len(block.encode('utf-8'))
    if '\x00' not in text and size <= remaining:
        blocks.append(block)
        remaining -= size
results = []
if '--after-only' in sys.argv:
    previous = json.loads((root / 'context_on_demand_benchmark.json').read_text(encoding='utf-8'))
    if previous['model'] == service.model:
        results = [r for r in previous['results'] if r['mode'] == 'snapshot_before']
for mode, context in [('snapshot_before', '\n\n'.join(blocks)),
                      ('on_demand_after', render_project_context(root, cache=ContextCache()))]:
    if mode == 'snapshot_before' and '--after-only' in sys.argv:
        continue
    for secret in [cfg.get('ai_api_key', ''), *(cfg.get('ai_provider_keys') or {}).values()]:
        if secret:
            context = context.replace(str(secret), '[REDACTED]')
    start = time.perf_counter()
    calls = []
    try:
        answer = service.converse('Найди значение константы PROJECT_CONTEXT_BYTES в services/ai_context.py. Ответ только число и путь. Используй приложенные данные, при необходимости прочитай нужный фрагмент.',
            context=context, tools=tools, tool_runner=lambda name,args: box.run(name,json.dumps(args)),
            on_tool=lambda name,args: calls.append({'name':name,'args':args}),
            should_stop=lambda: time.perf_counter()-start > 90)
        result = {'mode':mode, 'seconds':round(time.perf_counter()-start, 2),
                  'context_bytes':len(context.encode('utf-8')), 'answer':answer,
                  'correct':'850' in answer and 'ai_context.py' in answer,
                  'usage':service.last_usage, 'tools':calls}
    except Exception as exc:
        result = {'mode':mode,'error':type(exc).__name__, 'seconds':round(time.perf_counter()-start,2)}
    results.append(result)
    print(json.dumps(result,ensure_ascii=False),flush=True)
(root / 'context_on_demand_benchmark.json').write_text(json.dumps({'provider':service.provider_key,'model':service.model,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
