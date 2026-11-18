import sys,sqlite3,threading,http.client,json
from pathlib import Path
sys.path.insert(0,'/Users/dosthrk/Documents/Codex/2026-09-29/task/validation/repeatable-rc-stage-20261104/unpacked/src')
from test_fullsite_api import Tests
from fullsite_api import install
from website_analytics.analysis import service
from website_analytics.analysis.cases import CaseStore
x=Tests();x.setUp();old=service.dispatch
seen=[]
def spy(store,request,*a):
 seen.append(request['operation']);return {'contract':'batch-service/v1','operation':request['operation'],'status':'ok'}
service.dispatch=spy
install(x.r.path)
p=Path(x.tmp.name)/'cases';db=sqlite3.connect(p);db.execute('CREATE TABLE case_evidence(evidence_id TEXT)');db.close()
server=service.make_server(None,0,cases=CaseStore(p));t=threading.Thread(target=server.serve_forever,daemon=True);t.start();port=server.server_port
try:
 def request(method,path,body=None,headers={}):
  c=http.client.HTTPConnection('127.0.0.1',port,timeout=3);c.request(method,path,body,headers);r=c.getresponse();v=(r.status,json.loads(r.read()));c.close();return v
 code,health=request('GET','/healthz');assert code==200 and 'site_query'in health['operations']
 code,cat=request('POST','/v1/query',json.dumps({'contract':'batch-service/v1','operation':'site_catalog'}),{'Content-Type':'application/json'});assert code==200 and len(cat['datasets'])==2
 for op in ['list_cases','case_versions','case_observations','case_detail','case_trend','page_contribution','refresh_status']:
  code,r=request('POST','/v1/query',json.dumps({'contract':'batch-service/v1','operation':op}),{'Content-Type':'application/json'});assert code==200 and op in seen
 for headers in [{'Origin':'http://evil.test'},{'Host':'evil.test'},{'Transfer-Encoding':'chunked'},{'Content-Type':'text/plain'}]:
  code,r=request('POST','/v1/query','{}',{'Content-Type':'application/json',**headers});assert code in (400,403,415)
 print('HTTP PASS: health, catalog, seven legacy dispatch delegates, four hostile contexts/framing')
finally:server.shutdown();server.server_close();t.join();service.dispatch=old;x.tearDown()
