"""Repeatable bounded producer and separate publisher. No auth or scheduler.

Paths and registration are operator inputs, never accepted by HTTP. Per-request
intents/outcomes are immutable; uncertain capture never causes implicit refetch.
"""
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
import fcntl,hashlib,os,sqlite3,tempfile
from website_analytics.analysis.contracts import AnalysisError,canonical_json,fingerprint,timestamp,strict_keys,identifier
from website_analytics.analysis.case_validation import runtime as validate_runtime,digest,code
from website_analytics.analysis.cases import CaseStore,render_case,scoped_completeness
from website_analytics.analysis.case_refresh import collect,build
from website_analytics.analysis.worker import parse_request,RunLog
from website_analytics.analysis.publication import export,validate,publish,PublicationUncertain
from website_analytics.analysis.release_runner import CASE_ID,URLS,VARIANTS
from website_analytics.analysis.completeness import registration_ids
from website_analytics.analysis.diagnostics import diagnostic_notes
from website_analytics.analysis.refresh_status import validate_status

MAX_RUNS=512


class RefreshDeferred(AnalysisError):
    """Known no-fetch deferral; message is never exported."""
    pass

def _now():return datetime.now(timezone.utc)
def _sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def _safe(p):
    p=Path(p)
    if any(x.is_symlink() for x in [p,*p.parents]):raise AnalysisError('symlink state path rejected')
    return p

def _read(p):
    from website_analytics.analysis.readers import read_json
    return read_json(_safe(p))

def _mkdir(p):_safe(p).mkdir(mode=0o700)

