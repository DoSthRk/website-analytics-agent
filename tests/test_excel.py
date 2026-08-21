from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import parse_qsl, urlsplit

import pytest

from website_analytics.workbook_payload import DETAIL_SHEET_ORDER, build_workbook_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_payload_has_fixed_sheet_order_and_json_safe_typed_numbers() -> None:
    payload = build_workbook_payload(
        {
            "site": "demo",
            "display_name": "Demo Website",
            "date_range": {"start": "2026-08-03", "end": "2026-08-09"},
            "selection_timezone": "Asia/Shanghai",
            "freshness": "2026-08-10T01:00:00Z",
            "comparison": {
                "metrics": {
                    "ga4": {
                        "sessions": {"current": 10.0, "previous": 8.0, "delta": 2.0}
                    }
                }
            },
        },
        {
            "GSC Daily": [{"date": "2026-08-03", "clicks": 3.0, "ctr": 0.1}],
            "GA4 Daily": [
                {"date": "2026-08-03", "sessions": 10.0, "engagementRate": 0.5}
            ],
        },
        {"generated_at": "2026-08-10T01:00:00Z", "sources": {"ga4": {"rows": 1}}},
    )

    assert DETAIL_SHEET_ORDER == (
        "GA4 Daily",
        "GA4 Pages",
        "GSC Daily",
        "GSC Pages",
        "GSC Queries",
        "Inquiry Daily",
        "Inquiry Pages",
    )
    assert [sheet["name"] for sheet in payload["sheets"]] == [
        "README",
        "Executive Summary",
        "GA4 Daily",
        "GSC Daily",
        "Audit",
    ]
    assert payload["sheets"][2]["rows"] == [
        ["date", "sessions", "engagementRate"],
        ["2026-08-03", 10.0, 0.5],
    ]
    assert payload["sheets"][2]["detail"] is True
    semantics = [
        ["Selection timezone", "Asia/Shanghai"],
        [
            "Selection convention",
            "Local convention only for relative dates; explicit ISO dates are passed unchanged.",
        ],
        [
            "GA4 date boundary",
            "GA4 uses its property reporting timezone; verify it matches selection timezone.",
        ],
        [
            "GSC date boundary",
            "GSC uses Pacific Time (PT; UTC-7/UTC-8); daily boundaries can differ.",
        ],
    ]
    for sheet_index in (0, 1, -1):
        assert all(row in payload["sheets"][sheet_index]["rows"] for row in semantics)
    assert payload["sheets"][1]["rows"][-2:] == [
        ["Source", "Metric", "Current"],
        ["ga4", "sessions", 10.0],
    ]
    assert json.loads(json.dumps(payload)) == payload


def test_workbook_payload_redacts_sensitive_url_parameters_before_excel_export() -> None:
    payload = build_workbook_payload(
        {"site": "demo"},
        {
            "GA4 Pages": [
                {
                    "landingPagePlusQueryString": "/products?api_key=do-not-output&utm_source=partner",
                    "sessions": 1.0,
                }
            ]
        },
        {"sources": {"ga4": {"status": "ok"}}},
    )

    value = payload["sheets"][2]["rows"][1][0]
    parameters = dict(parse_qsl(urlsplit(value).query))
    assert parameters["api_key"] == "[REDACTED]"
    assert parameters["utm_source"] == "partner"


