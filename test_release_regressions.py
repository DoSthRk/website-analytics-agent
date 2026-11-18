"""Independent release regressions, synthetic records only."""
import copy
import gzip
import zlib
import io
from unittest import mock
from types import SimpleNamespace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from publish_fullsite import make, safe, backup_previous
sys.path.insert(0, str(Path(__file__).parent / 'api'))
from fullsite_api import Reader, QueryError
from fullsite_codec import decode_block, encode_block
import base64

class Fixture:
    def __init__(self, root):
        self.base=Path(root)
        self.out=self.base/'published'/'fullsite.sqlite'
        (self.base/'audits').mkdir()
        self.run=self.base/'sync-runs'/'genemedi-net-full-20261102T010000Z'
        self.run.mkdir(parents=True)
        self.range={'start':'2026-11-01','end':'2026-11-01'}
        self.caches={}
        self.raw={
          'ga4':{'GA4 Daily':[{'date':'2026-11-01','sessions':9,'activeUsers':7,'totalUsers':8}],
                 'GA4 Pages':[{'landingPagePlusQueryString':'/AbC%2FDeF?email=test@example.invalid','sessions':9,'activeUsers':7,'totalUsers':8}]},
          'gsc':{'GSC Daily':[{'date':'2026-11-01','clicks':2,'impressions':10}],
                 'GSC Queries':[{'query':'synthetic example','clicks':2,'impressions':10}]},
          'inquiry':{'Inquiry Daily':[{'date':'2026-11-01','storedSubmissions':2,'quarantinedSubmissions':0,'nonQuarantinedSubmissions':2}]}}
        audit={'request':{'site':'genemedi-net','date_range':self.range},'generated_at':'2026-11-02T01:00:00Z',
               'source_statuses':{'sources':{s:{'status':'ok'} for s in self.raw}}}
        self.write(self.base/'audits'/'day.json',audit)
        for source,rows in self.raw.items():
            request={'source':source,**self.range}
            digest=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            p=self.base/'cache'/'genemedi-net'/source/(digest+'.json')
            p.parent.mkdir(parents=True)
            self.caches[source]=p
            self.write(p,rows)
        self.overview=[{'周期键':'synthetic-month','周期开始':'2026-11-01','周期结束':'2026-11-30',
                        'GA4数据截至':'2026-11-01','GSC数据截至':'2026-11-01','询盘数据截至':'2026-11-01',
                        '官网访客数':7,'官网访问次数':9,'表单提交事件（GA4）':1}]
        self.write(self.run/'summary.json',{'site':'genemedi-net','status':'ok'})
        self.write(self.run/'overview.json',self.overview)
        self.records={'product_daily':[{'data_date':'2026-11-01','product_daily_key':'synthetic-product','product_name':'Synthetic Product','ga4_sessions':9}],
                      'information_daily':[{'data_date':'2026-11-01','information_daily_key':'synthetic-content','theme':'Synthetic Theme','ga4_sessions':0}]}
        self.write(self.run/'v3-daily.json',{'records':self.records})
        self.funnel={'product_funnel_daily':[{'data_date':'2026-11-01','product_funnel_daily_key':'synthetic-funnel','ga4_sessions':9}],
                     'product_funnel_stage_daily':[{'data_date':'2026-11-01','product_funnel_stage_daily_key':'synthetic-stage','stage_id':'synthetic','stage_value':9}]}
        self.write(self.run/'product-funnel-daily.json',{'records':self.funnel})
    @staticmethod
    def write(p,value):p.write_text(json.dumps(value,ensure_ascii=False))
    def publish(self):return make(self.out,self.base)
    def rows(self,dataset):
        with sqlite3.connect(self.out) as db:return [row for x in db.execute('SELECT payload FROM blocks WHERE dataset=? ORDER BY start,end,block_index',(dataset,)) for row in decode_block(x[0])]

