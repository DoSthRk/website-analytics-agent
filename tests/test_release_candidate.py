"""Local release fixtures only; production entry never calls injected clients."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import zipfile
from datetime import datetime,timezone

import pytest
import yaml

from test_analysis_cases import record
from test_analysis_worker import reader, request as worker_request
from test_analysis_case_service import running
from website_analytics.analysis.case_validation import runtime
from website_analytics.analysis.cases import CaseStore,make_case_record,query_case
from website_analytics.analysis.client import query_case as client_query
from website_analytics.analysis.publication import PublicationUncertain,validate
from website_analytics.analysis.release_integrity import build,verify,sha,provenance,ReleaseError,check_dependencies,lock_packages
from website_analytics.analysis.release_runner import Paths,URLS,VARIANTS,CASE_ID,source_pilot,publish_candidate,main
from website_analytics.config import load_sites,require_site

AT=datetime(2026,11,4,12,tzinfo=timezone.utc)


@pytest.fixture
def release(tmp_path):
    root=tmp_path/"source";deploy=root/"deploy/refresh-rc";deploy.mkdir(parents=True);wheels=tmp_path/"wheels";wheels.mkdir()
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,"w") as wheel:wheel.writestr("pyyaml-6.0.3.dist-info/WHEEL","Wheel-Version: 1.0\nTag: py3-none-any\n")
    name="pyyaml-6.0.3-py3-none-any.whl";(wheels/name).write_bytes(stream.getvalue());digest=sha(wheels/name)
    for kind in ["worker","query"]:(deploy/f"requirements-{kind}.lock").write_text(f"pyyaml==6.0.3 \\\n    --hash=sha256:{digest}\n")
    for name in ["source-pilot.py","publish-candidate.py","query.py"]:(deploy/name).write_text("# synthetic package fixture\n")
    for name in ["release_runner.py","release_integrity.py"]:
        p=root/"src/website_analytics/analysis"/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("# fixture\n")
    for name in ["ga4.py","gsc.py","inquiry.py"]:
        p=root/"src/website_analytics/adapters"/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("# fixture\n")
    for kind in ["page_classifications","product_mappings","information_mappings"]:
        p=root/"config"/kind/"genemedi-net.yaml";p.parent.mkdir(parents=True);p.write_text("fixture: true\n")
    output=tmp_path/"one.tar.gz";receipt=build(root,output,wheelhouse=wheels)
    extracted=tmp_path/"release";extracted.mkdir()
    with tarfile.open(output) as bundle:bundle.extractall(extracted,filter="data")
    return root,wheels,extracted,receipt


def synthetic_case():
    old=record();old_urls=old["draft"]["raw_urls"];old_variants=old["source_packet"]["variant_paths_not_merged"]
    replacements={u:URLS[i] for i,u in enumerate(old_urls)}
    replacements.update({u.split(".net",1)[1]:URLS[i].split(".net",1)[1] for i,u in enumerate(old_urls)})
    replacements.update({v:VARIANTS[i] for i,v in enumerate(old_variants)})
    def replace(value):
        if isinstance(value,str):return replacements.get(value,value)
        if isinstance(value,list):return [replace(v) for v in value]
        if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
        return value
    changed=replace(old)
    changed["draft"]["raw_urls"]=list(URLS);changed["current_intervention"]["raw_urls"]=list(URLS)
    changed["source_packet"]["submitted_paths"]=[u.split(".net",1)[1] for u in URLS]
    return make_case_record(CASE_ID,"continuous_trend",changed["draft"],changed["source_packet"],changed["url_evidence"],changed["current_intervention"])


def pilot(tmp_path,release):
    _,_,root,receipt=release
    config=tmp_path/"config";config.mkdir();state=tmp_path/"state";state.mkdir(mode=0o700);outbox=tmp_path/"outbox";outbox.mkdir(mode=0o700);live=tmp_path/"live";live.mkdir()
    paths=Paths(root,config,state,outbox,live)
    sites={"sites":{"genemedi-net":{"display_name":"Synthetic","domains":["genemedi.net","www.genemedi.net"],"timezone":"America/Los_Angeles","ga4_property_id":"123","gsc_property_url":"https://www.genemedi.net/","inquiry_source":{"kind":"legacy_contacts_mysql","credential_env":"WEBSITE_ANALYTICS_SYNTHETIC_DSN","site_scope":"fixed_registered_source","site_scope_evidence":"Synthetic fixture; not physical dedication.","registered_database":"v2_genemedi_net","registered_table":"contacts","allowed_hosts":["genemedi.net","www.genemedi.net"]}}}}
    (config/"sites.yaml").write_text(yaml.safe_dump(sites))
    old=synthetic_case();store=CaseStore(state/"cases.sqlite");eid=store.put(old);os.chmod(store.path,0o600)
    raw={"contract":"bounded-worker/v1","site":"genemedi-net","raw_urls":list(URLS),"variant_paths":list(VARIANTS),"weeks":worker_request()["weeks"]}
    (config/"request.json").write_text(json.dumps(raw));(config/"metadata-windows.json").write_text('{}')
    profile={"contract":"source-pilot/v1","release_manifest_sha256":receipt["manifest_sha256"],"source_config_sha256":sha(config/"sites.yaml"),"request_sha256":sha(config/"request.json"),"metadata_windows_sha256":sha(config/"metadata-windows.json"),"seed_database_sha256":sha(store.path),"previous_evidence_id":eid,"case_id":CASE_ID,"run_id":"fixture-pilot-1","capture_mode":"local_fixture","expected_python_version":platform.python_version()}
    (config/"pilot.json").write_text(json.dumps(profile));calls=[]
    def factory(config):
        calls.append(True);r=reader();r.site=require_site(load_sites(config),"genemedi-net");r.clock=lambda:AT;return r
    return paths,old,profile,calls,factory


def execute(paths,factory,**kw):
    return source_pilot(paths,production=False,reader_factory=factory,dependency_checker=lambda *a,**k:{},clock=lambda:AT,**kw)


def publication_profile(paths,receipt,previous=None):
    (paths.config/"publish.json").write_text(json.dumps({"contract":"fixed-publication/v1","release_manifest_sha256":receipt["release_manifest_sha256"],"candidate_sha256":receipt["candidate_sha256"],"expected_previous_sha256":previous}))


def test_reproducible_archive_manifest_and_lock_verification(tmp_path,release):
    root,wheels,extracted,first=release
    os.utime(root/"src/website_analytics/adapters/ga4.py",(999999,999999))
    second=build(root,tmp_path/"two.tar.gz",wheelhouse=wheels)
    assert first["artifact_sha256"]==second["artifact_sha256"] and first["manifest_sha256"]==second["manifest_sha256"]
    assert verify(extracted)[1]==first["manifest_sha256"]
    assert lock_packages(extracted/"deploy/refresh-rc/requirements-worker.lock")["pyyaml"]["version"]=="6.0.3"


@pytest.mark.parametrize("kind",["code","extra","symlink","lock","wheel"])
def test_package_tamper_fails_closed(tmp_path,release,kind):
    _,_,root,_=release
    if kind=="code":(root/"src/website_analytics/adapters/ga4.py").write_text("changed")
    elif kind=="extra":(root/"src/extra.py").write_text("extra")
    elif kind=="symlink":(root/"src/foreign").symlink_to(tmp_path,target_is_directory=True)
    elif kind=="lock":(root/"deploy/refresh-rc/requirements-query.lock").write_text("unhashed==1")
    else:next((root/"wheels").iterdir()).write_bytes(b"changed")
    with pytest.raises(ReleaseError):verify(root)


def test_runtime_v2_preserves_old_contract_and_exposes_actual_mode(release,tmp_path):
    root=release[2];cfg=tmp_path/"public.yaml";cfg.write_text("public: true")
    value=provenance(root,cfg,"local_fixture");runtime(value)
    assert value["capture_mode"]=="local_fixture" and "local_developer_edits_deployed" not in value
    legacy=record()["source_packet"]["runtime"];runtime(legacy)
    with pytest.raises(Exception):runtime({**legacy,"local_developer_edits_deployed":True})
    with pytest.raises(Exception):runtime({**value,"production_release":"invented"})


@pytest.mark.parametrize("kind",["manifest","config","scope","seed","python"])
def test_preflight_failure_never_initializes_source_clients(tmp_path,release,kind):
    paths,_,profile,calls,factory=pilot(tmp_path,release);before=sha(paths.state/"cases.sqlite")
    if kind=="manifest":profile["release_manifest_sha256"]="0"*64
    elif kind=="config":(paths.config/"sites.yaml").write_text("changed")
    elif kind=="scope":
        raw=json.loads((paths.config/"request.json").read_text());raw["raw_urls"][0]="https://www.genemedi.net/i/not-approved";(paths.config/"request.json").write_text(json.dumps(raw));profile["request_sha256"]=sha(paths.config/"request.json")
    elif kind=="seed":profile["seed_database_sha256"]="0"*64
    else:profile["expected_python_version"]="0.0.0"
    (paths.config/"pilot.json").write_text(json.dumps(profile))
    with pytest.raises(Exception):execute(paths,factory)
    assert calls==[] and sha(paths.state/"cases.sqlite")==before and not (paths.outbox/"cases.sqlite").exists()


def test_fixed_pilot_is_idempotent_and_does_not_publish(tmp_path,release):
    paths,old,_,calls,factory=pilot(tmp_path,release)
    done=execute(paths,factory);assert done["production_published"] is False and calls==[True]
    again=execute(paths,factory);assert again["status"]=="already_completed_no_source_request" and calls==[True]
    store=CaseStore(paths.state/"cases.sqlite");new=store.get(CASE_ID,done["evidence_id"])
    assert new["source_packet"]["runtime"]["capture_mode"]=="local_fixture" and store.get(CASE_ID,profile_id(old))==old
    assert not (paths.live/"cases.sqlite").exists()


def profile_id(value):
    from website_analytics.analysis.contracts import fingerprint
    return fingerprint(value)


@pytest.mark.parametrize("stage",["before_sources","after_capture","after_export"])
def test_interrupted_pilot_requires_inspection_and_never_refetches(tmp_path,release,stage):
    paths,_,_,calls,factory=pilot(tmp_path,release)
    def interrupt(at):
        if at==stage:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):execute(paths,factory,checkpoint=interrupt)
    previous=len(calls)
    with pytest.raises(ReleaseError):execute(paths,factory)
    assert len(calls)==previous and not (paths.live/"cases.sqlite").exists()


def test_fixed_publish_atomicity_uncertainty_and_query_rollback(tmp_path,release):
    paths,old,_,_,factory=pilot(tmp_path,release);original=tmp_path/"old-fixed.sqlite";shutil.copyfile(paths.state/"cases.sqlite",original);old_sha=sha(original)
    with running(CaseStore(original)) as port:before=client_query("case_detail",case_id=CASE_ID,evidence_id=profile_id(old),port=port)
    done=execute(paths,factory);publication_profile(paths,done)
    def interrupt(stage):
        if stage=="before_replace":raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):publish_candidate(paths,production=False,dependency_checker=lambda *a,**k:{},checkpoint=interrupt)
    assert not (paths.live/"cases.sqlite").exists() and not list(paths.live.glob(".candidate-*"))
    def after(stage):
        if stage=="after_replace":raise KeyboardInterrupt()
    with pytest.raises(PublicationUncertain):publish_candidate(paths,production=False,dependency_checker=lambda *a,**k:{},checkpoint=after)
    assert sha(paths.live/"cases.sqlite")==done["candidate_sha256"]
    assert publish_candidate(paths,production=False,dependency_checker=lambda *a,**k:{})["status"]=="already_published_verified"
    with running(CaseStore(paths.live/"cases.sqlite")) as port:
        new=client_query("case_detail",case_id=CASE_ID,evidence_id=done["evidence_id"],port=port)
        assert new["runtime_provenance"]["capture_mode"]=="local_fixture"
    # Rollback is a reader/code-route switch, not deletion of new history.
    with running(CaseStore(original)) as port:assert client_query("case_detail",case_id=CASE_ID,evidence_id=profile_id(old),port=port)==before
    assert sha(original)==old_sha and sha(paths.live/"cases.sqlite")==done["candidate_sha256"]


def test_entry_rejects_arguments_before_any_profile_or_source_read(monkeypatch):
    monkeypatch.setattr(sys,"argv",["source-pilot.py","--arbitrary-path","/private"])
    assert main("source")==2


def test_dependency_versions_and_extras_fail_closed(monkeypatch,release):
    from types import SimpleNamespace
    import website_analytics.analysis.release_integrity as integrity
    def dist(version,name='PyYAML'):return SimpleNamespace(metadata={'Name':name},version=version)
    monkeypatch.setattr(integrity.importlib.metadata,'distributions',lambda:[dist('0.0')])
    with pytest.raises(ReleaseError):check_dependencies(release[2],'worker')
    monkeypatch.setattr(integrity.importlib.metadata,'distributions',lambda:[dist('6.0.3'),dist('1','unapproved')])
    with pytest.raises(ReleaseError):check_dependencies(release[2],'worker')
    assert check_dependencies(release[2],'worker',strict_extras=False)=={'pyyaml':'6.0.3'}


def test_incompatible_wheel_is_rejected(tmp_path,release):
    root,wheels,_,_=release
    wheel=next(wheels.iterdir());wheel.rename(wheels/wheel.name.replace('py3-none-any','cp312-cp312-macosx_11_0_arm64'))
    with pytest.raises(ReleaseError):build(root,tmp_path/'wrong.tar.gz',wheelhouse=wheels)


def test_production_publisher_rejects_fixture_even_if_prior_hash_matches(tmp_path,release,monkeypatch):
    import website_analytics.analysis.release_runner as runner
    paths,_,_,_,factory=pilot(tmp_path,release);done=execute(paths,factory);publication_profile(paths,done)
    monkeypatch.setattr(runner,'_production_guard',lambda *a:None)
    with pytest.raises(ReleaseError,match='fixture evidence'):publish_candidate(paths,production=True,dependency_checker=lambda *a,**k:{})
    assert not (paths.live/'cases.sqlite').exists()
    shutil.copyfile(paths.outbox/'cases.sqlite',paths.live/'cases.sqlite')
    with pytest.raises(ReleaseError,match='fixture evidence'):publish_candidate(paths,production=True,dependency_checker=lambda *a,**k:{})


def test_unapproved_candidate_preserves_prior_target(tmp_path,release):
    paths,_,_,_,factory=pilot(tmp_path,release);done=execute(paths,factory)
    shutil.copyfile(paths.state/'cases.sqlite',paths.live/'cases.sqlite');before=sha(paths.live/'cases.sqlite')
    publication_profile(paths,{**done,'candidate_sha256':'0'*64},previous=before)
    with pytest.raises(ReleaseError,match='not approved'):publish_candidate(paths,production=False,dependency_checker=lambda *a,**k:{})
    assert sha(paths.live/'cases.sqlite')==before


def test_query_fixed_entry_does_not_load_source_environment(tmp_path,release,monkeypatch):
    import website_analytics.analysis.release_runner as runner
    import website_analytics.analysis.service as service
    paths,_,_,_,factory=pilot(tmp_path,release)
    shutil.copyfile(paths.state/'cases.sqlite',paths.live/'cases.sqlite')
    monkeypatch.setattr(runner,'_load_existing_environment',lambda *a:pytest.fail('query loaded credentials'))
    monkeypatch.setattr(sys,'argv',['fixed'])
    def serve():
        assert sys.argv==['fixed-query','--cases',str(paths.live/'cases.sqlite'),'--port','8765']
        return 0
    monkeypatch.setattr(service,'main',serve)
    assert runner.query_service(paths,production=False,dependency_checker=lambda *a,**k:{})==0


def test_nonempty_metadata_scope_is_bound_through_case_persistence(tmp_path,release):
    paths,_,profile,calls,factory=pilot(tmp_path,release)
    windows={'2026-10-19':{'start':'2026-10-19','end':'2026-10-27'}}
    (paths.config/'metadata-windows.json').write_text(json.dumps(windows));profile['metadata_windows_sha256']=sha(paths.config/'metadata-windows.json')
    (paths.config/'pilot.json').write_text(json.dumps(profile))
    done=execute(paths,factory)
    assert done['status']=='candidate_ready' and calls==[True]
    import sqlite3
    with sqlite3.connect(paths.state/'runs.sqlite') as db:
        assert db.execute('SELECT count(DISTINCT scope_digest) FROM events').fetchone()[0]==1


def test_long_manylinux_wheel_tags_package_reproducibly(tmp_path,release):
    root,wheels,_,_=release
    wheel=next(wheels.iterdir())
    tag=".".join(f"manylinux_2_{n}_x86_64" for n in (17,24,27,28))
    wheel.rename(wheels/f"pyyaml-6.0.3-py3-none-{tag}.whl")
    assert len(next(wheels.iterdir()).name)>100
    first=build(root,tmp_path/'long1.tar.gz',wheelhouse=wheels)
    second=build(root,tmp_path/'long2.tar.gz',wheelhouse=wheels)
    assert first['artifact_sha256']==second['artifact_sha256']