def test_workbook_payload_shows_comparison_provenance_before_previous_values() -> None:
    payload = build_workbook_payload(
        {
            "site": "demo",
            "display_name": "Demo Website",
            "date_range": {"start": "2026-08-03", "end": "2026-08-09"},
            "selection_timezone": "Asia/Shanghai",
            "freshness": "2026-08-10T01:00:00Z",
            "comparison": {
                "kind": "previous-period",
                "date_range": {"start": "2026-07-27", "end": "2026-08-02"},
                "freshness": "2026-08-03T01:00:00Z",
                "status": "partial",
                "previous_complete": False,
                "source_coverage_complete": True,
                "metric_coverage_complete": True,
                "complete": False,
                "sources": {"ga4": {"status": "ok"}, "gsc": {"status": "partial"}},
                "metrics": {
                    "ga4": {
                        "sessions": {
                            "current": 10.0,
                            "previous": 8.0,
                            "available": True,
                            "delta": 2.0,
                        }
                    }
                },
            },
        },
        {"GA4 Daily": [{"date": "2026-08-03", "sessions": 10.0}]},
        {"generated_at": "2026-08-10T01:00:00Z", "sources": {"ga4": {"status": "ok"}}},
    )

    readme_rows = payload["sheets"][0]["rows"]
    summary_rows = payload["sheets"][1]["rows"]
    assert ["Current date range", "2026-08-03 to 2026-08-09"] in readme_rows
    assert ["Comparison kind", "previous-period"] in readme_rows
    assert ["Previous date range", "2026-07-27 to 2026-08-02"] in readme_rows
    assert ["Comparison freshness", "Retrieved: 2026-08-03 01:00 UTC"] in readme_rows
    assert ["Comparison status", "Partial"] in readme_rows
    assert ["Previous source completeness", "Partial"] in readme_rows
    assert ["Metric coverage", "Complete"] in readme_rows
    assert ["Comparison kind", "previous-period"] in summary_rows
    assert ["Previous date range", "2026-07-27 to 2026-08-02"] in summary_rows
    assert summary_rows.index(["Previous date range", "2026-07-27 to 2026-08-02"]) < summary_rows.index(
        ["Source", "Metric", "Current", "Previous", "Delta"]
    )


def test_workbook_payload_adds_product_summary_and_mapping_when_configured() -> None:
    page_mapping = {
        "canonicalPath": "/i/gmp-generic-column",
        "productLineId": "GMP",
        "reportLineId": "GMP",
        "pageClass": "product_page",
        "includeInProductReport": True,
        "mappingRuleId": "gmp-prefix",
        "mappingStatus": "approved",
        "mappingReason": "Approved generic GMP rule.",
        "ga4Sessions": 7.0,
        "gscClicks": 5.0,
        "gscImpressions": 50.0,
        "gscCtr": 0.1,
    }
    product_report = {
        "mappingVersion": "2",
        **_page_classification_report([page_mapping]),
        "reportLines": [
            {
                "reportLineId": "GMP",
                "reportLine": "GMP 系列",
                "currentCanonicalPages": 1,
                "ga4SessionsCurrent": 7.0,
                "ga4SessionsPrevious": 2.0,
                "ga4SessionsDelta": 5.0,
                "gscClicksCurrent": 5.0,
                "gscClicksPrevious": 1.0,
                "gscClicksDelta": 4.0,
                "gscImpressionsCurrent": 50.0,
                "gscImpressionsPrevious": 10.0,
                "gscImpressionsDelta": 40.0,
                "gscCtrCurrent": 0.1,
                "gscCtrPrevious": 0.1,
                "gscCtrDelta": 0.0,
            }
        ],
    }
    payload = build_workbook_payload(
        {
            "site": "genemedi-net",
            "display_name": "GeneMedi.net",
            "date_range": {"start": "2026-08-03", "end": "2026-08-09"},
            "selection_timezone": "America/Los_Angeles",
            "freshness": "2026-08-10T01:00:00Z",
        },
        {"GA4 Pages": [{"landingPagePlusQueryString": "/i/gmp-generic-column", "sessions": 7.0}]},
        {"generated_at": "2026-08-10T01:00:00Z", "sources": {"ga4": {"rows": 1}}},
        product_report=product_report,
    )

    assert [sheet["name"] for sheet in payload["sheets"]] == [
        "README",
        "Executive Summary",
        "Product Weekly Summary",
        "Page Type Summary",
        "Page Classification",
        "Product Page Mapping",
        "GA4 Pages",
        "Audit",
    ]
    summary_rows = payload["sheets"][2]["rows"]
    assert ["Mapping version", "2"] in summary_rows
    assert summary_rows[-1][0:3] == ["GMP", "GMP 系列", 1]
    mapping_rows = payload["sheets"][5]["rows"]
    assert mapping_rows[0][0:4] == ["canonicalPath", "productLineId", "reportLineId", "pageClass"]
    assert mapping_rows[1][0:4] == ["/i/gmp-generic-column", "GMP", "GMP", "product_page"]


