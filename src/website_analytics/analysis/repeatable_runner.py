"""Fixed repeatable worker/publisher startup. No caller paths or source parameters.

Daily and manual entries are separate. The manual key is operator-owned input;
HTTP clients cannot enqueue, alter registration, or access this module.
"""
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime,timezone
import grp,json,logging,os,platform,pwd,re,stat,subprocess,sys

from website_analytics.analysis.release_integrity import ReleaseError,verify,check_dependencies,provenance,sha
from website_analytics.analysis.release_runner import Paths,_read,_fields,_secure_file,_private_dir,_production_guard,_load_existing_environment,_bound_clients
from website_analytics.analysis.refresh_loop import produce,publish_run,rolling_request,daily_key,_registration,_key,_safe,RefreshDeferred,MAX_RUNS
from website_analytics.analysis.completeness import registration_ids
from website_analytics.analysis.contracts import fingerprint,identifier,timestamp
from website_analytics.config import load_sites,require_site

@dataclass(frozen=True)
class LaunchPaths:
    release:Path
    config:Path
    private:Path
    live:Path=Path('/var/lib/website-analytics-query/live')


def fixed_paths(release):
    release=Path(release);_,digest=verify(release);rid='rc-'+digest[:16]
    return LaunchPaths(release,Path('/etc/website-analytics-refresh')/rid,Path('/var/lib/website-analytics-refresh/generations')/rid/'worker')


def _state_guard(paths,production):
    owner=pwd.getpwnam('websiteanalytics').pw_uid if production else os.getuid()
    for p in [paths.private,paths.private/'runs']:
        _safe(p);s=p.stat()
        if not p.is_dir() or s.st_uid!=owner or stat.S_IMODE(s.st_mode)!=0o700:raise ReleaseError('unsafe repeatable private directory')
    p=paths.private/'history.sqlite';_secure_file(p,owner=owner)
    if stat.S_IMODE(p.stat().st_mode)!=0o600:raise ReleaseError('unsafe repeatable history file')


def _common(paths,production,kind,dependency_checker):
    if production and paths!=fixed_paths(paths.release):raise ReleaseError('production paths cannot be overridden')
    profile=_read(paths.config/'loop.json')
    _fields(profile,{'contract','release_manifest_sha256','source_config_sha256','task_sha256','expected_python_version','capture_mode','include_open'})
    if profile['contract']!='repeatable-launcher/v1' or type(profile['include_open']) is not bool:raise ReleaseError('invalid repeatable profile')
    for key in ['release_manifest_sha256','source_config_sha256','task_sha256']:
        if not isinstance(profile[key],str) or not re.fullmatch(r'[a-f0-9]{64}',profile[key]):raise ReleaseError('invalid frozen digest')
    manifest,_=verify(paths.release,expected_manifest=profile['release_manifest_sha256'])
    if production and manifest.get('capabilities',{}).get('repeatable_fixed_launcher')!='repeatable-launcher/v1':raise ReleaseError('release lacks fixed repeatable capability')
    if profile['expected_python_version']!=platform.python_version():raise ReleaseError('Python patch mismatch')
    if production:
        _production_guard(Paths(paths.release,config=paths.config),profile,kind=='worker')
        if profile['capture_mode'] not in {'server_shadow','production_worker'}:raise ReleaseError('fixture is not production evidence')
    elif profile['capture_mode']!='local_fixture':raise ReleaseError('fixture mode required')
    dependency_checker(paths.release,kind,strict_extras=production)
    for filename,field in [('sites.yaml','source_config_sha256'),('task.json','task_sha256')]:
        _secure_file(paths.config/filename,owner=0 if production else None)
        if sha(paths.config/filename)!=profile[field]:raise ReleaseError('fixed input changed')
    site=require_site(load_sites(paths.config/'sites.yaml'),'genemedi-net');source=site.inquiry_source
    if not site.domains or not set(site.domains)<={'genemedi.net','www.genemedi.net'} or source is None or source.site_scope!='fixed_registered_source' or source.registered_database!='v2_genemedi_net' or source.registered_table!='contacts':raise ReleaseError('source binding differs from approved task')
    if production and (str(site.ga4_property_id)!='376561184' or site.gsc_property_url not in {'https://www.genemedi.net/','https://genemedi.net/','sc-domain:genemedi.net'}):raise ReleaseError('source property changed')
    runtime=provenance(paths.release,paths.config/'sites.yaml',profile['capture_mode'])
    task=_registration(_read(paths.config/'task.json'))
    if task['source_ids']!=registration_ids(site) or task['runtime_digest']!=fingerprint(runtime):raise ReleaseError('task/runtime/source fingerprint changed')
    _state_guard(paths,production)
    folders=list((paths.private/'runs').iterdir())
    if len(folders)>MAX_RUNS:raise ReleaseError('run capacity reached')
    for folder in folders:
        _safe(folder)
        if not folder.is_dir() or not re.fullmatch(r'[a-f0-9]{64}',folder.name):raise ReleaseError('unexpected private run entry')
        if (folder/'intent.json').exists():
            intent=_read(folder/'intent.json')
            if intent.get('registry_digest')!=fingerprint(task) or _key(task,intent.get('request_key'))!=folder.name:raise ReleaseError('registration rollover requires a new private generation')
    return profile,task,runtime,site


def _old_sync_idle():
    for unit in ['website-analytics-sync@full.service','website-analytics-sync@intraday.service']:
        r=subprocess.run(['/usr/bin/systemctl','show',unit,'-p','ActiveState','--value'],capture_output=True,text=True,check=True,timeout=5)
        if r.stdout.strip() not in {'inactive','failed'}:return False
    return True


