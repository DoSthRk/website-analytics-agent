"""Safe command-line entry points for the GA4 and Search Console reports."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.analytics.data_v1beta.types import RunReportResponse

from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.adapters.gsc import GSCAdapter, GSCQueryResult
from website_analytics.cache import write_audit_manifest, write_cached_json
from website_analytics.config import ConfigError, load_sites, require_site
from website_analytics.dates import DateRangeError, parse_date_range, previous_period
from website_analytics.models import DateRange, SiteConfig
from website_analytics.reporting import compare_totals
from website_analytics.url_safety import sanitize_url_values
from website_analytics.workbook_payload import build_workbook_payload


_GA4_INTERVAL_METRICS = (
    "sessions",
    "totalUsers",
    "activeUsers",
    "engagedSessions",
    "engagementRate",
    "screenPageViews",
    "keyEvents",
)
_GSC_TOTAL_METRICS = ("clicks", "impressions")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BUILDER_SCRIPT = _PROJECT_ROOT / "scripts" / "build_report_workbook.mjs"


class CLIInputError(ValueError):
    """Raised for invalid command-line input without printing from argparse."""


class DataSourceError(RuntimeError):
    """Raised when an approved data source or workbook builder cannot run."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIInputError(message)


class _FixtureGA4Client:
    """Fixture-only GA4 client that makes the public adapter do normalization."""

    def __init__(self, fixture_dir: Path) -> None:
        self._fixture_dir = fixture_dir

    def run_report(self, request: Any) -> RunReportResponse:
        raw = _read_fixture(self._fixture_dir, "ga4_report.json")
        dimensions = getattr(request, "dimensions", ())
        dimension_name = getattr(dimensions[0], "name", "") if dimensions else ""
        if not dimensions:
            for row in raw.get("rows", []):
                row["dimensionValues"] = []
        elif dimension_name == "landingPagePlusQueryString":
            for row in raw.get("rows", []):
                row["dimensionValues"][0]["value"] = "/fixture-landing-page"
        return RunReportResponse.from_json(json.dumps(raw))


class _FixtureGSCResource:
    def __init__(self, fixture_dir: Path) -> None:
        self._fixture_dir = fixture_dir
        self._body: Mapping[str, Any] | None = None

    def query(self, *, siteUrl: str, body: Mapping[str, Any]) -> "_FixtureGSCResource":
        del siteUrl
        self._body = body
        return self

    def execute(self) -> Mapping[str, Any]:
        if self._body is None:
            raise DataSourceError("fixture Search Console request was not prepared")
        dimensions = tuple(self._body.get("dimensions", ()))
        fixture_name = {
            ("date",): "gsc_daily.json",
            ("page",): "gsc_pages.json",
            ("query",): "gsc_queries.json",
        }.get(dimensions)
        if fixture_name is None:
            raise DataSourceError("unsupported fixture Search Console dimensions")
        return _read_fixture(self._fixture_dir, fixture_name)


class _FixtureGSCService:
    def __init__(self, fixture_dir: Path) -> None:
        self._resource = _FixtureGSCResource(fixture_dir)

    def searchanalytics(self) -> _FixtureGSCResource:
        return self._resource


