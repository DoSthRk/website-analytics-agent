"""Synthetic stand-ins only: no credentials, server or source network calls."""
import copy,json,os,threading,shutil,sqlite3
from pathlib import Path
from datetime import datetime,timedelta,timezone
from contextlib import contextmanager
import pytest
from test_release_candidate import synthetic_case
from test_analysis_worker import reader
from website_analytics.analysis.refresh_loop import registration,initialize,produce,publish_run,recover_capture,abandon_capture,rollback_target,_key,_sha
from website_analytics.analysis.refresh_status import StatusReader
from website_analytics.analysis.completeness import registration_ids
from website_analytics.analysis.cases import CaseStore,query_case
from website_analytics.analysis.contracts import AnalysisError,fingerprint
from website_analytics.analysis.worker import parse_request
from website_analytics.analysis.service import make_server
from website_analytics.analysis.client import query_case as remote_case
from website_analytics.analysis.release_runner import URLS,VARIANTS

AT=datetime(2026,11,4,7,30,tzinfo=timezone.utc)

@pytest.fixture
def env(tmp_path):
    seed=synthetic_case();base=CaseStore(tmp_path/'seed.sqlite');base.put(seed)
    private=tmp_path/'private';initialize(private,base.path);public=tmp_path/'public';public.mkdir();shutil.copyfile(base.path,public/'cases.sqlite');os.chmod(public/'cases.sqlite',0o440)
    now=[AT];calls=[];r=reader();runtime=seed['source_packet']['runtime'];reg=registration(seed,runtime,registration_ids(r.site))
    def factory():
        calls.append(True);new=reader();new.clock=lambda:now[0];return new
    return private,public,reg,runtime,now,calls,factory

def req():return {'contract':'bounded-worker/v2','site':'genemedi-net','raw_urls':list(URLS),'variant_paths':list(VARIANTS),'selection_timezone':'America/Los_Angeles','weeks':[{'start':'2026-10-26','end':'2026-11-01','window_kind':'completed_week'},{'start':'2026-11-02','end':'2026-11-03','window_kind':'open_week'}]}
def run(e,key='daily-1',mode='daily',request=None,**kw):
    p,_,reg,rt,now,_,factory=e
    return produce(p,reg,rt,request or req(),request_key=key,mode=mode,reader_factory=factory,clock=lambda:now[0],**kw)
def pub(e,key='daily-1',**kw):return publish_run(e[0],e[1],e[2],request_key=key,clock=lambda:e[4][0],**kw)

def test_repeated_daily_and_on_demand_dynamic_handoff(env):
    e=env;first=run(e);assert first['state']=='ready'
    assert run(e)==first and len(e[5])==1
    done=pub(e);assert done['state']=='published' and done['strict_complete'] is False
    assert pub(e)==done
    e[4][0]+=timedelta(minutes=10);second=run(e,'manual-2','on_demand');assert second['state']=='ready' and second['candidate_sha256']!=first['candidate_sha256']
    assert pub(e,'manual-2')['state']=='published' and len(e[5])==2
    s=StatusReader(e[1]/'refresh-status.json').query({'contract':'batch-service/v1','operation':'refresh_status','task_id':e[2]['task_id']})
    assert s['last_attempt']['mode']=='on_demand' and s['publication_matches_selected_evidence'] and s['status']=='partial'
    store=CaseStore(e[1]/'cases.sqlite');record=store.get(e[2]['case_id'],second['evidence_id'])
    detail=query_case(store,{'contract':'batch-service/v1','operation':'case_detail','case_id':e[2]['case_id'],'evidence_id':second['evidence_id']})
    assert record['source_packet']['protocol']=='seo-period-read/2' and detail['report']['contract']=='continuous-trend-report/v2'
    assert detail['report']['weekly_rows'][-1]['window_kind']=='open_week'
    assert record['draft']['raw_urls'][-1].endswith('/PurProX-AAVFull-Enrichment-Kit') and record['source_packet']['variant_paths_not_merged']==list(VARIANTS)
    assert all(r['reconciliation']['ga4_interval_users_independent_not_summed'] for r in detail['report']['weekly_rows'])
    assert all(r['maturity']['observed_complete_through'] is None for r in detail['scoped_completeness']['source_windows'])

