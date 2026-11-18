"""Fixed-launcher fixtures; no Linux services, credentials or real sources."""
import json,os,platform,shutil,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
import pytest,yaml
from test_release_candidate import release,synthetic_case
from test_analysis_worker import reader
from website_analytics.analysis.repeatable_runner import LaunchPaths,source_entry,publisher_entry,main,fixed_paths
from website_analytics.analysis.release_integrity import sha,provenance,ReleaseError
from website_analytics.analysis.refresh_loop import initialize,registration,_key
from website_analytics.analysis.completeness import registration_ids
from website_analytics.analysis.cases import CaseStore
from website_analytics.config import load_sites,require_site

AT=datetime(2026,11,4,7,30,tzinfo=timezone.utc)

@pytest.fixture
def configured(tmp_path,release):
    R=release[2];C=tmp_path/'config';C.mkdir();P=tmp_path/'worker';L=tmp_path/'live';L.mkdir()
    sites={'sites':{'genemedi-net':{'display_name':'Synthetic','domains':['genemedi.net','www.genemedi.net'],'timezone':'America/Los_Angeles','ga4_property_id':'123','gsc_property_url':'https://www.genemedi.net/','inquiry_source':{'kind':'legacy_contacts_mysql','credential_env':'WEBSITE_ANALYTICS_SYNTHETIC_DSN','site_scope':'fixed_registered_source','site_scope_evidence':'Synthetic fixture only','registered_database':'v2_genemedi_net','registered_table':'contacts','allowed_hosts':['genemedi.net','www.genemedi.net']}}}}
    (C/'sites.yaml').write_text(yaml.safe_dump(sites));site=require_site(load_sites(C/'sites.yaml'),'genemedi-net')
    seed=synthetic_case();store=CaseStore(tmp_path/'seed.sqlite');store.put(seed);initialize(P,store.path);shutil.copyfile(store.path,L/'cases.sqlite')
    runtime=provenance(R,C/'sites.yaml','local_fixture');task=registration(seed,runtime,registration_ids(site));(C/'task.json').write_text(json.dumps(task))
    profile={'contract':'repeatable-launcher/v1','release_manifest_sha256':release[3]['manifest_sha256'],'source_config_sha256':sha(C/'sites.yaml'),'task_sha256':sha(C/'task.json'),'expected_python_version':platform.python_version(),'capture_mode':'local_fixture','include_open':True}
    (C/'loop.json').write_text(json.dumps(profile));(C/'manual-request.json').write_text(json.dumps({'contract':'manual-refresh/v1','request_key':'manual-a'}))
    paths=LaunchPaths(R,C,P,L);now=[AT];calls=[];env=[]
    def factory(path):
        calls.append(path);r=reader();r.site=site;r.clock=lambda:now[0];return r
    def load(*args):env.append(True)
    return paths,profile,task,now,calls,env,factory,load


def src(v,mode='daily',**kw):
    p,_,_,now,_,_,factory,load=v
    return source_entry(p,mode,production=False,reader_factory=factory,environment_loader=load,dependency_checker=lambda *a,**k:{},clock=lambda:now[0],idle_checker=kw.pop('idle_checker',lambda:True),**kw)
def pub(v,**kw):return publisher_entry(v[0],production=False,dependency_checker=lambda *a,**k:{},clock=lambda:v[3][0],**kw)


def test_fixed_daily_and_manual_repeatable_source_to_separate_publisher(configured):
    v=configured;first=src(v);assert first['source_state']=='ready' and len(v[4])==len(v[5])==1
    assert src(v)['source_outcome']==first['source_outcome'] and len(v[4])==1
    a=pub(v);assert a['results'][-1]['result']['state']=='published' and a['source_fetch_started'] is False
    v[3][0]+=timedelta(minutes=10);assert src(v,'on_demand')['source_state']=='ready';assert pub(v)['results'][-1]['result']['state']=='published'
    assert CaseStore(v[0].live/'cases.sqlite').version_count(v[2]['case_id'])==3
    assert len(v[4])==2 and json.loads((v[0].live/'refresh-status.json').read_text())['last_attempt']['mode']=='on_demand'


def test_manual_retry_across_selection_week_never_refetches(configured):
    v=configured;first=src(v,'on_demand');v[3][0]+=timedelta(days=10)
    replay=src(v,'on_demand');assert replay['source_outcome']==first['source_outcome'] and len(v[4])==1
    assert pub(v)['results'][-1]['result']['state']=='published'


@pytest.mark.parametrize('kind',['manifest','site_hash','task_hash','runtime','schema','property','python','private_mode','manual_extra'])
def test_preflight_fails_before_environment_or_reader(configured,kind):
    v=configured;p,profile=v[0],v[1];mode='daily'
    if kind=='manifest':profile['release_manifest_sha256']='0'*64
    elif kind=='site_hash':profile['source_config_sha256']='0'*64
    elif kind=='task_hash':profile['task_sha256']='0'*64
    elif kind=='python':profile['expected_python_version']='0.0.0'
    elif kind=='private_mode':os.chmod(p.private,0o755)
    elif kind=='manual_extra':(p.config/'manual-request.json').write_text(json.dumps({'contract':'manual-refresh/v1','request_key':'manual-a','url':'https://outside.invalid'}));mode='on_demand'
    elif kind=='runtime':
        task=json.loads((p.config/'task.json').read_text());task['runtime_digest']='0'*64;(p.config/'task.json').write_text(json.dumps(task));profile['task_sha256']=sha(p.config/'task.json')
    else:
        sites=yaml.safe_load((p.config/'sites.yaml').read_text());site=sites['sites']['genemedi-net']
        if kind=='schema':site['inquiry_source']['registered_database']='different'
        else:site['domains']=['tarmart.com']
        (p.config/'sites.yaml').write_text(yaml.safe_dump(sites));profile['source_config_sha256']=sha(p.config/'sites.yaml')
    (p.config/'loop.json').write_text(json.dumps(profile))
    with pytest.raises(Exception):src(v,mode)
    assert not v[4] and not v[5]


