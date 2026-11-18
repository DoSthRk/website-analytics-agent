"""Validated local SQLite export and atomic publication, never source fetching.

Only an operator supplies paths. There is no HTTP writer or permission setup.
Deployment must provide a private staging area and an administrator-owned target.
"""
from __future__ import annotations

import fcntl
import argparse
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

from website_analytics.analysis.case_validation import safe_text
from website_analytics.analysis.cases import CaseStore
from website_analytics.analysis.catalog import RefreshCatalog
from website_analytics.analysis.contracts import AnalysisError, timestamp
from website_analytics.analysis.snapshots import SnapshotStore

MAX_BYTES=32*1024*1024
SCHEMA={
 "snapshots":("snapshot_id","payload"),
 "refresh_runs":("job_id","run_id","digest","receipt"),
 "refresh_heads":("job_id","snapshot_id","receipt"),
 "case_evidence":("case_id","evidence_id","payload","stored_at"),
}
DDL={
 "snapshots":"CREATE TABLE snapshots(snapshot_id TEXT PRIMARY KEY,payload TEXT NOT NULL)",
 "refresh_runs":"CREATE TABLE refresh_runs(job_id TEXT,run_id TEXT,digest TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(job_id,run_id))",
 "refresh_heads":"CREATE TABLE refresh_heads(job_id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,receipt TEXT NOT NULL)",
 "case_evidence":"CREATE TABLE case_evidence(case_id TEXT NOT NULL,evidence_id TEXT PRIMARY KEY,payload TEXT NOT NULL,stored_at TEXT NOT NULL)",
}


class PublicationUncertain(AnalysisError): pass


def _scan(value):
    if isinstance(value,str): safe_text(value,limit=512000,empty=True)
    elif isinstance(value,dict):
        for k,v in value.items(): _scan(k);_scan(v)
    elif isinstance(value,list):
        for v in value:_scan(v)


def _hash(path):
    with Path(path).open("rb") as stream:return hashlib.file_digest(stream,"sha256").hexdigest()


def validate(path,kind):
    path=Path(path)
    if kind not in {"refreshes","cases"} or path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_BYTES:
        raise AnalysisError("invalid evidence publication input")
    tables={"case_evidence"} if kind=="cases" else {"snapshots","refresh_runs","refresh_heads"}
    with sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise AnalysisError("invalid evidence database")
        objects=db.execute("SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if set(objects)!={("table",t) for t in tables}:raise AnalysisError("unexpected evidence schema objects")
        for table in tables:
            columns=tuple(r[1] for r in db.execute("PRAGMA table_info("+table+")"))
            if columns!=SCHEMA[table]:raise AnalysisError("unexpected evidence columns")
            if db.execute("SELECT count(*) FROM "+table).fetchone()[0]>512:raise AnalysisError("evidence row budget exceeded")
        if kind=="cases":
            for case,eid,body,stored in db.execute("SELECT * FROM case_evidence"):
                timestamp(stored);_scan(json.loads(body));CaseStore(path).get(case,eid)
        else:
            for sid,body in db.execute("SELECT * FROM snapshots"):
                _scan(json.loads(body));SnapshotStore(path).get(sid)
            catalog=RefreshCatalog(path)
            for row in db.execute("SELECT rowid,job_id,run_id,digest,receipt FROM refresh_runs"):
                _scan(json.loads(row[4]));catalog._entry(db,row)
            jobs={r[0] for r in db.execute("SELECT DISTINCT job_id FROM refresh_runs")}
            heads={r[0] for r in db.execute("SELECT job_id FROM refresh_heads")}
            if not heads<=jobs:raise AnalysisError("orphan evidence head")
            for job in jobs:catalog._state(db,job)
    return {"kind":kind,"sha256":_hash(path),"bytes":path.stat().st_size}


def export(source,output,kind):
    """Consistent private backup -> validate -> clean schema, no copied SQL code.

    Output must be new. Source and private run logs are never changed or exported.
    Requires sufficient bounded temporary space; retention is not automatic.
    """
    source,output=Path(source),Path(output)
    if source.is_symlink() or not source.is_file() or source.stat().st_size>MAX_BYTES or output.exists() or output.is_symlink():
        raise AnalysisError("invalid export paths")
    if not output.parent.is_dir():raise AnalysisError("staging directory must already exist")
    with tempfile.TemporaryDirectory(prefix=".evidence-export-",dir=output.parent) as folder:
        snapshot=Path(folder)/"private.sqlite";start=time.monotonic();page_size=4096
        def budget(status,remaining,total):
            if time.monotonic()-start>10 or total*page_size>MAX_BYTES:raise AnalysisError("export backup budget exceeded")
        with sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True) as src,sqlite3.connect(snapshot) as dst:
            page_size=src.execute("PRAGMA page_size").fetchone()[0]
            src.backup(dst,pages=128,progress=budget,sleep=.01)
        validate(snapshot,kind)
        created=False
        try:
            fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd);created=True
            tables=["case_evidence"] if kind=="cases" else ["snapshots","refresh_runs","refresh_heads"]
            with sqlite3.connect(snapshot.resolve().as_uri()+"?mode=ro",uri=True) as src,sqlite3.connect(output) as dst:
                for table in tables:
                    dst.execute(DDL[table]);cols=len(SCHEMA[table])
                    # Preserve rowid history cursors while rebuilding known tables.
                    for row in src.execute("SELECT rowid,* FROM "+table):
                        dst.execute("INSERT INTO "+table+"(rowid,"+",".join(SCHEMA[table])+") VALUES ("+",".join("?" for _ in range(cols+1))+")",row)
            receipt=validate(output,kind)
            os.chmod(output,0o400)
            with output.open("rb") as stream:os.fsync(stream.fileno())
            return receipt
        except BaseException:
            if created:output.unlink(missing_ok=True)
            raise