@pytest.mark.parametrize('kind',['foreign','changed_case','future','long','gap','runtime','wrong_kind'])
def test_scope_rejected_before_source(env,kind):
    raw=req();rt=env[3]
    if kind=='foreign':raw['raw_urls'][0]='https://tarmart.com/i/x'
    elif kind=='changed_case':raw['raw_urls'][-1]=raw['raw_urls'][-1].lower()
    elif kind=='future':raw['weeks'][-1]['end']='2026-11-05'
    elif kind=='long':raw['weeks'][0]['start']='2026-10-19'
    elif kind=='gap':raw['weeks'].reverse()
    elif kind=='runtime':rt={**rt,'production_release':'different'}
    else:raw['weeks'][-1]['window_kind']='completed_week'
    with pytest.raises(AnalysisError):produce(env[0],env[2],rt,raw,request_key='bad',mode='daily',reader_factory=env[6],clock=lambda:env[4][0])
    assert not env[5]

def test_request_key_conflict_and_concurrent_coalescing(env):
    entered=threading.Event();release=threading.Event();out=[]
    def pause(stage):
        if stage=='after_intent':entered.set();assert release.wait(5)
    thread=threading.Thread(target=lambda:out.append(run(env,checkpoint=pause)));thread.start();assert entered.wait(5)
    assert run(env)['state']=='busy';release.set();thread.join(10);assert out[0]['state']=='ready' and len(env[5])==1
    assert run(env)==out[0]
    with pytest.raises(AnalysisError):run(env,mode='on_demand')

@pytest.mark.parametrize('stage',['after_intent','after_capture','after_history','after_export'])
def test_crash_never_refetches_and_recovery_is_offline(env,stage):
    def crash(at):
        if at==stage:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):run(env,checkpoint=crash)
    calls=len(env[5]);assert run(env)['state']=='needs_inspection' and len(env[5])==calls
    assert run(env,'different')['state']=='needs_inspection' and len(env[5])==calls
    if stage=='after_intent':
        assert recover_capture(env[0],env[2],request_key='daily-1',clock=lambda:env[4][0])['state'].startswith('needs_explicit_abandon')
        assert abandon_capture(env[0],env[2],request_key='daily-1',clock=lambda:env[4][0])['state']=='failed'
    else:
        assert recover_capture(env[0],env[2],request_key='daily-1',clock=lambda:env[4][0])['state']=='ready'
        assert pub(env)['state']=='published'
    assert len(env[5])==calls

def test_source_failure_keeps_published_and_sanitizes_status(env):
    run(env);pub(env);before=_sha(env[1]/'cases.sqlite');env[4][0]+=timedelta(minutes=10)
    def fail():raise RuntimeError('mysql://private-secret-password')
    result=produce(env[0],env[2],env[3],req(),request_key='fail',mode='on_demand',reader_factory=fail,clock=lambda:env[4][0])
    assert result['state']=='failed' and pub(env,'fail')['state']=='failed' and _sha(env[1]/'cases.sqlite')==before
    s=json.loads((env[1]/'refresh-status.json').read_text());assert s['last_attempt']['state']=='failed' and s['last_published']
    assert 'private-secret' not in json.dumps(s)

@pytest.mark.parametrize('stage',['before_replace','after_replace','before_publication_receipt','before_status'])
def test_publication_failure_uncertainty_and_status_recovery(env,stage):
    run(env);before=_sha(env[1]/'cases.sqlite')
    def fault(at):
        if at==stage:
            if stage=='before_replace':raise OSError('private runtime detail')
            raise KeyboardInterrupt()
    if stage in {'before_publication_receipt','before_status'}:
        with pytest.raises(KeyboardInterrupt):pub(env,checkpoint=fault)
    else:assert pub(env,checkpoint=fault)['state']==('failed' if stage=='before_replace' else 'publication_uncertain')
    if stage=='before_replace':assert _sha(env[1]/'cases.sqlite')==before and pub(env)['state']=='failed'
    else:
        repaired=pub(env);assert repaired['state']=='published'
        assert StatusReader(env[1]/'refresh-status.json').query({'contract':'batch-service/v1','operation':'refresh_status','task_id':env[2]['task_id']})['publication_matches_selected_evidence']
    assert len(env[5])==1

