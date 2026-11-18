"""Offline import of the already-saved four-page case. Never contacts sources."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from website_analytics.analysis.cases import CaseStore, make_case_record
from website_analytics.analysis.contracts import AnalysisError, fingerprint, strict_keys
from website_analytics.analysis.readers import read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-dir", required=True)
    parser.add_argument("--continuous-dir", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--case-id", default="purprox-seo-four-pages")
    args = parser.parse_args(argv)
    try:
        preview, continuous = Path(args.preview_dir), Path(args.continuous_dir)
        context = read_json(continuous/"draft.json")
        notice = read_json(continuous/"interpretation-update.json")
        strict_keys(notice, {"contract", "current_intervention", "prior_reports_preserved", "prior_report_bytes_changed",
            "prior_october_comparison_meaning", "october_13_is_not_a_verified_single_intervention_or_completion", "stage_effects_identifiable"})
        if notice["current_intervention"] != context or notice["prior_report_bytes_changed"] is not False:
            raise AnalysisError("saved interpretation provenance mismatch")
        prior = list(notice["prior_reports_preserved"])+[fingerprint(notice)]
        records = [make_case_record(args.case_id, "approximate_window", read_json(preview/"draft.json"),
                    read_json(preview/"source-aggregates.json"), read_json(preview/"url-audit.json"), context, superseded=prior),
                   make_case_record(args.case_id, "continuous_trend", context, read_json(continuous/"source-weeks.json"),
                    read_json(continuous/"url-state.json"), context, freshness=read_json(continuous/"gsc-freshness.json"), superseded=prior)]
        ids = CaseStore(args.store).put_many(records)
        print(json.dumps({"status": "imported_offline", "case_id": args.case_id, "evidence_state": "provisional",
            "sample": False, "batch_confirmed": False, "versions": [{"kind": record["kind"], "evidence_id": key} for record, key in zip(records, ids, strict=True)],
            "source_reads": 0, "external_writes": 0}, indent=2))
        return 0
    except (AnalysisError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print(json.dumps({"status": "error", "error": "invalid_saved_case_evidence_no_import"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