def test_product_mapping_sheet_is_exportable_when_period_has_no_matched_pages() -> None:
    product_report = {
        "mappingVersion": "2",
        **_page_classification_report([]),
        "reportLines": [],
    }
    payload = build_workbook_payload(
        {
            "site": "genemedi-net",
            "date_range": {"start": "2026-08-03", "end": "2026-08-09"},
            "selection_timezone": "America/Los_Angeles",
        },
        {},
        {"sources": {}},
        product_report=product_report,
    )

    readme_rows = payload["sheets"][0]["rows"]
    mapping_rows = payload["sheets"][5]["rows"]
    assert ["Product mapping version", "2"] in readme_rows
    assert mapping_rows == [
        ["status"],
        [
            "No page-level GA4, GSC, or inquiry records in this export period matched the approved product rules."
        ],
    ]


def test_workbook_payload_adds_separate_product_inquiry_summary() -> None:
    payload = build_workbook_payload(
        {
            "site": "genemedi-net",
            "date_range": {"start": "2026-08-03", "end": "2026-08-09"},
            "selection_timezone": "America/Los_Angeles",
        },
        {},
        {"sources": {}},
        product_report={
            "mappingVersion": "2",
            **_page_classification_report([]),
            "reportLines": [],
            "inquiryReportLines": [
                {
                    "reportLineId": "GMP",
                    "reportLine": "GMP 绯诲垪",
                    "currentInquiryPages": 1,
                    "storedSubmissionsCurrent": 3.0,
                    "storedSubmissionsPrevious": 1.0,
                    "storedSubmissionsDelta": 2.0,
                    "quarantinedSubmissionsCurrent": 1.0,
                    "quarantinedSubmissionsPrevious": 0.0,
                    "quarantinedSubmissionsDelta": 1.0,
                    "nonQuarantinedSubmissionsCurrent": 2.0,
                    "nonQuarantinedSubmissionsPrevious": 1.0,
                    "nonQuarantinedSubmissionsDelta": 1.0,
                }
            ],
        },
    )

    inquiry_sheet = next(
        sheet for sheet in payload["sheets"] if sheet["name"] == "Product Inquiry Summary"
    )
    assert inquiry_sheet["kind"] == "product_inquiry_summary"
    assert inquiry_sheet["rows"][-1][0:4] == ["GMP", "GMP 绯诲垪", 1, 3.0]


def _page_classification_report(page_mappings: list[dict]) -> dict:
    return {
        "pageClassificationVersion": "1",
        "pageTypeLines": [],
        "classificationCoverage": {
            "ga4ClassifiedRate": 1.0,
            "gscClassifiedRate": 1.0,
            "inquiryClassifiedRate": 1.0,
        },
        "pageMappings": page_mappings,
        "productPageMappings": page_mappings,
    }


