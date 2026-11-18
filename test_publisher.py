import zlib,unittest,tempfile,pathlib,json,hashlib,sqlite3
from publish_fullsite import make,safe
from fullsite_codec import decode_block
class PublisherTests(unittest.TestCase):
 def test_privacy(self):
  self.assertEqual(safe('https://user:pass@host/AbC?email=a@b.com#secret',True),'https://host/AbC')
  self.assertEqual(safe('find alice@example.com'),'[PRIVATE_DIMENSION_REDACTED]')
 def test_encoded_path_privacy(self):
  self.assertEqual(safe('https://host/AbC/alice%40example.com',True),'https://host/AbC/[PRIVATE_DIMENSION_REDACTED]')
 def test_encoded_query_privacy(self):
  self.assertEqual(safe('find%20alice%40example.com'),'[PRIVATE_DIMENSION_REDACTED]')
  self.assertEqual(safe('Safe%20Product'),'Safe%20Product')
 def test_publish_and_fail_preserves(self):
  with tempfile.TemporaryDirectory() as t:
   b=pathlib.Path(t);(b/'audits').mkdir();run=b/'sync-runs/a';run.mkdir(parents=True)
   dr={'start':'2026-11-01','end':'2026-11-01'}
   a={'request':{'site':'genemedi-net','date_range':dr},'generated_at':'2026-11-02','source_statuses':{'sources':{s:{'status':'ok'} for s in ['ga4','gsc','inquiry']}}};(b/'audits/a.json').write_text(json.dumps(a))
   for src,title in [('ga4','GA4 Daily'),('gsc','GSC Daily'),('inquiry','Inquiry Daily')]:
    p=b/'cache/genemedi-net'/src;p.mkdir(parents=True);h=hashlib.sha256(json.dumps({'source':src,**dr},sort_keys=True,separators=(',',':')).encode()).hexdigest();(p/(h+'.json')).write_text(json.dumps({title:[{'date':'2026-11-01','sessions':1,'email':'NEVER PUBLISH'}]}))
   (run/'summary.json').write_text(json.dumps({'status':'ok','site':'genemedi-net'}));(run/'v3-daily.json').write_text(json.dumps({'records':{n:[{'data_date':'2026-11-01','daily_key':'one','ga4_sessions':1}] for n in ['product_daily','information_daily']}}))
   (run/'overview.json').write_text(json.dumps([{'周期键':'year','周期开始':'2026-01-01','周期结束':'2026-12-31','GA4数据截至':'2026-11-01','官网访客数':7,'官网访问次数':9}]))
   out=b/'out.sqlite';make(out,b)
   with sqlite3.connect(out) as db:
    end,payload=db.execute("SELECT end,payload FROM blocks WHERE dataset='ga4_totals'").fetchone();r=decode_block(payload)[0];self.assertEqual(end,'2026-11-01');self.assertEqual(r['activeUsers'],7);self.assertNotIn('totalUsers',r)
   with sqlite3.connect(out) as db:
    r=decode_block(db.execute("SELECT payload FROM blocks WHERE dataset='ga4_daily'").fetchone()[0])[0];self.assertEqual(r['date'],'2026-11-01')
   raw=out.read_bytes();self.assertNotIn(b'NEVER PUBLISH',raw);self.assertEqual(make(out,b),{'status':'unchanged'})
   (b/'audits/a.json').write_text('broken')
   with self.assertRaises(Exception):make(out,b)
   self.assertEqual(out.read_bytes(),raw)
   (b/'audits/a.json').write_text(json.dumps(a))
   ga=next((b/'cache/genemedi-net/ga4').glob('*.json'));ga.write_text(json.dumps({'GA4 Daily':[{'date':{'email':'alice@example.com'},'sessions':1}]}))
   with self.assertRaises(ValueError):make(out,b)
   self.assertEqual(out.read_bytes(),raw)
if __name__=='__main__':unittest.main()