def build_parser() -> argparse.ArgumentParser:
    """Build the deliberately small, allowlisted command surface."""
    parser = _Parser(prog="website-analytics", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-config", add_help=False)
    _add_site_options(validate)

    for name in ("fetch", "report", "export-excel"):
        command = commands.add_parser(name, add_help=False)
        _add_site_options(command)
        command.add_argument("--start", required=True, help="Inclusive ISO date (YYYY-MM-DD)")
        command.add_argument("--end", required=True, help="Inclusive ISO date (YYYY-MM-DD)")
        command.add_argument(
            "--fixture-dir",
            type=Path,
            help="Offline fixture directory; never initializes Google clients",
        )
        command.add_argument(
            "--cache-dir",
            type=Path,
            default=Path("cache"),
            help="Local redacted cache directory",
        )
        command.add_argument(
            "--audit-dir",
            type=Path,
            default=Path("audits"),
            help="Local audit-manifest directory",
        )
        if name in {"report", "export-excel"}:
            command.add_argument(
                "--compare",
                choices=("previous-period", "previous-4-weeks"),
                default="previous-period",
                help="Approved comparison window (default: previous-period)",
            )
        if name == "export-excel":
            command.add_argument(
                "--output",
                type=Path,
                required=True,
                help="Destination .xlsx file",
            )
    return parser


def _add_site_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--site", required=True, help="Registered site key")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/sites.yaml"),
        help="Registered-site configuration file",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command and honour the JSON stdout/stderr exit-code contract."""
    try:
        args = build_parser().parse_args(argv)
        site = require_site(load_sites(args.config), args.site)
        if args.command == "validate-config":
            return _write_stdout(
                {
                    "status": "ok",
                    "command": args.command,
                    "site": site.site_key,
                    "config": str(args.config),
                    "timezone": site.timezone,
                    "offline": True,
                }
            )

        date_range = _parse_cli_date_range(args.start, args.end)
        if args.command == "fetch":
            current = _collect_dataset(site, date_range, args.fixture_dir)
            result = _base_result(args.command, site, current)
            _persist_dataset(args, site, current, command=args.command)
            return _write_stdout(result, 0 if current["complete"] else 3)

        comparison_range = _comparison_range(date_range, args.compare)
        current = _collect_dataset(site, date_range, args.fixture_dir)
        previous = _collect_dataset(site, comparison_range, args.fixture_dir)
        comparison = compare_totals(current["totals"], previous["totals"])
        metric_coverage_complete = comparison["metric_coverage_complete"]
        comparison_complete = bool(previous["complete"] and comparison["complete"])
        result = _base_result(args.command, site, current)
        result["comparison"] = {
            "kind": args.compare,
            "date_range": _range_json(comparison_range),
            "timezone": site.timezone,
            "date_range_interpretation": _date_range_interpretation(site),
            "sources": previous["audit"]["sources"],
            "source_coverage_complete": comparison["source_coverage_complete"],
            "metric_coverage_complete": metric_coverage_complete,
            "complete": comparison_complete,
            "metrics": comparison["metrics"],
        }
        result["complete"] = bool(current["complete"] and comparison_complete)
        result["status"] = "ok" if result["complete"] else "partial"

        if args.command == "report":
            _persist_dataset(args, site, current, command=args.command, previous=previous)
            return _write_stdout(result, 0 if result["complete"] else 3)

        if args.command == "export-excel":
            _validate_output_path(args.output)
            if not result["complete"]:
                _persist_dataset(
                    args,
                    site,
                    current,
                    command=args.command,
                    previous=previous,
                )
                return _write_stdout(result, 3)
            payload = build_workbook_payload(
                {
                    "site": site.site_key,
                    "display_name": site.display_name,
                    "date_range": _range_json(date_range),
                    "timezone": site.timezone,
                    "date_range_interpretation": _date_range_interpretation(site),
                    "freshness": current["freshness"],
                    "comparison": comparison,
                },
                current["details"],
                current["audit"],
            )
            builder_result = _run_workbook_builder(payload, args.output)
            result["export"] = builder_result
            _persist_dataset(
                args,
                site,
                current,
                command=args.command,
                previous=previous,
                output_path=args.output,
            )
            return _write_stdout(result)

        raise CLIInputError("unsupported command")
    except (CLIInputError, ConfigError, DateRangeError) as error:
        return _write_stderr(error, 2)
    except DataSourceError as error:
        return _write_stderr(error, 3)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _write_stderr(DataSourceError("approved data processing failed"), 3)


def _collect_dataset(
    site: SiteConfig, date_range: DateRange, fixture_dir: Path | None
) -> dict[str, Any]:
    freshness = _freshness()
    try:
        ga4, gsc = _fixture_adapters(fixture_dir) if fixture_dir else _create_live_adapters()
    except Exception as error:
        raise DataSourceError("could not initialize approved GA4/GSC access") from error

    details: dict[str, list[dict[str, str | float]]] = {}
    totals: dict[str, dict[str, float]] = {}
    statuses: dict[str, dict[str, Any]] = {}

    def collect(
        source: str,
        runner: Callable[
            [],
            tuple[
                Mapping[str, list[dict[str, str | float]]],
                dict[str, float],
                Mapping[str, Any],
            ],
        ],
    ) -> None:
        try:
            source_details, source_totals, source_metadata = runner()
            source_details = dict(source_details)
            details.update(source_details)
            totals[source] = source_totals
            statuses[source] = {
                "status": "ok",
                "rows": sum(len(rows) for rows in source_details.values()),
                "freshness": freshness,
                **source_metadata,
            }
        except Exception as error:
            statuses[source] = {
                "status": "error",
                "rows": 0,
                "freshness": freshness,
                "error_type": type(error).__name__,
            }

    collect(
        "ga4",
        lambda: (
            {
                "GA4 Daily": ga4.daily(site, date_range),
                "GA4 Pages": ga4.pages(site, date_range),
            },
            _ga4_totals(ga4.aggregate(site, date_range)),
            {},
        ),
    )
    collect(
        "gsc",
        lambda: _gsc_source_data(gsc, site, date_range),
    )
    complete = all(status["status"] == "ok" for status in statuses.values())
    return {
        "date_range": date_range,
        "freshness": freshness,
        "details": details,
        "totals": totals,
        "complete": complete,
        "audit": {"generated_at": freshness, "sources": statuses},
    }


def _fixture_adapters(fixture_dir: Path | None) -> tuple[GA4Adapter, GSCAdapter]:
    if fixture_dir is None or not fixture_dir.is_dir():
        raise DataSourceError("--fixture-dir must be a readable directory")
    return GA4Adapter(_FixtureGA4Client(fixture_dir)), GSCAdapter(_FixtureGSCService(fixture_dir))


def _create_live_adapters() -> tuple[GA4Adapter, GSCAdapter]:
    """Create read-only clients only after a data command requests live mode."""
    import google.auth
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(
        scopes=(
            "https://www.googleapis.com/auth/analytics.readonly",
            "https://www.googleapis.com/auth/webmasters.readonly",
        )
    )
    return (
        GA4Adapter(BetaAnalyticsDataClient(credentials=credentials)),
        GSCAdapter(
            build(
                "searchconsole",
                "v1",
                credentials=credentials,
                cache_discovery=False,
            )
        ),
    )


def _read_fixture(fixture_dir: Path, name: str) -> dict[str, Any]:
    fixture = fixture_dir / name
    try:
        document = json.loads(fixture.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataSourceError(f"could not load fixture {name}") from error
    if not isinstance(document, dict):
        raise DataSourceError(f"fixture {name} must contain an object")
    return copy.deepcopy(document)


def _ga4_totals(interval_rows: Sequence[Mapping[str, str | float]]) -> dict[str, float]:
    """Use GA4's no-dimension report for interval metrics and unique users."""
    return {
        metric: _sum_metric(interval_rows, metric) for metric in _GA4_INTERVAL_METRICS
    }


