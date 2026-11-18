"""Local-only CLI: explicit confirmation writes a ledger; queries are read-only."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from website_analytics.analysis.batches import BatchStore
from website_analytics.analysis.contracts import AnalysisError, BatchDraft, Query
from website_analytics.analysis.query import analyze
from website_analytics.analysis.readers import FixtureReader, read_json
from website_analytics.page_classification import build_page_dimension, load_page_classification
from website_analytics.product_mapping import load_product_mapping

ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Versioned local batch analysis; no production connection")
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft", help="validate and show digest; no writes")
    draft.add_argument("--input", required=True)
    confirm = commands.add_parser("confirm", help="append a reviewed local confirmation")
    confirm.add_argument("--input", required=True)
    confirm.add_argument("--digest", required=True, help="exact digest reviewed in draft output")
    confirm.add_argument("--confirmed-by", required=True)
    confirm.add_argument("--store", required=True)
    show = commands.add_parser("show", help="read a pinned confirmation")
    show.add_argument("--store", required=True)
    show.add_argument("--batch-id", required=True)
    show.add_argument("--revision", type=int, required=True)
    query = commands.add_parser("query", help="read-only analysis using synthetic evidence only")
    query.add_argument("--request", required=True)
    query.add_argument("--store", required=True)
    query.add_argument("--sample-evidence", required=True)
    snapshot = commands.add_parser("snapshot", help="capture immutable aggregate sample evidence locally")
    snapshot.add_argument("--request", required=True)
    snapshot.add_argument("--store", required=True)
    snapshot.add_argument("--sample-evidence", required=True)
    snapshot.add_argument("--snapshots", required=True)
    replay = commands.add_parser("replay", help="read a pinned local snapshot, no source access")
    replay.add_argument("--snapshot-id", required=True)
    replay.add_argument("--snapshots", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"draft", "confirm"}:
            batch = BatchDraft.parse(read_json(args.input))
            if args.command == "draft":
                output = {"status": "draft_not_saved", "sample": batch.sample,
                    "digest": batch.digest, "batch": batch.document(),
                    "confirmation_required": "Review URLs, actual publication timestamp/timezone and change description before confirm"}
            else:
                output = {"status": "confirmed_locally", **BatchStore(args.store).confirm(batch, args.digest, args.confirmed_by).document()}
        elif args.command == "show":
            output = BatchStore(args.store).get(args.batch_id, args.revision).document()
        elif args.command == "replay":
            from website_analytics.analysis.snapshots import SnapshotStore
            output = SnapshotStore(args.snapshots).report(args.snapshot_id)
        else:
            request = read_json(args.request)
            if not isinstance(request, dict):
                raise AnalysisError("query must be an object")
            batch = BatchStore(args.store).get(request.get("batch_id"), request.get("revision"))
            selection = Query.parse(request, batch.draft)
            reader = FixtureReader(args.sample_evidence)
            config = load_page_classification(ROOT/"config/page_classifications/genemedi-net.yaml", "genemedi-net")
            dimension = build_page_dimension(config, reader.dimension_rows)
            mapping = load_product_mapping(ROOT/"config/product_mappings/genemedi-net.yaml", "genemedi-net")
            if args.command == "snapshot":
                from website_analytics.analysis.snapshots import SnapshotStore, capture
                payload = capture(batch, selection, reader, dimension, mapping)
                snapshot_id = SnapshotStore(args.snapshots).put(payload)
                output = {"status": SnapshotStore(args.snapshots).report(snapshot_id)["status"],
                          "sample": True, "snapshot_id": snapshot_id, "saved_locally": True}
            else:
                output = analyze(batch, selection, reader, dimension, mapping)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 3 if output.get("status") == "partial" else 0
    except (AnalysisError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        # Never include raw exception/input strings: paths and malformed fixtures
        # are untrusted and can contain data that should not be echoed.
        print(json.dumps({"status": "error", "error": "invalid_analysis_input_or_local_state"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
