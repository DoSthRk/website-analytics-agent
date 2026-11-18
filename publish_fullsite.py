#!/usr/bin/python3
import collections,zlib,gzip,grp,shutil,fcntl,pathlib,json,sqlite3,hashlib,datetime,os,re,urllib.parse,sys,tempfile,math
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from fullsite_codec import encode_payload,decode_payload,encode_block,BLOCK_ENCODING,MAX_BLOCK_BYTES
BASE=pathlib.Path('/opt/website-analytics/shared')
SPECS={'GA4 Daily':('ga4_daily','daily','date'),'GA4 Pages':('ga4_pages','interval','landingPagePlusQueryString'),'GSC Daily':('gsc_daily','daily','date'),'GSC Pages':('gsc_pages','interval','page'),'GSC Queries':('gsc_queries','interval','query'),'Inquiry Daily':('inquiry_daily','daily','date'),'Inquiry Pages':('inquiry_pages','interval','sourceUrl')}
METRICS=set('sessions totalUsers activeUsers engagedSessions engagementRate screenPageViews keyEvents clicks impressions ctr position storedSubmissions quarantinedSubmissions nonQuarantinedSubmissions'.split())
DAILY=set('daily_key site data_date data_status ga4_sessions ga4_key_events gsc_clicks gsc_impressions stored_submissions accepted_inquiries product_page_sessions information_page_sessions other_page_sessions classified_page_sessions page_classification_version product_mapping_version information_mapping_version refreshed_at product_daily_key product_line_id product_name category_l1 category_l2 category_l3 included_pages mapping_version information_daily_key theme_id theme content_type_id content_type'.split())
DAILY.update('product_funnel_daily_key product_funnel_stage_daily_key gsc_position_impression_sum ga4_product_landing_sessions_all_channels ga4_product_landing_engaged_sessions_all_channels ga4_product_landing_key_events_all_channels db_accepted_inquiries measurement_version ga4_organic_product_landing_sessions ga4_direct_product_arrival_sessions ga4_content_assisted_product_arrival_sessions ga4_other_assisted_product_arrival_sessions ga4_engaged_product_sessions ga4_form_start_sessions ga4_generate_lead_sessions stage_order stage_id stage_name stage_source stage_unit stage_value strict_transition_from_previous rate_from_previous rate_key'.split())
PERIOD=set('周期 周期键 产品周期键 产品大类 站点 周期开始 周期结束 数据更新时间 当前周期 统计粒度 滚动天数 GA4数据截至 GSC数据截至 询盘数据截至 是否最终值 数据状态 官网访问次数 官网访客数 表单提交事件（GA4） Google自然搜索点击 Google搜索曝光 搜索点击率 平均搜索排名 表单入库总数 官网入库询盘 纳入统计页面数 产品页访问次数 信息页访问次数 未分类页面访问次数'.split())
def metric(value):
 if value is None:return None
 if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('invalid numeric aggregate')
 return value
def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def safe(v,url=False):
 if not isinstance(v,str):return v
 if url:
  u=urllib.parse.urlsplit(v);return urllib.parse.urlunsplit((u.scheme,u.netloc.split('@')[-1],'/'.join(part if safe(urllib.parse.unquote(part))==urllib.parse.unquote(part) else '[PRIVATE_DIMENSION_REDACTED]' for part in u.path.split('/')),'',''))
 probe=urllib.parse.unquote(v)
 if '@' in probe or re.search(r'\+?\d[\d ()-]{8,}\d',probe):return '[PRIVATE_DIMENSION_REDACTED]'
 return v[:1000]
def ident(obj):return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def backup_previous(output):
 output=pathlib.Path(output)
 fd,name=tempfile.mkstemp(prefix='.previous-',suffix='.sqlite.gz',dir=output.parent);os.close(fd);backup=pathlib.Path(name)
 expected=hashlib.sha256();size=0
 try:
  with output.open('rb') as old,gzip.open(backup,'wb',compresslevel=3) as compressed:
   while True:
    chunk=old.read(1024*1024)
    if not chunk:break
    if shutil.disk_usage(output.parent).free<512*1024**2:raise RuntimeError('backup disk reserve reached')
    expected.update(chunk);size+=len(chunk);compressed.write(chunk)
  observed=hashlib.sha256();count=0
  with gzip.open(backup,'rb') as check:
   while True:
    chunk=check.read(1024*1024)
    if not chunk:break
    observed.update(chunk);count+=len(chunk)
  if observed.digest()!=expected.digest() or count!=size:raise ValueError('backup verification failed')
  with backup.open('rb') as f:os.fsync(f.fileno())
  os.chmod(backup,0o400);os.replace(backup,output.parent/'previous.sqlite.gz')
 except BaseException:
  backup.unlink(missing_ok=True);raise
