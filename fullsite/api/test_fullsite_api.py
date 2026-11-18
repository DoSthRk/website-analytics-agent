import unittest,tempfile,sqlite3,json
from pathlib import Path
from fullsite_api import Reader,QueryError,payload_json
import zlib
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name)/'db';db=sqlite3.connect(p)
  db.executescript('CREATE TABLE metadata(key TEXT,value TEXT);CREATE TABLE datasets(name TEXT,grain TEXT,metadata TEXT);CREATE TABLE rows(dataset TEXT,start TEXT,end TEXT,row_key TEXT,payload TEXT);')
  db.executemany('INSERT INTO metadata VALUES (?,?)',[(k,json.dumps(v)) for k,v in {'contract':'fullsite-data/v1','snapshot_id':'one','published_at':'now'}.items()])
  for name,grain in [('ga4_daily','daily'),('ga4_pages','interval')]:db.execute('INSERT INTO datasets VALUES (?,?,?)',(name,grain,json.dumps({'columns':['start','end','path','totalUsers','访问（GA4） 次数'],'coverage':{'truncated':False}})))
  for day,path,users in [('2026-11-01','/AbC',7),('2026-11-03','/abc',8)]:
   row={'start':day,'end':day,'path':path,'totalUsers':users,'访问（GA4） 次数':users};db.execute('INSERT INTO rows VALUES (?,?,?,?,?)',('ga4_daily',day,day,path,json.dumps(row)))
  db.execute('INSERT INTO rows VALUES (?,?,?,?,?)',('ga4_pages','2026-11-01','2026-11-03','x',json.dumps({'start':'2026-11-01','end':'2026-11-03','path':'/AbC','totalUsers':10})))
  db.commit();db.close();self.r=Reader(p)
 def tearDown(self):self.tmp.cleanup()
 def query(self,**kw):return self.r.query(dict(contract='batch-service/v1',operation='site_query',dataset='ga4_daily',start='2026-11-01',end='2026-11-03',**kw))
 def test_daily_missing_no_uv_sum(self):
  x=self.query();self.assertEqual(x['missing_date_count'],1);self.assertEqual([r['totalUsers'] for r in x['rows']],[7,8]);self.assertFalse(x['automatic_summation'])
 def test_case_and_paging(self):
  self.assertEqual(self.query(filters={'path':'/AbC'})['total_matching_rows'],1);self.assertEqual(self.query(limit=1)['next_offset'],1);self.assertEqual(self.query(offset=1)['rows'][0]['path'],'/abc')
 def test_unicode_metric(self):
  self.assertEqual(self.query(filters={'访问（GA4） 次数':7},sort_by='访问（GA4） 次数')['total_matching_rows'],1)
 def test_catalog_windows(self):
  x=self.r.query({'contract':'batch-service/v1','operation':'site_catalog','window_limit':1});d=next(a for a in x['datasets'] if a['dataset']=='ga4_daily');self.assertTrue(d['windows_truncated']);self.assertEqual(d['next_window_offset'],1)
 def test_exact_interval(self):
  r=dict(contract='batch-service/v1',operation='site_query',dataset='ga4_pages',start='2026-11-01',end='2026-11-02');self.assertEqual(self.r.query(r)['rows'],[]);r['end']='2026-11-03';self.assertEqual(self.r.query(r)['rows'][0]['totalUsers'],10)
 def test_negative(self):
  for kw in [{'sql':'DROP TABLE rows'},{'limit':10000},{'offset':-1},{'snapshot_id':'old'},{'filters':{'secret':'x'}},{'sort_by':'payload; DELETE'},{'limit':True},{'filters':{'totalUsers':float('nan')}},{'filters':{'totalUsers':2**100}}]:
   with self.assertRaises(QueryError):self.query(**kw)
 def test_catalog(self):self.assertEqual(len(self.r.query({'contract':'batch-service/v1','operation':'site_catalog'})['datasets']),2)
 def test_empty_known_scope(self):
  db=sqlite3.connect(self.r.path);row=db.execute("SELECT metadata FROM datasets WHERE name='ga4_pages'").fetchone();meta=json.loads(row[0]);meta['coverage']['scope_inventory']=[{'start':'2026-10-01','end':'2026-10-02','state':'cached_successful_empty','source_status':'ok'}];db.execute("UPDATE datasets SET metadata=? WHERE name='ga4_pages'",(json.dumps(meta),));db.commit();db.close()
  x=self.r.query(dict(contract='batch-service/v1',operation='site_query',dataset='ga4_pages',start='2026-10-01',end='2026-10-02'));self.assertTrue(x['exact_interval_available']);self.assertTrue(x['successful_empty_cached_request']);self.assertEqual(x['rows'],[])
  catalog=self.r.query({'contract':'batch-service/v1','operation':'site_catalog'});ds=next(x for x in catalog['datasets'] if x['dataset']=='ga4_pages');self.assertEqual(ds['window_count'],2)
 def test_compressed_equivalence(self):
  before=self.query(filters={'path':'/AbC'},sort_by='totalUsers');db=sqlite3.connect(self.r.path)
  for rowid,payload in db.execute('SELECT rowid,payload FROM rows').fetchall():db.execute('UPDATE rows SET payload=? WHERE rowid=?',(zlib.compress(payload.encode()),rowid))
  db.commit();db.close();self.assertEqual(before,self.query(filters={'path':'/AbC'},sort_by='totalUsers'));self.assertEqual(len(self.query()['rows']),2)
 def test_bad_compressed(self):
  for raw in [b'invalid',zlib.compress(b'x'*1000001),zlib.compress(b'{}')+b'extra',zlib.compress(b'{}')[:-1],zlib.compress(bytes([255]))]:
   with self.assertRaises(QueryError):payload_json(raw)
 def test_stored_inventory_validated(self):
  db=sqlite3.connect(self.r.path);meta=json.loads(db.execute("SELECT metadata FROM datasets WHERE name='ga4_pages'").fetchone()[0]);meta['coverage']['available_windows']=[['2026-10-01','2026-10-02']];db.execute("UPDATE datasets SET metadata=? WHERE name='ga4_pages'",(json.dumps(meta),));db.commit()
  x=self.r.query({'contract':'batch-service/v1','operation':'site_catalog'});ds=next(d for d in x['datasets'] if d['dataset']=='ga4_pages');self.assertEqual(ds['available_intervals'],[{'start':'2026-10-01','end':'2026-10-02'}])
  meta['coverage']['available_windows']=[['bad-date','2026-10-02']];db.execute("UPDATE datasets SET metadata=? WHERE name='ga4_pages'",(json.dumps(meta),));db.commit();db.close()
  with self.assertRaises(QueryError):self.r.query({'contract':'batch-service/v1','operation':'site_catalog'})
 def test_readonly(self):
  db=self.r.load()
  with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM rows')
  db.close()
if __name__=='__main__':unittest.main()