def _preserves_history(previous,candidate,kind):
    tables=["case_evidence"] if kind=="cases" else ["snapshots","refresh_runs"]
    with sqlite3.connect(previous.resolve().as_uri()+"?mode=ro",uri=True) as old,sqlite3.connect(candidate.resolve().as_uri()+"?mode=ro",uri=True) as new:
        for table in tables:
            # Whole row plus rowid preserves pinned evidence and history cursors.
            rows=set(new.execute("SELECT rowid,* FROM "+table))
            if not set(old.execute("SELECT rowid,* FROM "+table))<=rows:
                raise AnalysisError("publication would remove or rewrite history")
        if kind=="refreshes":
            for job, in old.execute("SELECT job_id FROM refresh_heads"):
                _,before,_=RefreshCatalog(previous)._state(old,job)
                _,after,_=RefreshCatalog(candidate)._state(new,job)
                if after is None or timestamp(after["captured_at"])<timestamp(before["captured_at"]):raise AnalysisError("publication would regress head")
                earlier={(r["source"],r["scope"]["scope_digest"]):r for r in before["sources"]}
                for r in after["sources"]:
                    p=earlier[(r["source"],r["scope"]["scope_digest"])]
                    if timestamp(r["fetched_at"])<timestamp(p["fetched_at"]) or r["complete_through"]<p["complete_through"]:
                        raise AnalysisError("publication would regress source data")


def publish(candidate,directory,kind,*,expected_candidate,expected_previous,read_gid=None,checkpoint=lambda stage:None):
    """CAS-guarded atomic replace. Never changes source credentials or services.

    expected_previous=None means first publication, not permission to overwrite.
    Failure after replacement is explicitly uncertain; inspect hashes before retry.
    """
    candidate,directory=Path(candidate),Path(directory)
    if kind not in {"refreshes","cases"} or directory.is_symlink() or not directory.is_dir():raise AnalysisError("invalid publication target")
    target=directory/(kind+".sqlite");temporary=None;replaced=False
    lock_fd=os.open(directory/".publish.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(lock_fd,"a+b") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            if target.is_symlink() or (target.exists() and _hash(target)!=expected_previous) or (not target.exists() and expected_previous is not None):
                raise AnalysisError("publication target changed; inspect before retry")
            # Copy only from a no-follow opened FD into a private, exclusive file.
            fd=os.open(candidate,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                metadata=os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>MAX_BYTES:raise AnalysisError("candidate size or type invalid")
                outfd,name=tempfile.mkstemp(prefix=".candidate-",dir=directory);temporary=Path(name)
                with os.fdopen(outfd,"wb") as dst,os.fdopen(fd,"rb",closefd=False) as src:
                    copied=0
                    while block:=src.read(64*1024):
                        copied+=len(block)
                        if copied>MAX_BYTES:raise AnalysisError("candidate size exceeded during copy")
                        dst.write(block)
                    dst.flush();os.fsync(dst.fileno())
            finally:os.close(fd)
            if _hash(temporary)!=expected_candidate:raise AnalysisError("candidate hash mismatch")
            receipt=validate(temporary,kind);checkpoint("validated")
            if target.exists():
                validate(target,kind)
                _preserves_history(target,temporary,kind)
                history=directory/".history"
                if history.is_symlink():raise AnalysisError("history must not be a symlink")
                history.mkdir(mode=0o700,exist_ok=True)
                saved=history/(kind+"-"+expected_previous+".sqlite")
                if saved.exists():
                    if saved.is_symlink() or _hash(saved)!=expected_previous:raise AnalysisError("history conflict")
                else:os.link(target,saved)
            if read_gid is not None:os.chown(temporary,-1,read_gid)
            os.chmod(temporary,0o440)
            checkpoint("before_replace")
            os.replace(temporary,target);replaced=True;temporary=None
            directory_fd=os.open(directory,os.O_RDONLY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
            checkpoint("after_replace")
            return {**receipt,"published":True,"previous_sha256":expected_previous,"source_fetch_performed":False}
        except BaseException:
            if replaced:raise PublicationUncertain("replacement_occurred_verify_target_before_retry") from None
            raise
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="operation",required=True)
    ex=commands.add_parser("export");ex.add_argument("--source",required=True);ex.add_argument("--output",required=True)
    pub=commands.add_parser("publish");pub.add_argument("--candidate",required=True);pub.add_argument("--directory",required=True);pub.add_argument("--candidate-sha",required=True)
    prior=pub.add_mutually_exclusive_group(required=True);prior.add_argument("--previous-sha");prior.add_argument("--expect-absent",action="store_true")
    pub.add_argument("--read-gid",type=int)
    for command in (ex,pub):command.add_argument("--kind",choices=["refreshes","cases"],required=True)
    args=parser.parse_args(argv)
    try:
        if args.operation=="export":result=export(args.source,args.output,args.kind)
        else:
            if args.read_gid is not None and args.read_gid<0:raise AnalysisError("invalid read group")
            result=publish(args.candidate,args.directory,args.kind,expected_candidate=args.candidate_sha,expected_previous=args.previous_sha,read_gid=args.read_gid)
        print(json.dumps({"status":"ok",**result}));return 0
    except PublicationUncertain:
        print(json.dumps({"status":"uncertain","error":"verify_target_hash_before_retry"}));return 4
    except Exception:
        print(json.dumps({"status":"error","error":"invalid_evidence_or_publication_state"}));return 2


if __name__=="__main__":raise SystemExit(main())
