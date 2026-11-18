"""Agent-oriented, read-only HTTP replay on 127.0.0.1 only. No live fetches."""
from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from website_analytics.analysis.contracts import AnalysisError, canonical_json, strict_keys
from website_analytics.analysis.readers import _unique_object
from website_analytics.analysis.snapshots import ENGINE_VERSION, SnapshotStore, snapshot_identifier
from website_analytics.analysis.cases import CASE_OPERATIONS, CaseStore, query_case
from website_analytics.analysis.catalog import CATALOG_OPERATIONS, RefreshCatalog

SERVICE_VERSION = "batch-service/v1"
MAX_REQUEST_BYTES = 4096


def dispatch(store: SnapshotStore | None, request: object, cases: CaseStore | None = None, refreshes: RefreshCatalog | None = None, refresh_status=None) -> dict:
    if isinstance(request,dict) and request.get("operation")=="refresh_status":
        if refresh_status is None:raise AnalysisError("refresh status is not configured")
        return refresh_status.query(request)
    if isinstance(request, dict) and request.get("operation") in CATALOG_OPERATIONS:
        if refreshes is None:
            raise AnalysisError("refresh catalog is not configured")
        return refreshes.query(request)
    if isinstance(request, dict) and request.get("operation") in CASE_OPERATIONS:
        if cases is None:
            raise AnalysisError("case evidence store is not configured")
        return query_case(cases, request)
    raw = strict_keys(request, {"contract", "operation", "snapshot_id"})
    if raw["contract"] != SERVICE_VERSION or raw["operation"] != "replay" or store is None:
        raise AnalysisError("unsupported service operation")
    snapshot_id = snapshot_identifier(raw["snapshot_id"])
    report = store.report(snapshot_id)
    from website_analytics.analysis.completeness import project_records
    from website_analytics.analysis.contracts import timestamp
    payload=store.get(snapshot_id)
    return {"contract": SERVICE_VERSION, "snapshot_id": snapshot_id,
            "status": report["status"], "sample": report["sample"],
            "evidence_state": "synthetic" if report["sample"] else "confirmed", "report": report,
            "scoped_completeness":project_records(payload["sources"],as_of=timestamp(payload["evaluated_at"]))}


def make_server(store: SnapshotStore | None, port: int = 0, *, cases: CaseStore | None = None, refreshes: RefreshCatalog | None = None, refresh_status=None) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise AnalysisError("invalid loopback port")
    if store is None and cases is None and refreshes is None:
        raise AnalysisError("an existing evidence store is required")
    if store is not None:
        if not store.path.is_file():
            raise AnalysisError("snapshot store must already exist")
        with sqlite3.connect(store.path.resolve().as_uri()+"?mode=ro", uri=True) as db:
            db.execute("SELECT snapshot_id FROM snapshots LIMIT 1").fetchone()
    if cases is not None:
        with cases.readonly() as db:
            db.execute("SELECT evidence_id FROM case_evidence LIMIT 1").fetchone()
    if refreshes is not None:
        with refreshes.readonly() as db:
            for table in ("refresh_runs", "refresh_heads", "snapshots"):
                db.execute("SELECT 1 FROM "+table+" LIMIT 1").fetchone()
    operations = (["replay"] if store is not None else []) + (list(CASE_OPERATIONS) if cases is not None else []) + (list(CATALOG_OPERATIONS) if refreshes is not None else []) + (["refresh_status"] if refresh_status is not None else [])

    class Handler(BaseHTTPRequestHandler):
        server_version = "BatchAnalysisLocal/1"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def log_message(self, *args):
            pass  # Never log URLs, bodies, headers or local file names.

        def reply(self, status: int, body: dict):
            data = canonical_json(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (OSError, TimeoutError):
                pass

        def error(self, status, code):
            self.reply(status, {"contract": SERVICE_VERSION, "status": "error", "error": code})

        def permitted_context(self):
            hosts = self.headers.get_all("Host", [])
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return (self.client_address[0] == "127.0.0.1" and len(hosts) == 1 and hosts[0] in expected
                    and not self.headers.get_all("Origin")
                    and self.headers.get("Sec-Fetch-Site", "none") in {"none", "same-origin"})

        def do_GET(self):
            if not self.permitted_context():
                return self.error(403, "request_context_not_allowed")
            if self.path == "/healthz":
                return self.reply(200, {"contract": SERVICE_VERSION, "status": "ok", "engine": ENGINE_VERSION,
                    "binding": "loopback_only", "operations": operations, "live_sources": False})
            return self.error(404, "route_not_found")

        def do_POST(self):
            if not self.permitted_context():
                return self.error(403, "request_context_not_allowed")
            if self.path != "/v1/query":
                return self.error(404, "route_not_found")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 6 or self.headers.get_all("Transfer-Encoding"):
                return self.error(400, "invalid_request_framing")
            length = int(lengths[0])
            if not 1 <= length <= MAX_REQUEST_BYTES:
                return self.error(413, "request_too_large")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.error(415, "json_required")
            try:
                data = self.rfile.read(length)
                if len(data) != length:
                    raise AnalysisError("incomplete request")
                request = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(AnalysisError("non-finite JSON")))
                result = dispatch(store, request, cases, refreshes, refresh_status)
            except KeyError:
                return self.error(404, "snapshot_not_found")
            except (AnalysisError, ValueError, TypeError, UnicodeError, RecursionError):
                return self.error(400, "invalid_or_unreplayable_request")
            except (OSError, sqlite3.Error):
                return self.error(503, "local_evidence_unavailable")
            return self.reply(200, result)

        def do_OPTIONS(self):
            return self.error(405, "method_not_allowed")

        do_PUT = do_PATCH = do_DELETE = do_HEAD = do_TRACE = do_OPTIONS

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 8

        def handle_error(self, request, client_address):
            pass  # Do not emit traceback/request contents to console.

    return LocalServer(("127.0.0.1", port), Handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-status",help="fixed operator-owned sanitized status file; read-only")
    parser.add_argument("--snapshots", help="existing snapshot store, never accepted in HTTP")
    parser.add_argument("--cases", help="existing offline-imported case store, never accepted in HTTP")
    parser.add_argument("--refreshes", help="existing refresh catalog; read-only discovery, never source fetch")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        from website_analytics.analysis.refresh_status import StatusReader
        server = make_server(SnapshotStore(args.snapshots) if args.snapshots else None, args.port,
                             cases=CaseStore(args.cases) if args.cases else None,
                             refreshes=RefreshCatalog(args.refreshes) if args.refreshes else None,
                             refresh_status=StatusReader(args.refresh_status) if args.refresh_status else None)
    except (AnalysisError, OSError, sqlite3.Error):
        print(canonical_json({"status": "error", "error": "local_service_start_failed"}))
        return 2
    print(canonical_json({"contract": SERVICE_VERSION, "status": "ready", "host": "127.0.0.1", "port": server.server_port, "live_sources": False}), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
