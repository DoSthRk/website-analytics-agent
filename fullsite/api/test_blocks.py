import unittest,json,sqlite3,zlib,time,resource
from test_fullsite_api import Tests
class Blocks(Tests):
 def setUp(self):
  super().setUp();db=sqlite3.connect(self.r.path);db.execute("UPDATE metadata SET value=? WHERE key='contract'",(json.dumps('fullsite-data/v2'),));db.execute('CREATE TABLE blocks(dataset TEXT,start TEXT,end TEXT,block_index INTEGER,payload BLOB,row_count INTEGER)')
  for dataset,start,end,key,payload in db.execute('SELECT dataset,start,end,row_key,payload FROM rows').fetchall():
   row=json.loads(payload);row['row_id']=key;db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?)',(dataset,start,end,0,b'FSB2'+zlib.compress(json.dumps([row]).encode()),1))
  for name in ('ga4_daily','ga4_pages'):
   meta=json.loads(db.execute('SELECT metadata FROM datasets WHERE name=?',(name,)).fetchone()[0]);meta['columns'].append('source_status');meta['coverage']['available_windows']=[list(x) for x in db.execute('SELECT start,end FROM rows WHERE dataset=?',(name,))];db.execute('UPDATE datasets SET metadata=? WHERE name=?',(json.dumps(meta),name))
  db.commit();db.close()
 def test_sorted_cursor(self):
  a=self.query(sort_by='totalUsers',descending=True,limit=1);self.assertEqual(a['rows'][0]['totalUsers'],8);b=self.query(sort_by='totalUsers',descending=True,limit=1,cursor=a['next_cursor']);self.assertEqual(b['rows'][0]['totalUsers'],7);self.assertIsNone(b['next_cursor'])
 def test_benchmark(self):
  db=sqlite3.connect(self.r.path);db.execute("DELETE FROM blocks WHERE dataset='ga4_pages'")
  for n in range(0,250000,500):
   rows=[{'start':'2026-11-01','end':'2026-11-03','row_id':str(i).zfill(32),'path':'/P'+str(i),'totalUsers':i,'source_status':'legacy_claim_unverified','note':'x'*512} for i in range(n,n+500)]
   db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?)',('ga4_pages','2026-11-01','2026-11-03',n//500,b'FSB2'+zlib.compress(json.dumps(rows).encode()),500))
  db.commit();db.close();req={'contract':'batch-service/v1','operation':'site_query','dataset':'ga4_pages','start':'2026-11-01','end':'2026-11-03','sort_by':'totalUsers','descending':True,'limit':200,'filters':{'source_status':'legacy_claim_unverified'}};t=time.monotonic();x=self.r.query(req);elapsed=time.monotonic()-t;self.assertEqual(x['rows'][0]['totalUsers'],249999);self.assertEqual(x['total_matching_rows'],250000);self.assertLess(elapsed,25);print('BENCH250K',elapsed,resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
# Reuse semantic tests, except tests intentionally mutating legacy physical rows.
for name in ('test_compressed_equivalence','test_bad_compressed','test_readonly'):
 setattr(Blocks,name,getattr(Tests,name))
if __name__=='__main__':unittest.main()
