"""Deterministic release packaging/integrity; stdlib only, no source clients."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import platform
from pathlib import Path
import re
import sys
import tarfile
import zipfile

CONTRACT="analytics-release/v1"
MANIFEST="release-manifest.json"
PUBLIC_CONFIG=("config/page_classifications/genemedi-net.yaml","config/product_mappings/genemedi-net.yaml","config/information_mappings/genemedi-net.yaml")


class ReleaseError(RuntimeError):pass


def encoded(value):return (json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True)+"\n").encode()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def canonical_name(name):return re.sub(r"[-_.]+","-",name).lower()


def lock_packages(path):
    result={};current=None
    for raw in Path(path).read_text().splitlines():
        line=raw.strip()
        if not line or line.startswith("#"):continue
        match=re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)\s*\\?",line)
        if match:
            current=canonical_name(match[1])
            if current in result:raise ReleaseError("duplicate dependency")
            result[current]={"version":match[2],"hashes":[]}
        elif current and re.fullmatch(r"--hash=sha256:[a-f0-9]{64}\s*\\?",line):
            result[current]["hashes"].append(line.split(":",1)[1].rstrip(" \\"))
        else:raise ReleaseError("unsupported dependency lock syntax")
    if not result or any(not item["hashes"] for item in result.values()):raise ReleaseError("hashed dependencies required")
    return result


def _payload_files(root):
    paths=list((root/"src/website_analytics").rglob("*.py"))
    paths+=list((root/"deploy/refresh-rc").glob("*"))
    paths+=[root/p for p in PUBLIC_CONFIG]
    selected={}
    for path in paths:
        if not path.is_file():continue
        rel=path.relative_to(root).as_posix()
        if path.is_symlink() or ".." in Path(rel).parts:raise ReleaseError("release symlink rejected")
        if rel.startswith("deploy/") and path.suffix not in {".py",".json",".example",".md",".in",".lock"}:raise ReleaseError("unexpected deployment file")
        selected[rel]=path.read_bytes()
    if not selected:raise ReleaseError("empty release")
    required=set(PUBLIC_CONFIG)|{"src/website_analytics/analysis/release_runner.py","src/website_analytics/analysis/release_integrity.py",
        "src/website_analytics/adapters/ga4.py","src/website_analytics/adapters/gsc.py","src/website_analytics/adapters/inquiry.py",
        "deploy/refresh-rc/source-pilot.py","deploy/refresh-rc/publish-candidate.py","deploy/refresh-rc/query.py",
        "deploy/refresh-rc/requirements-worker.lock","deploy/refresh-rc/requirements-query.lock"}
    if "src/website_analytics/analysis/repeatable_runner.py" in selected:
        required|={"src/website_analytics/analysis/refresh_loop.py","src/website_analytics/analysis/refresh_status.py",
            "deploy/refresh-rc/refresh-daily.py","deploy/refresh-rc/refresh-on-demand.py","deploy/refresh-rc/publish-repeatable.py",
            "deploy/refresh-rc/website-analytics-refresh-daily-v2.service.example","deploy/refresh-rc/website-analytics-refresh-manual-v2.service.example","deploy/refresh-rc/website-analytics-refresh-publish-v2.service.example",
            "deploy/refresh-rc/repeatable-loop.json.example","deploy/refresh-rc/manual-request.json.example","deploy/refresh-rc/REPEATABLE-RELEASE.md"}
    if not required<=selected.keys():raise ReleaseError("required release payload missing")
    return selected


def _linux_wheel(name):
    try:py,abi,platforms=name[:-4].rsplit("-",3)[-3:]
    except ValueError:return False
    if abi=="cp312":python_ok=py=="cp312"
    elif abi=="abi3":python_ok=bool(re.fullmatch(r"cp3\d+",py)) and int(py[3:])<=12
    else:python_ok=abi=="none" and bool(set(py.split("."))&{"py3","py312","cp312"})
    platform_ok=platforms=="any" or all(tag=="manylinux2014_x86_64" or bool(re.fullmatch(r"manylinux_2_\d+_x86_64",tag)) and int(tag.split("_")[2])<=28 for tag in platforms.split("."))
    return python_ok and platform_ok


def build(root,output,*,wheelhouse):
    root,output,wheelhouse=Path(root),Path(output),Path(wheelhouse)
    if output.exists() or not output.parent.is_dir():raise ReleaseError("release output must be new")
    payload=_payload_files(root)
    locks={kind:lock_packages(root/f"deploy/refresh-rc/requirements-{kind}.lock") for kind in ("worker","query")}
    inventory={}
    for wheel in sorted(wheelhouse.glob("*.whl")):
        if wheel.is_symlink():raise ReleaseError("wheel symlink rejected")
        if not _linux_wheel(wheel.name) or not zipfile.is_zipfile(wheel):raise ReleaseError("wheel incompatible with frozen target")
        parts=wheel.name.split("-")
        name=canonical_name(parts[0]);version=parts[1]
        digest=sha(wheel)
        if name not in locks["worker"] or version!=locks["worker"][name]["version"] or digest not in locks["worker"][name]["hashes"] or name in inventory:
            raise ReleaseError("wheel does not match pinned lock")
        inventory[name]={"filename":wheel.name,"version":version,"sha256":digest}
        payload["wheels/"+wheel.name]=wheel.read_bytes()
    if set(inventory)!=set(locks["worker"]) or not set(locks["query"])<=set(inventory):raise ReleaseError("incomplete offline wheelhouse")
    manifest={"contract":CONTRACT,"target":{"implementation":"cpython","python_abi":"cp312","platform":"manylinux_2_28_x86_64"},
        "source_control":"uncommitted_content_pinned_by_hash_not_commit_attestation",
        "files":{p:{"sha256":hashlib.sha256(value).hexdigest(),"bytes":len(value)} for p,value in sorted(payload.items())},
        "dependency_locks":{kind:{"path":f"deploy/refresh-rc/requirements-{kind}.lock","sha256":sha(root/f"deploy/refresh-rc/requirements-{kind}.lock"),"packages":{name:item["version"] for name,item in locks[kind].items()}} for kind in locks},
        "wheels":inventory}
    if "src/website_analytics/analysis/repeatable_runner.py" in payload:
        manifest["capabilities"]={"repeatable_fixed_launcher":"repeatable-launcher/v1","read_only_status":"refresh-health/v1","scheduler_included":False}
    body=encoded(manifest);payload[MANIFEST]=body
    fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,"wb") as stream,gzip.GzipFile(filename="",mode="wb",fileobj=stream,mtime=0) as compressed:
            with tarfile.open(fileobj=compressed,mode="w",format=tarfile.PAX_FORMAT) as archive:
                for name,value in sorted(payload.items()):
                    info=tarfile.TarInfo(name);info.size=len(value);info.mode=0o644;info.mtime=0;info.uid=info.gid=0;info.uname=info.gname=""
                    archive.addfile(info,io.BytesIO(value))
    except BaseException:
        output.unlink(missing_ok=True);raise
    return {"artifact_sha256":sha(output),"manifest_sha256":hashlib.sha256(body).hexdigest(),"bytes":output.stat().st_size,"files":len(manifest["files"]),"manifest":manifest}


def verify(root,*,expected_manifest=None):
    root=Path(root)
    path=root/MANIFEST
    if path.is_symlink() or not path.is_file():raise ReleaseError("release manifest unavailable")
    digest=sha(path)
    if expected_manifest is not None and digest!=expected_manifest:raise ReleaseError("release manifest mismatch")
    manifest=json.loads(path.read_text())
    if manifest.get("contract")!=CONTRACT or not isinstance(manifest.get("files"),dict):raise ReleaseError("unsupported release manifest")
    for name,item in manifest["files"].items():
        rel=Path(name)
        if rel.is_absolute() or ".." in rel.parts:raise ReleaseError("unsafe release path")
        candidate=root/rel
        if any(p.is_symlink() for p in [candidate,*candidate.parents] if p!=root.parent and root in [p,*p.parents]):raise ReleaseError("release symlink rejected")
        if not candidate.is_file() or candidate.stat().st_size!=item["bytes"] or sha(candidate)!=item["sha256"]:raise ReleaseError("release file mismatch")
    expected=set(manifest["files"])
    scanned=[p for folder in ("src","deploy","config","wheels") for p in (root/folder).rglob("*")]
    if any(p.is_symlink() for p in scanned):raise ReleaseError("release symlink rejected")
    actual={p.relative_to(root).as_posix() for p in scanned if p.is_file()}
    if actual!=expected:raise ReleaseError("unexpected or missing release payload")
    for kind,item in manifest["dependency_locks"].items():
        packages=lock_packages(root/item["path"])
        if sha(root/item["path"])!=item["sha256"] or {n:v["version"] for n,v in packages.items()}!=item["packages"]:raise ReleaseError("dependency lock mismatch")
    return manifest,digest


def check_dependencies(root,kind,*,strict_extras=True):
    manifest,_=verify(root);expected=manifest["dependency_locks"][kind]["packages"]
    actual={canonical_name(d.metadata["Name"]):d.version for d in importlib.metadata.distributions() if d.metadata["Name"]}
    if any(actual.get(name)!=value for name,value in expected.items()):raise ReleaseError("installed dependency version mismatch")
    if strict_extras and set(actual)-set(expected)-{"pip","setuptools","wheel"}:raise ReleaseError("unexpected installed dependency")
    return expected


def provenance(root,source_config,mode):
    manifest,digest=verify(root)
    if mode not in {"local_fixture","server_shadow","production_worker"}:raise ReleaseError("invalid capture mode")
    return {"contract":"runtime-provenance/v2","execution_model":"fixed-release-launcher/v1",
        "production_release":"rc-"+digest[:16],"capture_mode":mode,"release_manifest_sha256":digest,
        "dependency_lock_sha256":manifest["dependency_locks"]["worker"]["sha256"],"source_config_sha256":sha(source_config),
        "python_version":platform.python_version(),"platform":sys.platform+"_"+platform.machine(),
        "public_source_sha256":{p:manifest["files"][p]["sha256"] for p in ("src/website_analytics/adapters/ga4.py","src/website_analytics/adapters/gsc.py","src/website_analytics/adapters/inquiry.py")}}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--root",required=True);parser.add_argument("--output",required=True);parser.add_argument("--wheelhouse",required=True)
    args=parser.parse_args();result=build(args.root,args.output,wheelhouse=args.wheelhouse)
    print(json.dumps({k:v for k,v in result.items() if k!="manifest"},sort_keys=True))


if __name__=="__main__":main()
