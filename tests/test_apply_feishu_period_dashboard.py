import copy
import importlib.util
import json
from pathlib import Path

import pytest

from website_analytics.feishu_period_facts import period_contract

MANIFEST = Path("config/feishu_dashboard/period/product_diagnosis_dashboard.json")


def _module():
    spec = importlib.util.spec_from_file_location("apply_period_dashboard", "scripts/apply_feishu_period_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_matches_the_period_contract():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    _module().validate_manifest(manifest, period_contract())
    blocks = manifest["dashboards"][0]["blocks"]
    assert [block["type"] for block in blocks].count("line") == 9
    # Every trend chart pairs a value with its own baseline column.
    for block in blocks:
        if block["type"] == "line":
            value, baseline = (series["field_name"] for series in block["data_config"]["series"])
            assert baseline == f"基准·{value}"


@pytest.mark.parametrize("mutate,message", [
    (lambda b: b[3]["data_config"]["series"].__setitem__(0, {"field_name": "不存在", "rollup": "SUM"}), "invalid series"),
    (lambda b: b[1]["data_config"].__setitem__("table_name", "别的表"), "unknown table"),
    (lambda b: b[2]["data_config"]["filter"]["conditions"][0].__setitem__("value", "true"), "checkbox"),
    (lambda b: b[4].__setitem__("name", b[5]["name"]), "duplicate block names"),
])
def test_manifest_validation_rejects_mistakes(mutate, message):
    manifest = copy.deepcopy(json.loads(MANIFEST.read_text(encoding="utf-8")))
    mutate(manifest["dashboards"][0]["blocks"])
    with pytest.raises(ValueError, match=message):
        _module().validate_manifest(manifest, period_contract())