class ReleaseRegressions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.f=Fixture(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_dates_active_users_and_effective_cutoff(self):
        self.f.publish()
        day=self.f.rows('ga4_daily')[0]
        self.assertEqual(day['date'],'2026-11-01')
        self.assertEqual(day['totalUsers'],8)
        period=self.f.rows('overview_periods')[0]
        self.assertEqual(period['GA4数据截至'],'2026-11-01')
        total=self.f.rows('ga4_totals')[0]
        self.assertEqual(total['activeUsers'],7)
        self.assertNotIn('totalUsers',total)
        self.assertEqual(total['end'],'2026-11-01')
        self.assertEqual(total['intended_end'],'2026-11-30')
    def test_null_and_missing_cutoff_do_not_invent_interval(self):
        for mode in ('missing','null','before_start'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as t:
                f=Fixture(t)
                if mode=='missing':del f.overview[0]['GA4数据截至']
                else:f.overview[0]['GA4数据截至']=None if mode=='null' else '2026-10-31'
                f.write(f.run/'overview.json',f.overview);f.publish()
                self.assertEqual(f.rows('ga4_totals'),[])
    def test_nested_private_dimension_rejected(self):
        self.f.raw['ga4']['GA4 Pages'][0]['landingPagePlusQueryString']={'email':'synthetic@example.invalid'}
        self.f.write(self.f.caches['ga4'],self.f.raw['ga4'])
        with self.assertRaisesRegex(ValueError,'dimension'):self.f.publish()
        self.assertFalse(self.f.out.exists())
    def test_url_percent_private_and_safe_case_preservation(self):
        self.assertEqual(safe('https://user:pass@host/AbC%2FDeF?email=synthetic@example.invalid#private',True),'https://host/AbC%2FDeF')
        self.assertNotIn('example',safe('https://host/i/synthetic%40example.invalid',True))
        self.assertIn('[PRIVATE_DIMENSION_REDACTED]',safe('https://host/i/synthetic%40example.invalid',True))
    def test_stage_dataset_matches_api(self):
        self.f.publish()
        result=Reader(self.f.out).query({'contract':'batch-service/v1','operation':'site_catalog'})
        names={r['dataset'] for r in result['datasets']}
        self.assertIn('product_funnel_stages_daily',names)
        self.assertNotIn('product_funnel_stage_daily',names)
        self.assertEqual(len(self.f.rows('product_funnel_stages_daily')),1)
    def test_missing_stable_key_preserves_last_good(self):
        self.f.publish();before=self.f.out.read_bytes()
        del self.f.records['product_daily'][0]['product_daily_key']
        self.f.write(self.f.run/'v3-daily.json',{'records':self.f.records})
        with self.assertRaisesRegex(ValueError,'missing daily key'):self.f.publish()
        self.assertEqual(before,self.f.out.read_bytes())
    def meta(self):
        with sqlite3.connect(self.f.out) as db:
            info={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM metadata')}
            sets={n:json.loads(m) for n,m in db.execute('SELECT name,metadata FROM datasets')}
        return info,sets
    def test_source_dataset_window_disappearance_carries_last_published_rows(self):
        self.f.publish();first,_=self.meta()
        original=self.f.rows('gsc_queries')
        del self.f.raw['gsc']['GSC Queries']
        self.f.write(self.f.caches['gsc'],self.f.raw['gsc'])
        self.f.publish()
        info,sets=self.meta()
        carried=self.f.rows('gsc_queries')
        self.assertEqual(len(carried),len(original))
        for old,new in zip(original,carried):
            self.assertTrue(new['carried_forward'])
            self.assertEqual(new['carried_forward_from_snapshot'],first['snapshot_id'])
            self.assertEqual({k:v for k,v in new.items() if not k.startswith('carried_forward')},old)
        self.assertEqual(info['carried_forward_window_count'],1)
        record=sets['gsc_queries']['coverage']['carried_forward_windows'][0]
        self.assertEqual((record['start'],record['end'],record['last_input_publication']),('2026-11-01','2026-11-01',first['published_at']))
        self.assertEqual(sets['ga4_daily']['coverage']['carried_forward_windows'],[])
    def test_carry_keeps_first_carry_identity_across_publications(self):
        self.f.publish()
        del self.f.raw['gsc']['GSC Queries'];self.f.write(self.f.caches['gsc'],self.f.raw['gsc'])
        self.f.publish();_,sets=self.meta();first=sets['gsc_queries']['coverage']['carried_forward_windows'][0]
        self.f.overview[0]['官网访问次数']=10;self.f.write(self.f.run/'overview.json',self.f.overview)
        self.assertNotEqual(self.f.publish(),{'status':'unchanged'})
        _,sets=self.meta()
        self.assertEqual(sets['gsc_queries']['coverage']['carried_forward_windows'],[first])
        self.assertEqual(self.f.rows('gsc_queries')[0]['carried_forward_since'],first['carried_since'])
    def test_reappearing_window_replaces_carried_rows(self):
        self.f.publish()
        saved=copy.deepcopy(self.f.raw['gsc'])
        del self.f.raw['gsc']['GSC Queries'];self.f.write(self.f.caches['gsc'],self.f.raw['gsc'])
        self.f.publish()
        saved['GSC Queries'][0]['clicks']=5;self.f.write(self.f.caches['gsc'],saved)
        self.f.publish();info,sets=self.meta()
        row=self.f.rows('gsc_queries')[0]
        self.assertNotIn('carried_forward',row);self.assertEqual(row['clicks'],5)
        self.assertEqual(info['carried_forward_window_count'],0)
        self.assertEqual(sets['gsc_queries']['coverage']['carried_forward_windows'],[])
    def test_failed_audit_status_carries_instead_of_freezing(self):
        # Another day keeps gsc_daily present, as in production where only some days fail.
        other={'start':'2026-10-31','end':'2026-10-31'}
        self.f.write(self.f.base/'audits'/'other.json',{'request':{'site':'genemedi-net','date_range':other},'generated_at':'2026-11-01T01:00:00Z','source_statuses':{'sources':{'gsc':{'status':'ok'}}}})
        digest=hashlib.sha256(json.dumps({'source':'gsc',**other},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.f.write(self.f.caches['gsc'].parent/(digest+'.json'),{'GSC Daily':[{'date':'2026-10-31','clicks':1,'impressions':3}]})
        self.f.publish()
        audit=json.loads((self.f.base/'audits'/'day.json').read_text())
        audit['source_statuses']['sources']['gsc']={'status':'error'}
        self.f.write(self.f.base/'audits'/'day.json',audit)
        self.f.publish()
        self.assertTrue(all(r['carried_forward'] for r in self.f.rows('gsc_queries')))
        self.assertEqual({r['date']:r.get('carried_forward',False) for r in self.f.rows('gsc_daily')},{'2026-10-31':False,'2026-11-01':True})
        self.assertNotIn('carried_forward',self.f.rows('ga4_daily')[0])
    def test_large_sudden_window_loss_preserves_last_good(self):
        self.f.publish();before=self.f.out.read_bytes()
        import publish_fullsite
        with mock.patch.object(publish_fullsite,'MAX_NEW_CARRY_WINDOWS',0),mock.patch.object(publish_fullsite,'MAX_NEW_CARRY_FRACTION',0):
            del self.f.raw['gsc']['GSC Queries']
            self.f.write(self.f.caches['gsc'],self.f.raw['gsc'])
            with self.assertRaisesRegex(ValueError,'too many previously published windows'):self.f.publish()
        self.assertEqual(before,self.f.out.read_bytes())
    def test_changed_period_payload_invalidates_skip(self):
        self.f.publish()
        self.assertEqual(self.f.publish(),{'status':'unchanged'})
        self.f.overview[0]['官网访问次数']=123
        self.f.write(self.f.run/'overview.json',self.f.overview)
        self.assertNotEqual(self.f.publish(),{'status':'unchanged'})
        self.assertEqual(self.f.rows('ga4_totals')[0]['sessions'],123)
    def test_invalid_date_rejected(self):
        self.f.raw['ga4']['GA4 Daily'][0]['date']='2026-11-99'
        self.f.write(self.f.caches['ga4'],self.f.raw['ga4'])
        with self.assertRaises(ValueError):self.f.publish()
        self.assertFalse(self.f.out.exists())

    def test_compressed_backup_round_trip(self):
        self.f.publish();before=self.f.out.read_bytes()
        backup_previous(self.f.out)
        backup=self.f.out.parent/'previous.sqlite.gz'
        with gzip.open(backup,'rb') as stream:self.assertEqual(stream.read(),before)
        self.assertEqual(backup.stat().st_mode & 0o777,0o400)
        self.assertEqual(self.f.out.read_bytes(),before)
        self.assertEqual(list(self.f.out.parent.glob('.previous-*.sqlite.gz')),[])
    def test_compression_failure_preserves_current_and_prior(self):
        self.f.publish();before=self.f.out.read_bytes()
        prior=self.f.out.parent/'previous.sqlite.gz';prior.write_bytes(b'synthetic-previous-backup')
        with mock.patch('publish_fullsite.gzip.open',side_effect=OSError('synthetic compression failure')):
            with self.assertRaises(OSError):backup_previous(self.f.out)
        self.assertEqual(self.f.out.read_bytes(),before)
        self.assertEqual(prior.read_bytes(),b'synthetic-previous-backup')
        self.assertEqual(list(self.f.out.parent.glob('.previous-*.sqlite.gz')),[])
    def test_backup_verification_failure_preserves_prior(self):
        self.f.publish();before=self.f.out.read_bytes()
        prior=self.f.out.parent/'previous.sqlite.gz';prior.write_bytes(b'synthetic-previous-backup')
        real_open=gzip.open
        def corrupted_read(path,mode,*args,**kwargs):
            if mode=='rb':return io.BytesIO(b'synthetic incorrect decoded bytes')
            return real_open(path,mode,*args,**kwargs)
        with mock.patch('publish_fullsite.gzip.open',side_effect=corrupted_read):
            with self.assertRaisesRegex(ValueError,'backup verification'):backup_previous(self.f.out)
        self.assertEqual(self.f.out.read_bytes(),before)
        self.assertEqual(prior.read_bytes(),b'synthetic-previous-backup')
        self.assertEqual(list(self.f.out.parent.glob('.previous-*.sqlite.gz')),[])
    def test_publish_backup_failure_preserves_live(self):
        self.f.publish();before=self.f.out.read_bytes()
        self.f.overview[0]['官网访问次数']=987
        self.f.write(self.f.run/'overview.json',self.f.overview)
        with mock.patch('publish_fullsite.backup_previous',side_effect=OSError('synthetic backup failure')):
            with self.assertRaises(OSError):self.f.publish()
        self.assertEqual(self.f.out.read_bytes(),before)
        self.assertEqual(list(self.f.out.parent.glob('.fullsite-*.sqlite')),[])
    def test_disk_guard_preserves_live_before_staging(self):
        self.f.publish();before=self.f.out.read_bytes()
        self.f.overview[0]['官网访问次数']=987
        self.f.write(self.f.run/'overview.json',self.f.overview)
        with mock.patch('publish_fullsite.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(RuntimeError,'insufficient staging space'):self.f.publish()
        self.assertEqual(self.f.out.read_bytes(),before)
        self.assertEqual(list(self.f.out.parent.glob('.fullsite-*.sqlite')),[])

    def test_compressed_publisher_rows_query_and_filter(self):
        self.f.publish()
        result=Reader(self.f.out).query({'contract':'batch-service/v1','operation':'site_query','dataset':'ga4_daily','start':'2026-11-01','end':'2026-11-01','filters':{'activeUsers':7},'sort_by':'totalUsers','descending':True})
        self.assertEqual(result['total_matching_rows'],1)
        self.assertEqual(result['rows'][0]['sessions'],9)
        self.assertEqual(result['rows'][0]['activeUsers'],7)
        self.assertEqual(result['rows'][0]['totalUsers'],8)

class BlockReleaseRegressions(unittest.TestCase):
    setUp=ReleaseRegressions.setUp
    tearDown=ReleaseRegressions.tearDown
    def request(self, **extra):
        return dict(contract='batch-service/v1',operation='site_query',dataset='gsc_queries',start='2026-11-01',end='2026-11-01',**extra)
    def populate(self, n=1203):
        self.f.raw['gsc']['GSC Queries']=[{'query':'synthetic term %04d'%i,'clicks':i%7,'impressions':i} for i in range(n)]
        self.f.write(self.f.caches['gsc'],self.f.raw['gsc']);self.f.publish()
    def test_block_sorted_cursor_all_ties_no_loss(self):
        self.populate()
        for descending in (False,True):
            found=[];cursor=None
            while True:
                request=self.request(sort_by='clicks',descending=descending,limit=113)
                if cursor:request['cursor']=cursor
                result=Reader(self.f.out).query(request)
                self.assertEqual(result['total_matching_rows'],1203)
                found.extend(result['rows']);cursor=result['next_cursor']
                if not cursor:break
                self.assertLess(len(found),1300)
            self.assertEqual(len(found),1203)
            self.assertEqual(len({r['row_id'] for r in found}),1203)
            self.assertEqual([r['clicks'] for r in found],sorted([r['clicks'] for r in found],reverse=descending))
    def test_block_over_5000_count_and_tail_filter(self):
        self.populate(5101)
        result=Reader(self.f.out).query(self.request(filters={'impressions':5100}))
        self.assertEqual(result['total_matching_rows'],1)
        self.assertEqual(result['rows'][0]['query'],'synthetic term 5100')
        result=Reader(self.f.out).query(self.request(offset=5099,limit=10))
        self.assertEqual(result['total_matching_rows'],5101)
        self.assertEqual(len(result['rows']),2)
        self.assertIsNone(result['next_offset'])
    def test_conflicting_source_dimensions_preserved(self):
        self.f.raw['gsc']['GSC Queries']=[{'query':'same','clicks':1},{'query':'same','clicks':2}]
        self.f.write(self.f.caches['gsc'],self.f.raw['gsc'])
        self.f.publish()
        rows=self.f.rows('gsc_queries')
        self.assertEqual(len(rows),2)
        self.assertEqual({r['clicks'] for r in rows},{1,2})
        self.assertEqual(len({r['row_id'] for r in rows}),2)
        self.assertTrue(all(r.get('source_dimension_ambiguous') is True and r.get('source_dimension_rows')==2 and r.get('aggregation_safe') is False for r in rows))
    def test_sanitized_url_collisions_remain_distinct(self):
        self.f.raw['ga4']['GA4 Pages']=[{'landingPagePlusQueryString':'/Path?x=one','activeUsers':3},{'landingPagePlusQueryString':'/Path?x=two','activeUsers':4}]
        self.f.write(self.f.caches['ga4'],self.f.raw['ga4']);self.f.publish()
        rows=self.f.rows('ga4_pages')
        self.assertEqual(len(rows),2)
        self.assertEqual({r['landingPagePlusQueryString'] for r in rows},{'/Path'})
        self.assertEqual(len({r['row_id'] for r in rows}),2)
        self.assertEqual({r['activeUsers'] for r in rows},{3,4})
    def test_identical_cached_rows_preserved_without_assumed_dedupe(self):
        row={'query':'same exact source','clicks':2,'impressions':3}
        self.f.raw['gsc']['GSC Queries']=[row,copy.deepcopy(row)]
        self.f.write(self.f.caches['gsc'],self.f.raw['gsc']);self.f.publish()
        rows=self.f.rows('gsc_queries')
        self.assertEqual(len(rows),2)
        self.assertEqual(len({r['row_id'] for r in rows}),2)
        self.assertTrue(all(r.get('source_dimension_ambiguous') is True and r.get('source_dimension_rows')==2 and r.get('aggregation_safe') is False for r in rows))
        first=Reader(self.f.out).query(self.request(sort_by='clicks',limit=1))
        self.assertEqual(first['selected_scope_warning_count'],1)
        self.assertFalse(first['selected_scope_warnings_truncated'])
        self.assertEqual(len(first['selected_scope_warnings']),1)
        second=Reader(self.f.out).query(self.request(sort_by='clicks',limit=1,cursor=first['next_cursor']))
        self.assertNotEqual(first['rows'][0]['row_id'],second['rows'][0]['row_id'])
        self.assertIsNone(second['next_cursor'])
    def test_nested_period_metric_rejected(self):
        self.f.overview[0]['官网访客数']={'email':'synthetic@example.invalid'}
        self.f.write(self.f.run/'overview.json',self.f.overview)
        with self.assertRaises(ValueError):self.f.publish()
        self.assertFalse(self.f.out.exists())
    def test_block_cursor_bad_shape_rejected(self):
        self.populate(3)
        for bad in ([],None,42,'text'):
            token=base64.urlsafe_b64encode(json.dumps(bad).encode()).decode()
            with self.subTest(bad=bad), self.assertRaises(QueryError):
                Reader(self.f.out).query(self.request(sort_by='clicks',cursor=token))
    def test_block_malformed_payload_rejected(self):
        self.populate(3)
        self.f.out.chmod(0o600)
        for malformed in (b'bad',b'FSB2'+zlib.compress(b'['+b' '*8388609+b']'),b'FSB2'+zlib.compress(json.dumps([{}]*501).encode())):
            with sqlite3.connect(self.f.out) as db:db.execute('UPDATE blocks SET payload=? WHERE dataset=?',(malformed,'gsc_queries'))
            with self.subTest(length=len(malformed)), self.assertRaises(QueryError):
                Reader(self.f.out).query(self.request())

if __name__=='__main__':unittest.main()