def test_exported_workbook_keeps_a_self_describing_readme(
    tmp_path: Path,
) -> None:
    payload = build_workbook_payload(
        {
            "site": "demo",
            "display_name": "Demo Website",
            "date_range": {"start": "2026-08-03", "end": "2026-08-09"},
            "selection_timezone": "Asia/Shanghai",
            "freshness": "2026-08-10T01:00:00Z",
            "comparison": {
                "kind": "previous-period",
                "date_range": {"start": "2026-07-27", "end": "2026-08-02"},
                "freshness": "2026-08-03T01:00:00Z",
                "status": "ok",
                "previous_complete": True,
                "source_coverage_complete": True,
                "metric_coverage_complete": True,
                "complete": True,
                "sources": {"ga4": {"status": "ok"}, "gsc": {"status": "ok"}},
                "metrics": {
                    "ga4": {
                        "sessions": {
                            "current": 1.0,
                            "previous": 0.0,
                            "available": True,
                            "delta": 1.0,
                        }
                    }
                },
            },
        },
        {
            "GA4 Daily": [{"date": "2026-08-03", "sessions": 1.0}],
            "GSC Daily": [{"date": "2026-08-03", "clicks": 1.0}],
        },
        {
            "generated_at": "2026-08-10T01:00:00Z",
            "sources": {"ga4": {"status": "ok"}, "gsc": {"status": "ok"}},
        },
    )
    readme_rows = payload["sheets"][0]["rows"]
    expected_rows = [
        ["Metric semantics"],
        ["GA4 sessions", "Visits/session starts tracked by GA4."],
        [
            "GA4 users",
            "Users are unique within the selected interval; interval aggregates are not daily sums.",
        ],
        ["GA4 key events", "Configured GA4 key-event count."],
        ["GSC clicks", "Google Search result clicks."],
        ["GSC impressions", "Google Search result impressions."],
        ["GSC CTR", "Clicks divided by impressions."],
        ["GSC position", "Impression-weighted average search position."],
        ["Limitations"],
        [
            "GA4 vs GSC",
            "GA4 sessions are not GSC clicks; the platforms measure different actions.",
        ],
        [
            "GA4 user aggregation",
            "Users are unique within an interval; do not add daily user values.",
        ],
        [
            "GSC detail scope",
            "GSC page and query rows can be bounded or capped; partial reports are not exhaustive.",
        ],
    ]
    assert all(row in readme_rows for row in expected_rows)

    input_path = tmp_path / "payload.json"
    output_path = tmp_path / "report.xlsx"
    render_dir = tmp_path / "renders"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    _build_fixture_workbook(input_path, output_path, render_dir)

    with zipfile.ZipFile(output_path) as archive:
        workbook_xml = b"\n".join(
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("xl/") and name.endswith(".xml")
        ).decode("utf-8")
    for expected in (
        "GA4 sessions",
        "GA4 users",
        "GA4 key events",
        "GSC clicks",
        "GSC impressions",
        "GSC CTR",
        "GSC position",
        "GA4 sessions are not GSC clicks",
        "GSC page and query rows can be bounded or capped",
        "Comparison kind",
        "previous-period",
        "Previous date range",
        "2026-07-27 to 2026-08-02",
        "Comparison freshness",
        "Comparison status",
    ):
        assert expected in workbook_xml


def test_artifact_tool_builder_exports_a_valid_xlsx_and_renders_every_sheet(
    tmp_path: Path,
) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    output_path = tmp_path / "fixture.xlsx"
    render_dir = tmp_path / "rendered"

    completed = subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "scripts/build_report_workbook.mjs",
            "--input",
            os.fspath(fixture_path),
            "--output",
            os.fspath(output_path),
            "--render-dir",
            os.fspath(render_dir),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["outputsVerified"] is True
    assert result["workerExitCode"] == 3221226505
    assert "renderer worker exited" in completed.stderr
    assert zipfile.is_zipfile(output_path)
    with zipfile.ZipFile(output_path) as archive:
        assert "xl/workbook.xml" in archive.namelist()
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        audit_sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet8.xml"))
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    audit_rows = next(sheet["rows"] for sheet in fixture["sheets"] if sheet["name"] == "Audit")
    audit_header_row = audit_rows.index(["Source", "Status", "Rows", "Freshness"]) + 1
    assert [sheet.attrib["name"] for sheet in workbook.findall("{*}sheets/{*}sheet")] == [
        "README",
        "Executive Summary",
        "GA4 Daily",
        "GA4 Pages",
        "GSC Daily",
        "GSC Pages",
        "GSC Queries",
        "Audit",
    ]
    audit_header_styles = [
        cell.attrib.get("s")
        for cell in audit_sheet.findall(f"{{*}}sheetData/{{*}}row[@r='{audit_header_row}']/{{*}}c")
    ]
    assert len(audit_header_styles) == 4
    assert len(set(audit_header_styles)) == 1
    assert sorted(path.name for path in render_dir.glob("*.png")) == [
        "audit.png",
        "executive-summary.png",
        "ga4-daily.png",
        "ga4-pages.png",
        "gsc-daily.png",
        "gsc-pages.png",
        "gsc-queries.png",
        "readme.png",
    ]


