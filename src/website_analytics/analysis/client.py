"""Minimal agent client. Destination fixed to loopback, environment proxies off."""
from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from website_analytics.analysis.contracts import AnalysisError, canonical_json
from website_analytics.analysis.service import SERVICE_VERSION
from website_analytics.analysis.snapshots import snapshot_identifier
from website_analytics.analysis.cases import CASE_OPERATIONS, validate_case_request
from website_analytics.analysis.catalog import CATALOG_OPERATIONS, validate_catalog_request


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AnalysisError("local query redirects are forbidden")


def _query(payload: dict, *, port: int) -> dict:
    if type(port) is not int or not 1 <= port <= 65535:
        raise AnalysisError("invalid loopback port")
    body = canonical_json(payload).encode()
    request = Request(f"http://127.0.0.1:{port}/v1/query", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=5) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise AnalysisError("local response exceeds size limit")
    result = json.loads(data)
    if result.get("contract") != SERVICE_VERSION or result.get("status") not in {"ok", "partial"}:
        raise AnalysisError("unexpected local query response")
    return result


def query_snapshot(snapshot_id: str, *, port: int = 8765) -> dict:
    snapshot_identifier(snapshot_id)
    result = _query({"contract": SERVICE_VERSION, "operation": "replay", "snapshot_id": snapshot_id}, port=port)
    if result.get("snapshot_id") != snapshot_id:
        raise AnalysisError("snapshot response identity mismatch")
    return result


def query_case(operation: str, *, case_id=None, evidence_id=None, page_key=None, port=8765,limit=None,before_sequence=None):
    request = {"contract": SERVICE_VERSION, "operation": operation}
    request.update({k: v for k, v in (("case_id", case_id), ("evidence_id", evidence_id), ("page_key", page_key)) if v is not None})
    request.update({k:v for k,v in (("limit",limit),("before_sequence",before_sequence)) if v is not None})
    validate_case_request(request)
    result = _query(request, port=port)
    if any(result.get(key) != value for key, value in request.items()):
        raise AnalysisError("case response identity mismatch")
    return result


def query_refresh(operation, *, job_id=None, snapshot_id=None, after_job=None, before_sequence=None, limit=None, port=8765):
    request = {"contract": SERVICE_VERSION, "operation": operation}
    request.update({k: v for k, v in (("job_id", job_id), ("snapshot_id", snapshot_id), ("after_job", after_job),
                    ("before_sequence", before_sequence), ("limit", limit)) if v is not None})
    validate_catalog_request(request)
    result = _query(request, port=port)
    if any(result.get(k) != v for k, v in request.items()):
        raise AnalysisError("refresh response identity mismatch")
    return result


def query_status(task_id,*,port=8765):
    from website_analytics.analysis.contracts import identifier
    identifier(task_id)
    result=_query({'contract':SERVICE_VERSION,'operation':'refresh_status','task_id':task_id},port=port)
    if result.get('task_id')!=task_id or result.get('operation')!='refresh_status':raise AnalysisError('health response identity mismatch')
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-id")
    parser.add_argument("--task-id")
    parser.add_argument("--operation", choices=["replay", "refresh_status", *CASE_OPERATIONS, *CATALOG_OPERATIONS], default="replay")
    parser.add_argument("--case-id")
    parser.add_argument("--evidence-id")
    parser.add_argument("--page-key")
    parser.add_argument("--job-id")
    parser.add_argument("--after-job")
    parser.add_argument("--before-sequence", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.operation=='refresh_status':
            if any(v is not None for v in (args.case_id,args.evidence_id,args.page_key,args.snapshot_id,args.job_id,args.after_job,args.before_sequence,args.limit)):raise AnalysisError('health query does not accept data selectors')
            result=query_status(args.task_id,port=args.port)
        elif args.task_id is not None:raise AnalysisError('task ID only supported for health query')
        elif args.operation in CATALOG_OPERATIONS:
            if args.case_id or args.evidence_id or args.page_key:
                raise AnalysisError("refresh and fixed-case requests cannot be mixed")
            result = query_refresh(args.operation, job_id=args.job_id, snapshot_id=args.snapshot_id,
                after_job=args.after_job, before_sequence=args.before_sequence, limit=args.limit, port=args.port)
        elif any(v is not None for v in (args.job_id,args.after_job)) or args.operation!="case_versions" and any(v is not None for v in (args.before_sequence,args.limit)):
            raise AnalysisError("catalog arguments require a catalog operation")
        elif args.operation == "replay":
            if args.case_id or args.evidence_id or args.page_key:
                raise AnalysisError("snapshot and case requests cannot be mixed")
            result = query_snapshot(args.snapshot_id, port=args.port)
        else:
            if args.snapshot_id:
                raise AnalysisError("case request cannot select a snapshot")
            result = query_case(args.operation, case_id=args.case_id, evidence_id=args.evidence_id, page_key=args.page_key, port=args.port,limit=args.limit,before_sequence=args.before_sequence)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result["status"] == "ok" else 3
    except (AnalysisError, HTTPError, URLError, OSError, ValueError, TypeError):
        print(canonical_json({"status": "error", "error": "local_query_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
