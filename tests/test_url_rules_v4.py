"""Acceptance test: every reviewed URL decision (2026-11-18) maps as decided."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from website_analytics.page_classification import (
    PageClassificationError,
    build_page_dimension,
    load_page_classification,
)
from website_analytics.product_mapping import load_product_mapping, match_product_rule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config"
DECISIONS = json.loads(
    (PROJECT_ROOT / "tests" / "fixtures" / "url_rules_v4_decisions.json").read_text(encoding="utf-8")
)["decisions"]


def _route_rows() -> list[dict]:
    """Rebuild the route-table state each reviewed URL was observed in."""
    rows: list[dict] = []
    for index, item in enumerate(DECISIONS, start=1):
        row = {
            "route_url": item["path"],
            "route_page_id": index,
            "route_source": "product_generic",
            "content_page_id": None,
            "template": None,
        }
        if item["route_state"] == "product_route":
            rows.append(row)
        elif item["route_state"] == "duplicate_route":
            rows.extend([row, dict(row, route_page_id=10_000 + index)])
    return rows


@pytest.fixture(scope="module")
def dimension():
    config = load_page_classification(CONFIG / "page_classifications" / "genemedi-net.yaml", "genemedi-net")
    return build_page_dimension(config, _route_rows())


@pytest.fixture(scope="module")
def mapping():
    return load_product_mapping(CONFIG / "product_mappings" / "genemedi-net.yaml", "genemedi-net")


def test_every_reviewed_decision_is_applied(dimension, mapping) -> None:
    mismatches = []
    for item in DECISIONS:
        entry = dimension.classify(item["path"])
        actual = {"page_class": entry.page_class}
        if entry.page_class == "product_page":
            rule = match_product_rule(mapping, entry.canonical_path, entry.page_class, entry.template)
            actual["report_line_id"] = rule.report_line_id if rule else None
        expected = {k: item[k] for k in ("page_class", "report_line_id") if k in item}
        if actual != expected:
            mismatches.append((item["path"], item["decision"], actual))
    assert mismatches == []
    assert len(DECISIONS) == 218


def test_new_report_lines_have_reviewed_hierarchy(mapping) -> None:
    lines = {line.identifier: line for line in mapping.report_lines}
    assert (lines["AAV_OPTOGENETICS"].category_l1, lines["AAV_OPTOGENETICS"].category_l2) == (
        "GENE THERAPY",
        "AAV OPTOGENETICS",
    )
    assert lines["FLOW_ANTIBODY"].category_l1 == "Immunology & Cell Therapy"
    assert lines["CAR"].category_l1 == "Immunology & Cell Therapy"
    assert lines["COVID19"].category_l1 == "COVID-19"
    assert mapping.version == "4"


def test_unconfigured_target_antibody_pages_keep_their_line(mapping) -> None:
    # New conjugate rule must not capture plain GM-Tg-hg antibodies.
    rule = match_product_rule(mapping, "/i/gm-tg-hg-mp0250-ab", "product_page", "")
    assert rule is not None and rule.report_line_id == "TARGET_ANTIBODY"


def test_path_override_resolves_duplicate_routes_and_counts(dimension) -> None:
    entry = dimension.classify("/i/aav-rh74-rep-cap-plasmid")
    assert entry.page_class == "product_page"
    assert entry.classification_status == "path_override"
    assert dimension.summary["pathOverrides"] == 16
    assert dimension.classify("/blog/any-new-article-en").page_class == "information_page"


def test_path_override_paths_must_be_unique(tmp_path: Path) -> None:
    config = tmp_path / "pages.yaml"
    config.write_text(
        "version: '1'\nsite: demo\npath_overrides:\n"
        "  - {path: /i/a, page_class: product_page, reason: r}\n"
        "  - {path: /i/A/, page_class: information_page, reason: r}\n",
        encoding="utf-8",
    )
    with pytest.raises(PageClassificationError, match="unique"):
        load_page_classification(config, "demo")
