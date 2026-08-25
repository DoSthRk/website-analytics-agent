from __future__ import annotations

import importlib.util
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "apply_feishu_page_product_mapping.py"
SPEC = importlib.util.spec_from_file_location("apply_feishu_page_product_mapping", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_payload_batches_respect_windows_command_limit() -> None:
    contract = json.loads(
        (PROJECT_ROOT / "config" / "feishu_dashboard" / "v3" / "page_product_mapping_contract.json").read_text(
            encoding="utf-8"
        )
    )
    records = [_record(index) for index in range(250)]

    batches = list(MODULE.payload_batches(contract, records))

    assert sum(len(batch["rows"]) for batch in batches) == 250
    assert all(len(batch["rows"]) <= MODULE.MAX_BATCH_ROWS for batch in batches)
    assert all(
        len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")))
        <= MODULE.MAX_JSON_ARGUMENT_CHARS + 500
        for batch in batches
    )


def _record(index: int) -> dict[str, str]:
    value = f"/i/product-{index}-" + ("x" * 180)
    base = {
        "url_key": f"genemedi-net|{value}",
        "site": "genemedi-net",
        "hostname": "www.genemedi.net",
        "full_url": f"https://www.genemedi.net{value}",
        "canonical_path": value,
        "page_id": str(index),
        "template": "indexwithSideBar",
        "page_class": "product_page",
        "classification_status": "template_rule",
        "product_category_id": "OTHER_PRODUCT",
        "product_category": "其他产品",
        "category_l1": "OTHER",
        "category_l2": "OTHER PRODUCT",
        "category_l3": "",
        "mapping_rule_id": "other-product-fallback",
        "mapping_status": "approved",
        "include_in_product_report": "是",
        "page_classification_version": "3",
        "product_mapping_version": "3",
        "source_snapshot_at": "2026-08-24T09:04:05Z",
        "row_hash": f"{index:064x}",
    }
    return base
