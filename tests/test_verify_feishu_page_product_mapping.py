from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "verify_feishu_page_product_mapping.py"
SPEC = importlib.util.spec_from_file_location("verify_feishu_page_product_mapping", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_normalize_cell_handles_empty_text_and_select_values() -> None:
    assert MODULE.normalize_cell(None, "text") == ""
    assert MODULE.normalize_cell([], "select") is None
    assert MODULE.normalize_cell(["product_page"], "select") == "product_page"
    assert MODULE.normalize_cell("product_page", "select") == "product_page"


def test_normalize_cell_rejects_multiple_select_values() -> None:
    with pytest.raises(AssertionError, match="single-select"):
        MODULE.normalize_cell(["one", "two"], "select")


def test_normalize_cell_normalizes_datetime() -> None:
    assert (
        MODULE.normalize_cell("2026-08-24T09:04:05+00:00", "datetime")
        == "2026-08-24 09:04:05"
    )
