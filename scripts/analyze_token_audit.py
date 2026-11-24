"""Summarize provider token totals and explicitly labelled local text estimates."""
import collections
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scratch/token_audit_tools'))
import tiktoken
enc=tiktoken.get_encoding('o200k_base')
folder=Path(sys.argv[1])
requests=json.loads((folder/'requests.json').read_text(encoding='utf-8'))
summary={'encoding_estimate':'o200k_base; text estimates are not provider accounting',
         'request_count':len(requests),'provider_input_tokens':0,'provider_output_tokens':0,
         'requests_without_usage':[],
         'provider_cached_input_tokens':0,'cache_usage_reported':False,
         'text_estimated_tokens_by_category':{},'estimated_repeated_text_tokens':0,
         'tool_schema_and_protocol_residual':0,'rounds':[]}
categories=collections.Counter();seen=set();largest={};actors=collections.Counter()
for r in requests:
    p=json.loads((folder/f"request_{r['id']:03}.json").read_text(encoding='utf-8'))
    usage=r.get('usage',{}); actual=usage.get('prompt_tokens',usage.get('input_tokens',0))
    summary['provider_input_tokens']+=actual
    if not actual:summary['requests_without_usage'].append(r['id'])
    summary['provider_output_tokens']+=usage.get('completion_tokens',usage.get('output_tokens',0))
    actors[r['actor']]+=actual
    detail=usage.get('prompt_tokens_details') or {}
    cached=usage.get('cache_read_input_tokens',detail.get('cached_tokens'))
    if cached is not None:summary['cache_usage_reported']=True;summary['provider_cached_input_tokens']+=cached
    callnames={c['id']:c['function']['name'] for m in p.get('messages',[]) for c in m.get('tool_calls',[])}
    parts=[]
    for m in p.get('messages',[]):
        content=m.get('content') or ''
        if not isinstance(content,str):content=json.dumps(content,ensure_ascii=False)
        kind={'system':'system','user':'task_or_dependency','assistant':'assistant_text','tool':'result:'+callnames.get(m.get('tool_call_id'),'unknown')}.get(m['role'],m['role'])
        if content:parts.append((kind,content))
        for c in m.get('tool_calls',[]):
            parts.append(('tool_arguments',c['function']['name']+'\n'+c['function']['arguments']))
    text_total=0;repeated=0;bycat=collections.Counter()
    for kind,text in parts:
        n=len(enc.encode(text,disallowed_special=()));bycat[kind]+=n;text_total+=n
        key=r['actor'],kind,hashlib.sha256(text.encode()).hexdigest()
        if key in seen:repeated+=n
        else:
            seen.add(key)
            if kind.startswith('result:'):largest[key]={'type':kind,'estimated_text_tokens':n,'first_request':r['id'],'preview':text[:180]}
    if actual:
        categories.update(bycat)
        summary['estimated_repeated_text_tokens']+=repeated
    residual=actual-text_total if actual else None
    if residual is not None:summary['tool_schema_and_protocol_residual']+=residual
    summary['rounds'].append({'id':r['id'],'actor':r['actor'],'input_tokens':actual,'output_tokens':usage.get('completion_tokens',0),
                              'seconds':r.get('seconds'),'calls':[c['name'] for c in r.get('tool_calls',[])],
                              'estimated_text_tokens':text_total,'estimated_repeated_text_tokens':repeated,
                              'residual_schemas_and_protocol':residual,'estimated_text_by_category':dict(bycat)})
summary['text_estimated_tokens_by_category']=dict(categories)
summary['input_tokens_by_actor']=dict(actors)
summary['largest_unique_results']=sorted(largest.values(),key=lambda x:-x['estimated_text_tokens'])[:10]
(folder/'analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k not in {'rounds','largest_unique_results'}},ensure_ascii=False,indent=2))