def test_stale_and_tampered_handoffs_fail_closed(env):
    ready=run(env);p=env[0]/'runs'/_key(env[2],'daily-1')/'outcome.json';value=json.loads(p.read_text());value['candidate_sha256']='0'*64;p.write_text(json.dumps(value));before=_sha(env[1]/'cases.sqlite')
    with pytest.raises(AnalysisError):pub(env)
    assert _sha(env[1]/'cases.sqlite')==before

def test_readonly_status_context_and_reader_rollback(env):
    run(env);done=pub(env);old=rollback_target(env[1],env[2],request_key='daily-1');assert old.is_file()
    server=make_server(None,0,cases=CaseStore(env[1]/'cases.sqlite'),refresh_status=StatusReader(env[1]/'refresh-status.json'))
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    try:
        from urllib.request import build_opener,ProxyHandler,Request
        body={'contract':'batch-service/v1','operation':'refresh_status','task_id':env[2]['task_id']}
        response=json.loads(build_opener(ProxyHandler({})).open(Request('http://127.0.0.1:'+str(server.server_port)+'/v1/query',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})).read())
        assert response['status']=='partial' and response['live_sources'] is False
        assert remote_case('case_versions',case_id=env[2]['case_id'],port=server.server_port)['versions']
    finally:server.shutdown();server.server_close();thread.join()
    assert CaseStore(old).version_count(env[2]['case_id'])==1 and CaseStore(env[1]/'cases.sqlite').version_count(env[2]['case_id'])==2


def test_history_beyond_32_paginated_no_deletions(env):
    # 40 distinct immutable fixture revisions; no source network or publication loop.
    from website_analytics.analysis.cases import make_case_record
    store=CaseStore(env[0]/'history.sqlite');seed=synthetic_case();original=fingerprint(seed)
    for n in range(40):
        p=copy.deepcopy(seed['source_packet']);p['queried_at']=(AT+timedelta(minutes=n)).isoformat()
        record=make_case_record(seed['case_id'],seed['kind'],seed['draft'],p,seed['url_evidence'],seed['current_intervention']);store.put(record)
    cursor=None;seen=[]
    while True:
        body={'contract':'batch-service/v1','operation':'case_versions','case_id':seed['case_id'],'limit':7}
        if cursor:body['before_sequence']=cursor
        page=query_case(store,body);seen.extend(v['evidence_id'] for v in page['versions']);cursor=page['next_before_sequence']
        if cursor is None:break
    assert len(set(seen))==41 and original in seen and store.get(seed['case_id'],original)==seed
    assert len(store.list_cases()[0]['versions'])==20
    assert query_case(store,{'contract':'batch-service/v1','operation':'case_observations','case_id':seed['case_id']})['selection_scan_limit']==64


def test_sunday_open_interval_and_legacy_mode_do_not_leak(env):
    from website_analytics.analysis.case_refresh import collect
    from website_analytics.analysis.worker import RunLog
    from test_analysis_worker import request as old_request
    sunday=datetime(2026,11,9,7,tzinfo=timezone.utc) # Sunday PT, current day still open
    raw=req();raw['weeks']=[{'start':'2026-11-02','end':'2026-11-08','window_kind':'open_week'}]
    env[4][0]=sunday
    done=run(env,request=raw);assert done['state']=='ready'
    rec=CaseStore(env[0]/'history.sqlite').get(env[2]['case_id'],done['evidence_id'])
    assert rec['source_packet']['weeks'][0]['window_kind']=='open_week'
    r=reader();r.clock=lambda:sunday
    from website_analytics.analysis.worker import query
    query(raw,r,run_id='v2',run_log=RunLog(env[0]/'v2.sqlite'),as_of=sunday)
    query(old_request(),r,run_id='v1',run_log=RunLog(env[0]/'v1.sqlite'),as_of=sunday)
    assert r.adapters['gsc']._service.requests[-1]['body']['dataState']=='all' # metadata query
    assert any(q['body'].get('dataState')=='final' for q in r.adapters['gsc']._service.requests)


