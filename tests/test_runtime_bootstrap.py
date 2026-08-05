from __future__ import annotations

from pathlib import Path

import pytest

from website_analytics import cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_script_requires_explicit_artifact_tool_modules_and_refuses_overwrite() -> None:
    script = PROJECT_ROOT / "scripts" / "setup-artifact-tool-runtime.ps1"

    text = script.read_text(encoding="utf-8")

    assert "NodeModulesPath" in text
    assert "@oai" in text and "artifact-tool" in text
    assert "New-Item -ItemType Junction" in text
    assert "Refusing to overwrite" in text
    assert "npm " not in text.casefold()


def test_excel_preflight_explains_how_to_restore_missing_local_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_PROJECT_ROOT", tmp_path)

    with pytest.raises(cli.DataSourceError, match="setup-artifact-tool-runtime.ps1"):
        cli._preflight_artifact_runtime()


def test_excel_builder_stops_before_writing_when_local_runtime_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_find_node", lambda: "unused-node")
    output = tmp_path / "report.xlsx"

    with pytest.raises(cli.DataSourceError, match="setup-artifact-tool-runtime.ps1"):
        cli._run_workbook_builder({"sheets": []}, output)

    assert not output.exists()


def test_readme_documents_codex_loader_node_and_bootstrap_steps() -> None:
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "load workspace dependencies" in text
    assert "WEBSITE_ANALYTICS_NODE" in text
    assert "setup-artifact-tool-runtime.ps1" in text