def _gsc_source_data(
    gsc: GSCAdapter, site: SiteConfig, date_range: DateRange
) -> tuple[
    Mapping[str, list[dict[str, str | float]]], dict[str, float], Mapping[str, Any]
]:
    daily = gsc.query_result(site, date_range, ["date"])
    pages = gsc.query_result(site, date_range, ["page"])
    queries = gsc.query_result(site, date_range, ["query"])
    details = {
        "GSC Daily": list(daily.rows),
        "GSC Pages": list(pages.rows),
        "GSC Queries": list(queries.rows),
    }
    capped_details = {
        name: _gsc_detail_metadata(result)
        for name, result in (("GSC Pages", pages), ("GSC Queries", queries))
    }
    truncated = any(metadata["truncated"] for metadata in capped_details.values())
    return (
        details,
        _gsc_totals(details),
        {
            "status": "partial" if truncated else "ok",
            "truncated": truncated,
            "details": capped_details,
        },
    )


def _gsc_detail_metadata(result: GSCQueryResult) -> dict[str, Any]:
    return {
        "rows": len(result.rows),
        "row_cap": result.row_cap,
        "truncated": result.truncated,
    }


def _gsc_totals(details: Mapping[str, list[dict[str, str | float]]]) -> dict[str, float]:
    daily = details.get("GSC Daily", [])
    totals = {metric: _sum_metric(daily, metric) for metric in _GSC_TOTAL_METRICS}
    impressions = totals["impressions"]
    if impressions:
        totals["ctr"] = totals["clicks"] / impressions
        weighted_position = sum(
            _numeric(row.get("position")) * _numeric(row.get("impressions"))
            for row in daily
        )
        totals["position"] = weighted_position / impressions
    return totals


def _sum_metric(rows: Sequence[Mapping[str, str | float]], metric: str) -> float:
    return sum(_numeric(row.get(metric)) for row in rows)


def _numeric(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataSourceError("source returned a non-numeric metric")
    return float(value)


def _comparison_range(current: DateRange, kind: str) -> DateRange:
    if kind == "previous-period":
        return previous_period(current)
    if kind == "previous-4-weeks":
        try:
            end = current.start - timedelta(days=1)
            return DateRange(start=end - timedelta(days=27), end=end)
        except OverflowError as error:
            raise DateRangeError("no available previous period for this date range") from error
    raise CLIInputError("unsupported comparison")


def _parse_cli_date_range(start: str, end: str) -> DateRange:
    if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start, end)):
        raise CLIInputError("--start and --end must use YYYY-MM-DD")
    return parse_date_range(start, end)


def _base_result(command: str, site: SiteConfig, dataset: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok" if dataset["complete"] else "partial",
        "command": command,
        "site": site.site_key,
        "display_name": site.display_name,
        "date_range": _range_json(dataset["date_range"]),
        "timezone": site.timezone,
        "date_range_interpretation": _date_range_interpretation(site),
        "freshness": dataset["freshness"],
        "complete": dataset["complete"],
        "sources": dataset["audit"]["sources"],
        "totals": dataset["totals"],
    }


def _range_json(date_range: DateRange) -> dict[str, str]:
    return {"start": date_range.start.isoformat(), "end": date_range.end.isoformat()}


def _date_range_interpretation(site: SiteConfig) -> str:
    return f"Date range is interpreted in {site.timezone}."