def source_entry(paths,mode,*,production=True,reader_factory=None,environment_loader=None,dependency_checker=check_dependencies,clock=None,idle_checker=None,checkpoint=lambda stage:None):
    if mode not in {'daily','on_demand'}:raise ReleaseError('unknown invocation mode')
    if production and (reader_factory is not None or environment_loader is not None or clock is not None or idle_checker is not None or dependency_checker is not check_dependencies):raise ReleaseError('production fixture override rejected')
    if not production and reader_factory is None:raise ReleaseError('fixture reader required')
    profile,task,runtime,site=_common(paths,production,'worker',dependency_checker)
    now_fn=clock or (lambda:datetime.now(timezone.utc));now=now_fn()
    if mode=='daily':key=daily_key(now)
    else:
        p=paths.config/'manual-request.json';_secure_file(p,owner=0 if production else None)
        manual=_read(p);_fields(manual,{'contract','request_key'})
        if manual['contract']!='manual-refresh/v1':raise ReleaseError('invalid manual request')
        key=identifier(manual['request_key'])
    request=rolling_request(task,as_of=now,include_open=profile['include_open'])
    # A manual retry days later replays its original interval, not a new fetch.
    existing=paths.private/'runs'/_key(task,key)/'intent.json'
    if existing.exists():
        old=_read(_safe(existing))
        if old.get('registry_digest')!=fingerprint(task) or old.get('request_key')!=key:raise ReleaseError('existing invocation changed')
        request=old['request']
    def factory():
        if not (idle_checker or _old_sync_idle)():raise RefreshDeferred('old sync active')
        if production:_load_existing_environment(Paths(paths.release,config=paths.config),site)
        elif environment_loader:environment_loader(paths,site)
        if reader_factory:reader=reader_factory(paths.config/'sites.yaml')
        else:
            from website_analytics.analysis.worker import registered_reader
            reader=registered_reader(paths.config/'sites.yaml')
        if reader.site!=site:raise ReleaseError('reader differs from fixed configuration')
        if production:_bound_clients(reader)
        return reader
    result=produce(paths.private,task,runtime,request,request_key=key,mode=mode,reader_factory=factory,clock=now_fn,checkpoint=checkpoint)
    return {'contract':'repeatable-launch-result/v1','task_id':task['task_id'],'request_key':key,'source_state':result['state'],'source_outcome':result,'publication_started':False,'scheduler_installed':False}


def _requests(paths,task):
    folders=list((paths.private/'runs').iterdir())
    if len(folders)>MAX_RUNS:raise ReleaseError('run scan capacity reached')
    found=[]
    for folder in folders:
        _safe(folder)
        if not folder.is_dir() or not re.fullmatch(r'[a-f0-9]{64}',folder.name):raise ReleaseError('unexpected run state')
        if not (folder/'intent.json').is_file():raise ReleaseError('run intent incomplete; inspect without refetch')
        intent=_read(folder/'intent.json')
        if intent.get('registry_digest')!=fingerprint(task) or _key(task,intent.get('request_key'))!=folder.name:raise ReleaseError('queue registration mismatch')
        found.append((timestamp(intent['started_at']),intent['request_key'],folder))
    return sorted(found,key=lambda r:(r[0],r[1]))


def publisher_entry(paths,*,production=True,dependency_checker=check_dependencies,clock=None,checkpoint=lambda stage:None):
    if production and (clock is not None or dependency_checker is not check_dependencies):raise ReleaseError('production fixture override rejected')
    _,task,_,_=_common(paths,production,'query',dependency_checker)
    requests=_requests(paths,task);pending=[]
    for _,key,folder in requests:
        journal=paths.live/'.publication-journal'/_key(task,key)
        settled=(journal/'resolved.json').exists()
        if not settled and (journal/'outcome.json').exists():settled=_read(journal/'outcome.json').get('state') in {'published','failed'}
        if not settled:pending.append(key)
    if not pending and requests:pending=[requests[-1][1]] # repair receipt/status after a crash
    results=[];now_fn=clock or (lambda:datetime.now(timezone.utc))
    for key in pending[:16]:
        result=publish_run(paths.private,paths.live,task,request_key=key,read_gid=grp.getgrnam('analyticsquery').gr_gid if production else None,clock=now_fn,checkpoint=checkpoint)
        results.append({'request_key':key,'result':result})
        if result['state'] in {'needs_inspection','publication_uncertain','busy'}:break
    return {'contract':'repeatable-publication-result/v1','task_id':task['task_id'],'results':results,'remaining_unattempted':max(0,len(pending)-len(results)),'source_fetch_started':False,'scheduler_installed':False}


def main(role):
    if sys.argv[1:] or role not in {'daily','on_demand','publish'}:
        print(json.dumps({'status':'error','code':'fixed_entry_rejects_arguments_or_role'}));return 2
    try:
        logging.disable(logging.CRITICAL)
        paths=fixed_paths(Path(__file__).resolve().parents[3])
        result=publisher_entry(paths) if role=='publish' else source_entry(paths,role)
        print(json.dumps(result,sort_keys=True))
        states=[v['result']['state'] for v in result.get('results',[])] if role=='publish' else [result['source_state']]
        if any(s in {'needs_inspection','publication_uncertain'} for s in states):return 4
        if 'busy' in states or result.get('remaining_unattempted',0):return 75
        return 3 if 'failed' in states else 0
    except BaseException:
        print(json.dumps({'status':'error','code':'fixed_repeatable_preflight_or_state_failed'}));return 2
