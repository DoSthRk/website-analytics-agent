"""Run the synthetic local slice. No production clients, credentials or network."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))

from website_analytics.analysis.batches import BatchStore
from website_analytics.analysis.contracts import AnalysisError, BatchDraft, Query
from website_analytics.analysis.query import analyze
from website_analytics.analysis.readers import FixtureReader, read_json
from website_analytics.page_classification import build_page_dimension, load_page_classification
from website_analytics.product_mapping import load_product_mapping


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="local synthetic demo directory")
    parser.add_argument("--snapshot", action="store_true", help="also append an immutable aggregate snapshot; does not start a service")
    args = parser.parse_args(argv)
    try:
        examples = ROOT/"examples/batch-analysis"
        draft = BatchDraft.parse(read_json(examples/"draft.json"))
        if not draft.sample:
            raise AnalysisError("demo may only confirm synthetic samples")
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        store = BatchStore(output/"synthetic-batches.sqlite")
        if store.path.exists():
            confirmed = store.get(draft.batch_id, draft.revision)
            if confirmed.draft.digest != draft.digest:
                raise AnalysisError("demo output contains a different batch")
        else:
            confirmed = store.confirm(draft, draft.digest, "synthetic-demo-not-user-confirmation")
        query = Query.parse(read_json(examples/"query.json"), draft)
        reader = FixtureReader(examples/"evidence.json")
        config = load_page_classification(ROOT/"config/page_classifications/genemedi-net.yaml", "genemedi-net")
        dimension = build_page_dimension(config, reader.dimension_rows)
        mapping = load_product_mapping(ROOT/"config/product_mappings/genemedi-net.yaml", "genemedi-net")
        snapshot_id = None
        if args.snapshot:
            from website_analytics.analysis.snapshots import SnapshotStore, capture
            payload = capture(confirmed, query, reader, dimension, mapping)
            store = SnapshotStore(output/"snapshots.sqlite")
            snapshot_id = store.put(payload)
            report = store.report(snapshot_id)
        else:
            report = analyze(confirmed, query, reader, dimension, mapping)
        path = output/"synthetic-report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
        print(json.dumps({"sample": True, "status": report["status"], "report": str(path),
            "snapshot_id": snapshot_id,
            "result_digest": report["result_digest"],
            "warning": "Synthetic example only; no real publication, user batch or measured effect."}, indent=2))
        return 0 if report["status"] == "ok" else 3
    except (AnalysisError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "error", "error": "invalid_local_demo_state"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