def _new(p,value):
    p=_safe(p);fd,name=tempfile.mkstemp(prefix='.pending-',dir=p.parent);temp=Path(name)
    try:
        os.chmod(temp,0o600)
        with os.fdopen(fd,'wb') as f:f.write((canonical_json(value)+'\n').encode());f.flush();os.fsync(f.fileno())
        os.link(temp,p) # atomic no-overwrite publication of a fully flushed document
        fd=os.open(p.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temp.unlink(missing_ok=True)

@contextmanager
def _lock(p):
    fd=os.open(_safe(p),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a+b') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:yield False
        else:
            try:yield True
            finally:fcntl.flock(f,fcntl.LOCK_UN)


def registration(seed,runtime,source_ids,*,task_id='purprox-refresh'):
    render_case(seed);validate_runtime(runtime)
    value={'contract':'repeatable-task/v1','task_id':task_id,'case_id':seed['case_id'],'raw_urls':seed['current_intervention']['raw_urls'],'variant_paths':seed['source_packet']['variant_paths_not_merged'],'intervention_digest':fingerprint(seed['current_intervention']),'runtime_digest':fingerprint(runtime),'source_ids':source_ids}
    return _registration(value)

def _registration(v):
    strict_keys(v,{'contract','task_id','case_id','raw_urls','variant_paths','intervention_digest','runtime_digest','source_ids'})
    if v['contract']!='repeatable-task/v1' or v['case_id']!=CASE_ID or v['raw_urls']!=list(URLS) or v['variant_paths']!=list(VARIANTS):raise AnalysisError('fixed registered task only')
    identifier(v['task_id']);digest(v['intervention_digest']);digest(v['runtime_digest']);strict_keys(v['source_ids'],{'ga4','gsc','inquiry'})
    for x in v['source_ids'].values():digest(x)
    return v

def initialize(private,seed):
    private=Path(private);_mkdir(private);_mkdir(private/'runs')
    validate(seed,'cases');export(seed,private/'history.sqlite','cases');os.chmod(private/'history.sqlite',0o600)

def daily_key(as_of):
    """Selection-calendar idempotency key, not an installed schedule."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:raise AnalysisError('aware selection time required')
    return 'daily-'+as_of.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()


def rolling_request(reg,*,as_of,include_open=True):
    _registration(reg)
    if as_of.tzinfo is None or as_of.utcoffset() is None:raise AnalysisError('aware selection time required')
    if type(include_open) is not bool:raise AnalysisError('explicit interval policy required')
    today=as_of.astimezone(ZoneInfo('America/Los_Angeles')).date();monday=today-timedelta(days=today.weekday())
    if include_open:
        weeks=[{'start':(monday-timedelta(days=7)).isoformat(),'end':(monday-timedelta(days=1)).isoformat(),'window_kind':'completed_week'},{'start':monday.isoformat(),'end':today.isoformat(),'window_kind':'open_week'}]
    else:
        weeks=[{'start':(monday-timedelta(days=n)).isoformat(),'end':(monday-timedelta(days=n-6)).isoformat(),'window_kind':'completed_week'} for n in (14,7)]
    raw={'contract':'bounded-worker/v2','site':'genemedi-net','raw_urls':list(reg['raw_urls']),'variant_paths':list(reg['variant_paths']),'selection_timezone':'America/Los_Angeles','weeks':weeks}
    parse_request(raw,as_of=as_of);return raw


def _key(reg,key):identifier(key);return fingerprint({'task_id':reg['task_id'],'request_key':key,'registration_digest':fingerprint(reg)})

def _previous(store,reg):
    with store.readonly() as db:row=db.execute("SELECT evidence_id FROM case_evidence WHERE case_id=? AND json_extract(payload,'$.kind')='continuous_trend' ORDER BY julianday(json_extract(payload,'$.source_packet.queried_at')) DESC,rowid DESC LIMIT 1",(reg['case_id'],)).fetchone()
    if not row:raise AnalysisError('continuous history seed required')
    record=store.get(reg['case_id'],row[0])
    if fingerprint(record['current_intervention'])!=reg['intervention_digest']:raise AnalysisError('intervention registration mismatch')
    return record

def _check_record(record,reg,intent):
    render_case(record);packet=record['source_packet'];validate_runtime(packet['runtime'])
    if record['case_id']!=reg['case_id'] or fingerprint(packet['runtime'])!=reg['runtime_digest'] or packet.get('worker_source_ids')!=reg['source_ids'] or fingerprint(record['current_intervention'])!=reg['intervention_digest'] or record['draft']['raw_urls']!=reg['raw_urls'] or packet['variant_paths_not_merged']!=reg['variant_paths']:raise AnalysisError('capture registration mismatch')
    if [(r['start'],r['end']) for r in packet['weeks']]!=[(r['start'],r['end']) for r in intent['request']['weeks']]:raise AnalysisError('capture interval mismatch')
    if timestamp(packet['queried_at'])<timestamp(intent['started_at']):raise AnalysisError('capture precedes intent')
    if (packet['protocol']=='seo-period-read/2')!=(intent['request']['contract']=='bounded-worker/v2'):raise AnalysisError('interval protocol mismatch')


def _ready(private,folder,reg,intent,record,clock,checkpoint):
    _check_record(record,reg,intent)
    if any(r['request']['state']!='succeeded' for r in scoped_completeness(record)['source_windows']):raise AnalysisError('source_request_failed')
    CaseStore(private/'history.sqlite').put(record);checkpoint('after_history')
    candidate=folder/'candidate.sqlite'
    if not candidate.exists():export(private/'history.sqlite',candidate,'cases')
    validate(candidate,'cases');checkpoint('after_export')
    out={'contract':'refresh-handoff/v1','registry_digest':fingerprint(reg),'intent_digest':fingerprint(intent),'state':'ready','finished_at':clock().isoformat(),'evidence_id':fingerprint(record),'candidate_sha256':_sha(candidate),'error_codes':[]}
    _new(folder/'outcome.json',out);return out


def produce(private,reg,runtime,request,*,request_key,mode,reader_factory,clock=_now,checkpoint=lambda stage:None):
    private=_safe(private);now=clock()
    if now.tzinfo is None or now.utcoffset() is None:raise AnalysisError('aware capture time required')
    _registration(reg);validate_runtime(runtime)
    if fingerprint(runtime)!=reg['runtime_digest'] or request['raw_urls']!=reg['raw_urls'] or request['variant_paths']!=reg['variant_paths']:raise AnalysisError('task scope/runtime mismatch')
    if mode not in {'daily','on_demand'}:raise AnalysisError('invalid invocation mode')
    runs=_safe(private/'runs');token=_key(reg,request_key);folder=runs/token
    validation_time=timestamp(_read(folder/'intent.json')['started_at']) if (folder/'intent.json').exists() else now
    parse_request(request,as_of=validation_time)
    operation={'registry_digest':fingerprint(reg),'request':request,'request_key':request_key,'mode':mode}
    with _lock(private/'.producer.lock') as locked:
        if not locked:return {'state':'busy','source_fetch_started':False}
        if folder.exists():
            if not (folder/'intent.json').exists():return {'state':'needs_inspection','source_fetch_started':False}
            intent=_read(folder/'intent.json')
            if any(intent[k]!=v for k,v in operation.items()):raise AnalysisError('immutable request-key conflict')
            return _read(folder/'outcome.json') if (folder/'outcome.json').exists() else {'state':'needs_inspection','source_fetch_started':False}
        if any(not (p/'outcome.json').exists() for p in runs.iterdir() if p.is_dir()):return {'state':'needs_inspection','source_fetch_started':False}
        if len(list(runs.iterdir()))>=MAX_RUNS or CaseStore(private/'history.sqlite').version_count(reg['case_id'])>=512:raise AnalysisError('capacity reached before fetch; no evidence deleted')
        previous=_previous(CaseStore(private/'history.sqlite'),reg);_mkdir(folder)
        intent={'contract':'refresh-intent/v1',**operation,'started_at':now.isoformat(),'previous_evidence_id':fingerprint(previous)}
        _new(folder/'intent.json',intent);checkpoint('after_intent')
        try:
            reader=reader_factory()
            if registration_ids(reader.site)!=reg['source_ids']:raise AnalysisError('source registration mismatch')
            packet=collect(previous,request,reader,run_id=request_key,run_log=RunLog(folder/'events.sqlite'),runtime=runtime,clock=clock)
            record=build(previous,request,packet,as_of=clock());_new(folder/'capture.json',record);checkpoint('after_capture')
            return _ready(private,folder,reg,intent,record,clock,checkpoint)
        except Exception as error:
            out={'contract':'refresh-handoff/v1','registry_digest':fingerprint(reg),'intent_digest':fingerprint(intent),'state':'failed','finished_at':clock().isoformat(),'evidence_id':None,'candidate_sha256':None,'error_codes':['old_sync_active_no_fetch'] if isinstance(error,RefreshDeferred) else (list(diagnostic_notes(error))[:8] or ['capture_failed'])}
            _new(folder/'outcome.json',out);return out


def recover_capture(private,reg,*,request_key,clock=_now,checkpoint=lambda stage:None):
    """Explicit saved-capture recovery. Never creates a reader or refetches."""
    private=_safe(private);_registration(reg);folder=private/'runs'/_key(reg,request_key)
    with _lock(private/'.producer.lock') as locked:
        if not locked:raise AnalysisError('producer busy')
        intent=_read(folder/'intent.json')
        if intent['registry_digest']!=fingerprint(reg):raise AnalysisError('registry changed')
        if (folder/'outcome.json').exists():return _read(folder/'outcome.json')
        if not (folder/'capture.json').exists():return {'state':'needs_explicit_abandon_no_saved_capture','source_fetch_started':False}
        record=_read(folder/'capture.json');render_case(record)
        return _ready(private,folder,reg,intent,record,clock,checkpoint)


def abandon_capture(private,reg,*,request_key,clock=_now):
    """Explicit operator decision to unblock a pre-capture crash; keep intent."""
    private=_safe(private);folder=private/'runs'/_key(reg,request_key)
    with _lock(private/'.producer.lock') as locked:
        if not locked:raise AnalysisError('producer busy')
        intent=_read(folder/'intent.json')
        if intent['registry_digest']!=fingerprint(reg) or (folder/'capture.json').exists():raise AnalysisError('saved capture must be recovered')
        if (folder/'outcome.json').exists():return _read(folder/'outcome.json')
        out={'contract':'refresh-handoff/v1','registry_digest':fingerprint(reg),'intent_digest':fingerprint(intent),'state':'failed','finished_at':clock().isoformat(),'evidence_id':None,'candidate_sha256':None,'error_codes':['operator_abandoned_uncertain_capture']}
        _new(folder/'outcome.json',out);return out


def _handoff(private,reg,key):
    folder=_safe(private/'runs'/_key(reg,key));intent=_read(folder/'intent.json');out=_read(folder/'outcome.json')
    strict_keys(intent,{'contract','registry_digest','request','request_key','mode','started_at','previous_evidence_id'})
    strict_keys(out,{'contract','registry_digest','intent_digest','state','finished_at','evidence_id','candidate_sha256','error_codes'})
    if intent['contract']!='refresh-intent/v1' or out['contract']!='refresh-handoff/v1' or intent['request_key']!=key or intent['registry_digest']!=out['registry_digest'] or out['registry_digest']!=fingerprint(reg) or out['intent_digest']!=fingerprint(intent) or out['state'] not in {'ready','failed'}:raise AnalysisError('invalid handoff identity')
    timestamp(intent['started_at']);timestamp(out['finished_at'])
    if timestamp(out['finished_at'])<timestamp(intent['started_at']):raise AnalysisError('handoff clock order mismatch')
    if out['state']=='ready':digest(out['evidence_id']);digest(out['candidate_sha256'])
    elif out['evidence_id'] is not None or out['candidate_sha256'] is not None:raise AnalysisError('failed capture cannot name a candidate')
    parse_request(intent['request'],as_of=timestamp(intent['started_at']))
    if intent['mode'] not in {'daily','on_demand'} or intent['request']['raw_urls']!=reg['raw_urls'] or intent['request']['variant_paths']!=reg['variant_paths']:raise AnalysisError('handoff scope mismatch')
    if not isinstance(out['error_codes'],list) or len(out['error_codes'])>8:raise AnalysisError('invalid diagnostic budget')
    for x in out['error_codes']:code(x)
    return folder,intent,out


def _status(public,reg,intent,out,state,published=None,gid=None):
    path=public/'refresh-status.json';old=validate_status(_read(path)) if path.exists() else None
    if old and old['task_id']!=reg['task_id']:raise AnalysisError('status belongs to another registered task')
    attempt={'run_id':intent['request_key'],'mode':intent['mode'],'started_at':intent['started_at'],'finished_at':out['finished_at'],'state':state,'error_codes':out['error_codes']}
    if old and timestamp(old['last_attempt']['started_at'])>timestamp(attempt['started_at']):return
    value={'contract':'refresh-health/v1','task_id':reg['task_id'],'last_attempt':attempt,'last_published':published or (old['last_published'] if old else None)}
    validate_status(value)
    if old==value:return
    tmp=public/'.status-pending'
    if tmp.exists():
        # A crash before rename may leave only this fixed, validated sidecar.
        validate_status(_read(tmp));tmp.unlink()
    _new(tmp,value)
    if gid is not None:os.chown(tmp,-1,gid)
    os.chmod(tmp,0o440);os.replace(tmp,path)
    fd=os.open(public,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def publish_run(private,public,reg,*,request_key,read_gid=None,clock=_now,checkpoint=lambda stage:None):
    """Administrator role; derives only whitelisted paths, never calls sources."""
    private=_safe(private);public=_safe(public);_registration(reg)
    folder=_safe(private/'runs'/_key(reg,request_key))
    if (folder/'intent.json').exists() and not (folder/'outcome.json').exists():
        intent=_read(folder/'intent.json')
        strict_keys(intent,{'contract','registry_digest','request','request_key','mode','started_at','previous_evidence_id'})
        if intent['contract']!='refresh-intent/v1' or intent['request_key']!=request_key or intent['registry_digest']!=fingerprint(reg):raise AnalysisError('pending registration mismatch')
        out={'finished_at':None,'error_codes':['capture_outcome_unknown_no_refetch']}
        with _lock(public/'.coordinator.lock') as locked:
            if not locked:return {'state':'busy','publication_started':False}
            _status(public,reg,intent,out,'needs_inspection',gid=read_gid)
        return {'state':'needs_inspection','source_fetch_started':False}
    folder,intent,out=_handoff(private,reg,request_key)
    with _lock(public/'.coordinator.lock') as locked:
        if not locked:return {'state':'busy','publication_started':False}
        journal=public/'.publication-journal'
        if not journal.exists():_mkdir(journal)
        token=_key(reg,request_key);own=journal/token
        if not own.exists():_mkdir(own)
        # Do not silently bypass another uncertain publication.
        for other in journal.iterdir():
            if other!=own and (other/'intent.json').exists() and not (other/'resolved.json').exists():
                if not (other/'outcome.json').exists() or _read(other/'outcome.json')['state']=='publication_uncertain':return {'state':'needs_inspection','publication_started':False}
        outcome=own/'outcome.json';resolved=own/'resolved.json';target=public/'cases.sqlite'
        saved=_read(resolved) if resolved.exists() else (_read(outcome) if outcome.exists() else None)
        if saved and saved['state']!='publication_uncertain':
            _status(public,reg,intent,{**out,'error_codes':saved.get('error_codes',[])},saved['state'],saved.get('last_published'),read_gid)
            return saved
        if out['state']=='failed':
            value={'state':'failed','stage':'capture','candidate_sha256':None,'error_codes':out['error_codes']}
            _new(outcome,value);_status(public,reg,intent,out,'failed',gid=read_gid);return value
        candidate=folder/'candidate.sqlite';validate(candidate,'cases')
        if _sha(candidate)!=out['candidate_sha256']:raise AnalysisError('handoff candidate changed')
        record=CaseStore(candidate).get(reg['case_id'],out['evidence_id']);packet=record['source_packet']
        _check_record(record,reg,intent)
        if fingerprint(packet['runtime'])!=reg['runtime_digest'] or packet.get('worker_source_ids')!=reg['source_ids'] or fingerprint(record['current_intervention'])!=reg['intervention_digest'] or record['draft']['raw_urls']!=reg['raw_urls'] or packet['variant_paths_not_merged']!=reg['variant_paths']:raise AnalysisError('candidate identity changed')
        if [(x['start'],x['end']) for x in packet['weeks']]!=[(x['start'],x['end']) for x in intent['request']['weeks']]:raise AnalysisError('candidate periods changed')
        facets=scoped_completeness(record)
        if any(r['request']['state']!='succeeded' for r in facets['source_windows']):raise AnalysisError('candidate source request failed')
        existing=set()
        if target.exists():
            with CaseStore(target).readonly() as db:existing={r[0] for r in db.execute('SELECT evidence_id FROM case_evidence')}
        with CaseStore(candidate).readonly() as db:added=[r for r in db.execute('SELECT case_id,evidence_id FROM case_evidence') if r[1] not in existing]
        for case_id,eid in added:
            if case_id!=reg['case_id']:raise AnalysisError('unregistered extra candidate case')
            extra=CaseStore(candidate).get(case_id,eid);ep=extra['source_packet']
            if fingerprint(ep['runtime'])!=reg['runtime_digest'] or ep.get('worker_source_ids')!=reg['source_ids'] or fingerprint(extra['current_intervention'])!=reg['intervention_digest'] or extra['draft']['raw_urls']!=reg['raw_urls'] or ep['variant_paths_not_merged']!=reg['variant_paths']:raise AnalysisError('unregistered extra candidate evidence')
            bounded={'contract':'bounded-worker/v2' if ep['protocol']=='seo-period-read/2' else 'bounded-worker/v1','site':'genemedi-net','raw_urls':reg['raw_urls'],'variant_paths':reg['variant_paths'],'weeks':[{k:r[k] for k in (('start','end','window_kind') if ep['protocol']=='seo-period-read/2' else ('start','end'))} for r in ep['weeks']]}
            if bounded['contract']=='bounded-worker/v2':bounded['selection_timezone']=ep['selection_timezone']
            parse_request(bounded,as_of=timestamp(ep['queried_at']))
        pi=own/'intent.json'
        if not pi.exists():
            previous=_sha(target) if target.exists() else None
            if target.exists():
                validate(target,'cases');prior=_previous(CaseStore(target),reg)
                if timestamp(packet['queried_at'])<=timestamp(prior['source_packet']['queried_at']):raise AnalysisError('stale publication rejected')
            _new(pi,{'candidate_sha256':out['candidate_sha256'],'previous_sha256':previous,'intent_digest':fingerprint(intent)})
        approved=_read(pi)
        if approved['candidate_sha256']!=out['candidate_sha256'] or approved['intent_digest']!=fingerprint(intent):raise AnalysisError('publication intent conflict')
        recovered=target.exists() and _sha(target)==out['candidate_sha256']
        if not recovered:
            if saved:return {'state':'needs_inspection','publication_started':False}
            try:
                publish(candidate,public,'cases',expected_candidate=out['candidate_sha256'],expected_previous=approved['previous_sha256'],read_gid=read_gid,checkpoint=checkpoint)
                checkpoint('before_publication_receipt')
            except PublicationUncertain:
                value={'state':'publication_uncertain','stage':'publish','candidate_sha256':out['candidate_sha256'],'error_codes':['inspect_target_before_retry']}
                _new(outcome,value);_status(public,reg,intent,{**out,'error_codes':value['error_codes']},'publication_uncertain',gid=read_gid);return value
            except Exception:
                value={'state':'failed','stage':'publish','candidate_sha256':out['candidate_sha256'],'error_codes':['publication_failed']}
                _new(outcome,value);_status(public,reg,intent,{**out,'error_codes':value['error_codes']},'failed',gid=read_gid);return value
        validate(target,'cases')
        last={'evidence_id':out['evidence_id'],'candidate_sha256':out['candidate_sha256'],'captured_at':packet['queried_at'],'publication_verified_at':clock().isoformat(),'strict_complete':facets['strict_complete'],'sources':[{'source':r['source'],'scope_digest':r['scope_digest'],'start':r['scope']['start'],'end':r['scope']['end'],'calendar':r['calendar']['name'],'observed_complete_through':r['maturity']['observed_complete_through'],'request_state':r['request']['state']} for r in facets['source_windows']]}
        value={'state':'published','evidence_id':out['evidence_id'],'candidate_sha256':out['candidate_sha256'],'inspected_existing_target_without_republication':recovered,'strict_complete':facets['strict_complete'],'last_published':last,'error_codes':[]}
        _new(resolved if outcome.exists() else outcome,value);checkpoint('before_status')
        _status(public,reg,intent,out,'published',last,read_gid)
        return value


def rollback_target(public,reg,*,request_key):
    """Verified prior immutable reader generation; no live overwrite or deletion."""
    public=_safe(public);approved=_read(public/'.publication-journal'/_key(reg,request_key)/'intent.json');previous=approved['previous_sha256']
    if previous is None:raise AnalysisError('no prior generation')
    digest(previous);path=public/'.history'/('cases-'+previous+'.sqlite');validate(path,'cases')
    if _sha(path)!=previous:raise AnalysisError('rollback generation changed')
    return path