def test_old_sync_activity_defers_without_credentials_and_is_observable(configured):
    v=configured;result=src(v,idle_checker=lambda:False)
    assert result['source_state']=='failed' and result['source_outcome']['error_codes']==['old_sync_active_no_fetch']
    assert not v[4] and not v[5];assert pub(v)['results'][-1]['result']['state']=='failed'
    state=json.loads((v[0].live/'refresh-status.json').read_text());assert state['last_attempt']['error_codes']==['old_sync_active_no_fetch']


def test_queue_recovers_unknown_publication_without_source_or_duplicate_write(configured):
    v=configured;src(v)
    def fail(stage):
        if stage=='after_replace':raise KeyboardInterrupt()
    assert pub(v,checkpoint=fail)['results'][-1]['result']['state']=='publication_uncertain'
    assert pub(v)['results'][-1]['result']['inspected_existing_target_without_republication']
    assert len(v[4])==1


def test_fixed_paths_and_cli_are_not_caller_selectable(configured,monkeypatch):
    path=fixed_paths(configured[0].release);rid=path.config.name
    assert path.config==Path('/etc/website-analytics-refresh')/rid and rid.startswith('rc-')
    monkeypatch.setattr(sys,'argv',['entry','--source','/private'])
    assert main('daily')==2
    monkeypatch.setattr(sys,'argv',['entry']);assert main('unknown')==2
    with pytest.raises(ReleaseError):source_entry(configured[0],'daily',reader_factory=configured[6])


def test_generation_keys_bind_registration(configured):
    v=configured;key=_key(v[2],'daily-2026-11-03');other={**v[2],'runtime_digest':'0'*64}
    assert key!=_key(other,'daily-2026-11-03')


def test_more_than_32_generations_and_bounded_queue_drain(configured):
    v=configured
    for n in range(34):
        v[3][0]=AT+timedelta(minutes=n+1)
        (v[0].config/'manual-request.json').write_text(json.dumps({'contract':'manual-refresh/v1','request_key':'manual-'+str(n)}))
        assert src(v,'on_demand')['source_state']=='ready'
    calls=len(v[4]);a=pub(v);assert len(a['results'])==16 and a['remaining_unattempted']==18
    b=pub(v);assert len(b['results'])==16 and b['remaining_unattempted']==2
    c=pub(v);assert len(c['results'])==2 and c['remaining_unattempted']==0 and len(v[4])==calls
    store=CaseStore(v[0].live/'cases.sqlite');assert store.version_count(v[2]['case_id'])==35
    page=store.version_page(v[2]['case_id'],limit=10);assert page['next_before_sequence'] and len(page['versions'])==10


def test_publisher_deferred_queue_does_not_read_environment(configured):
    v=configured;src(v);v[5].clear();v[4].clear()
    assert pub(v)['results'][-1]['result']['state']=='published'
    assert pub(v)['results'][-1]['result']['state']=='published'
    assert not v[5] and not v[4]


def test_templates_have_fixed_roles_no_timer_or_implicit_source_retry():
    root=Path(__file__).resolve().parents[1]/'deploy/refresh-rc'
    source=(root/'website-analytics-refresh-daily-v2.service.example').read_text()
    publisher=(root/'website-analytics-refresh-publish-v2.service.example').read_text()
    assert 'User=websiteanalytics' in source and 'Restart=no' in source and 'ReadWritePaths=/var/lib/website-analytics-refresh/generations/@RELEASE_ID@/worker' in source
    assert 'OnSuccess=' not in source and '[Timer]' not in source
    assert 'User=root' in publisher and 'PrivateNetwork=yes' in publisher and 'CapabilityBoundingSet=CAP_CHOWN CAP_DAC_READ_SEARCH' in publisher
    assert 'EnvironmentFile=' not in publisher and 'RestrictAddressFamilies=AF_UNIX' in publisher
    assert 'ReadWritePaths=/var/lib/website-analytics-query/live' in publisher


def test_repeatable_payload_cannot_be_packaged_without_fixed_entries(tmp_path,release):
    from website_analytics.analysis.release_integrity import build
    root,wheels,_,_=release
    (root/'src/website_analytics/analysis/repeatable_runner.py').write_text('# fixture new feature marker\n')
    with pytest.raises(ReleaseError,match='required release payload missing'):build(root,tmp_path/'incomplete.tar.gz',wheelhouse=wheels)


def test_changed_valid_registration_cannot_reuse_generation_before_source(configured):
    v=configured;src(v);v[4].clear();v[5].clear()
    task=json.loads((v[0].config/'task.json').read_text());task['task_id']='new-approved-task-name'
    (v[0].config/'task.json').write_text(json.dumps(task));v[1]['task_sha256']=sha(v[0].config/'task.json');(v[0].config/'loop.json').write_text(json.dumps(v[1]))
    with pytest.raises(ReleaseError,match='new private generation'):src(v)
    assert not v[4] and not v[5]
