from __future__ import annotations

from pathlib import Path

from website_analytics.information_mapping import (
    build_information_report,
    classify_information_page,
    load_information_mapping,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAPPING_PATH = (
    PROJECT_ROOT / "config" / "information_mappings" / "genemedi-net.yaml"
)


def test_information_mapping_uses_template_topic_and_slug_content_type() -> None:
    mapping = load_information_mapping(MAPPING_PATH, "genemedi-net")
    assert mapping is not None

    target = classify_information_page(
        mapping,
        path="/i/itd-egfr",
        template="index-target",
        page_class="information_page",
    )
    assert target["informationThemeId"] == "TARMART_TARGET"
    assert target["informationContentTypeId"] == "TARGET_REFERENCE"
    assert target["informationThemeStatus"] == "matched"

    protocol = classify_information_page(
        mapping,
        path="/i/protocol-aav-transduction",
        template="index-gene_therapy",
        page_class="information_page",
    )
    assert protocol["informationThemeId"] == "GENE_THERAPY"
    assert protocol["informationContentTypeId"] == "PROTOCOL_APPLICATION"

    fallback = classify_information_page(
        mapping,
        path="/i/general-guide",
        template="index-general",
        page_class="information_page",
    )
    assert fallback["informationThemeId"] == "GENERAL"
    assert fallback["informationContentTypeId"] == "GENERAL_INFORMATION"
    assert fallback["informationThemeStatus"] == "fallback"

    product = classify_information_page(
        mapping,
        path="/i/gmp-product",
        template="indexwithSideBar",
        page_class="product_page",
    )
    assert product["informationThemeStatus"] == "not_applicable"
    assert product["informationThemeId"] == ""


def test_information_report_keeps_theme_and_content_type_as_separate_dimensions() -> None:
    mapping = load_information_mapping(MAPPING_PATH, "genemedi-net")
    assert mapping is not None
    current = [
        _page(mapping, "/i/itd-egfr", "index-target", sessions=10, clicks=3),
        _page(
            mapping,
            "/i/protocol-aav-transduction",
            "index-gene_therapy",
            sessions=4,
            clicks=1,
        ),
        _page(mapping, "/i/general-guide", "index-general", sessions=2, clicks=0),
    ]
    previous = [
        _page(mapping, "/i/itd-egfr", "index-target", sessions=6, clicks=2)
    ]

    report = build_information_report(mapping, current, previous)
    themes = {row["themeId"]: row for row in report["informationThemeLines"]}
    content_types = {
        row["contentTypeId"]: row
        for row in report["informationContentTypeLines"]
    }
    assert themes["TARMART_TARGET"]["ga4SessionsDelta"] == 4.0
    assert themes["GENE_THERAPY"]["currentCanonicalPages"] == 1
    assert content_types["PROTOCOL_APPLICATION"]["gscClicksCurrent"] == 1.0
    assert report["informationClassificationCoverage"] == {
        "observedInformationPages": 3,
        "explicitThemePages": 2,
        "explicitThemeRate": 2 / 3,
        "fallbackThemePages": 1,
        "explicitContentTypePages": 2,
        "explicitContentTypeRate": 2 / 3,
        "fallbackContentTypePages": 1,
    }


def _page(
    mapping,
    path: str,
    template: str,
    *,
    sessions: int,
    clicks: int,
) -> dict[str, object]:
    dimensions = classify_information_page(
        mapping,
        path=path,
        template=template,
        page_class="information_page",
    )
    return {
        "canonicalPath": path,
        "pageClass": "information_page",
        "ga4Sessions": float(sessions),
        "gscClicks": float(clicks),
        "gscImpressions": float(clicks * 10),
        "storedSubmissions": 0.0,
        "quarantinedSubmissions": 0.0,
        "nonQuarantinedSubmissions": 0.0,
        **dimensions,
    }
