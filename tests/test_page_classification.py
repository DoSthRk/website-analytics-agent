from pathlib import Path

import pytest

from website_analytics.page_classification import (
    PageClassificationError,
    build_page_dimension,
    canonical_page_path,
    load_page_classification,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config" / "page_classifications" / "genemedi-net.yaml"


def test_template_dimension_distinguishes_product_and_information_under_i() -> None:
    config = load_page_classification(CONFIG, "genemedi-net")
    dimension = build_page_dimension(
        config,
        [
            _row("i/gmp-product", 1, 1, "indexwithSideBar"),
            _row("i/gmp-information", 2, 2, "index-general"),
            _row("i/GMP-AD-Pig-9", 1066, 1066, ""),
            _row("about", 3111, 3111, "indexwithSidebar_about_us"),
            _row("i/orphan", 9000, None, None),
            _row("", 9991, 9991, "index"),
            _row("", 60892, None, None),
        ],
    )

    assert dimension.classify("/i/gmp-product").page_class == "product_page"
    assert dimension.classify("https://www.genemedi.net/i/gmp-information?x=1").page_class == (
        "information_page"
    )
    assert dimension.classify("/i/GMP-AD-Pig-9").classification_status == "manual_override"
    assert dimension.classify("/about").page_class == "information_page"
    assert dimension.classify("/i/orphan").page_class == "invalid_broken"
    assert dimension.classify("/").page_class == "information_page"
    assert dimension.classify("/").has_orphan_route is True
    assert dimension.classify("/i/not-mapped").page_class == "unknown_unmapped"
    assert dimension.classify("/pdf/file.pdf").page_class == "pdf_asset"
    assert dimension.summary == {
        "sourceRows": 7,
        "canonicalPaths": 6,
        "productPages": 2,
        "informationPages": 3,
        "dynamicProductPages": 0,
        "dynamicInformationPages": 0,
        "invalidBrokenPaths": 1,
        "orphanRoutes": 2,
        "duplicatePaths": 1,
        "unapprovedRouteSourceRows": 0,
        "overridesApplied": 2,
        "routeAliasRules": 3,
        "routeSourceRules": 12,
        "pathRules": 6,
    }


def test_approved_route_alias_still_requires_an_exact_template_backed_target() -> None:
    config = load_page_classification(CONFIG, "genemedi-net")
    dimension = build_page_dimension(
        config,
        [
            _row("i/gm-tg-hg-se1284-ab", 1, 1, "indexwithSideBa_product"),
            _row("i/itd-tg-hg-gm-t02328", 2, 2, "index-target"),
            _row("i/diagnostic-animal-health-avian", 3, 3, "index-diagnostic-AD"),
        ],
    )

    antibody = dimension.classify("/antibody/gm-tg-hg-se1284-ab")
    assert antibody.page_class == "product_page"
    assert antibody.page_id == 1
    assert antibody.canonical_path == "/antibody/gm-tg-hg-se1284-ab"
    assert antibody.classification_status == "template_rule_via_route_alias"
    assert "/i/gm-tg-hg-se1284-ab" in antibody.classification_evidence

    target = dimension.classify("/itd-tg-hg-gm-t02328")
    assert target.page_class == "information_page"
    assert target.page_id == 2

    duplicated = dimension.classify("/i/i/diagnostic-animal-health-avian")
    assert duplicated.page_class == "information_page"
    assert duplicated.page_id == 3

    assert dimension.classify("/antibody/not-in-database").page_class == "unknown_unmapped"
    assert dimension.classify("/oligo/not-in-database").page_class == "unknown_unmapped"


def test_dynamic_route_sources_and_runtime_paths_use_approved_rules() -> None:
    config = load_page_classification(CONFIG, "genemedi-net")
    dimension = build_page_dimension(
        config,
        [
            _row(
                "i/new-drupal-product",
                8001,
                None,
                None,
                route_source="product_generic",
            ),
            _row(
                "i/basic-page",
                8002,
                None,
                None,
                route_source="drupal_page",
            ),
            _row(
                "i/unapproved-source",
                8003,
                None,
                None,
                route_source="unexpected_table",
            ),
        ],
    )

    product = dimension.classify("/i/new-drupal-product")
    assert product.page_class == "product_page"
    assert product.classification_status == "dynamic_route_rule"
    assert product.template == "product_generic.htm"

    information = dimension.classify("/i/basic-page")
    assert information.page_class == "information_page"
    assert information.classification_status == "dynamic_route_rule"

    assert dimension.classify("/i/unapproved-source").page_class == "invalid_broken"
    assert dimension.classify("/g/pgmlp000001.html").page_class == "product_page"
    assert dimension.classify("/a/example.html").page_class == "information_page"
    assert dimension.classify("/search").page_class == "technical_page"
    assert dimension.classify("/(not set)").page_class == "technical_page"
    assert dimension.summary["dynamicProductPages"] == 1
    assert dimension.summary["dynamicInformationPages"] == 1
    assert dimension.summary["unapprovedRouteSourceRows"] == 1


def test_page_classification_rejects_overlapping_route_aliases(tmp_path: Path) -> None:
    path = tmp_path / "classification.yaml"
    path.write_text(
        """
version: "2"
site: genemedi-net
route_aliases:
  - {id: broad, source_prefix: /i/, target_prefix: /legacy/, reason: broad}
  - {id: nested, source_prefix: /i/i/, target_prefix: /i/, reason: nested}
overrides: []
""".lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(PageClassificationError, match="must not overlap"):
        load_page_classification(path, "genemedi-net")


def test_canonical_path_decodes_legacy_space_and_removes_query_fragment() -> None:
    assert canonical_page_path("https://www.genemedi.net/i/A%20B/?x=1#y") == "/i/a b"
    assert canonical_page_path("i/A B") == "/i/a b"


def test_page_classification_rejects_duplicate_override_ids(tmp_path: Path) -> None:
    path = tmp_path / "classification.yaml"
    path.write_text(
        """
version: "1"
site: genemedi-net
overrides:
  - {page_id: 1, page_class: product_page, reason: first}
  - {page_id: 1, page_class: information_page, reason: duplicate}
""".lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(PageClassificationError, match="unique"):
        load_page_classification(path, "genemedi-net")


def _row(
    url: str,
    route_id: int,
    content_id: int | None,
    template: str | None,
    *,
    route_source: str = "pages",
) -> dict:
    return {
        "route_url": url,
        "route_page_id": route_id,
        "route_source": route_source,
        "content_page_id": content_id,
        "template": template,
    }
