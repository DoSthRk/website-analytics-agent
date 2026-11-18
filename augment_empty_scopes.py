import json,pathlib,hashlib,sqlite3,sys,datetime
base=pathlib.Path('/opt/website-analytics/shared');out=pathlib.Path(sys.argv[1]);scopes={}
titles={'GA4 Pages':'ga4_pages','GSC Pages':'gsc_pages','GSC Queries':'gsc_queries','Inquiry Pages':'inquiry_pages'}
for f in sorted((base/'audits').glob('*.json')):
 a=json.loads(f.read_text());r=a.get('request',{});dr=r.get('date_range',{});statuses=a.get('source_statuses',{}).get('sources',{})
 if r.get('site')!='genemedi-net' or set(dr)<{'start','end'}:continue
 for source in ('ga4','gsc','inquiry'):
  st=statuses.get(source,{}).get('status')
  if st not in ('ok','partial'):continue
  h=hashlib.sha256(json.dumps({'source':source,**dr},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest();p=base/'cache/genemedi-net'/source/(h+'.json')
  if not p.is_file():continue
  before=p.stat();d=json.loads(p.read_text());after=p.stat()
  if (before.st_ino,before.st_mtime_ns,before.st_size)!=(after.st_ino,after.st_mtime_ns,after.st_size):raise ValueError('source changed')
  for title,name in titles.items():
   if title in d and d[title]==[]:scopes.setdefault(name,{})[(dr['start'],dr['end'])]={'start':dr['start'],'end':dr['end'],'state':'cached_successful_empty','source_status':st,'collected_at':a.get('generated_at')}
with sqlite3.connect(out) as db:
 for name,inventory in scopes.items():
  old=db.execute('SELECT metadata FROM datasets WHERE name=?',(name,)).fetchone()
  if not old:raise ValueError('unexpected missing empty dataset')
  meta=json.loads(old[0]);meta['coverage']['scope_inventory']=list(inventory.values());db.execute('UPDATE datasets SET metadata=? WHERE name=?',(json.dumps(meta),name))
print(json.dumps({'empty_scope_counts':{k:len(v) for k,v in scopes.items()},'metadata_only':True}))
