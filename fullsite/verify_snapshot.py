"""Read-only real-source checksum validation. Prints counts/hashes only."""
import sys,pathlib,json,sqlite3,hashlib
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from fullsite_codec import decode_block
from publish_fullsite import safe,METRICS
p=pathlib.Path(sys.argv[1]);db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
result={'bytes':p.stat().st_size,'quick_check':db.execute('PRAGMA quick_check').fetchone()[0],'datasets':[],'source_checks':[]}
for name,grain,meta in db.execute('SELECT name,grain,metadata FROM datasets ORDER BY name'):
 m=json.loads(meta);actual=db.execute('SELECT COALESCE(sum(row_count),0),count(*) FROM blocks WHERE dataset=?',(name,)).fetchone();assert actual[0]==m['coverage']['row_count'];result['datasets'].append({'name':name,'rows':actual[0],'blocks':actual[1],'min':m['coverage']['min_date'],'max':m['coverage']['max_date']})
for dataset,source,title,dim in [('ga4_pages','ga4','GA4 Pages','landingPagePlusQueryString'),('gsc_queries','gsc','GSC Queries','query'),('inquiry_pages','inquiry','Inquiry Pages','sourceUrl')]:
 scope=db.execute('SELECT start,end FROM blocks WHERE dataset=? ORDER BY start DESC,end DESC LIMIT 1',(dataset,)).fetchone()
 if not scope:continue
 start,end=scope;h=hashlib.sha256(json.dumps({'source':source,'start':start,'end':end},sort_keys=True,separators=(',',':')).encode()).hexdigest();f=pathlib.Path('/opt/website-analytics/shared/cache/genemedi-net')/source/(h+'.json')
 raw=json.loads(f.read_text())[title];seen=set();expected=[]
 for r in raw:
  sig=json.dumps(r,sort_keys=True,ensure_ascii=False)
  seen.add(sig);n={k:v for k,v in r.items() if k in METRICS};n[dim]=safe(r[dim],url=dim in ('landingPagePlusQueryString','sourceUrl','page'));expected.append(n)
 actual=[]
 for payload, in db.execute('SELECT payload FROM blocks WHERE dataset=? AND start=? AND end=? ORDER BY block_index',(dataset,start,end)):
  for r in decode_block(payload):actual.append({k:v for k,v in r.items() if k in METRICS or k==dim})
 def digest(rows):return hashlib.sha256('\n'.join(sorted(json.dumps(r,sort_keys=True,ensure_ascii=False,separators=(',',':')) for r in rows)).encode()).hexdigest()
 assert len(actual)==len(expected) and digest(actual)==digest(expected),dataset
 result['source_checks'].append({'dataset':dataset,'start':start,'end':end,'rows':len(actual),'normalized_dimension_metric_sha256':digest(actual),'source_equal':True})
result['metadata']={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM metadata') if k in ('contract','published_at','snapshot_id','payload_encoding','latest_successful_sync','source_successful_runs','source_cache_files','unmapped_cache_references')}
print(json.dumps(result,indent=2));db.close()
