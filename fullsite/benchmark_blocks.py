import pathlib,tempfile,json,hashlib,sqlite3,time,resource
from publish_fullsite import make
from fullsite_codec import decode_block
with tempfile.TemporaryDirectory() as td:
 b=pathlib.Path(td);(b/'audits').mkdir();run=b/'sync-runs/test';run.mkdir(parents=True)
 dr={'start':'2026-11-01','end':'2026-11-01'}
 a={'request':{'site':'genemedi-net','date_range':dr},'generated_at':'2026-11-02','source_statuses':{'sources':{s:{'status':'ok'} for s in ('ga4','gsc','inquiry')}}};(b/'audits/a.json').write_text(json.dumps(a))
 for source,title in [('ga4','GA4 Daily'),('gsc','GSC Daily'),('inquiry','Inquiry Daily')]:
  d={title:[{'date':'2026-11-01','sessions':1}]}
  if source=='ga4':d['GA4 Pages']=[{'landingPagePlusQueryString':f'/i/Page-{i%1000}?email=test{i}@example.com','sessions':i%9,'activeUsers':i%3} for i in range(100001)]
  p=b/'cache/genemedi-net'/source;p.mkdir(parents=True);h=hashlib.sha256(json.dumps({'source':source,**dr},sort_keys=True,separators=(',',':')).encode()).hexdigest();(p/(h+'.json')).write_text(json.dumps(d))
 (run/'summary.json').write_text(json.dumps({'status':'ok','site':'genemedi-net'}));(run/'v3-daily.json').write_text(json.dumps({'records':{n:[{'data_date':'2026-11-01','daily_key':'one','ga4_sessions':1}] for n in ('product_daily','information_daily')}}))
 start=time.monotonic();out=b/'out.sqlite';make(out,b);elapsed=time.monotonic()-start
 count=total=0;ids=set()
 with sqlite3.connect(out) as db:
  blocks=db.execute("SELECT payload,row_count FROM blocks WHERE dataset='ga4_pages' ORDER BY block_index").fetchall()
  for payload,n in blocks:
   rows=decode_block(payload);assert len(rows)==n<=500
   for r in rows:
    assert '?' not in r['landingPagePlusQueryString'];assert r['dimension_redacted'];ids.add(r['row_id']);count+=1;total+=r['sessions']
 assert count==len(ids)==100001 and total==sum(i%9 for i in range(100001))
 print(json.dumps({'rows':count,'blocks':len(blocks),'db_bytes':out.stat().st_size,'seconds':elapsed,'unique_ids':len(ids),'source_metric_checksum_preserved':True,'private_url_parameters_removed':True,'max_rss_platform_units':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