def test_artifact_tool_builder_previews_a_large_detail_sheet_without_rendering_all_rows(
    tmp_path: Path,
) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    query_sheet = next(sheet for sheet in payload["sheets"] if sheet["name"] == "GSC Queries")
    query_sheet["rows"].extend(
        [f"long-tail query {index}", index, index * 10, 0.1, 12.5]
        for index in range(1, 3_001)
    )
    input_path = tmp_path / "large-payload.json"
    output_path = tmp_path / "large-report.xlsx"
    render_dir = tmp_path / "large-rendered"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "scripts/build_report_workbook.mjs",
            "--input",
            os.fspath(input_path),
            "--output",
            os.fspath(output_path),
            "--render-dir",
            os.fspath(render_dir),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert zipfile.is_zipfile(output_path)
    assert (render_dir / "gsc-queries.png").is_file()


def test_verifier_rejects_a_pk_prefixed_file_that_is_not_a_zip(tmp_path: Path) -> None:
    invalid_xlsx = tmp_path / "not-a-workbook.xlsx"
    invalid_xlsx.write_bytes(b"PK\x03\x04not a real ZIP archive")
    rendered_sheet = tmp_path / "readme.png"
    rendered_sheet.write_bytes(b"not-empty-png-placeholder")

    verification = subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "--input-type=module",
            "--eval",
            (
                "import { verifyGeneratedArtifacts } from "
                "'./scripts/build_report_workbook.mjs'; "
                "const verified = await verifyGeneratedArtifacts(process.argv[1], "
                "[process.argv[2]]); process.stdout.write(String(verified));"
            ),
            os.fspath(invalid_xlsx),
            os.fspath(rendered_sheet),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert verification.stdout == "false"


def test_supervisor_rejects_forced_worker_failure_despite_stale_valid_outputs(
    tmp_path: Path,
) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    output_path = tmp_path / "stale.xlsx"
    render_dir = tmp_path / "stale-rendered"
    subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "scripts/build_report_workbook.mjs",
            "--input",
            os.fspath(fixture_path),
            "--output",
            os.fspath(output_path),
            "--render-dir",
            os.fspath(render_dir),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_pngs = sorted(render_dir.glob("*.png"))
    stale_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [output_path, *expected_pngs]
    }

    forced_failure = subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "--input-type=module",
            "--eval",
            (
                "import fs from 'node:fs/promises'; import path from 'node:path'; "
                "import { superviseRenderer } from './scripts/build_report_workbook.mjs'; "
                "const [input, output, render] = process.argv.slice(1); "
                "try { await superviseRenderer({input, output, renderDir: render}, { "
                "workerRunner: async (staged) => { "
                "await fs.copyFile(output, staged.output); "
                "await fs.mkdir(staged.renderDir, {recursive: true}); "
                "for (const entry of await fs.readdir(render)) { "
                "await fs.copyFile(path.join(render, entry), path.join(staged.renderDir, entry)); "
                "} return {exitCode: 1, signal: null, stdout: 'forced stdout', "
                "stderr: 'forced stderr'}; } }); process.exit(0); "
                "} catch (error) { console.error(error.message); process.exit(7); }"
            ),
            os.fspath(fixture_path),
            os.fspath(output_path),
            os.fspath(render_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert forced_failure.returncode == 7
    assert "exit 1" in forced_failure.stderr
    assert "worker stdout: forced stdout" in forced_failure.stderr
    assert "worker stderr: forced stderr" in forced_failure.stderr
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [output_path, *expected_pngs]
    } == stale_hashes


