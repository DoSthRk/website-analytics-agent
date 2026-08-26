from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_runner():
    path = Path("scripts/run_analytics_sync.py")
    spec = importlib.util.spec_from_file_location("run_analytics_sync", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v3_only_requires_v3_contract_and_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_analytics_sync.py",
            "--scope",
            "intraday",
            "--cache-dir",
            "cache",
            "--audit-dir",
            "audits",
            "--output-dir",
            "outputs",
            "--v3-only",
        ],
    )

    with pytest.raises(
        ValueError,
        match="--v3-only requires --v3-contract and --v3-target",
    ):
        runner.main()


def test_production_unit_uses_explicit_proxy_and_v3_only() -> None:
    unit = Path("deploy/systemd/website-analytics-sync@.service").read_text(
        encoding="utf-8"
    )

    assert "Environment=HTTP_PROXY=http://127.0.0.1:8890" in unit
    assert "Environment=HTTPS_PROXY=http://127.0.0.1:8890" in unit
    assert "Environment=NO_PROXY=127.0.0.1,localhost" in unit
    assert "--v3-only --apply" in unit