def test_source_error_inside_reader_and_symlink_fail_closed(env):
    def factory():
        r=env[6]();r.adapters['ga4']._client.run_report=lambda **kw:(_ for _ in ()).throw(RuntimeError('private payload'))
        return r
    before=_sha(env[1]/'cases.sqlite')
    result=produce(env[0],env[2],env[3],req(),request_key='source-error',mode='daily',reader_factory=factory,clock=lambda:env[4][0])
    assert result['state']=='failed' and pub(env,'source-error')['state']=='failed' and _sha(env[1]/'cases.sqlite')==before
    env[4][0]+=timedelta(minutes=1);ready=run(env,'valid');p=env[0]/'runs'/_key(env[2],'valid')/'candidate.sqlite';p.unlink();p.symlink_to(env[1]/'cases.sqlite')
    with pytest.raises(AnalysisError):pub(env,'valid')


def test_published_generation_rejects_stale_and_capacity_before_fetch(env,monkeypatch):
    run(env);pub(env);before=_sha(env[1]/'cases.sqlite')
    env[4][0]-=timedelta(minutes=1)
    assert run(env,'stale')['state']=='failed'
    assert _sha(env[1]/'cases.sqlite')==before
    monkeypatch.setattr(CaseStore,'version_count',lambda *a:512)
    previous=len(env[5])
    with pytest.raises(AnalysisError):run(env,'full')
    assert len(env[5])==previous


def test_pending_capture_is_visible_without_refetch(env):
    def crash(stage):
        if stage=='after_intent':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):run(env,checkpoint=crash)
    assert pub(env)['state']=='needs_inspection'
    result=StatusReader(env[1]/'refresh-status.json').query({'contract':'batch-service/v1','operation':'refresh_status','task_id':env[2]['task_id']})
    assert result['last_attempt']['state']=='needs_inspection' and result['last_attempt']['finished_at'] is None and not env[5]


def test_extra_unregistered_case_in_candidate_is_rejected(env):
    from website_analytics.analysis.publication import export
    from test_analysis_cases import record
    run(env);folder=env[0]/'runs'/_key(env[2],'daily-1');candidate=folder/'candidate.sqlite';candidate.unlink()
    store=CaseStore(env[0]/'history.sqlite');store.put(record())
    out=json.loads((folder/'outcome.json').read_text());export(store.path,candidate,'cases');out['candidate_sha256']=_sha(candidate);(folder/'outcome.json').write_text(json.dumps(out))
    before=_sha(env[1]/'cases.sqlite')
    with pytest.raises(AnalysisError,match='unregistered extra'):pub(env)
    assert _sha(env[1]/'cases.sqlite')==before


def test_export_more_than_32_and_client_history_paging(env):
    from website_analytics.analysis.cases import make_case_record
    from website_analytics.analysis.publication import export,validate
    store=CaseStore(env[0]/'history.sqlite');seed=synthetic_case()
    for n in range(35):
        packet=copy.deepcopy(seed['source_packet']);packet['queried_at']=(AT+timedelta(minutes=n)).isoformat()
        store.put(make_case_record(seed['case_id'],seed['kind'],seed['draft'],packet,seed['url_evidence'],seed['current_intervention']))
    out=env[0]/'history-export.sqlite';export(store.path,out,'cases');validate(out,'cases')
    server=make_server(None,0,cases=CaseStore(out));thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    try:
        page=remote_case('case_versions',case_id=seed['case_id'],limit=10,port=server.server_port)
        assert page['version_count']==36 and len(page['versions'])==10
        second=remote_case('case_versions',case_id=seed['case_id'],limit=10,before_sequence=page['next_before_sequence'],port=server.server_port)
        assert not {x['evidence_id'] for x in page['versions']} & {x['evidence_id'] for x in second['versions']}
    finally:server.shutdown();server.server_close();thread.join()