def make(output,base=BASE):
 base=pathlib.Path(base)
 def read_json(p):
  if p.is_symlink() or not p.resolve().is_relative_to(base.resolve()):raise ValueError('unsafe source path')
  before=p.stat();data=p.read_bytes();after=p.stat()
  if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('source changed while reading')
  return json.loads(data)
 output=pathlib.Path(output);output.parent.mkdir(parents=True,exist_ok=True)
 def input_paths():return sorted([*(base/'audits').glob('*.json'),*(base/'cache/genemedi-net').glob('*/*.json'),*(base/'sync-runs').glob('*/summary.json'),*(base/'sync-runs').glob('*/overview.json'),*(base/'sync-runs').glob('*/products.json'),*(base/'sync-runs').glob('*/v3-daily.json'),*(base/'sync-runs').glob('*/product-funnel-daily.json')])
 inputs=input_paths()
 signature=ident(['publisher-v8-block-ambiguity',[(str(x.relative_to(base)),x.stat().st_ino,x.stat().st_size,x.stat().st_mtime_ns) for x in inputs]])
 if output.is_file():
  with sqlite3.connect(output.as_uri()+'?mode=ro',uri=True) as old:
   previous=old.execute("SELECT value FROM metadata WHERE key='input_signature'").fetchone()
   if previous and json.loads(previous[0])==signature:return {'status':'unchanged'}
 required=max(3*1024**3,int(output.stat().st_size*2.1)+512*1024**2) if output.exists() else 3*1024**3
 if shutil.disk_usage(output.parent).free < required:raise RuntimeError('insufficient staging space; prior snapshot preserved')
 fd,tmp=tempfile.mkstemp(prefix='.fullsite-',suffix='.sqlite',dir=output.parent);os.close(fd)
 db=sqlite3.connect(tmp);db.executescript('PRAGMA auto_vacuum=FULL; PRAGMA journal_mode=OFF; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE datasets(name TEXT PRIMARY KEY,grain TEXT,metadata TEXT); CREATE TABLE rows(dataset TEXT,start TEXT,end TEXT,row_key TEXT,payload TEXT,PRIMARY KEY(dataset,start,end,row_key)) WITHOUT ROWID; CREATE TABLE blocks(dataset TEXT,start TEXT,end TEXT,block_index INTEGER,payload BLOB,row_count INTEGER,PRIMARY KEY(dataset,start,end,block_index)) WITHOUT ROWID;')
 row_salt=os.urandom(32)
 duplicate_scopes={};captured={};kinds={};columns={};empty_scopes={};errors=[];cache_seen=set();accepted=0
 pending=[];block_bytes=2;block_scope=None;block_index=0
 def flush_block():
  nonlocal pending,block_bytes,block_index
  if pending:
   db.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?)',(*block_scope,block_index,sqlite3.Binary(encode_block(pending)),len(pending)))
   block_index+=1;pending=[];block_bytes=2
 def begin_scope(name,start,end):
  nonlocal block_scope,block_index
  flush_block();block_scope=(name,start,end);block_index=0
  db.execute('DELETE FROM blocks WHERE dataset=? AND start=? AND end=?',block_scope)
 def append_block(row):
  nonlocal block_bytes
  size=len(json.dumps(row,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode())+1
  if size+2>MAX_BLOCK_BYTES:raise ValueError('single row exceeds block budget')
  if pending and (len(pending)>=500 or block_bytes+size>MAX_BLOCK_BYTES):flush_block()
  pending.append(row);block_bytes+=size
 def put(name,grain,start,end,key,row,direct_block=False):
  nonlocal accepted
  if not isinstance(key,str) or not key:raise ValueError('missing stable key')
  key=hashlib.sha256(row_salt+key.encode()).digest()[:16]
  if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',start) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',end):raise ValueError('invalid date')
  if datetime.date.fromisoformat(start)>datetime.date.fromisoformat(end):raise ValueError('inverted interval')
  observed=row.get('collected_at',row.get('refreshed_at'))
  if isinstance(observed,str) and observed:captured.setdefault(name,set()).add(observed)
  row.update(start=start,end=end,row_id=key.hex(),source_status='legacy_claim_unverified',finality_basis='configured_lag_estimate_not_provider_verified'); kinds[name]=grain;columns.setdefault(name,set()).update(row)
  if direct_block:append_block(row)
  else:db.execute('INSERT OR REPLACE INTO rows VALUES(?,?,?,?,?)',(name,start,end,key,sqlite3.Binary(encode_payload(row))))
  accepted+=1
 try:
  audits=[]
  for f in (base/'audits').glob('*.json'):
   a=read_json(f);r=a.get('request',{});dr=r.get('date_range',{})
   if r.get('site')=='genemedi-net' and set(dr)>={'start','end'}:audits.append((a.get('generated_at',''),a,dr))
  for collected,a,dr in sorted(audits,key=lambda x:x[0]):
   statuses=a.get('source_statuses',{}).get('sources',{})
   for source in ('ga4','gsc','inquiry'):
    if statuses.get(source,{}).get('status') not in ('ok','partial'):continue
    request={'source':source,**dr};h=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    f=base/'cache/genemedi-net'/source/(h+'.json')
    if not f.is_file():errors.append({'source':source,'start':dr['start'],'end':dr['end']});continue
    data=read_json(f);cache_seen.add(str(f));meta=statuses[source]
    for title,rawrows in data.items():
     if title not in SPECS:continue
     name,grain,dimension=SPECS[title]
     kinds[name]=grain;columns.setdefault(name,set()).update([dimension,'start','end','row_id'])
     if grain=='interval' and not rawrows:empty_scopes.setdefault(name,{})[(dr['start'],dr['end'])]={'start':dr['start'],'end':dr['end'],'state':'cached_successful_empty','source_status':meta.get('status'),'collected_at':collected}
     if grain=='interval':
      if rawrows:empty_scopes.setdefault(name,{}).pop((dr['start'],dr['end']),None)
      begin_scope(name,dr['start'],dr['end'])
     seen_dimensions={};duplicate_count=0
     dimension_counts=collections.Counter(r.get(dimension) for r in rawrows if isinstance(r.get(dimension),str))
     for source_ordinal,raw in enumerate(rawrows):
      value=raw.get(dimension)
      if not isinstance(value,str):raise ValueError('dimension must be a string')
      raw_signature=ident(raw)
      if grain=='daily' and value in seen_dimensions:
       if seen_dimensions[value]!=raw_signature:raise ValueError('conflicting source rows for same original dimension; preserve prior')
       duplicate_count+=1;continue
      seen_dimensions[value]=raw_signature
      start,end=dr['start'],dr['end']
      if grain=='daily':
       start=end=str(value);start=end=(start[:4]+'-'+start[4:6]+'-'+start[6:8]) if re.fullmatch(r'\d{8}',start) else start
      if grain=='daily' and not dr['start']<=start<=dr['end']:raise ValueError('source date outside audit window')
      clean={k:v for k,v in raw.items() if k in METRICS and (v is None or type(v) in (int,float) and math.isfinite(v))}
      clean[dimension]=start if dimension=='date' else safe(value,url=dimension in ('page','sourceUrl','landingPagePlusQueryString'))
      if grain=='interval':clean['aggregation_safe']=False
      if grain=='interval' and dimension_counts[value]>1:clean.update(source_dimension_ambiguous=True,source_dimension_rows=dimension_counts[value],aggregation_safe=False,ambiguity_reason='Cached dimension repeats; source URL normalization may have removed distinguishing information; all source array rows retained')
      clean.update(collected_at=collected,source=source,truncated=meta.get('details',{}).get(title,{}).get('truncated',meta.get('truncated')),source_request_status=meta.get('status'),source_reported_rows=meta.get('details',{}).get(title,{}).get('rows'),provider_finality='unknown',dimension_redacted=clean[dimension]!=value)
      if clean['truncated'] is not None and type(clean['truncated']) is not bool:raise ValueError('invalid truncation metadata')
      if clean['source_reported_rows'] is not None and type(clean['source_reported_rows']) is not int:raise ValueError('invalid row metadata')
      put(name,grain,start,end,ident({dimension:value,**({'source_row':source_ordinal} if grain=='interval' else {})}),clean,direct_block=grain=='interval')
     if grain=='interval':
      flush_block()
      if any(n>1 for n in dimension_counts.values()):duplicate_scopes.setdefault(name,{})[(dr['start'],dr['end'])]={'start':dr['start'],'end':dr['end'],'repeated_cached_dimensions':sum(n>1 for n in dimension_counts.values()),'affected_source_rows':sum(n for n in dimension_counts.values() if n>1),'policy':'Every interval source array row retained, including identical records; pre-cache URL normalization can remove distinctions; do not aggregate ambiguous rows'}
      else:duplicate_scopes.setdefault(name,{}).pop((dr['start'],dr['end']),None)
    if shutil.disk_usage(output.parent).free<512*1024**2:raise RuntimeError('build disk reserve reached')
    db.commit()
  runs=[]
  for f in (base/'sync-runs').glob('*/summary.json'):
   s=read_json(f)
   if s.get('status')=='ok' and s.get('site')=='genemedi-net':runs.append(f.parent)
  for folder in sorted(runs,key=lambda p: (re.search(r'\d{8}T\d{6}Z',p.name).group() if re.search(r'\d{8}T\d{6}Z',p.name) else '',p.name)):
   for file,name in [('overview.json','overview_periods'),('products.json','product_periods')]:
    p=folder/file
    if not p.is_file():continue
    for raw in read_json(p):
     row={k:v for k,v in raw.items() if k in PERIOD and isinstance(v,(str,int,float,bool,type(None)))}
     for k in ('滚动天数','官网访问次数','官网访客数','表单提交事件（GA4）','Google自然搜索点击','Google搜索曝光','搜索点击率','平均搜索排名','表单入库总数','官网入库询盘','纳入统计页面数','产品页访问次数','信息页访问次数','未分类页面访问次数'):
      if k in raw:row[k]=metric(raw[k])
     row['collected_at']=str(raw.get('数据更新时间',''));row['provider_finality']='unknown'
     key=raw.get('产品周期键',raw.get('周期键'))
     if not isinstance(key,str) or not key:raise ValueError('missing period key')
     put(name,'interval',raw['周期开始'][:10],raw['周期结束'][:10],ident(key),row)
     if name=='overview_periods':
      cutoff=str(raw.get('GA4数据截至',''))[:10]
      if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',cutoff) or cutoff<raw['周期开始'][:10]:continue
      datetime.date.fromisoformat(cutoff)
      put('ga4_totals','interval',raw['周期开始'][:10],min(raw['周期结束'][:10],str(raw.get('GA4数据截至',raw['周期结束']))[:10]),'exact_interval',{'intended_start':raw['周期开始'][:10],'intended_end':raw['周期结束'][:10],'source_available_through':str(raw.get('GA4数据截至',''))[:10],'sessions':metric(raw.get('官网访问次数')),'activeUsers':metric(raw.get('官网访客数')),'metric_note':'legacy 官网访客数 is GA4 activeUsers; totalUsers unavailable here','keyEvents':metric(raw.get('表单提交事件（GA4）')),'collected_at':row['collected_at'],'provider_finality':'unknown'})
   for filename in ('v3-daily.json','product-funnel-daily.json'):
    p=folder/filename
    if p.is_file():
     records=read_json(p).get('records',{})
     for name in ('overview_daily','product_daily','information_daily','product_funnel_daily','product_funnel_stage_daily'):
      for raw in records.get(name,[]):
       row={k:v for k,v in raw.items() if k in DAILY and isinstance(v,(str,int,float,bool,type(None)))}
       for k in raw:
        if k in DAILY and (k.startswith(('ga4_','gsc_')) or k in ('stored_submissions','accepted_inquiries','product_page_sessions','information_page_sessions','other_page_sessions','classified_page_sessions','included_pages','db_accepted_inquiries','stage_order','stage_value','rate_from_previous')):row[k]=metric(raw[k])
       key=next((raw[k] for k in ('product_funnel_stage_daily_key','product_funnel_daily_key','product_daily_key','information_daily_key','daily_key') if k in raw),None)
       if not isinstance(key,str) or not key:raise ValueError('missing daily key')
       put('product_funnel_stages_daily' if name=='product_funnel_stage_daily' else name,'daily',raw['data_date'][:10],raw['data_date'][:10],ident(key),row)
   db.commit()
  if not accepted or not all(n in kinds for n in ('ga4_daily','gsc_daily','inquiry_daily','product_daily','information_daily')):raise ValueError('required datasets missing')
  flush_block()
  last=None
  for name,start,end,key,payload in db.execute('SELECT dataset,start,end,row_key,payload FROM rows ORDER BY dataset,start,end,row_key'):
   scope=(name,start,end)
   if scope!=last:begin_scope(*scope);last=scope
   row=decode_payload(payload);row.update(start=start,end=end,row_id=key.hex());append_block(row)
  flush_block();db.execute('DROP TABLE rows');db.commit()
  if output.exists():
   with sqlite3.connect(output.as_uri()+'?mode=ro',uri=True) as prior:
    for dataset,start,end in prior.execute('SELECT DISTINCT dataset,start,end FROM blocks'):
     if not db.execute('SELECT 1 FROM blocks WHERE dataset=? AND start=? AND end=? LIMIT 1',(dataset,start,end)).fetchone():raise ValueError('previously published window missing; preserve prior')
  for name,grain in kinds.items():
   count,lo,hi=db.execute('SELECT COALESCE(sum(row_count),0),min(start),max(end) FROM blocks WHERE dataset=?',(name,)).fetchone()
   windows=db.execute('SELECT DISTINCT start,end FROM blocks WHERE dataset=? ORDER BY start,end',(name,)).fetchall()
   metadata={'columns':sorted(columns[name]),'coverage':{'min_date':lo,'max_date':hi,'row_count':count,'available_windows':windows,'scope_inventory':list(empty_scopes.get(name,{}).values()),'duplicate_dimension_scopes':list(duplicate_scopes.get(name,{}).values()),'source_timestamp_min_raw':min(captured.get(name,[]) or ['unknown']),'source_timestamp_max_raw':max(captured.get(name,[]) or ['unknown']),'timestamp_note':'Original producer timestamps; offsets absent in some legacy records; publication time is not source collection time.','completeness':'unknown','note':'Windows are observed cached request scopes, not proof of gap-free or provider-final data.'},'privacy':'URL query strings/fragments removed; personal-looking search dimensions redacted; rows remain distinct and must not be merged for unique users','aggregation':'No summing overlapping intervals or users. Counts retain legacy source meaning; nonQuarantinedSubmissions is not qualified leads.'}
   db.execute('INSERT INTO datasets VALUES(?,?,?)',(name,grain,json.dumps(metadata,ensure_ascii=False)))
  info={'payload_encoding':BLOCK_ENCODING,'input_signature':signature,'contract':'fullsite-data/v2','published_at':stamp(),'snapshot_id':hashlib.sha256(os.urandom(32)).hexdigest(),'source_cache_files':len(cache_seen),'source_successful_runs':len(runs),'latest_successful_sync':max((re.search(r'\d{8}T\d{6}Z',p.name).group() for p in runs if re.search(r'\d{8}T\d{6}Z',p.name)),default=None),'latest_sync_timestamp_basis':'successful saved run identifier; not a new source request','unmapped_cache_references':len(errors),'missing_source_windows':errors,'source_finality':'unknown; historical producer completeness claims are not independently verified','inquiry_day_timezone':'unknown','url_path_case_preserved':True}
  for k,v in info.items():db.execute('INSERT INTO metadata VALUES(?,?)',(k,json.dumps(v)))
  if signature != ident(['publisher-v8-block-ambiguity',[(str(x.relative_to(base)),x.stat().st_ino,x.stat().st_size,x.stat().st_mtime_ns) for x in input_paths()]]):raise ValueError('source changed during import; preserve previous')
  db.commit();assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok';db.close();os.chmod(tmp,0o440)
  if os.getuid()==0:os.chown(tmp,0,grp.getgrnam('analyticsquery').gr_gid)
  if output.exists():
   backup_previous(output)
  os.replace(tmp,output);return info
 except BaseException:
  db.close();os.unlink(tmp);raise
if __name__=='__main__':
 if len(sys.argv)!=2:raise SystemExit('one fixed output path required')
 with open(str(sys.argv[1])+'.lock','w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  for pattern in ('.fullsite-*.sqlite','.previous-*.sqlite','.previous-*.sqlite.gz'):
   for stale in pathlib.Path(sys.argv[1]).parent.glob(pattern):
    if stale.is_file() and not stale.is_symlink():stale.unlink()
  print(json.dumps(make(sys.argv[1])))
