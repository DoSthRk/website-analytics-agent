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
    trends = [block for block in blocks if block["type"] == "line" and not block["name"].startswith("同比")]
    year_over_year = [block for block in blocks if block["name"].startswith("同比")]
    assert len(trends) == 9 and len(year_over_year) == 4
    # Every trend chart pairs a value with its own baseline column.
    for block in trends:
        value, baseline = (series["field_name"] for series in block["data_config"]["series"])
        assert baseline == f"基准·{value}"
    # Year-over-year charts put the week/month number on the x-axis and draw one line per year.
    for block in year_over_year:
        config = block["data_config"]
        assert block["type"] == "line" and len(config["series"]) == 1
        assert [group["field_name"] for group in config["group_by"]] == ["期序号", "年份"]
        assert config["series"][0]["field_name"].split("（")[0] in block["name"]


@pytest.mark.parametrize("mutate,message", [
    (lambda b: b[3]["data_config"]["series"].__setitem__(0, {"field_name": "不存在", "rollup": "SUM"}), "invalid series"),
    (lambda b: b[1]["data_config"].__setitem__("table_name", "别的表"), "unknown table"),
    (lambda b: b[1]["data_config"]["filter"]["conditions"][0].__setitem__("value", "true"), "checkbox"),
    (lambda b: b[4].__setitem__("name", b[5]["name"]), "duplicate block names"),
    (lambda b: b[5].__setitem__("previous_names", [b[6]["name"]]), "duplicate block names"),
    (lambda b: b.append({**b[0], "name": "未排版"}), "place every block"),
])
def test_manifest_validation_rejects_mistakes(mutate, message):
    manifest = copy.deepcopy(json.loads(MANIFEST.read_text(encoding="utf-8")))
    mutate(manifest["dashboards"][0]["blocks"])
    with pytest.raises(ValueError, match=message):
        _module().validate_manifest(manifest, period_contract())


def test_layout_rows_fill_the_grid():
    manifest = copy.deepcopy(json.loads(MANIFEST.read_text(encoding="utf-8")))
    manifest["dashboards"][0]["layout"]["rows"][0][0][1] = 6
    with pytest.raises(ValueError, match="fill 12 columns"):
        _module().validate_manifest(manifest, period_contract())
