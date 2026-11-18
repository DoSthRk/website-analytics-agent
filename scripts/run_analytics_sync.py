"""Fetch approved analytics periods and optionally upsert aggregate Feishu rows."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from website_analytics.config import load_sites, require_site
from website_analytics.adapters.page_dimension import create_page_dimension_adapter
from website_analytics.dashboard_sync import build_dashboard_records, load_period_details
from website_analytics.dashboard_v3 import build_v3_daily_records
from website_analytics.feishu_records import (
    LarkCLIRecordClient,
    load_feishu_target,
    sync_record_sets,
)
from website_analytics.feishu_product_funnel import (
    additive_totals as product_funnel_additive_totals,
    validate_backfill as validate_product_funnel_backfill,
    validate_contract as validate_product_funnel_contract,
)
from website_analytics.feishu_product_funnel_sync import (
    load_product_funnel_target,
    sync_product_funnel_record_sets,
)
from website_analytics.feishu_v3 import (
    additive_totals,
    load_json_object,
    validate_contract,
)
from website_analytics.feishu_v3_sync import (
    load_feishu_v3_target,
    sync_v3_record_sets,
)
from website_analytics.information_mapping import load_information_mapping
from website_analytics.periods import AnalyticsPeriod, period_key, previous_analytics_period
from website_analytics.page_classification import (
    build_page_dimension,
    load_page_classification,
)
from website_analytics.product_mapping import load_product_mapping
from website_analytics.product_funnel import build_product_funnel_observation_daily
from website_analytics.product_funnel_backfill import (
    TABLE_KEYS as PRODUCT_FUNNEL_TABLE_KEYS,
)
from website_analytics.sync_plan import load_sync_profile, select_sync_periods
from website_analytics.sync_status import daily_coverage, previous_success, save_status


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("intraday", "full"), required=True)
    parser.add_argument("--anchor", help="Optional YYYY-MM-DD override for controlled runs")
    parser.add_argument("--profile", type=Path, default=Path("config/sync_profiles/genemedi-net.json"))
    parser.add_argument("--site-config", type=Path, default=Path("config/sites.yaml"))
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path("config/product_mappings/genemedi-net.yaml"),
    )
    parser.add_argument(
        "--page-classification",
        type=Path,
        default=Path("config/page_classifications/genemedi-net.yaml"),
    )
    parser.add_argument(
        "--information-mapping",
        type=Path,
        default=Path("config/information_mappings/genemedi-net.yaml"),
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("config/feishu_dashboard/v2/sync_target.json"),
    )
    parser.add_argument(
        "--v3-contract",
        type=Path,
        help="Optional validated V3 daily-table contract",
    )
    parser.add_argument(
        "--v3-target",
        type=Path,
        help="Optional registered V3 daily-table target",
    )
    parser.add_argument(
        "--product-funnel-contract",
        type=Path,
        help="Optional validated aggregate product-funnel contract",
    )
    parser.add_argument(
        "--product-funnel-target",
        type=Path,
        help="Optional registered aggregate product-funnel target",
    )
    parser.add_argument(
        "--v3-only",
        action="store_true",
        help="Write only V3 daily tables and leave legacy V2 tables unchanged",
    )
    parser.add_argument("--apply", action="store_true", help="Write aggregate rows to Feishu")
    return parser.parse_args()


def _period(row: dict[str, Any]) -> AnalyticsPeriod:
    kind = row.get("kind")
    if kind not in {"day", "week", "month", "quarter", "year", "rolling", "custom"}:
        raise ValueError("sync plan contains an invalid period kind")
    start = date.fromisoformat(str(row["start"]))
    end = date.fromisoformat(str(row["end"]))
    window_days = row.get("windowDays")
    if window_days is not None and (isinstance(window_days, bool) or not isinstance(window_days, int)):
        raise ValueError("sync plan contains an invalid rolling window")
    return AnalyticsPeriod(kind=kind, start=start, end=end, window_days=window_days)


def _run_cli(arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "website_analytics", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    raw = completed.stdout or completed.stderr
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("website-analytics CLI returned a non-JSON response") from error
    if not isinstance(result, dict):
        raise RuntimeError("website-analytics CLI returned an invalid response")
    result["_exit_code"] = completed.returncode
    return result


def _validate(site: str, config: Path) -> None:
    result = _run_cli(
        ["validate-config", "--site", site, "--config", str(config)]
    )
    if result.get("_exit_code") != 0 or result.get("status") != "ok":
        raise RuntimeError("registered analytics site configuration is invalid")


class FetchIncompleteError(RuntimeError):
    """Safe per-source failure details, without payloads or exception text."""

    def __init__(self, statuses: dict[str, Any]) -> None:
        super().__init__("three-source fetch did not complete")
        self.source_statuses = statuses


def _fetch(
    site: str,
    config: Path,
    period: AnalyticsPeriod,
    cache_dir: Path,
    audit_dir: Path,
) -> dict[str, Any]:
    arguments = [
        "fetch",
        "--site",
        site,
        "--start",
        period.start.isoformat(),
        "--end",
        period.end.isoformat(),
        "--config",
        str(config),
        "--cache-dir",
        str(cache_dir),
        "--audit-dir",
        str(audit_dir),
    ]
    last: dict[str, Any] | None = None
    for attempt in range(5):
        last = _run_cli(arguments)
        if (
            last.get("_exit_code") == 0
            and last.get("status") == "ok"
            and last.get("complete") is True
        ):
            last.pop("_exit_code", None)
            return last
        if attempt < 4:
            time.sleep(3 * (attempt + 1))
    sources = last.get("sources") if isinstance(last, dict) else None
    statuses = {
        name: {
            "status": value.get("status"),
            "error_type": value.get("error_type"),
        }
        for name, value in sources.items()
        if isinstance(name, str) and isinstance(value, dict)
    } if isinstance(sources, dict) else {}
    raise FetchIncompleteError(statuses)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _arguments()
    if (args.v3_contract is None) != (args.v3_target is None):
        raise ValueError("--v3-contract and --v3-target must be configured together")
    if (args.product_funnel_contract is None) != (
        args.product_funnel_target is None
    ):
        raise ValueError(
            "--product-funnel-contract and --product-funnel-target "
            "must be configured together"
        )
    if args.v3_only and args.v3_contract is None:
        raise ValueError("--v3-only requires --v3-contract and --v3-target")
    generated_at = datetime.now(timezone.utc)
    # The full-site publisher orders runs by the first ``\d{8}T\d{6}Z`` in the
    # directory name; keep that token intact and make it unique with a suffix.
    batch = (
        f"{args.scope}-{generated_at.strftime('%Y%m%dT%H%M%SZ')}"
        f"-{generated_at.microsecond:06d}"
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = args.output_dir / batch
    run_dir.mkdir(exist_ok=False)
    status: dict[str, Any] = {
        "status": "running", "stage": "validation", "scope": args.scope,
        "batch": batch, "started_at": generated_at.isoformat(),
        "last_successful_check_at": previous_success(args.output_dir),
        "publication": "not_started" if args.apply else "not_requested",
        "remote_history_coverage": "not_verified",
    }
    save_status(args.output_dir, run_dir, status)
    try:
        summary = _execute(args, run_dir, batch, generated_at, status)
    except Exception as error:
        status.update(status="failed", finished_at=datetime.now(timezone.utc).isoformat(),
                      error_type=type(error).__name__)
        if isinstance(error, FetchIncompleteError):
            status["source_statuses"] = error.source_statuses
        # A write-stage failure can leave a partially applied batch. Never call
        # the previous published data invalid or claim it was rolled back.
        if args.apply and status["stage"] == "publication":
            status["publication"] = "failed_or_partial"
        save_status(args.output_dir, run_dir, status)
        print(json.dumps(status, ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return 1
    checked_at = datetime.now(timezone.utc).isoformat()
    status.update(status="ok", stage="completed", finished_at=checked_at,
                  last_successful_check_at=checked_at)
    save_status(args.output_dir, run_dir, status)
    summary["run_status"] = status
    _write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


def _execute(args: argparse.Namespace, run_dir: Path, batch: str,
             generated_at: datetime, status: dict[str, Any]) -> dict[str, Any]:
    def checkpoint(stage: str, **values: Any) -> None:
        status.update(stage=stage, **values)
        save_status(args.output_dir, run_dir, status)

    v3_contract: dict[str, Any] | None = None
    v3_target = None
    if args.v3_contract is not None and args.v3_target is not None:
        v3_contract = load_json_object(args.v3_contract)
        validate_contract(v3_contract)
        v3_target = load_feishu_v3_target(args.v3_target)
    product_funnel_contract: dict[str, Any] | None = None
    product_funnel_target = None
    if (
        args.product_funnel_contract is not None
        and args.product_funnel_target is not None
    ):
        product_funnel_contract = load_json_object(args.product_funnel_contract)
        validate_product_funnel_contract(product_funnel_contract)
        product_funnel_target = load_product_funnel_target(
            args.product_funnel_target
        )
    profile = load_sync_profile(args.profile)
    site = require_site(load_sites(args.site_config), profile.site)
    if site.timezone != profile.selection_timezone:
        raise ValueError("sync profile timezone must match the registered site timezone")
    mapping = load_product_mapping(args.mapping, profile.site)
    if mapping is None:
        raise ValueError("approved product mapping is required")
    information_mapping = load_information_mapping(
        args.information_mapping, profile.site
    )
    if information_mapping is None:
        raise ValueError("approved information mapping is required")
    if site.inquiry_source is None:
        raise ValueError("approved page dimension requires the registered read-only database source")
    _validate(profile.site, args.site_config)
    checkpoint("page_dimension", site=profile.site)
    classification = load_page_classification(args.page_classification, profile.site)
    page_dimension = build_page_dimension(
        classification,
        create_page_dimension_adapter(site.inquiry_source).query(),
    )
    checkpoint("planning")
    anchor = (
        date.fromisoformat(args.anchor)
        if args.anchor
        else datetime.now(ZoneInfo(profile.selection_timezone)).date()
    )
    target_rows = select_sync_periods(profile, anchor, args.scope, v3_only=args.v3_only)
    if args.v3_only and not target_rows:
        raise ValueError("V3 sync selection contains no finalized daily facts")
    checkpoint("fetch", coverage=daily_coverage(target_rows, []))
    target_periods = [_period(row) for row in target_rows]
    plan_by_key = {str(row["periodKey"]): row for row in target_rows}
    required_by_key: dict[str, AnalyticsPeriod] = {}
    for period in target_periods:
        required_by_key[period_key(profile.site, period)] = period
        if not args.v3_only:
            previous = previous_analytics_period(period)
            required_by_key[period_key(profile.site, previous)] = previous

    fetched: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for index, key in enumerate(sorted(required_by_key), start=1):
        period = required_by_key[key]
        checkpoint("fetch", active_period_key=key, fetched_periods=index - 1, required_periods=len(required_by_key))
        print(
            json.dumps(
                {"event": "fetch", "index": index, "total": len(required_by_key), "periodKey": key}
            ),
            file=sys.stderr,
            flush=True,
        )
        fetched[key] = _fetch(
            profile.site,
            args.site_config,
            period,
            args.cache_dir,
            args.audit_dir,
        )
        details[key] = load_period_details(args.cache_dir, profile.site, period)

    checkpoint("build", active_period_key=None, fetched_periods=len(fetched))
    overview: list[dict[str, Any]] = []
    products: list[dict[str, Any]] = []
    for period in (() if args.v3_only else target_periods):
        key = period_key(profile.site, period)
        previous_key = period_key(profile.site, previous_analytics_period(period))
        records = build_dashboard_records(
            site=profile.site,
            period=period,
            plan=plan_by_key[key],
            current_result=fetched[key],
            current_details=details[key],
            previous_result=fetched[previous_key],
            previous_details=details[previous_key],
            mapping=mapping,
            page_dimension=page_dimension,
            sync_batch=batch,
            information_mapping=information_mapping,
        )
        overview.append(records["overview"])
        products.extend(records["products"])

    finalized_periods = [
        period
        for period in target_periods
        if period.kind == "day"
        and plan_by_key[period_key(profile.site, period)].get("isFinal") is True
    ]
    if (v3_contract is not None or product_funnel_contract is not None) and not (
        finalized_periods
    ):
        raise ValueError("sync selection contains no finalized daily facts")

    v3_document: dict[str, Any] | None = None
    if args.v3_contract is not None:
        v3_records: dict[str, list[dict[str, Any]]] = {
            "overview_daily": [],
            "product_daily": [],
            "information_daily": [],
        }
        finalized_days: list[str] = []
        for period in finalized_periods:
            key = period_key(profile.site, period)
            payload = build_v3_daily_records(
                site=profile.site,
                data_date=period.start,
                fetch_result=fetched[key],
                details=details[key],
                product_mapping=mapping,
                page_dimension=page_dimension,
                information_mapping=information_mapping,
            )
            for logical_name in v3_records:
                rows = payload["records"].get(logical_name)
                if not isinstance(rows, list):
                    raise ValueError("V3 daily payload is missing a declared table")
                v3_records[logical_name].extend(rows)
            finalized_days.append(period.start.isoformat())
        record_counts = {name: len(rows) for name, rows in v3_records.items()}
        v3_document = {
            "schema_version": "3",
            "mode": "incremental_daily_sync",
            "write_enabled": args.apply,
            "site": profile.site,
            "date_range": {
                "start": min(finalized_days),
                "end": max(finalized_days),
                "inclusive": True,
                "days": len(finalized_days),
            },
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "records": v3_records,
            "reconciliation": {
                "status": "passed",
                "complete_days": len(finalized_days),
                "record_counts": record_counts,
                "unique_stable_keys": True,
                "additive_totals": additive_totals(v3_records),
                "page_dimension": dict(page_dimension.summary),
                "page_classification_version": page_dimension.version,
                "product_mapping_version": mapping.version,
                "information_mapping_version": information_mapping.version,
            },
        }

    product_funnel_document: dict[str, Any] | None = None
    if product_funnel_contract is not None:
        product_funnel_records: dict[str, list[dict[str, Any]]] = {
            key: [] for key in PRODUCT_FUNNEL_TABLE_KEYS
        }
        product_funnel_days: list[str] = []
        for period in finalized_periods:
            key = period_key(profile.site, period)
            payload = build_product_funnel_observation_daily(
                site=profile.site,
                data_date=period.start,
                fetch_result=fetched[key],
                details=details[key],
                product_mapping=mapping,
                page_dimension=page_dimension,
            )
            for logical_name in PRODUCT_FUNNEL_TABLE_KEYS:
                rows = payload["records"].get(logical_name)
                if not isinstance(rows, list):
                    raise ValueError(
                        "product funnel daily payload is missing a declared table"
                    )
                product_funnel_records[logical_name].extend(rows)
            product_funnel_days.append(period.start.isoformat())
        product_funnel_counts = {
            name: len(rows) for name, rows in product_funnel_records.items()
        }
        product_funnel_document = {
            "schema_version": "1",
            "mode": "incremental_daily_sync",
            "write_enabled": False,
            "site": profile.site,
            "date_range": {
                "start": min(product_funnel_days),
                "end": max(product_funnel_days),
                "inclusive": True,
                "days": len(product_funnel_days),
            },
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "records": product_funnel_records,
            "reconciliation": {
                "status": "passed",
                "complete_days": len(product_funnel_days),
                "record_counts": product_funnel_counts,
                "unique_stable_keys": True,
                "additive_totals": product_funnel_additive_totals(
                    product_funnel_records
                ),
                "page_dimension": dict(page_dimension.summary),
                "page_classification_version": page_dimension.version,
                "product_mapping_version": mapping.version,
            },
        }
        # Validate the exact publishable shape before creating any local artifact.
        validate_product_funnel_backfill(
            product_funnel_document,
            validate_product_funnel_contract(product_funnel_contract),
        )

    if not args.v3_only:
        _write_json(run_dir / "overview.json", overview)
        _write_json(run_dir / "products.json", products)
    if v3_document is not None:
        _write_json(run_dir / "v3-daily.json", v3_document)
        checkpoint("publication" if args.apply else "dry_run", coverage=daily_coverage(target_rows, finalized_days))
    if product_funnel_document is not None:
        _write_json(run_dir / "product-funnel-daily.json", product_funnel_document)
    write_result: dict[str, int | str] | None = None
    v3_write_result: dict[str, dict[str, int]] | None = None
    product_funnel_write_result: dict[str, dict[str, int]] | None = None
    if args.apply:
        checkpoint("publication", publication="in_progress")
        if args.v3_only:
            write_result = {"status": "skipped", "reason": "v3_only"}
        else:
            target = load_feishu_target(args.target)
            write_result = sync_record_sets(
                LarkCLIRecordClient(target),
                target,
                overview,
                products,
            )
        if v3_document is not None and v3_target is not None and v3_contract is not None:
            v3_write_result = sync_v3_record_sets(
                LarkCLIRecordClient(v3_target.record_client_target()),
                v3_target,
                v3_contract,
                v3_document,
            )
        if (
            product_funnel_document is not None
            and product_funnel_target is not None
            and product_funnel_contract is not None
        ):
            product_funnel_write_result = sync_product_funnel_record_sets(
                LarkCLIRecordClient(
                    product_funnel_target.record_client_target()
                ),
                product_funnel_target,
                product_funnel_contract,
                product_funnel_document,
            )
        checkpoint("publication", publication="verified" if v3_write_result is not None else "completed_without_v3_readback")
    summary = {
        "status": "ok",
        "mode": "apply" if args.apply else "dry-run",
        "scope": args.scope,
        "site": profile.site,
        "anchor": anchor.isoformat(),
        "batch": batch,
        "fetched_periods": len(required_by_key),
        "overview_records": len(overview),
        "product_records": len(products),
        "page_dimension": dict(page_dimension.summary),
        "product_mapping_version": mapping.version,
        "information_mapping_version": information_mapping.version,
        "feishu": write_result,
        "feishu_v3": {
            "date_range": v3_document["date_range"],
            "record_counts": v3_document["reconciliation"]["record_counts"],
            "write_result": v3_write_result,
        } if v3_document is not None else None,
        "product_funnel": {
            "date_range": product_funnel_document["date_range"],
            "record_counts": product_funnel_document["reconciliation"][
                "record_counts"
            ],
            "write_result": product_funnel_write_result,
        } if product_funnel_document is not None else None,
        "output_dir": str(run_dir),
    }
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
