from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from website_analytics.workbook_payload import DETAIL_SHEET_ORDER, build_workbook_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE = Path(
    r"C:\Users\dosth\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)


def test_payload_has_fixed_sheet_order_and_json_safe_typed_numbers() -> None:
    payload = build_workbook_payload(
        {
            "site": "demo",
            "display_name": "Demo Website",
            "date_range": {"start": "2026-08-03", "end": "2026-08-09"},
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
    assert payload["sheets"][0]["rows"][3][1] == "Retrieved: 2026-08-10 01:00 UTC"
    assert payload["sheets"][1]["rows"][3][1] == "Retrieved: 2026-08-10 01:00 UTC"
    assert payload["sheets"][-1]["rows"][3][1] == "Retrieved: 2026-08-10 01:00 UTC"
    assert json.loads(json.dumps(payload)) == payload


def test_artifact_tool_builder_exports_a_valid_xlsx_and_renders_every_sheet(
    tmp_path: Path,
) -> None:
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "workbook_payload.json"
    output_path = tmp_path / "fixture.xlsx"
    render_dir = tmp_path / "rendered"

    subprocess.run(
        [
            os.fspath(NODE),
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

    assert zipfile.is_zipfile(output_path)
    with zipfile.ZipFile(output_path) as archive:
        assert "xl/workbook.xml" in archive.namelist()
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        audit_sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet8.xml"))
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
        for cell in audit_sheet.findall("{*}sheetData/{*}row[@r='3']/{*}c")
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
