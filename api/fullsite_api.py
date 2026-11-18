"""Read-only structured querying of an operator-published sanitized dataset."""
import datetime,json,math,zlib
from pathlib import Path
DATASETS=frozenset('ga4_daily ga4_pages ga4_totals gsc_daily gsc_pages gsc_queries inquiry_daily inquiry_pages overview_daily product_daily information_daily overview_periods product_periods product_funnel_daily product_funnel_stages_daily page_catalog'.split())
class QueryError(ValueError):pass
def date(v):
 if not isinstance(v,str) or len(v)!=10:raise QueryError('invalid date')
 try:return datetime.date.fromisoformat(v).isoformat()
 except ValueError:raise QueryError('invalid date')
def payload_json(value):
 if isinstance(value,str):
  if len(value.encode('utf-8'))>1000000:raise QueryError('payload too large')
  return value
 if not isinstance(value,bytes):raise QueryError('invalid payload type')
 decoder=zlib.decompressobj()
 try:
  raw=decoder.decompress(value,1000001)
  if len(raw)>1000000 or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:raise QueryError('invalid or oversized compressed payload')
  return raw.decode('utf-8')
 except (zlib.error,UnicodeError) as e:raise QueryError('invalid compressed payload') from e
class LegacyReader:
 def __init__(self,path):self.path=Path(path)
 def load(self):
  import sqlite3
  db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)
  db.execute('PRAGMA query_only=ON')
  db.create_function('payload_json',1,payload_json,deterministic=True)
  return db
 def query(self,r):
  if not isinstance(r,dict) or r.get('contract')!='batch-service/v1':raise QueryError('invalid contract')
  op=r.get('operation');allowed={'contract','operation','window_offset','window_limit'} if op=='site_catalog' else {'contract','operation','dataset','start','end','filters','sort_by','descending','limit','offset','snapshot_id'}
  if op not in ('site_catalog','site_query') or set(r)-allowed:raise QueryError('invalid fields')
  db=self.load()
  try:
   meta={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM metadata')}
   sha=meta.get('snapshot_id',meta.get('published_at'))
   if meta.get('contract')!='fullsite-data/v1':raise QueryError('invalid snapshot')
   out={'contract':'batch-service/v1','operation':op,'status':'partial','snapshot_id':sha,'published_at':meta.get('published_at'),'saved_data_only':True,'automatic_summation':False,'source_finality':meta.get('source_finality','unknown'),'inquiry_day_timezone':meta.get('inquiry_day_timezone','unknown'),'legacy_completeness_notice':'Original row final/complete labels are producer claims, not independent source-finality verification.'}
   datasets={n:dict(json.loads(m),grain=g) for n,g,m in db.execute('SELECT name,grain,metadata FROM datasets')}
   if not set(datasets)<=DATASETS:raise QueryError('invalid datasets')
   if op=='site_catalog':
    offset=r.get('window_offset',0);limit=r.get('window_limit',100)
    if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=200:raise QueryError('invalid window pagination')
    out['source_inventory']={k:meta.get(k) for k in ('unmapped_cache_references','source_finality','inquiry_day_timezone')}
    missing=meta.get('missing_source_windows',[])
    out['source_inventory'].update(missing_source_window_count=len(missing),missing_source_windows=missing[offset:offset+limit],next_missing_window_offset=offset+limit if offset+limit<len(missing) else None)
    out['datasets']=[]
    for n,v in sorted(datasets.items()):
     coverage={k:x for k,x in v.get('coverage',{}).items() if k not in ('available_windows','available_intervals','observed_dates','scope_inventory')}
     stored=v.get('coverage',{}).get('available_windows')
     if stored is None:pairs={(a,b) for a,b in db.execute('SELECT DISTINCT start,end FROM rows WHERE dataset=?',(n,))}
     else:
      if not isinstance(stored,list) or len(stored)>100000:raise QueryError('invalid window inventory')
      pairs=set()
      for pair in stored:
       if not isinstance(pair,(list,tuple)) or len(pair)!=2:raise QueryError('invalid window inventory')
       a,b=map(date,pair)
       if b<a:raise QueryError('invalid window inventory')
       pairs.add((a,b))
     empty=v.get('coverage',{}).get('scope_inventory',[])
     if not isinstance(empty,list) or len(empty)>100000:raise QueryError('invalid empty inventory')
     for x in empty:
      if not isinstance(x,dict):raise QueryError('invalid empty inventory')
      if x.get('state')=='cached_successful_empty':
       a,b=date(x.get('start')),date(x.get('end'))
       if b<a:raise QueryError('invalid empty inventory')
       pairs.add((a,b))
     count=len(pairs);windows=[{'start':a,'end':b} for a,b in sorted(pairs)[offset:offset+limit+1]]
     item={k:x for k,x in v.items() if k not in ('coverage','available_windows','available_intervals')}
     item.update(dataset=n,coverage=coverage,available_intervals=windows[:limit],window_count=count,window_offset=offset,windows_truncated=len(windows)>limit,next_window_offset=offset+limit if len(windows)>limit else None)
     out['datasets'].append(item)
    return out
   name=r.get('dataset')
   if name not in datasets:raise QueryError('unknown dataset')
   ds=datasets[name];start=date(r.get('start'));end=date(r.get('end'))
   if end<start:raise QueryError('invalid interval')
   if r.get('snapshot_id',sha)!=sha:raise QueryError('snapshot changed')
   cols=ds.get('columns',[]);filters=r.get('filters',{})
   if not isinstance(filters,dict) or len(filters)>8 or not set(filters)<=set(cols):raise QueryError('invalid filters')
   limit=r.get('limit',100);offset=r.get('offset',0);sort=r.get('sort_by');desc=r.get('descending',False)
   if type(limit)is not int or not 1<=limit<=200 or type(offset)is not int or not 0<=offset<=10000000 or type(desc)is not bool:raise QueryError('invalid pagination')
   if sort is not None and sort not in cols:raise QueryError('invalid sort')
   empty_scope=next((x for x in ds.get('coverage',{}).get('scope_inventory',[]) if x.get('start')==start and x.get('end')==end and x.get('state')=='cached_successful_empty'),None)
   daily=ds['grain']=='daily';where='dataset=? AND start>=? AND start<=? AND end=start' if daily else 'dataset=? AND start=? AND end=?';params=[name,start,end]
   for k,v in filters.items():
    if type(v) not in (str,int,float,bool) or type(v)is int and not -(2**63)<=v<2**63 or isinstance(v,float) and not math.isfinite(v) or isinstance(v,str) and len(v)>512 or not isinstance(k,str):raise QueryError('invalid filter')
    where+=' AND json_extract(payload_json(payload),?)=?';params+=['$.'+json.dumps(k,ensure_ascii=False),v]
   total=db.execute('SELECT count(*) FROM rows WHERE '+where,params).fetchone()[0]
   sortsql='start,end,row_key';sortparams=[]
   if sort:
    if not isinstance(sort,str):raise QueryError('invalid sort')
    sortsql='json_extract(payload_json(payload),?)';sortparams=['$.'+json.dumps(sort,ensure_ascii=False)]
   selected=db.execute('SELECT payload FROM rows WHERE '+where+' ORDER BY '+sortsql+(' DESC' if desc else ' ASC')+',start,end,row_key LIMIT ? OFFSET ?',params+sortparams+[limit,offset])
   rows=[];size=0
   for (raw,) in selected:
    raw=payload_json(raw)
    if len(raw.encode())>750000:raise QueryError('row too large')
    if size+len(raw.encode())>750000:break
    rows.append(json.loads(raw));size+=len(raw.encode())
   dates=[x[0] for x in db.execute('SELECT DISTINCT start FROM rows WHERE dataset=? AND start>=? AND start<=? AND end=start ORDER BY start',(name,start,end))] if daily else []
   missing=(datetime.date.fromisoformat(end)-datetime.date.fromisoformat(start)).days+1-len(dates) if daily else None
   out.update(dataset=name,grain=ds['grain'],requested_interval={'start':start,'end':end},selection='daily_rows_not_summed' if daily else 'exact_interval_only',coverage={k:v for k,v in ds.get('coverage',{}).items() if k not in ('available_windows','available_intervals','observed_dates','scope_inventory')},observed_dates=dates,missing_date_count=missing,missing_dates_are_not_zero=True,total_matching_rows=total,offset=offset,rows=rows,next_offset=offset+len(rows) if offset+len(rows)<total else None,cached_empty_request=empty_scope,successful_empty_cached_request=bool(empty_scope and empty_scope.get('source_status')=='ok'),exact_interval_available=bool(empty_scope or db.execute('SELECT 1 FROM rows WHERE dataset=? AND start=? AND end=? LIMIT 1',(name,start,end)).fetchone()) if not daily else None,non_additive_notice='Users and overlapping intervals must not be summed; ratios are not averaged. Missing rows are not zeroes.')
   return out
  finally:db.close()

class Reader(LegacyReader):
 def query(self,r):
  import base64,hashlib,heapq
  db=self.load()
  try:
   meta={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM metadata')}
   if meta.get('contract')!='fullsite-data/v2':return super().query(r)
   if not isinstance(r,dict) or r.get('contract')!='batch-service/v1':raise QueryError('invalid contract')
   op=r.get('operation');allowed={'contract','operation','window_offset','window_limit'} if op=='site_catalog' else {'contract','operation','dataset','start','end','filters','sort_by','descending','limit','offset','snapshot_id','cursor'}
   if op not in ('site_catalog','site_query') or set(r)-allowed:raise QueryError('invalid fields')
   datasets={n:dict(json.loads(m),grain=g) for n,g,m in db.execute('SELECT name,grain,metadata FROM datasets')}
   if not set(datasets)<=DATASETS:raise QueryError('invalid datasets')
   sha=meta.get('snapshot_id',meta.get('published_at'))
   out={'contract':'batch-service/v1','operation':op,'status':'partial','snapshot_id':sha,'published_at':meta.get('published_at'),'saved_data_only':True,'automatic_summation':False,'source_finality':meta.get('source_finality','unknown'),'inquiry_day_timezone':meta.get('inquiry_day_timezone','unknown'),'legacy_completeness_notice':'Legacy complete labels are producer claims, not verified finality.'}
   def windows(ds):
    raw=ds.get('coverage',{}).get('available_windows',[])
    if not isinstance(raw,list) or len(raw)>100000:raise QueryError('bad windows')
    result=set()
    for pair in raw:
     if not isinstance(pair,list) or len(pair)!=2:raise QueryError('bad window')
     a,b=map(date,pair)
     if b<a:raise QueryError('bad window')
     result.add((a,b))
    for x in ds.get('coverage',{}).get('scope_inventory',[]):
     if x.get('state')=='cached_successful_empty':result.add((date(x['start']),date(x['end'])))
    return sorted(result)
   def coverage(ds):
    c={k:v for k,v in ds.get('coverage',{}).items() if k not in ('available_windows','scope_inventory','observed_dates','duplicate_dimension_scopes')}
    c['duplicate_dimension_scope_count']=len(ds.get('coverage',{}).get('duplicate_dimension_scopes',[]));return c
   if op=='site_catalog':
    offset=r.get('window_offset',0);limit=r.get('window_limit',100)
    if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=200:raise QueryError('bad pagination')
    out['datasets']=[]
    for n,ds in sorted(datasets.items()):
     ws=windows(ds);item={k:v for k,v in ds.items() if k not in ('coverage','available_windows')};item.update(dataset=n,coverage=coverage(ds),available_intervals=[{'start':a,'end':b} for a,b in ws[offset:offset+limit]],window_count=len(ws),window_offset=offset,windows_truncated=offset+limit<len(ws),next_window_offset=offset+limit if offset+limit<len(ws) else None);out['datasets'].append(item)
    missing=meta.get('missing_source_windows',[]);out['source_inventory']={k:meta.get(k) for k in ('unmapped_cache_references','source_finality','inquiry_day_timezone')};out['source_inventory'].update(missing_source_window_count=len(missing),missing_source_windows=missing[offset:offset+limit],next_missing_window_offset=offset+limit if offset+limit<len(missing) else None);return out
   name=r.get('dataset')
   if name not in datasets:raise QueryError('unknown dataset')
   ds=datasets[name];start=date(r.get('start'));end=date(r.get('end'));limit=r.get('limit',100);offset=r.get('offset',0);sort=r.get('sort_by');desc=r.get('descending',False);filters=r.get('filters',{});cursor=r.get('cursor')
   if end<start or type(limit)is not int or not 1<=limit<=200 or type(offset)is not int or offset<0 or type(desc)is not bool:raise QueryError('bad range/pagination')
   if not isinstance(filters,dict) or len(filters)>8 or not set(filters)<=set(ds.get('columns',[])):raise QueryError('bad filters')
   for v in filters.values():
    if type(v)not in (str,int,float,bool) or type(v)is int and not -(2**63)<=v<2**63 or isinstance(v,float) and not math.isfinite(v) or isinstance(v,str) and len(v)>512:raise QueryError('bad filter')
   if sort is not None and (not isinstance(sort,str) or sort not in ds.get('columns',[])):raise QueryError('bad sort')
   if r.get('snapshot_id',sha)!=sha:raise QueryError('snapshot changed')
   if sort and offset:raise QueryError('sorted queries use cursor not offset')
   if cursor and not sort:raise QueryError('cursor requires sort')
   fingerprint=hashlib.sha256(json.dumps([name,start,end,filters,sort,desc],sort_keys=True).encode()).hexdigest()
   last=None
   if cursor:
    if not isinstance(cursor,str) or len(cursor)>8192:raise QueryError('bad cursor')
    try:c=json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:raise QueryError('bad cursor')
    if not isinstance(c,dict) or set(c)!={'snapshot_id','request','key'} or c.get('snapshot_id')!=sha or c.get('request')!=fingerprint or not isinstance(c.get('key'),list) or len(c['key'])!=5:raise QueryError('cursor mismatch')
    k=c['key']
    if type(k[0])is not int or k[0]not in (0,1,2,3) or any(not isinstance(v,str) for v in k[2:]) or len(k[4])>512:raise QueryError('bad cursor key')
    if (k[0]in(0,3) and not isinstance(k[1],str)) or (k[0]==1 and type(k[1])is not bool) or (k[0]==2 and (type(k[1])not in(int,float) or type(k[1])is int and not -(2**63)<=k[1]<2**63 or type(k[1])is float and not math.isfinite(k[1]))):raise QueryError('bad cursor value')
    date(k[2]);date(k[3]);last=tuple(k)
   daily=ds['grain']=='daily';where='dataset=? AND start>=? AND start<=? AND end=start' if daily else 'dataset=? AND start=? AND end=?'
   total=0;observed=set()
   def key(row):
    v=row.get(sort)
    kind=0 if v is None else 1 if isinstance(v,bool) else 2 if isinstance(v,(int,float)) else 3
    return (kind,'' if v is None else v if kind in (1,2) else str(v),row['start'],row['end'],str(row.get('row_id','')))
   def records():
    nonlocal total
    for a,b,raw in db.execute('SELECT start,end,payload FROM blocks WHERE '+where+' ORDER BY start,end,block_index',(name,start,end)):
     raw=bytes(raw)
     if not raw.startswith(b'FSB2'):raise QueryError('invalid block encoding')
     raw=raw[4:]
     decoder=zlib.decompressobj()
     try:data=decoder.decompress(raw,8388609)
     except zlib.error as e:raise QueryError('bad compressed block') from e
     if len(data)>8388608 or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:raise QueryError('bad block')
     try:rows=json.loads(data)
     except (UnicodeError,ValueError,RecursionError) as e:raise QueryError('bad block JSON') from e
     if not isinstance(rows,list) or not 1<=len(rows)<=500:raise QueryError('bad block rows')
     if daily:observed.add(a)
     for row in rows:
      if not isinstance(row,dict):raise QueryError('bad row')
      row['start']=a;row['end']=b
      if all(type(row.get(k))is type(v) and row.get(k)==v or type(row.get(k))in(int,float) and type(v)in(int,float) and row.get(k)==v for k,v in filters.items()):
       total+=1
       if sort:
        k=key(row)
        if last is not None and not (k<last if desc else k>last):continue
        yield (k,row)
       else:yield row
   if sort:
    # Unique source row_id is the deterministic tie breaker; bounded top K.
    chosen=(heapq.nlargest if desc else heapq.nsmallest)(limit+1,records(),key=lambda x:x[0]);more=len(chosen)>limit;chosen=chosen[:limit];rows=[x[1] for x in chosen];next_cursor=base64.urlsafe_b64encode(json.dumps({'snapshot_id':sha,'request':fingerprint,'key':chosen[-1][0]},ensure_ascii=False).encode()).decode() if more else None
   else:
    rows=[]
    for i,row in enumerate(records()):
     if offset<=i<offset+limit:rows.append(row)
    more=offset+len(rows)<total;next_cursor=None
   # Bound wire size without silently skipping selected rows.
   bounded=[];size=0
   for row in rows:
    n=len(json.dumps(row,ensure_ascii=False).encode())
    if n>750000:raise QueryError('row too large')
    if size+n>750000:break
    bounded.append(row);size+=n
   if sort and bounded and (more or len(bounded)<len(rows)):next_cursor=base64.urlsafe_b64encode(json.dumps({'snapshot_id':sha,'request':fingerprint,'key':key(bounded[-1])},ensure_ascii=False).encode()).decode()
   warnings=[x for x in ds.get('coverage',{}).get('duplicate_dimension_scopes',[]) if isinstance(x,dict) and x.get('start')==start and x.get('end')==end]
   out['selected_scope_warnings']=warnings[:100];out['selected_scope_warning_count']=len(warnings);out['selected_scope_warnings_truncated']=len(warnings)>100
   rows=bounded;empty=next((x for x in ds.get('coverage',{}).get('scope_inventory',[]) if x.get('start')==start and x.get('end')==end),None)
   out.update(dataset=name,grain=ds['grain'],requested_interval={'start':start,'end':end},selection='daily_rows_not_summed' if daily else 'exact_interval_only',coverage=coverage(ds),observed_dates=sorted(observed),missing_date_count=(datetime.date.fromisoformat(end)-datetime.date.fromisoformat(start)).days+1-len(observed) if daily else None,missing_dates_are_not_zero=True,total_matching_rows=total,rows=rows,offset=offset,next_offset=offset+len(rows) if not sort and offset+len(rows)<total else None,next_cursor=next_cursor,exact_interval_available=(start,end)in windows(ds) if not daily else None,cached_empty_request=empty,successful_empty_cached_request=bool(empty and empty.get('source_status')=='ok'),non_additive_notice='Do not sum users or overlapping intervals; empty rows are not zero metrics.')
   return out
  finally:db.close()

def install(path):
 """Preserve frozen service dispatch and HTTP handler; add only two operations."""
 from website_analytics.analysis import service
 from website_analytics.analysis.contracts import AnalysisError
 reader=Reader(path);reader.load().close();old=service.dispatch
 def dispatch(store,request,cases=None,refreshes=None,refresh_status=None):
  if isinstance(request,dict) and request.get('operation') in ('site_catalog','site_query'):
   try:return reader.query(request)
   except (QueryError,KeyError,TypeError,json.JSONDecodeError) as e:raise AnalysisError('invalid fullsite request') from e
  return old(store,request,cases,refreshes,refresh_status)
 service.dispatch=dispatch
 service.CASE_OPERATIONS=tuple(service.CASE_OPERATIONS)+('site_catalog','site_query')
 return reader
