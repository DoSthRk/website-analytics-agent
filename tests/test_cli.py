from __future__ import annotations

import json
import os
import shutil
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

from website_analytics import cli
from website_analytics.cli import build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _renderer_node_or_skip() -> str:
    configured = os.environ.get("WEBSITE_ANALYTICS_NODE")
    node = configured or shutil.which("node")
    if not node or not Path(node).is_file():
        pytest.skip(
            "Renderer integration requires WEBSITE_ANALYTICS_NODE or node on PATH. "
            "Run the documented Excel runtime bootstrap first."
        )
    if not (PROJECT_ROOT / "node_modules" / "@oai" / "artifact-tool").is_dir():
        pytest.skip(
            "Renderer integration requires local @oai/artifact-tool. "
            "Run scripts/setup-artifact-tool-runtime.ps1 with the Codex dependency loader output."
        )
    return node


def test_validate_config_parser_accepts_registered_site() -> None:
    args = build_parser().parse_args(["validate-config", "--site", "demo"])

    assert args.command == "validate-config"
    assert args.site == "demo"


def test_report_defaults_to_previous_period_comparison() -> None:
    args = build_parser().parse_args(
        [
            "report",
            "--site",
            "demo",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
        ]
    )

    assert args.compare == "previous-period"


def test_validate_config_is_fully_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)

    def unexpected_google_client_creation() -> object:
        raise AssertionError("validate-config must not create Google clients")

    monkeypatch.setattr(cli, "_create_live_adapters", unexpected_google_client_creation)

    code = cli.main(["validate-config", "--site", "demo", "--config", str(config)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "status": "ok",
        "command": "validate-config",
        "site": "demo",
        "config": str(config),
        "selection_timezone": "Asia/Shanghai",
        "offline": True,
    }


def test_report_and_audit_state_the_site_timezone_for_each_date_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    monkeypatch.setattr(
        cli,
        "_create_live_adapters",
        lambda: (_ for _ in ()).throw(AssertionError("fixture mode used Google clients")),
    )

    code = cli.main(
        [
            "report",
            "--site",
            "demo",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            "--fixture-dir",
            str(FIXTURES),
            "--cache-dir",
            str(cache_dir),
            "--audit-dir",
            str(audit_dir),
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    audit = json.loads(next(audit_dir.glob("*.json")).read_text(encoding="utf-8"))
    interpretation = {
        "selection": (
            "Asia/Shanghai is a local convention for relative date requests; "
            "explicit ISO dates are passed unchanged."
        ),
        "ga4": (
            "GA4 uses its property reporting timezone; verify it matches "
            "configured selection_timezone (Asia/Shanghai)."
        ),
        "gsc": (
            "GSC startDate and endDate use Pacific Time (PT; UTC-7/UTC-8); "
            "daily boundaries can differ from selection_timezone."
        ),
    }
    assert code == 0
    assert result["selection_timezone"] == "Asia/Shanghai"
    assert "timezone" not in result
    assert result["date_range_interpretation"] == interpretation
    assert result["comparison"]["selection_timezone"] == "Asia/Shanghai"
    assert "timezone" not in result["comparison"]
    assert result["comparison"]["date_range_interpretation"] == interpretation
    assert audit["request"]["selection_timezone"] == "Asia/Shanghai"
    assert "timezone" not in audit["request"]
    assert audit["request"]["date_range_interpretation"] == interpretation


def test_fixture_report_and_excel_export_are_end_to_end_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    output = tmp_path / "demo.xlsx"
    monkeypatch.setenv("WEBSITE_ANALYTICS_NODE", _renderer_node_or_skip())
    monkeypatch.setattr(
        cli,
        "_create_live_adapters",
        lambda: (_ for _ in ()).throw(AssertionError("fixture mode used Google clients")),
    )
    common = [
        "--site",
        "demo",
        "--start",
        "2026-08-03",
        "--end",
        "2026-08-09",
        "--config",
        str(config),
        "--fixture-dir",
        str(FIXTURES),
        "--cache-dir",
        str(cache_dir),
        "--audit-dir",
        str(audit_dir),
    ]

    report_code = cli.main(["report", *common])

    report_capture = capsys.readouterr()
    report = json.loads(report_capture.out)
    assert report_code == 0
    assert report_capture.err == ""
    assert report["complete"] is True
    assert report["comparison"]["kind"] == "previous-period"
    assert report["comparison"]["date_range"] == {
        "start": "2026-07-27",
        "end": "2026-08-02",
    }
    assert report["comparison"]["previous_complete"] is True
    assert report["comparison"]["freshness"]
    assert report["totals"]["ga4"]["sessions"] == 10.0
    assert report["totals"]["gsc"]["clicks"] == 10.0
    assert len(list(cache_dir.rglob("*.json"))) == 4
    assert len(list(audit_dir.glob("*.json"))) == 1

    export_code = cli.main(["export-excel", *common, "--output", str(output)])

    export_capture = capsys.readouterr()
    exported = json.loads(export_capture.out)
    assert export_code == 0
    assert export_capture.err == ""
    assert exported["export"]["outputs_verified"] is True
    assert zipfile.is_zipfile(output)
    with zipfile.ZipFile(output) as archive:
        workbook_xml = b"\n".join(
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("xl/") and name.endswith(".xml")
        ).decode("utf-8")
    for expected in (
        "Comparison kind",
        "previous-period",
        "Previous date range",
        "2026-07-27 to 2026-08-02",
        "Comparison freshness",
        "Comparison status",
    ):
        assert expected in workbook_xml
    assert sorted(path.name for path in output.with_suffix("").with_name("demo.renders").glob("*.png")) == [
        "audit.png",
        "executive-summary.png",
        "ga4-daily.png",
        "ga4-pages.png",
        "gsc-daily.png",
        "gsc-pages.png",
        "gsc-queries.png",
        "readme.png",
    ]


def test_fixture_export_automatically_adds_genemedi_product_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "sites.yaml"
    config.write_text(
        """
sites:
  genemedi-net:
    display_name: GeneMedi.net
    domains:
      - genemedi.net
    timezone: America/Los_Angeles
    ga4_property_id: "123456789"
    gsc_property_url: sc-domain:genemedi.net
    key_events:
      - inquiry
""".lstrip(),
        encoding="utf-8",
    )
    output = tmp_path / "genemedi.xlsx"
    monkeypatch.setenv("WEBSITE_ANALYTICS_NODE", _renderer_node_or_skip())
    monkeypatch.setattr(
        cli,
        "_create_live_adapters",
        lambda: (_ for _ in ()).throw(AssertionError("fixture mode used Google clients")),
    )

    code = cli.main(
        [
            "export-excel",
            "--site",
            "genemedi-net",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            "--fixture-dir",
            str(FIXTURES),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--audit-dir",
            str(tmp_path / "audits"),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    exported = json.loads(captured.out)
    assert code == 0
    assert exported["product_mapping"] == {
        "status": "configured",
        "version": "2",
        "report_lines": ["GMP", "SOLIDEX", "AAV_PROCESSING"],
    }
    assert exported["page_classification"]["status"] == "configured"
    assert exported["page_classification"]["version"] == "3"
    assert exported["page_classification"]["dimension"]["productPages"] == 2
    assert zipfile.is_zipfile(output)
    assert sorted(path.name for path in output.with_suffix("").with_name("genemedi.renders").glob("*.png")) == [
        "audit.png",
        "executive-summary.png",
        "ga4-daily.png",
        "ga4-pages.png",
        "gsc-daily.png",
        "gsc-pages.png",
        "gsc-queries.png",
        "page-classification.png",
        "page-type-summary.png",
        "product-page-mapping.png",
        "product-weekly-summary.png",
        "readme.png",
    ]


def test_live_adapter_factory_returns_both_approved_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.auth
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from googleapiclient import discovery

    credentials = object()
    ga4_client = object()
    gsc_service = object()
    observed: dict[str, object] = {}

    def fake_default(*, scopes: tuple[str, ...]) -> tuple[object, None]:
        observed["scopes"] = scopes
        return credentials, None

    def fake_ga4_client(*, credentials: object) -> object:
        observed["ga4_credentials"] = credentials
        return ga4_client

    def fake_build(
        service_name: str,
        version: str,
        *,
        credentials: object,
        cache_discovery: bool,
    ) -> object:
        observed["gsc_build"] = (
            service_name,
            version,
            credentials,
            cache_discovery,
        )
        return gsc_service

    monkeypatch.setattr(google.auth, "default", fake_default)
    monkeypatch.setattr(BetaAnalyticsDataClient, "__new__", lambda cls, **kwargs: fake_ga4_client(**kwargs))
    monkeypatch.setattr(discovery, "build", fake_build)

    ga4, gsc = cli._create_live_adapters()

    assert ga4._client is ga4_client
    assert gsc._service is gsc_service
    assert observed == {
        "scopes": (
            "https://www.googleapis.com/auth/analytics.readonly",
            "https://www.googleapis.com/auth/webmasters.readonly",
        ),
        "ga4_credentials": credentials,
        "gsc_build": ("searchconsole", "v1", credentials, False),
    }


def test_fixture_export_adds_source_separated_database_inquiry_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "sites.yaml"
    config.write_text(
        """
sites:
  genemedi-net:
    display_name: GeneMedi.net
    domains: [genemedi.net]
    timezone: America/Los_Angeles
    ga4_property_id: "123456789"
    gsc_property_url: sc-domain:genemedi.net
    key_events: [inquiry]
    inquiry_source:
      kind: legacy_contacts_mysql
      credential_env: WEBSITE_ANALYTICS_GENEMEDI_NET_INQUIRY_DSN
""".lstrip(),
        encoding="utf-8",
    )
    output = tmp_path / "genemedi-with-inquiry.xlsx"
    monkeypatch.setenv("WEBSITE_ANALYTICS_NODE", _renderer_node_or_skip())
    monkeypatch.setattr(
        cli,
        "_create_live_adapters",
        lambda: (_ for _ in ()).throw(AssertionError("fixture mode used Google clients")),
    )

    code = cli.main(
        [
            "export-excel",
            "--site",
            "genemedi-net",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            "--fixture-dir",
            str(FIXTURES),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--audit-dir",
            str(tmp_path / "audits"),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    exported = json.loads(captured.out)
    assert code == 0
    assert exported["totals"]["inquiry"] == {
        "storedSubmissions": 6.0,
        "quarantinedSubmissions": 1.0,
        "nonQuarantinedSubmissions": 5.0,
    }
    assert exported["sources"]["inquiry"]["status"] == "ok"
    assert "inquiry" in exported["date_range_interpretation"]
    assert zipfile.is_zipfile(output)
    with zipfile.ZipFile(output) as archive:
        workbook_xml = b"\n".join(
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("xl/") and name.endswith(".xml")
        ).decode("utf-8")
    assert "Product Inquiry Summary" in workbook_xml
    assert "nonQuarantinedSubmissions" in workbook_xml
    assert sorted(
        path.name
        for path in output.with_suffix("").with_name("genemedi-with-inquiry.renders").glob("*.png")
    ) == [
        "audit.png",
        "executive-summary.png",
        "ga4-daily.png",
        "ga4-pages.png",
        "gsc-daily.png",
        "gsc-pages.png",
        "gsc-queries.png",
        "inquiry-daily.png",
        "inquiry-pages.png",
        "page-classification.png",
        "page-type-summary.png",
        "product-inquiry-summary.png",
        "product-page-mapping.png",
        "product-weekly-summary.png",
        "readme.png",
    ]


def test_invalid_input_is_json_on_stderr_and_uses_exit_code_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)

    code = cli.main(
        [
            "fetch",
            "--site",
            "demo",
            "--start",
            "not-a-date",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == 2


def test_even_help_like_input_cannot_break_the_json_output_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["--help"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == 2


def test_data_commands_require_hyphenated_iso_dates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)

    code = cli.main(
        [
            "fetch",
            "--site",
            "demo",
            "--start",
            "20260803",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            "--fixture-dir",
            str(FIXTURES),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "YYYY-MM-DD" in json.loads(captured.err)["error"]


def test_json_stdout_sanitizes_sensitive_url_parameters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._write_stdout({"value": "/landing?signature=nonpersisted-value&utm_source=email"})

    output = json.loads(capsys.readouterr().out)["value"]
    parameters = dict(parse_qsl(urlsplit(output).query))
    assert parameters["signature"] == "[REDACTED]"
    assert parameters["utm_source"] == "email"


@pytest.mark.parametrize("forbidden_argument", ["--dimensions", "--url", "--credentials"])
def test_parser_rejects_unsupported_arbitrary_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    forbidden_argument: str,
) -> None:
    config = _copy_config(tmp_path)

    code = cli.main(
        [
            "fetch",
            "--site",
            "demo",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            forbidden_argument,
            "unapproved-value",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["status"] == "error"


def test_fixture_source_failure_is_partial_and_uses_exit_code_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)
    incomplete_fixtures = tmp_path / "fixtures"
    incomplete_fixtures.mkdir()
    shutil.copy(FIXTURES / "ga4_report.json", incomplete_fixtures / "ga4_report.json")
    shutil.copy(FIXTURES / "gsc_daily.json", incomplete_fixtures / "gsc_daily.json")

    code = cli.main(
        [
            "fetch",
            "--site",
            "demo",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            "--fixture-dir",
            str(incomplete_fixtures),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--audit-dir",
            str(tmp_path / "audits"),
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == 3
    assert captured.err == ""
    assert result["status"] == "partial"
    assert result["sources"]["ga4"]["status"] == "ok"
    assert result["sources"]["gsc"]["status"] == "error"


def test_cli_interval_totals_use_ga4_aggregate_not_summed_daily_uniques(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class IntervalGA4:
        def __init__(self) -> None:
            self.aggregate_calls = 0

        def daily(self, site: object, date_range: object) -> list[dict[str, float | str]]:
            return [
                {"date": "2026-08-03", "sessions": 10.0, "totalUsers": 8.0, "activeUsers": 7.0, "engagedSessions": 6.0, "engagementRate": 0.6, "screenPageViews": 20.0, "keyEvents": 1.0},
                {"date": "2026-08-04", "sessions": 12.0, "totalUsers": 8.0, "activeUsers": 7.0, "engagedSessions": 7.0, "engagementRate": 7 / 12, "screenPageViews": 24.0, "keyEvents": 1.0},
            ]

        def pages(self, site: object, date_range: object) -> list[dict[str, float | str]]:
            return []

        def aggregate(self, site: object, date_range: object) -> list[dict[str, float]]:
            self.aggregate_calls += 1
            return [{"sessions": 22.0, "totalUsers": 10.0, "activeUsers": 9.0, "engagedSessions": 13.0, "engagementRate": 13 / 22, "screenPageViews": 44.0, "keyEvents": 2.0}]

    class StubGSC:
        def query(self, site: object, date_range: object, dimensions: object) -> list[dict[str, float | str]]:
            return [{str(list(dimensions)[0]): "safe", "clicks": 1.0, "impressions": 10.0, "ctr": 0.1, "position": 2.0}]

    ga4 = IntervalGA4()
    monkeypatch.setattr(cli, "_fixture_adapters", lambda fixture_dir: (ga4, StubGSC()))

    dataset = cli._collect_dataset(
        cli.require_site(cli.load_sites(_copy_config(tmp_path)), "demo"),
        cli.DateRange(start=date(2026, 8, 3), end=date(2026, 8, 4)),
        tmp_path,
    )

    assert ga4.aggregate_calls == 1
    assert dataset["totals"]["ga4"]["totalUsers"] == 10.0
    assert dataset["totals"]["ga4"]["activeUsers"] == 9.0


def test_capped_gsc_page_or_query_detail_returns_partial_with_audit_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class StubGA4:
        def daily(self, site: object, date_range: object) -> list[dict[str, float | str]]:
            return []

        def pages(self, site: object, date_range: object) -> list[dict[str, float | str]]:
            return []

        def aggregate(self, site: object, date_range: object) -> list[dict[str, float]]:
            return [{"sessions": 1.0, "totalUsers": 1.0, "activeUsers": 1.0, "engagedSessions": 1.0, "engagementRate": 1.0, "screenPageViews": 1.0, "keyEvents": 1.0}]

    class CappedGSC:
        def query_result(self, site: object, date_range: object, dimensions: object) -> SimpleNamespace:
            dimension = str(list(dimensions)[0])
            return SimpleNamespace(
                rows=[{dimension: "safe", "clicks": 1.0, "impressions": 10.0, "ctr": 0.1, "position": 2.0}],
                truncated=dimension in {"page", "query"},
                row_cap=50000,
                dimensions=(dimension,),
            )

    config = _copy_config(tmp_path)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    monkeypatch.setattr(cli, "_fixture_adapters", lambda fixture_dir: (StubGA4(), CappedGSC()))

    code = cli.main(
        [
            "fetch",
            "--site",
            "demo",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            "--fixture-dir",
            str(tmp_path),
            "--cache-dir",
            str(cache_dir),
            "--audit-dir",
            str(audit_dir),
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    audit = json.loads(next(audit_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert code == 3
    assert captured.err == ""
    assert result["status"] == "partial"
    assert result["sources"]["gsc"]["status"] == "partial"
    assert result["sources"]["gsc"]["details"]["GSC Pages"]["truncated"] is True
    assert result["sources"]["gsc"]["details"]["GSC Queries"]["row_cap"] == 50000
    assert audit["source_statuses"]["sources"]["gsc"]["truncated"] is True
    assert list(cache_dir.rglob("*.json"))
    assert list((cache_dir / "demo" / "gsc").glob("*.json"))


def test_partial_export_retains_audit_context_and_returns_json_exit_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    output = tmp_path / "partial.xlsx"
    partial_dataset = {
        "date_range": cli.DateRange(start=date(2026, 8, 3), end=date(2026, 8, 9)),
        "freshness": "2026-08-10T00:00:00Z",
        "details": {"GA4 Daily": [], "GA4 Pages": [], "GSC Daily": [], "GSC Pages": [], "GSC Queries": []},
        "totals": {"ga4": {"sessions": 1.0}, "gsc": {"clicks": 1.0}},
        "complete": False,
        "audit": {
            "generated_at": "2026-08-10T00:00:00Z",
            "sources": {
                "ga4": {"status": "ok", "rows": 0},
                "gsc": {
                    "status": "partial",
                    "rows": 50000,
                    "truncated": True,
                    "details": {"GSC Pages": {"truncated": True, "row_cap": 50000, "rows": 50000}},
                },
            },
        },
    }
    monkeypatch.setattr(cli, "_collect_dataset", lambda site, date_range, fixture_dir: partial_dataset)

    code = cli.main(
        [
            "export-excel",
            "--site",
            "demo",
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-09",
            "--config",
            str(config),
            "--fixture-dir",
            str(tmp_path),
            "--cache-dir",
            str(cache_dir),
            "--audit-dir",
            str(audit_dir),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == 3
    assert captured.err == ""
    assert result["status"] == "partial"
    assert result["sources"]["gsc"]["truncated"] is True
    assert list(audit_dir.glob("*.json"))
    assert list(cache_dir.rglob("*.json"))
    assert not output.exists()


def test_previous_period_truncation_makes_report_and_export_unambiguously_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    output = tmp_path / "previous-partial.xlsx"
    current = _period_dataset(
        cli.DateRange(start=date(2026, 8, 3), end=date(2026, 8, 9)),
        gsc_status="ok",
        truncated=False,
    )
    previous = _period_dataset(
        cli.DateRange(start=date(2026, 7, 27), end=date(2026, 8, 2)),
        gsc_status="partial",
        truncated=True,
    )
    monkeypatch.setattr(
        cli,
        "_collect_dataset",
        lambda site, date_range, fixture_dir: current
        if date_range.start == date(2026, 8, 3)
        else previous,
    )
    common = [
        "--site",
        "demo",
        "--start",
        "2026-08-03",
        "--end",
        "2026-08-09",
        "--config",
        str(config),
        "--fixture-dir",
        str(tmp_path),
        "--cache-dir",
        str(cache_dir),
        "--audit-dir",
        str(audit_dir),
    ]

    report_code = cli.main(["report", *common])

    report_capture = capsys.readouterr()
    report = json.loads(report_capture.out)
    assert report_code == 3
    assert report_capture.err == ""
    assert report["status"] == "partial"
    assert report["complete"] is False
    assert report["sources"]["gsc"]["status"] == "ok"
    assert report["comparison"]["complete"] is False
    assert report["comparison"]["metric_coverage_complete"] is True
    assert report["comparison"]["sources"]["gsc"]["status"] == "partial"
    assert report["comparison"]["sources"]["gsc"]["truncated"] is True
    assert list(audit_dir.glob("*.json"))
    assert list((cache_dir / "demo" / "gsc").glob("*.json"))

    export_code = cli.main(["export-excel", *common, "--output", str(output)])

    export_capture = capsys.readouterr()
    exported = json.loads(export_capture.out)
    assert export_code == 3
    assert export_capture.err == ""
    assert exported["status"] == "partial"
    assert exported["comparison"]["sources"]["gsc"]["truncated"] is True
    assert not output.exists()


def test_zero_impression_comparison_marks_missing_gsc_ratios_unavailable_and_blocks_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    output = tmp_path / "zero-impressions.xlsx"
    current = _period_dataset(
        cli.DateRange(start=date(2026, 8, 3), end=date(2026, 8, 9)),
        gsc_status="ok",
        truncated=False,
    )
    previous = _period_dataset(
        cli.DateRange(start=date(2026, 7, 27), end=date(2026, 8, 2)),
        gsc_status="ok",
        truncated=False,
    )
    current["totals"] = {"ga4": {"sessions": 1.0}, "gsc": {"clicks": 0.0, "impressions": 0.0}}
    previous["totals"] = {
        "ga4": {"sessions": 1.0},
        "gsc": {"clicks": 4.0, "impressions": 20.0, "ctr": 0.2, "position": 5.5},
    }
    monkeypatch.setattr(
        cli,
        "_collect_dataset",
        lambda site, date_range, fixture_dir: current
        if date_range.start == date(2026, 8, 3)
        else previous,
    )
    common = [
        "--site",
        "demo",
        "--start",
        "2026-08-03",
        "--end",
        "2026-08-09",
        "--config",
        str(config),
        "--fixture-dir",
        str(tmp_path),
        "--cache-dir",
        str(cache_dir),
        "--audit-dir",
        str(audit_dir),
    ]

    report_code = cli.main(["report", *common])

    report_capture = capsys.readouterr()
    report = json.loads(report_capture.out)
    assert report_code == 3
    assert report["status"] == "partial"
    assert report["sources"]["gsc"]["status"] == "ok"
    assert report["comparison"]["metric_coverage_complete"] is False
    assert report["comparison"]["complete"] is False
    assert report["comparison"]["metrics"]["gsc"]["ctr"] == {
        "current": None,
        "previous": 0.2,
        "available": False,
        "delta": None,
    }
    assert report["comparison"]["metrics"]["gsc"]["position"] == {
        "current": None,
        "previous": 5.5,
        "available": False,
        "delta": None,
    }

    export_code = cli.main(["export-excel", *common, "--output", str(output)])

    export_capture = capsys.readouterr()
    exported = json.loads(export_capture.out)
    assert export_code == 3
    assert exported["status"] == "partial"
    assert exported["comparison"]["metric_coverage_complete"] is False
    assert not output.exists()


def _period_dataset(
    date_range: cli.DateRange, *, gsc_status: str, truncated: bool
) -> dict[str, object]:
    details = {
        "GA4 Daily": [],
        "GA4 Pages": [],
        "GSC Daily": [],
        "GSC Pages": [],
        "GSC Queries": [],
    }
    return {
        "date_range": date_range,
        "freshness": "2026-08-10T00:00:00Z",
        "details": details,
        "totals": {"ga4": {"sessions": 1.0}, "gsc": {"clicks": 1.0}},
        "complete": gsc_status == "ok",
        "audit": {
            "generated_at": "2026-08-10T00:00:00Z",
            "sources": {
                "ga4": {"status": "ok", "rows": 0},
                "gsc": {
                    "status": gsc_status,
                    "rows": 50000 if truncated else 0,
                    "truncated": truncated,
                    "details": {
                        "GSC Pages": {
                            "truncated": truncated,
                            "row_cap": 50000,
                            "rows": 50000 if truncated else 0,
                        }
                    },
                },
            },
        },
    }


def _copy_config(tmp_path: Path) -> Path:
    destination = tmp_path / "sites.yaml"
    shutil.copy(PROJECT_ROOT / "config" / "sites.example.yaml", destination)
    return destination
