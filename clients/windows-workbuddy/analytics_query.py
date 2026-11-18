"""Optional stdlib-only read client; Windows PowerShell client needs no Python."""
import argparse,json,re,sys
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler

OPERATIONS=('list_cases','case_versions','case_observations','case_detail','case_trend','page_contribution')
NOTICE={'saved_data_only':True,'automatic_refresh_not_enabled':True,'windows_device_acceptance':'not_verified','data_status_is_not_causal_evidence':True}
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('redirect rejected')

def payload(operation,case_id=None,evidence_id=None,page_key=None):
    if operation not in OPERATIONS:raise ValueError('read operation required')
    need_case=operation!='list_cases';need_evidence=operation not in ('list_cases','case_versions','case_observations');need_page=operation=='page_contribution'
    for need,value,pattern in [(need_case,case_id,r'[a-z][a-z0-9-]{0,63}'),(need_evidence,evidence_id,r'[a-f0-9]{64}'),(need_page,page_key,r'[a-f0-9]{64}')]:
        if need and (not isinstance(value,str) or re.fullmatch(pattern,value) is None) or not need and value is not None:raise ValueError('invalid read selectors')
    return {'contract':'batch-service/v1','operation':operation,**{k:v for k,v in [('case_id',case_id),('evidence_id',evidence_id),('page_key',page_key)] if v is not None}}

def query(body):
    if not isinstance(body,dict) or body!=payload(body.get('operation'),body.get('case_id'),body.get('evidence_id'),body.get('page_key')):raise ValueError('invalid read request fields')
    # No configurable destination, proxy, source URL, auth token, path or shell.
    request=Request('http://127.0.0.1:8765/v1/query',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=10) as response:data=response.read(1_000_001)
    if len(data)>1_000_000:raise ValueError('response budget exceeded')
    result=json.loads(data)
    if result.get('contract')!='batch-service/v1' or result.get('status') not in {'ok','partial'} or any(result.get(k)!=v for k,v in body.items()):raise ValueError('response identity mismatch')
    return {'client_contract':'genemedi-readonly-client/v1','notice':NOTICE,'server_result':result}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--operation',choices=OPERATIONS,default='list_cases');p.add_argument('--case-id');p.add_argument('--evidence-id');p.add_argument('--page-key');a=p.parse_args(argv)
    try:print(json.dumps(query(payload(a.operation,a.case_id,a.evidence_id,a.page_key)),ensure_ascii=False));return 0
    except Exception:print(json.dumps({'client_contract':'genemedi-readonly-client/v1','status':'error','code':'local_query_failed_check_approved_tunnel','notice':NOTICE}));return 2
if __name__=='__main__':raise SystemExit(main())
