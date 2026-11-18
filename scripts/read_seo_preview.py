"""Owner-run, one-shot read bridge. Not part of the shared local HTTP service.

Uses the existing authorized SSH query connection and legacy reader helpers.
New wrapper code runs only in memory; no deployment or server-file writes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from website_analytics.analysis.preview import PreviewDraft
from website_analytics.analysis.continuous import ContinuousDraft, parse_intervention
from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.readers import read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", required=True)
    parser.add_argument("--legacy-query-project", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--variant-path", action="append", default=[])
    parser.add_argument("--weekly-start")
    parser.add_argument("--weekly-end")
    parser.add_argument("--availability-only", action="store_true", help="read scoped GSC freshness metadata only, no weekly recollection")
    args = parser.parse_args(argv)
    draft = parse_intervention(read_json(args.draft))
    legacy = Path(args.legacy_query_project).resolve()
    # Import via the existing package path; reuse its validated SSH construction.
    import website_analytics
    website_analytics.__path__.append(str(legacy/"src/website_analytics"))
    from website_analytics.handoff_query import load_connection, ssh_command
    from website_analytics.analysis.contracts import page_path
    for path in args.variant_path:
        page_path(path)  # safety only; original case remains unchanged
    if isinstance(draft, ContinuousDraft):
        request = {"protocol": "seo-weekly-read/1", "submitted_paths": list(draft.paths),
                   "variant_paths": args.variant_path, "weeks": draft.weeks(args.weekly_start, args.weekly_end)}
        if args.availability_only:
            request["availability_only"] = True
    else:
        if args.availability_only or args.weekly_start or args.weekly_end:
            raise AnalysisError("weekly options require a continuous intervention draft")
        request = {"protocol": "seo-preview-read/1", "submitted_paths": list(draft.paths),
                   "variant_paths": args.variant_path, "scenarios": draft.scenarios(days=7)}
    protocol = (legacy/"src/website_analytics/query_protocol.py").read_text()
    old_worker = (legacy/"scripts/query_server.py").read_text()
    worker = (ROOT/"scripts/seo_preview_server.py").read_text()
    bootstrap = "import sys,json,types\n_protocol=types.ModuleType('_handoff_protocol')\nsys.modules[_protocol.__name__]=_protocol\nexec("+repr(protocol)+",_protocol.__dict__)\n"
    wire = (bootstrap+old_worker+"\nSEO_PREVIEW_REQUEST=json.loads("+repr(json.dumps(request))+ ")\n"+worker).encode()
    connection = load_connection(legacy/"config/query_connection.json")
    result = subprocess.run(ssh_command(connection, root=legacy), input=wire, capture_output=True, timeout=240)
    if result.returncode or len(result.stdout) > 2_000_000:
        print(json.dumps({"status": "error", "error": "readonly_bridge_failed", "writes": 0}))
        return 2
    try:
        response = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        print(json.dumps({"status": "error", "error": "invalid_readonly_bridge_response", "writes": 0}))
        return 2
    if response.get("protocol") != request["protocol"] or response.get("writes") != 0:
        print(json.dumps({"status": "error", "error": "readonly_bridge_did_not_complete", "error_category": response.get("error_category"), "writes": 0}))
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        print(json.dumps({"status": "error", "error": "output_exists_no_overwrite"}))
        return 2
    with output.open("x", encoding="utf-8") as stream:
        json.dump(response, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    if request["protocol"] == "seo-weekly-read/1":
        print(json.dumps({"status": "weekly_read_complete", "sample": False, "batch_confirmed": False,
                          "output": str(output), "weeks": len(response["weeks"]), "source_outcomes": [
                              {"start": row["start"], "end": row["end"], "status": {s: v["status"] for s, v in row.items() if isinstance(v, dict) and "status" in v}} for row in response["weeks"]], "writes_on_server": 0}))
        return 0
    print(json.dumps({"status": "preview_read_complete", "sample": False, "batch_confirmed": False,
                      "output": str(output), "source_outcomes": [{"offset": x["offset_days"], "periods": {k: {s: v["status"] for s,v in row.items() if isinstance(v,dict) and "status" in v} for k,row in x["periods"].items()}} for x in response["scenarios"]], "writes_on_server": 0}))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except (AnalysisError, OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        print(json.dumps({"status": "error", "error": "readonly_preview_input_or_transport_failed", "writes": 0}))
        code = 2
    raise SystemExit(code)