def test_supervisor_rejects_existing_directory_as_output_without_modifying_it(
    tmp_path: Path,
) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    output_directory = tmp_path / "not-an-xlsx"
    output_directory.mkdir()
    sentinel = output_directory / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    render_dir = tmp_path / "renders"

    rejected = _run_supervisor_with_noop_worker(
        fixture_path, output_directory, render_dir
    )

    assert rejected.returncode == 7
    assert "--output must be an absent or non-symlink regular .xlsx file" in rejected.stderr
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not render_dir.exists()


def test_supervisor_rejects_existing_file_as_render_dir_without_modifying_it(
    tmp_path: Path,
) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    output_path = tmp_path / "report.xlsx"
    render_file = tmp_path / "not-a-directory"
    render_file.write_text("unchanged", encoding="utf-8")

    rejected = _run_supervisor_with_noop_worker(
        fixture_path, output_path, render_file
    )

    assert rejected.returncode == 7
    assert "--render-dir must be an absent or non-symlink directory" in rejected.stderr
    assert render_file.read_text(encoding="utf-8") == "unchanged"
    assert not output_path.exists()


def test_promotion_failure_rolls_back_xlsx_and_all_expected_pngs(tmp_path: Path) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    output_path = tmp_path / "report.xlsx"
    render_dir = tmp_path / "renders"
    _build_fixture_workbook(fixture_path, output_path, render_dir)

    alternative_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    alternative_payload["sheets"][0]["rows"][0][0] = "Alternative report"
    alternative_payload_path = tmp_path / "alternative-payload.json"
    alternative_payload_path.write_text(
        json.dumps(alternative_payload), encoding="utf-8"
    )
    alternative_output = tmp_path / "alternative.xlsx"
    alternative_render_dir = tmp_path / "alternative-renders"
    _build_fixture_workbook(
        alternative_payload_path, alternative_output, alternative_render_dir
    )
    original_hashes = _hash_artifacts(output_path, render_dir)

    rejected = subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "--input-type=module",
            "--eval",
            (
                "import fs from 'node:fs/promises'; import path from 'node:path'; "
                "import { superviseRenderer } from './scripts/build_report_workbook.mjs'; "
                "const [input, output, render, alternateOutput, alternateRender] = process.argv.slice(1); "
                "try { await superviseRenderer({input, output, renderDir: render}, { "
                "workerRunner: async (staged) => { "
                "await fs.copyFile(alternateOutput, staged.output); "
                "await fs.mkdir(staged.renderDir, {recursive: true}); "
                "for (const entry of await fs.readdir(alternateRender)) { "
                "await fs.copyFile(path.join(alternateRender, entry), path.join(staged.renderDir, entry)); "
                "} return {exitCode: 3221226505, signal: null, stdout: '', stderr: ''}; }, "
                "move: async (source, destination) => { "
                "if (source.includes('.staging-') && source.endsWith('readme.png')) { "
                "throw new Error('forced second promotion failure'); } "
                "await fs.rename(source, destination); } }); process.exit(0); "
                "} catch (error) { console.error(error.message); process.exit(7); }"
            ),
            os.fspath(fixture_path),
            os.fspath(output_path),
            os.fspath(render_dir),
            os.fspath(alternative_output),
            os.fspath(alternative_render_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode == 7
    assert "forced second promotion failure" in rejected.stderr
    assert _hash_artifacts(output_path, render_dir) == original_hashes


def test_failed_restore_preserves_the_unrestored_backup_for_recovery(
    tmp_path: Path,
) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    output_path = tmp_path / "report.xlsx"
    render_dir = tmp_path / "renders"
    _build_fixture_workbook(fixture_path, output_path, render_dir)
    original_hashes = _hash_artifacts(output_path, render_dir)

    alternative_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    alternative_payload["sheets"][0]["rows"][0][0] = "Alternative report"
    alternative_payload_path = tmp_path / "alternative-payload.json"
    alternative_payload_path.write_text(
        json.dumps(alternative_payload), encoding="utf-8"
    )
    alternative_output = tmp_path / "alternative.xlsx"
    alternative_render_dir = tmp_path / "alternative-renders"
    _build_fixture_workbook(
        alternative_payload_path, alternative_output, alternative_render_dir
    )

    rejected = subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "--input-type=module",
            "--eval",
            (
                "import fs from 'node:fs/promises'; import path from 'node:path'; "
                "import { superviseRenderer } from './scripts/build_report_workbook.mjs'; "
                "const [input, output, render, alternateOutput, alternateRender] = process.argv.slice(1); "
                "try { await superviseRenderer({input, output, renderDir: render}, { "
                "workerRunner: async (staged) => { "
                "await fs.copyFile(alternateOutput, staged.output); "
                "await fs.mkdir(staged.renderDir, {recursive: true}); "
                "for (const entry of await fs.readdir(alternateRender)) { "
                "await fs.copyFile(path.join(alternateRender, entry), path.join(staged.renderDir, entry)); "
                "} return {exitCode: 3221226505, signal: null, stdout: '', stderr: ''}; }, "
                "move: async (source, destination) => { "
                "if (source.includes('.staging-') && source.endsWith('readme.png')) { "
                "throw new Error('forced forward failure'); } "
                "if (source.includes('.readme.png.previous-') && destination.endsWith('readme.png')) { "
                "throw new Error('forced restore failure'); } "
                "await fs.rename(source, destination); } }); process.exit(0); "
                "} catch (error) { console.error(error.message); process.exit(7); }"
            ),
            os.fspath(fixture_path),
            os.fspath(output_path),
            os.fspath(render_dir),
            os.fspath(alternative_output),
            os.fspath(alternative_render_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    backup_directories = list(render_dir.glob(".readme.png.previous-*"))
    assert rejected.returncode == 7
    assert len(backup_directories) == 1
    recovery_path = backup_directories[0] / "readme.png"
    assert hashlib.sha256(recovery_path.read_bytes()).hexdigest() == original_hashes[
        "readme.png"
    ]
    assert hashlib.sha256(output_path.read_bytes()).hexdigest() == original_hashes[
        "report.xlsx"
    ]
    assert not (render_dir / "readme.png").exists()
    assert str(recovery_path) in rejected.stderr
    assert "forced restore failure" in rejected.stderr


def _run_supervisor_with_noop_worker(
    input_path: Path, output_path: Path, render_path: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "--input-type=module",
            "--eval",
            (
                "import { superviseRenderer } from './scripts/build_report_workbook.mjs'; "
                "const [input, output, render] = process.argv.slice(1); "
                "try { await superviseRenderer({input, output, renderDir: render}, { "
                "workerRunner: async () => { throw new Error('worker should not run'); } }); "
                "process.exit(0); } catch (error) { console.error(error.message); process.exit(7); }"
            ),
            os.fspath(input_path),
            os.fspath(output_path),
            os.fspath(render_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def _build_fixture_workbook(input_path: Path, output_path: Path, render_dir: Path) -> None:
    subprocess.run(
        [
            os.fspath(_renderer_node_or_skip()),
            "scripts/build_report_workbook.mjs",
            "--input",
            os.fspath(input_path),
            "--output",
            os.fspath(output_path),
            "--render-dir",
            os.fspath(render_dir),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _hash_artifacts(output_path: Path, render_dir: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [output_path, *sorted(render_dir.glob("*.png"))]
    }
