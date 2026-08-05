from __future__ import annotations

import json
import hashlib
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

    completed = subprocess.run(
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

    result = json.loads(completed.stdout)
    assert result["outputsVerified"] is True
    assert result["workerExitCode"] == 3221226505
    assert "renderer worker exited" in completed.stderr
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


def test_verifier_rejects_a_pk_prefixed_file_that_is_not_a_zip(tmp_path: Path) -> None:
    invalid_xlsx = tmp_path / "not-a-workbook.xlsx"
    invalid_xlsx.write_bytes(b"PK\x03\x04not a real ZIP archive")
    rendered_sheet = tmp_path / "readme.png"
    rendered_sheet.write_bytes(b"not-empty-png-placeholder")

    verification = subprocess.run(
        [
            os.fspath(NODE),
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
    expected_pngs = sorted(render_dir.glob("*.png"))
    stale_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [output_path, *expected_pngs]
    }

    forced_failure = subprocess.run(
        [
            os.fspath(NODE),
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
            os.fspath(NODE),
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


def _run_supervisor_with_noop_worker(
    input_path: Path, output_path: Path, render_path: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            os.fspath(NODE),
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
            os.fspath(NODE),
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
