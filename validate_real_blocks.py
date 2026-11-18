"""Read-only aggregate verification. Prints no dimensions, URLs, IDs or source rows."""
import collections,hashlib,json,math,pathlib,re,sqlite3,sys
from fullsite_codec import decode_block
from publish_fullsite import SPECS,METRICS,PERIOD,DAILY

def numeric(row,fields):
 return {k:v for k,v in row.items() if k in fields and (v is None or type(v) in (int,float) and math.isfinite(v))}
def digest(rows):
 return hashlib.sha256('\n'.join(sorted(json.dumps(r,sort_keys=True,separators=(',',':')) for r in rows)).encode()).hexdigest()
def verify(base,output):
 base=pathlib.Path(base);db=sqlite3.connect(pathlib.Path(output).resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
 audits=[];scope={};daily={};results=[]
 for p in (base/'audits').glob('*.json'):
  a=json.loads(p.read_bytes());r=a.get('request',{});dr=r.get('date_range',{})
  if r.get('site')=='genemedi-net' and set(dr)>={'start','end'}:audits.append((a.get('generated_at',''),a,dr))
 for collected,a,dr in sorted(audits,key=lambda x:x[0]):
  for source,meta in a.get('source_statuses',{}).get('sources',{}).items():
   if source not in ('ga4','gsc','inquiry') or meta.get('status') not in ('ok','partial'):continue
   request={'source':source,**dr};h=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
   p=base/'cache/genemedi-net'/source/(h+'.json')
   if not p.is_file():continue
   data=json.loads(p.read_bytes())
   for title,raw in data.items():
    if title not in SPECS:continue
    name,grain,dimension=SPECS[title]
    if grain=='interval':scope[(name,dr['start'],dr['end'])]=(p,title,len(raw))
    else:
     for row in raw:
      value=row[dimension];date=value[:4]+'-'+value[4:6]+'-'+value[6:8] if re.fullmatch(r'\d{8}',value) else value
      daily.setdefault(name,{})[date]=numeric(row,METRICS)
 def published(name,start=None,end=None):
  sql='SELECT payload FROM blocks WHERE dataset=?';args=[name]
  if start is not None:sql+=' AND start=? AND end=?';args.extend([start,end])
  for (payload,) in db.execute(sql,args):
   for row in decode_block(payload):yield row
 def compare(name,start,end,expected,actual):
  a=list(actual);e=list(expected);ok=len(a)==len(e) and digest(a)==digest(e)
  results.append({'dataset':name,'start':start,'end':end,'source_rows_preserved':len(e),'published_rows':len(a),'numeric_multiset_equal':ok})
  if not ok:raise RuntimeError('aggregate mismatch: '+name+' '+str(start)+' '+str(end))
 byname=collections.defaultdict(list)
 for key,v in scope.items():byname[key[0]].append((v[2],key,v))
 for name,items in sorted(byname.items()):
  items.sort(key=lambda x:(x[0],x[1]));samples={items[0][1]:items[0][2],items[-1][1]:items[-1][2]}
  for (name,start,end),(path,title,_) in samples.items():
   raw=json.loads(path.read_bytes())[title];expected=[numeric(row,METRICS) for row in raw]
   compare(name,start,end,expected,(numeric(r,METRICS) for r in published(name,start,end)))
 for name,rows in sorted(daily.items()):
  compare(name,'all saved daily','latest per date',({'date':d,**r} for d,r in rows.items()),({'date':r['start'],**numeric(r,METRICS)} for r in published(name)))
 # Reconstruct latest successful derived records by stable source keys, without printing keys.
 derived=collections.defaultdict(dict);runs=[]
 for path in (base/'sync-runs').glob('*/summary.json'):
  summary=json.loads(path.read_bytes())
  if summary.get('site')=='genemedi-net' and summary.get('status')=='ok':runs.append(path.parent)
 for folder in sorted(runs,key=lambda p:(re.search(r'\d{8}T\d{6}Z',p.name).group() if re.search(r'\d{8}T\d{6}Z',p.name) else '',p.name)):
  for filename,name in [('overview.json','overview_periods'),('products.json','product_periods')]:
   path=folder/filename
   if not path.is_file():continue
   for row in json.loads(path.read_bytes()):
    start,end=row['周期开始'][:10],row['周期结束'][:10];key=row.get('产品周期键',row.get('周期键'))
    derived[name][(start,end,key)]={'start':start,'end':end,**numeric(row,PERIOD)}
    cutoff=str(row.get('GA4数据截至',''))[:10]
    if name=='overview_periods' and re.fullmatch(r'\d{4}-\d{2}-\d{2}',cutoff) and cutoff>=start:
     effective=min(end,cutoff)
     derived['ga4_totals'][(start,effective,'exact_interval')]={'start':start,'end':effective,'sessions':row.get('官网访问次数'),'activeUsers':row.get('官网访客数'),'keyEvents':row.get('表单提交事件（GA4）')}
  for filename in ('v3-daily.json','product-funnel-daily.json'):
   path=folder/filename
   if not path.is_file():continue
   for name,rows in json.loads(path.read_bytes()).get('records',{}).items():
    if name not in ('overview_daily','product_daily','information_daily','product_funnel_daily','product_funnel_stage_daily'):continue
    name='product_funnel_stages_daily' if name=='product_funnel_stage_daily' else name
    for row in rows:
     key=next((row[k] for k in ('product_funnel_stage_daily_key','product_funnel_daily_key','product_daily_key','information_daily_key','daily_key') if k in row),None);day=row['data_date'][:10]
     derived[name][(day,day,key)]={'start':day,'end':day,**numeric(row,DAILY)}
 for name,expected in sorted(derived.items()):
  fields=METRICS if name=='ga4_totals' else PERIOD if name.endswith('_periods') else DAILY
  compare(name,'all saved derived','latest stable source key',expected.values(),({'start':r['start'],'end':r['end'],**numeric(r,fields)} for r in published(name)))
 sys.path.insert(0,str(pathlib.Path(__file__).parent/'api'))
 from fullsite_api import Reader
 reader=Reader(output);offset=0;seen=collections.defaultdict(set);counts={}
 while True:
  result=reader.query({'contract':'batch-service/v1','operation':'site_catalog','window_offset':offset,'window_limit':37});more=False
  for ds in result['datasets']:
   name=ds['dataset'];counts[name]=ds['window_count']
   for pair in ds['available_intervals']:
    key=(pair['start'],pair['end'])
    if key in seen[name]:raise RuntimeError('catalog duplicated interval')
    seen[name].add(key)
   more|=ds['next_window_offset'] is not None
  if not more:break
  offset+=37
 if any(len(seen[n])!=count for n,count in counts.items()):raise RuntimeError('catalog truncated or lost intervals')
 print(json.dumps({'status':'pass','comparisons':results,'catalog_dataset_count':len(counts),'catalog_interval_count':sum(counts.values()),'catalog_pagination_complete':True,'sqlite_quick_check':db.execute('PRAGMA quick_check').fetchone()[0]},ensure_ascii=False))
 db.close()
if __name__=='__main__':verify(*sys.argv[1:])
