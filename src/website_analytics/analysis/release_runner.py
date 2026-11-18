"""Fixed, argument-free source/publisher entries; never used by HTTP agents.

Production paths and identities are constants. Internal dependency injection is
for isolated tests only; command-line entries cannot select paths or factories.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import grp
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import sqlite3
import stat
import subprocess
import sys

from website_analytics.analysis.release_integrity import ReleaseError, verify, check_dependencies, provenance, sha, encoded

CASE_ID="purprox-seo-four-pages"
URLS=("https://www.genemedi.net/i/purprox-aavpure-affinity-resin",
    "https://www.genemedi.net/i/purprox-aavpure-pre-packed-aav-affinity-chromatography-column",
    "https://www.genemedi.net/i/purprox-aaveasy-spin-columns",
    "https://www.genemedi.net/i/PurProX-AAVFull-Enrichment-Kit")
VARIANTS=("/i/purprox-aavfull-enrichment-kit",)


@dataclass(frozen=True)
class Paths:
    release:Path
    config:Path=Path("/etc/website-analytics-refresh")
    state:Path=Path("/var/lib/website-analytics-refresh/worker")
    outbox:Path=Path("/var/lib/website-analytics-refresh/outbox")
    live:Path=Path("/var/lib/website-analytics-query/live")
    env:Path=Path("/opt/website-analytics/shared/analytics.env")
    adc:Path=Path("/var/lib/website-analytics/.config/gcloud/application_default_credentials.json")


def _read(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>2_000_000:raise ReleaseError("invalid fixed input")
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ReleaseError("duplicate input key")
            result[key]=value
        return result
    return json.loads(path.read_text(),object_pairs_hook=unique)


def _fields(value,expected):
    if not isinstance(value,dict) or set(value)!=set(expected):raise ReleaseError("invalid fixed profile fields")


def _digest(value):
    if not isinstance(value,str) or not re.fullmatch(r"[a-f0-9]{64}",value):raise ReleaseError("invalid approved digest")


def _secure_file(path,*,owner=None):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ReleaseError("fixed file unavailable")
    s=path.stat()
    if s.st_mode&0o022 or owner is not None and s.st_uid!=owner:raise ReleaseError("unsafe fixed file ownership/mode")


def _private_dir(path):
    s=path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(s.st_mode) or stat.S_IMODE(s.st_mode)!=0o700 or s.st_uid!=os.getuid():raise ReleaseError("private directory preflight failed")


def _production_guard(paths,profile,source):
    if sys.platform!="linux" or platform.machine()!="x86_64" or sys.version_info[:2]!=(3,12):raise ReleaseError("unvalidated production runtime platform")
    if source and os.getuid()!=pwd.getpwnam("websiteanalytics").pw_uid or not source and os.geteuid()!=0:raise ReleaseError("wrong service identity")
    config=paths.config.lstat()
    if paths.config.is_symlink() or config.st_uid!=0 or config.st_mode&0o022:raise ReleaseError("unsafe fixed config directory")
    for p in paths.config.iterdir():
        if p.is_file():_secure_file(p,owner=0)
    for p in [paths.release,*paths.release.rglob("*")]:
        if "__pycache__" not in p.parts and not p.is_symlink():
            s=p.stat()
            if s.st_uid!=0 or s.st_mode&0o022:raise ReleaseError("release must be root owned and not group writable")
    if source and profile["capture_mode"] not in {"server_shadow","production_worker"}:raise ReleaseError("fixture capture cannot run as production")


def _load_existing_environment(paths,site):
    key=site.inquiry_source.credential_env
    value=None
    for line in paths.env.open():
        name,sep,text=line.strip().partition("=")
        if sep and name==key:
            words=shlex.split(text,comments=False)
            if len(words)!=1:raise ReleaseError("existing credential reference unreadable")
            value=words[0];break
    if not value:raise ReleaseError("existing source credential unavailable")
    os.environ[key]=value
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"]=str(paths.adc)
    _secure_file(paths.adc,owner=os.getuid())
    if stat.S_IMODE(paths.adc.stat().st_mode)!=0o600:raise ReleaseError("unexpected ADC file mode")
    result=subprocess.run(["/usr/bin/systemctl","show","website-analytics-sync@full.service","--value","-p","Environment"],capture_output=True,text=True,check=True,timeout=5)
    for item in shlex.split(result.stdout):
        name,sep,value=item.partition("=")
        if sep and name.lower() in {"http_proxy","https_proxy","no_proxy","all_proxy"}:os.environ[name]=value


def _bound_clients(reader):
    from website_analytics.retry import RetryPolicy
    ga,gs=reader.adapters["ga4"],reader.adapters["gsc"]
    ga._retry_policy=gs._retry_policy=RetryPolicy(max_attempts=1)
    if hasattr(ga,"_client"):
        original=ga._client.run_report
        ga._client.run_report=lambda request:original(request=request,timeout=20,retry=None)
    http=getattr(getattr(gs,"_service",None),"_http",None)
    if hasattr(http,"http"):http.http.timeout=20


def _write_receipt(path,value):
    temporary=path.with_name(".receipt-pending")
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,"wb") as stream:stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path)  # no overwrite on a concurrent/existing result
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)


def source_pilot(paths,*,production=True,reader_factory=None,environment_loader=None,dependency_checker=check_dependencies,checkpoint=lambda stage:None,clock=None):
    """No public evidence writes. An interrupted run cannot silently re-fetch."""
    profile_path=paths.config/"pilot.json";profile=_read(profile_path)
    _fields(profile,{"contract","release_manifest_sha256","source_config_sha256","request_sha256","metadata_windows_sha256","seed_database_sha256","previous_evidence_id","case_id","run_id","capture_mode","expected_python_version"})
    if profile["contract"]!="source-pilot/v1" or profile["case_id"]!=CASE_ID or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}",profile["run_id"]):raise ReleaseError("invalid fixed pilot identity")
    for key in ("release_manifest_sha256","source_config_sha256","request_sha256","metadata_windows_sha256","seed_database_sha256","previous_evidence_id"):_digest(profile[key])
    verify(paths.release,expected_manifest=profile["release_manifest_sha256"])
    if platform.python_version()!=profile["expected_python_version"]:raise ReleaseError("Python patch version mismatch")
    if production:_production_guard(paths,profile,True)
    elif profile["capture_mode"]!="local_fixture" or reader_factory is None:raise ReleaseError("isolated fixture requires injected reader")
    dependency_checker(paths.release,"worker",strict_extras=production)
    _private_dir(paths.state);_private_dir(paths.outbox)
    for filename,key in [("sites.yaml","source_config_sha256"),("request.json","request_sha256"),("metadata-windows.json","metadata_windows_sha256")]:
        _secure_file(paths.config/filename,owner=0 if production else None)
        if sha(paths.config/filename)!=profile[key]:raise ReleaseError("approved fixed input changed")
    from website_analytics.analysis.worker import parse_request,parse_metadata_windows,validate_metadata_windows,RunLog,registered_reader
    from website_analytics.analysis.contracts import fingerprint
    from website_analytics.analysis.cases import CaseStore
    from website_analytics.analysis.case_refresh import run
    from website_analytics.analysis.publication import export,validate
    from website_analytics.config import load_sites,require_site
    from datetime import datetime,timezone
    if production and clock is not None:raise ReleaseError("fixture clock cannot be used in production")
    raw=_read(paths.config/"request.json");now=clock() if clock is not None else datetime.now(timezone.utc)
    _,_,periods=parse_request(raw,as_of=now)
    if tuple(raw["raw_urls"])!=URLS or tuple(raw["variant_paths"])!=VARIANTS:raise ReleaseError("pilot URL scope changed")
    windows=parse_metadata_windows(_read(paths.config/"metadata-windows.json"));validate_metadata_windows(periods,windows,now)
    site=require_site(load_sites(paths.config/"sites.yaml"),"genemedi-net")
    source=site.inquiry_source
    if source is None or source.site_scope!="fixed_registered_source" or source.registered_database!="v2_genemedi_net" or source.registered_table!="contacts":raise ReleaseError("pilot source binding not approved")
    store=CaseStore(paths.state/"cases.sqlite");_secure_file(store.path,owner=os.getuid())
    receipt_path=paths.state/"pilot-receipt.json";candidate=paths.outbox/"cases.sqlite";profile_sha=sha(profile_path)
    if receipt_path.is_symlink() or (paths.state/"runs.sqlite").is_symlink():raise ReleaseError("unsafe private pilot state")
    lock_fd=os.open(paths.state/".pilot.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(lock_fd,"a+b") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if receipt_path.exists():
            done=_read(receipt_path)
            if done.get("profile_sha256")!=profile_sha or sha(candidate)!=done.get("candidate_sha256"):raise ReleaseError("completed pilot state changed")
            validate(candidate,"cases");store.get(CASE_ID,done["evidence_id"])
            return {**done,"status":"already_completed_no_source_request"}
        if candidate.exists() or candidate.is_symlink() or sha(store.path)!=profile["seed_database_sha256"]:raise ReleaseError("pending or changed pilot state; inspect before retry")
        log_path=paths.state/"runs.sqlite"
        if log_path.exists():
            with sqlite3.connect(log_path.resolve().as_uri()+"?mode=ro",uri=True) as db:
                if db.execute("SELECT count(*) FROM events WHERE run_id=?",(profile["run_id"],)).fetchone()[0]:raise ReleaseError("prior run outcome requires inspection; no automatic re-fetch")
        previous=store.get(CASE_ID,profile["previous_evidence_id"])
        scope=fingerprint({"request":raw,"metadata_windows":{k:{"start":v.start.isoformat(),"end":v.end.isoformat()} for k,v in windows.items()}})
        log=RunLog(log_path);log.append(profile["run_id"],scope,"preflight","started")
        stage="preflight"
        try:
            checkpoint("before_sources")
            if production:(environment_loader or _load_existing_environment)(paths,site)
            elif environment_loader is not None:environment_loader(paths,site)
            reader=(reader_factory or registered_reader)(paths.config/"sites.yaml")
            if reader.site!=site:raise ReleaseError("reader registry differs from approved config")
            _bound_clients(reader) if production else None
            runtime=provenance(paths.release,paths.config/"sites.yaml",profile["capture_mode"])
            stage="capture"
            receipt=run(previous,raw,reader,store,run_id=profile["run_id"],run_log=log,runtime=runtime,metadata_windows=windows,as_of=clock() if clock is not None else None)
            checkpoint("after_capture")
            stage="export"
            exported=export(store.path,candidate,"cases");checkpoint("after_export")
            done={"contract":"source-pilot-receipt/v1","run_id":profile["run_id"],"profile_sha256":profile_sha,
                "release_manifest_sha256":profile["release_manifest_sha256"],"capture_mode":profile["capture_mode"],
                "evidence_id":receipt["evidence_id"],"candidate_sha256":exported["sha256"],"candidate_bytes":exported["bytes"],
                "production_published":False,"data_finality_claim":False,"status":"candidate_ready"}
            _write_receipt(receipt_path,done);return done
        except BaseException:
            log.append(profile["run_id"],scope,stage,"failed")
            raise


def publish_candidate(paths,*,production=True,dependency_checker=check_dependencies,checkpoint=lambda stage:None):
    profile=_read(paths.config/"publish.json")
    _fields(profile,{"contract","release_manifest_sha256","candidate_sha256","expected_previous_sha256"})
    if profile["contract"]!="fixed-publication/v1":raise ReleaseError("invalid fixed publication profile")
    for key in ("release_manifest_sha256","candidate_sha256"):_digest(profile[key])
    if profile["expected_previous_sha256"] is not None:_digest(profile["expected_previous_sha256"])
    verify(paths.release,expected_manifest=profile["release_manifest_sha256"])
    if production:_production_guard(paths,profile,False)
    dependency_checker(paths.release,"query",strict_extras=production)
    from website_analytics.analysis.publication import publish,validate
    from website_analytics.analysis.cases import CaseStore
    def reject_fixture(path):
        if not production:return
        store=CaseStore(path)
        with store.readonly() as db:
            for encoded_row, in db.execute("SELECT payload FROM case_evidence"):
                if json.loads(encoded_row)["source_packet"]["runtime"].get("capture_mode")=="local_fixture":raise ReleaseError("fixture evidence cannot be published by production entry")
    target=paths.live/"cases.sqlite"
    if target.exists() and not target.is_symlink() and sha(target)==profile["candidate_sha256"]:
        validate(target,"cases");reject_fixture(target);return {"status":"already_published_verified","sha256":profile["candidate_sha256"]}
    candidate=paths.outbox/"cases.sqlite";validate(candidate,"cases")
    if sha(candidate)!=profile["candidate_sha256"]:raise ReleaseError("candidate not approved")
    reject_fixture(candidate)
    result=publish(candidate,paths.live,"cases",expected_candidate=profile["candidate_sha256"],expected_previous=profile["expected_previous_sha256"],read_gid=grp.getgrnam("analyticsquery").gr_gid if production else None,checkpoint=checkpoint)
    return {"status":"published",**result}


def query_service(paths,*,production=True,dependency_checker=check_dependencies):
    verify(paths.release)
    if production:
        if sys.platform!="linux" or platform.machine()!="x86_64" or sys.version_info[:2]!=(3,12) or os.getuid()!=pwd.getpwnam("analyticsquery").pw_uid:raise ReleaseError("wrong query runtime identity")
        for path in [paths.release,*paths.release.rglob("*")]:
            if not path.is_symlink() and (path.stat().st_uid!=0 or path.stat().st_mode&0o022):raise ReleaseError("unsafe query release")
    dependency_checker(paths.release,"query",strict_extras=production)
    from website_analytics.analysis.publication import validate
    validate(paths.live/"cases.sqlite","cases")
    from website_analytics.analysis.service import main as serve
    sys.argv=["fixed-query","--cases",str(paths.live/"cases.sqlite"),"--port","8765"]
    if (paths.live/"refresh-status.json").exists():sys.argv.extend(["--refresh-status",str(paths.live/"refresh-status.json")])
    return serve()


def main(role):
    if sys.argv[1:]:print(json.dumps({"status":"error","code":"fixed_entry_accepts_no_arguments"}));return 2
    paths=Paths(Path(__file__).resolve().parents[3])
    try:
        logging.disable(logging.CRITICAL)  # Third-party error bodies never reach the journal.
        if role=="query":return query_service(paths)
        result=source_pilot(paths) if role=="source" else publish_candidate(paths)
        print(json.dumps(result,sort_keys=True));return 0
    except BaseException as error:
        from website_analytics.analysis.publication import PublicationUncertain
        uncertain=isinstance(error,PublicationUncertain)
        print(json.dumps({"status":"uncertain" if uncertain else "error","code":"inspect_target_before_retry" if uncertain else "fixed_release_entry_failed"}));return 4 if uncertain else 2
