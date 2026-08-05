from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from website_analytics import cli
from website_analytics.cli import build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
NODE = Path(
    r"C:\Users\dosth\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)


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
        "offline": True,
    }


def test_fixture_report_and_excel_export_are_end_to_end_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _copy_config(tmp_path)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    output = tmp_path / "demo.xlsx"
    monkeypatch.setenv("WEBSITE_ANALYTICS_NODE", str(NODE))
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


def _copy_config(tmp_path: Path) -> Path:
    destination = tmp_path / "sites.yaml"
    shutil.copy(PROJECT_ROOT / "config" / "sites.example.yaml", destination)
    return destination