def _persist_dataset(
    args: argparse.Namespace,
    site: SiteConfig,
    current: Mapping[str, Any],
    *,
    command: str,
    previous: Mapping[str, Any] | None = None,
    output_path: Path | None = None,
) -> None:
    _write_cache(args.cache_dir, site.site_key, current)
    if previous is not None:
        _write_cache(args.cache_dir, site.site_key, previous)
    audit_sources = dict(current["audit"]["sources"])
    if previous is not None:
        for source, status in previous["audit"]["sources"].items():
            audit_sources[f"{source}_previous"] = status
    request = {
        "command": command,
        "site": site.site_key,
        "date_range": _range_json(current["date_range"]),
        "timezone": site.timezone,
        "date_range_interpretation": _date_range_interpretation(site),
    }
    if previous is not None:
        request["previous_date_range"] = _range_json(previous["date_range"])
    audit_path = args.audit_dir / f"{_safe_audit_name(site.site_key, current['date_range'])}.json"
    write_audit_manifest(
        audit_path,
        request,
        {"generated_at": current["freshness"], "sources": audit_sources},
        output_path,
    )


def _write_cache(root: Path, site_key: str, dataset: Mapping[str, Any]) -> None:
    date_range = _range_json(dataset["date_range"])
    for source, detail_names in {
        "ga4": ("GA4 Daily", "GA4 Pages"),
        "gsc": ("GSC Daily", "GSC Pages", "GSC Queries"),
    }.items():
        status = dataset["audit"]["sources"].get(source, {})
        if status.get("status") not in {"ok", "partial"}:
            continue
        rows = {name: dataset["details"].get(name, []) for name in detail_names}
        write_cached_json(root, site_key, source, {"source": source, **date_range}, rows)


def _safe_audit_name(site_key: str, date_range: DateRange) -> str:
    digest = hashlib.sha256(site_key.encode("utf-8")).hexdigest()[:12]
    return f"{digest}-{date_range.start.isoformat()}-{date_range.end.isoformat()}"


def _validate_output_path(output: Path) -> None:
    if output.suffix.casefold() != ".xlsx":
        raise CLIInputError("--output must end with .xlsx")


def _run_workbook_builder(payload: Mapping[str, Any], output: Path) -> dict[str, Any]:
    node = _find_node()
    _preflight_artifact_runtime()
    if not _BUILDER_SCRIPT.is_file():
        raise DataSourceError("Artifact Tool workbook builder is unavailable")
    render_dir = output.with_suffix("").with_name(f"{output.stem}.renders")
    try:
        with tempfile.TemporaryDirectory(prefix="website-analytics-") as temporary:
            input_path = Path(temporary) / "workbook-payload.json"
            input_path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
            completed = subprocess.run(
                [
                    node,
                    str(_BUILDER_SCRIPT),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output),
                    "--render-dir",
                    str(render_dir),
                ],
                cwd=_PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
    except OSError as error:
        raise DataSourceError("could not run the Artifact Tool workbook builder") from error
    if completed.returncode != 0:
        raise DataSourceError("Artifact Tool workbook export failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DataSourceError("Artifact Tool workbook builder returned invalid JSON") from error
    if not isinstance(result, Mapping) or result.get("outputsVerified") is not True:
        raise DataSourceError("Artifact Tool workbook outputs were not verified")
    return {
        "path": str(output),
        "render_dir": str(render_dir),
        "outputs_verified": True,
    }


def _find_node() -> str:
    configured = os.environ.get("WEBSITE_ANALYTICS_NODE")
    if configured:
        node = Path(configured)
        if node.is_file():
            return str(node)
        raise DataSourceError("WEBSITE_ANALYTICS_NODE must point to a Node.js executable")
    discovered = shutil.which("node")
    if discovered:
        return discovered
    raise DataSourceError(
        "Node.js is required for Excel export; set WEBSITE_ANALYTICS_NODE to its executable"
    )


def _preflight_artifact_runtime() -> None:
    runtime_path = _PROJECT_ROOT / "node_modules" / "@oai" / "artifact-tool"
    if not runtime_path.is_dir():
        raise DataSourceError(
            "Artifact Tool runtime is unavailable. Run scripts/setup-artifact-tool-runtime.ps1 "
            "with the explicit Node modules directory returned by the Codex dependency loader."
        )


def _freshness() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_stdout(payload: Mapping[str, Any], code: int = 0) -> int:
    sys.stdout.write(
        json.dumps(sanitize_url_values(payload), ensure_ascii=False, allow_nan=False) + "\n"
    )
    return code


def _write_stderr(error: Exception, code: int) -> int:
    sys.stderr.write(
        json.dumps({"status": "error", "code": code, "error": str(error)}, ensure_ascii=False)
        + "\n"
    )
    return code
