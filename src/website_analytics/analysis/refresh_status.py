"""Credential-free read-only refresh-health projection; no writer imports."""
from pathlib import Path
import hashlib
from website_analytics.analysis.contracts import AnalysisError,strict_keys,timestamp,identifier,calendar_date,timezone_name
from website_analytics.analysis.case_validation import digest,code


def validate_status(value):
    strict_keys(value,{'contract','task_id','last_attempt','last_published'})
    if value['contract']!='refresh-health/v1':raise AnalysisError('invalid health contract')
    identifier(value['task_id']);a=value['last_attempt']
    strict_keys(a,{'run_id','mode','started_at','finished_at','state','error_codes'})
    identifier(a['run_id']);timestamp(a['started_at'])
    if a['finished_at'] is not None:timestamp(a['finished_at'])
    elif a['state']!='needs_inspection':raise AnalysisError('unknown finish time requires pending inspection')
    if a['mode'] not in {'daily','on_demand'} or a['state'] not in {'published','failed','publication_uncertain','needs_inspection'}:raise AnalysisError('invalid health state')
    if not isinstance(a['error_codes'],list) or len(a['error_codes'])>8:raise AnalysisError('invalid diagnostic budget')
    for item in a['error_codes']:code(item)
    p=value['last_published']
    if p is not None:
        strict_keys(p,{'evidence_id','candidate_sha256','captured_at','publication_verified_at','strict_complete','sources'})
        digest(p['evidence_id']);digest(p['candidate_sha256']);timestamp(p['captured_at']);timestamp(p['publication_verified_at'])
        if type(p['strict_complete']) is not bool or not isinstance(p['sources'],list) or len(p['sources'])>36:raise AnalysisError('invalid health summary')
        for row in p['sources']:
            strict_keys(row,{'source','scope_digest','start','end','calendar','observed_complete_through','request_state'})
            if row['source'] not in {'ga4','gsc','inquiry'} or row['request_state'] not in {'succeeded','failed','unknown','unavailable'}:raise AnalysisError('invalid source health')
            digest(row['scope_digest']);calendar_date(row['start']);calendar_date(row['end'])
            if row['calendar'] is not None:timezone_name(row['calendar'])
            if row['observed_complete_through'] is not None:calendar_date(row['observed_complete_through'])
    return value


class StatusReader:
    """Only startup configuration selects the status path, never HTTP inputs."""
    def __init__(self,path):self.path=Path(path)
    def query(self,request):
        strict_keys(request,{'contract','operation','task_id'})
        if request['contract']!='batch-service/v1' or request['operation']!='refresh_status':raise AnalysisError('unsupported health query')
        identifier(request['task_id'])
        if self.path.is_symlink() or self.path.stat().st_size>100000:raise AnalysisError('invalid health file')
        from website_analytics.analysis.readers import read_json
        value=validate_status(read_json(self.path))
        if request['task_id']!=value['task_id']:raise KeyError('task not found')
        p=value['last_published'];target=self.path.parent/'cases.sqlite'
        matches=bool(p and target.is_file() and not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest()==p['candidate_sha256'])
        return {**value,'contract':'batch-service/v1','status_contract':'refresh-health/v1','operation':'refresh_status','status':'ok' if matches and value['last_attempt']['state']=='published' and p['strict_complete'] else 'partial','live_sources':False,'publication_matches_selected_evidence':matches,'scheduler_installed_by_this_module':False}
