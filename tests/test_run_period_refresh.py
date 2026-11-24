import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    spec = importlib.util.spec_from_file_location("run_period_refresh", "scripts/run_period_refresh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare(monkeypatch, tmp_path, apply, mismatches=()):
    module = _module()
    env = tmp_path / "env"
    env.write_text("WEBSITE_ANALYTICS_TEST_DSN=x\n", encoding="utf-8")
    site = SimpleNamespace(site_key="genemedi-net", inquiry_source=object())
    monkeypatch.setattr(module, "load_sites", lambda path: {})
    monkeypatch.setattr(module, "require_site", lambda sites, key: site)
    monkeypatch.setattr(module, "load_product_mapping", lambda *a: "mapping")
    monkeypatch.setattr(module, "load_information_mapping", lambda *a: "information")
    monkeypatch.setattr(module, "load_page_classification", lambda *a: "classification")
    monkeypatch.setattr(module, "create_page_dimension_adapter", lambda source: SimpleNamespace(query=lambda: []))
    monkeypatch.setattr(module, "build_page_dimension", lambda *a: "dimension")
    monkeypatch.setattr(module, "load_feishu_v3_target", lambda path: SimpleNamespace(record_client_target=lambda: None))
    monkeypatch.setattr(module, "load_json_object", lambda path: {"tables": {"overview_daily": "tblO"}})
    v3_rows = [("r1", {"数据日期": "2026-11-19T00:00:00.000+08:00"}), ("r2", {"数据日期": "2026-11-21T00:00:00.000+08:00"}),
               ("r3", {"数据日期": "2026-11-20T00:00:00.000+08:00"})]
    monkeypatch.setattr(module, "LarkCLIRecordClient", lambda target: SimpleNamespace(list_records=lambda table: v3_rows))
    seen = {}

    def recompute(**kwargs):
        seen["days"] = kwargs["days"]
        days = [day.isoformat() for day in kwargs["days"]]
        return {
            "completed_days": days, "skipped_days": [], "mismatches": list(mismatches),
            "records": {
                "overview_daily": [{"data_date": day} for day in days],
                "product_funnel_daily": [{"data_date": day, "product_line_id": "A"} for day in days],
            },
        }

    monkeypatch.setattr(module, "recompute_days", recompute)
    monkeypatch.setattr(module, "refresh_period_tables", lambda **kwargs: seen.setdefault("refresh", kwargs) and {"ok": True})
    argv = ["run_period_refresh.py", "--site-config", "s", "--env-file", str(env), "--mapping", "m",
            "--page-classification", "p", "--information-mapping", "i", "--cache-dir", "c", "--audit-dir", "a",
            "--v3-target", "v", "--store", str(tmp_path / "store"), "--period-target", "t", "--exclusions", "e"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    return module, seen


def test_apply_recomputes_fourteen_days_to_latest_final_and_saves_them(monkeypatch, tmp_path, capsys):
    module, seen = _prepare(monkeypatch, tmp_path, apply=True)
    assert module.main() == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["last_final"] == "2026-11-21"
    assert seen["days"][0] == date(2026, 11, 8) and seen["days"][-1] == date(2026, 11, 21) and len(seen["days"]) == 14
    assert summary["saved_days"] == 14
    assert len(list((tmp_path / "store").glob("*.json"))) == 14
    assert seen["refresh"]["apply"] is True


def test_dry_run_saves_nothing(monkeypatch, tmp_path, capsys):
    module, seen = _prepare(monkeypatch, tmp_path, apply=False)
    assert module.main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert "saved_days" not in summary
    assert not (tmp_path / "store").exists()
    assert seen["refresh"]["apply"] is False


def test_mismatches_stop_before_saving(monkeypatch, tmp_path):
    module, seen = _prepare(monkeypatch, tmp_path, apply=True, mismatches=[{"data_date": "2026-11-21"}])
    with pytest.raises(SystemExit, match="mismatches"):
        module.main()
    assert not (tmp_path / "store").exists() and "refresh" not in seen