def test_status_source_failure_cannot_look_fresh_and_wrong_paths_rejected(env):
    from website_analytics.analysis.client import query_status
    run(env);pub(env)
    server=make_server(None,0,cases=CaseStore(env[1]/'cases.sqlite'),refresh_status=StatusReader(env[1]/'refresh-status.json'));thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    try:
        state=query_status(env[2]['task_id'],port=server.server_port);assert state['status']=='partial'
        from urllib.request import Request,build_opener,ProxyHandler
        from urllib.error import HTTPError
        for body in [{'contract':'batch-service/v1','operation':'refresh_status','task_id':env[2]['task_id'],'path':'/etc/passwd'},{'contract':'batch-service/v1','operation':'refresh_status','task_id':'unknown-task'}]:
            with pytest.raises(HTTPError):build_opener(ProxyHandler({})).open(Request('http://127.0.0.1:'+str(server.server_port)+'/v1/query',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}))
    finally:server.shutdown();server.server_close();thread.join()


def test_realistic_advancing_clock_is_not_frozen_before_source_reads(env):
    from website_analytics.analysis.contracts import timestamp
    def factory():
        r=reader()
        def advancing():env[4][0]+=timedelta(milliseconds=100);return env[4][0]
        r.clock=advancing;return r
    result=produce(env[0],env[2],env[3],req(),request_key='advancing',mode='on_demand',reader_factory=factory,clock=lambda:env[4][0])
    assert result['state']=='ready'
    record=CaseStore(env[0]/'history.sqlite').get(env[2]['case_id'],result['evidence_id']);p=record['source_packet']
    assert timestamp(p['queried_at'])>AT and timestamp(p['worker_captured_at'])<=timestamp(p['queried_at'])
    assert all(timestamp(r['evidence']['fetched_at'])<=timestamp(p['worker_captured_at']) for r in p['worker_window_evidence'])


def test_concurrent_publish_and_old_receipt_cannot_regress_latest(env):
    run(env);entered=threading.Event();release=threading.Event();results=[]
    def pause(stage):
        if stage=='before_replace':entered.set();assert release.wait(5)
    thread=threading.Thread(target=lambda:results.append(pub(env,checkpoint=pause)));thread.start();assert entered.wait(5)
    assert pub(env)['state']=='busy';release.set();thread.join(10);assert results[0]['state']=='published'
    env[4][0]+=timedelta(minutes=10);run(env,'manual-new','on_demand');pub(env,'manual-new');before=_sha(env[1]/'cases.sqlite')
    pub(env)
    status=StatusReader(env[1]/'refresh-status.json').query({'contract':'batch-service/v1','operation':'refresh_status','task_id':env[2]['task_id']})
    assert status['last_attempt']['run_id']=='manual-new' and _sha(env[1]/'cases.sqlite')==before


def test_deterministic_rolling_request_and_daily_key(env):
    from website_analytics.analysis.refresh_loop import rolling_request,daily_key
    assert rolling_request(env[2],as_of=AT)==req()
    assert daily_key(AT)=='daily-2026-11-03'
    closed=rolling_request(env[2],as_of=AT,include_open=False)
    assert closed['weeks'][0]['start']=='2026-10-19' and closed['weeks'][-1]['end']=='2026-11-01'
    assert all(w['window_kind']=='completed_week' for w in closed['weeks'])
    sunday=datetime(2026,11,9,7,tzinfo=timezone.utc)
    assert rolling_request(env[2],as_of=sunday)['weeks'][-1]['window_kind']=='open_week'


def test_naive_time_rejected_before_source(env):
    from website_analytics.analysis.refresh_loop import daily_key,rolling_request
    naive=AT.replace(tzinfo=None)
    with pytest.raises(AnalysisError):daily_key(naive)
    with pytest.raises(AnalysisError):rolling_request(env[2],as_of=naive)
    with pytest.raises(AnalysisError):produce(env[0],env[2],env[3],req(),request_key='naive',mode='daily',reader_factory=env[6],clock=lambda:naive)
    assert not env[5]
