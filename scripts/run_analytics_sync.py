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
from website_analytics.feishu_records import (
    LarkCLIRecordClient,
    load_feishu_target,
    sync_record_sets,
)
from website_analytics.periods import AnalyticsPeriod, period_key, previous_analytics_period
from website_analytics.page_classification import (
    build_page_dimension,
    load_page_classification,
)
from website_analytics.product_mapping import load_product_mapping
from website_analytics.sync_plan import load_sync_profile, select_sync_periods


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
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("config/feishu_dashboard/v2/sync_target.json"),
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
    raise RuntimeError(
        "three-source fetch did not complete: "
        + json.dumps(statuses, ensure_ascii=True, sort_keys=True)
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _arguments()
    profile = load_sync_profile(args.profile)
    site = require_site(load_sites(args.site_config), profile.site)
    if site.timezone != profile.selection_timezone:
        raise ValueError("sync profile timezone must match the registered site timezone")
    mapping = load_product_mapping(args.mapping, profile.site)
    if mapping is None:
        raise ValueError("approved product mapping is required")
    if site.inquiry_source is None:
        raise ValueError("approved page dimension requires the registered read-only database source")
    classification = load_page_classification(args.page_classification, profile.site)
    page_dimension = build_page_dimension(
        classification,
        create_page_dimension_adapter(site.inquiry_source).query(),
    )
    anchor = (
        date.fromisoformat(args.anchor)
        if args.anchor
        else datetime.now(ZoneInfo(profile.selection_timezone)).date()
    )
    _validate(profile.site, args.site_config)

    target_rows = select_sync_periods(profile, anchor, args.scope)
    target_periods = [_period(row) for row in target_rows]
    plan_by_key = {str(row["periodKey"]): row for row in target_rows}
    required_by_key: dict[str, AnalyticsPeriod] = {}
    for period in target_periods:
        required_by_key[period_key(profile.site, period)] = period
        previous = previous_analytics_period(period)
        required_by_key[period_key(profile.site, previous)] = previous

    fetched: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for index, key in enumerate(sorted(required_by_key), start=1):
        period = required_by_key[key]
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

    generated_at = datetime.now(timezone.utc)
    batch = f"{profile.site}-{args.scope}-{generated_at.strftime('%Y%m%dT%H%M%SZ')}"
    overview: list[dict[str, Any]] = []
    products: list[dict[str, Any]] = []
    for period in target_periods:
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
        )
        overview.append(records["overview"])
        products.extend(records["products"])

    run_dir = args.output_dir / batch
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(run_dir / "overview.json", overview)
    _write_json(run_dir / "products.json", products)
    write_result: dict[str, int] | None = None
    if args.apply:
        target = load_feishu_target(args.target)
        write_result = sync_record_sets(
            LarkCLIRecordClient(target),
            target,
            overview,
            products,
        )
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
        "feishu": write_result,
        "output_dir": str(run_dir),
    }
    _write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
