"""One-shot aggregate preview, executed in memory using the existing server readers.

No remote files, caches, credentials or service configuration are written.
Legacy query_server.py helpers must already exist in this process's namespace.
"""


def gsc_incomplete_date(metadata):
    """Official discovery JSON uses camelCase; accept a snake_case adapter too."""
    from datetime import date
    first, second = metadata.get("firstIncompleteDate"), metadata.get("first_incomplete_date")
    if first is not None and second is not None and first != second:
        raise ValueError("conflicting_gsc_cutoffs")
    value = first if first is not None else second
    if value is not None:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("invalid_gsc_cutoff")
    return value


def run_seo_preview(request):
    site, profile = context()  # Registers the existing server source path first.
    import re
    from datetime import datetime, date, timedelta, timezone
    from urllib.parse import urlsplit, urlunsplit
    from google.analytics.data_v1beta.types import Filter, FilterExpression, FilterExpressionList
    from website_analytics.adapters.ga4 import GA4Adapter
    from website_analytics.adapters.gsc import GSCAdapter
    from website_analytics.adapters.page_dimension import create_page_dimension_adapter
    from website_analytics.models import DateRange
    from website_analytics.cli import _create_live_adapters
    from website_analytics.page_classification import load_page_classification, build_page_dimension, canonical_page_path
    from website_analytics.product_mapping import load_product_mapping, match_product_rule

    weekly = request.get("protocol") == "seo-weekly-read/1"
    required = {"protocol", "submitted_paths", "variant_paths", "weeks" if weekly else "scenarios"}
    if not required <= set(request) or set(request)-required-({"availability_only"} if weekly else set()) or request["protocol"] not in {"seo-preview-read/1", "seo-weekly-read/1"}:
        raise ValueError("unsupported_preview_request")
    if "availability_only" in request and type(request["availability_only"]) is not bool:
        raise ValueError("invalid_availability_flag")
    submitted, variants = request["submitted_paths"], request["variant_paths"]
    allpaths = submitted + variants
    if not isinstance(submitted, list) or not 1 <= len(submitted) <= 20 or not isinstance(variants, list) or len(variants) > 5 or len(allpaths) != len(set(allpaths)):
        raise ValueError("invalid_preview_paths")
    for path in allpaths:
        if not isinstance(path, str) or not re.fullmatch(r"/i/[A-Za-z0-9_-]{1,200}", path):
            raise ValueError("invalid_preview_path")
    periods = []
    scenarios = request["weeks"] if weekly else request["scenarios"]
    if not isinstance(scenarios, list) or not (1 <= len(scenarios) <= 16 if weekly else len(scenarios) == 3):
        raise ValueError("bounded_periods_required")
    for index, row in enumerate(scenarios):
        if not weekly and set(row) != {"assumed_change_date", "offset_days", "assumption_not_confirmed_date", "before", "after"}:
            raise ValueError("invalid_scenario")
        if weekly and set(row) != {"start", "end", "interpretation"}:
            raise ValueError("invalid_week")
        pivot = date.fromisoformat(row["assumed_change_date"]) if not weekly else None
        for label in (("week",) if weekly else ("before", "after")):
            raw = {"start": row["start"], "end": row["end"]} if weekly else row[label]
            if set(raw) != {"start", "end"}:
                raise ValueError("invalid_period")
            period = DateRange(date.fromisoformat(raw["start"]), date.fromisoformat(raw["end"]))
            if (period.end-period.start).days != 6 or not date(2026, 1, 1) <= period.start <= period.end <= datetime.now(timezone.utc).date():
                raise ValueError("bounded_historical_week_required")
            if not weekly and ((label == "before" and period.end >= pivot) or (label == "after" and period.start <= pivot)):
                raise ValueError("exclude_assumed_change_day")
            if weekly and (period.start.weekday() != 0 or period.end.weekday() != 6 or (index and period.start != periods[-1][2].end+timedelta(days=1))):
                raise ValueError("consecutive_full_weeks_required")
            periods.append((index if weekly else row["offset_days"], label, period))
    if site.site_key != "genemedi-net" or not site.domains or not set(site.domains) <= {"genemedi.net", "www.genemedi.net"}:
        raise ValueError("site_boundary_violation")
    google_environment()
    ga4, gsc = _create_live_adapters()
    iq = None if request.get("availability_only") else inquiry_adapter(site)
    audits = {"ga4": [], "gsc": []}

    def path_pattern(paths):
        return "^(?:"+"|".join(re.escape(path) for path in paths)+")/?(?:\\?.*)?$"

    class ScopedGA:
        def __init__(self, paths): self.paths = paths
        def run_report(self, req):
            req.dimension_filter = FilterExpression(and_group=FilterExpressionList(expressions=[
                req.dimension_filter,
                FilterExpression(filter=Filter(field_name="landingPagePlusQueryString", string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.FULL_REGEXP, value=path_pattern(self.paths), case_sensitive=True))) ]))
            response = ga4._client.run_report(req)
            meta = response.metadata
            samples = [{"samples_read_count": int(x.samples_read_count), "sampling_space_size": int(x.sampling_space_size)} for x in getattr(meta, "sampling_metadatas", ())]
            audits["ga4"].append({"fetched_at": datetime.now(timezone.utc).isoformat(), "time_zone": getattr(meta, "time_zone", None),
                "data_loss_from_other_row": bool(getattr(meta, "data_loss_from_other_row", False)),
                "subject_to_thresholding": bool(getattr(meta, "subject_to_thresholding", False)), "sampling": samples,
                "row_count": int(response.row_count), "scope_paths": self.paths})
            return response

    class ScopedGSC:
        def __init__(self, paths): self.paths = paths
        def searchanalytics(self): return self
        def query(self, *, siteUrl, body):
            body = dict(body)
            pattern = "^https?://(?:"+"|".join(re.escape(d) for d in site.domains)+")"+path_pattern(self.paths)[1:]
            body["dimensionFilterGroups"] = [{"groupType": "and", "filters": [{"dimension": "page", "operator": "includingRegex", "expression": pattern}]}]
            source_request = gsc._service.searchanalytics().query(siteUrl=siteUrl, body=body)
            class Query:
                def execute(inner):
                    result = source_request.execute()
                    audits["gsc"].append({"fetched_at": datetime.now(timezone.utc).isoformat(), "dataState": body["dataState"],
                        "responseAggregationType": result.get("responseAggregationType"), "rows_returned": len(result.get("rows", [])), "scope_paths": self.paths})
                    return result
            return Query()

    def safe_call(fn):
        try:
            return {"status": "ok", "result": fn(), "fetched_at": datetime.now(timezone.utc).isoformat()}
        except Exception as error:
            return {"status": "error", "error_category": type(error).__name__, "fetched_at": datetime.now(timezone.utc).isoformat()}

    def gsc_result(period, paths, dimensions):
        result = GSCAdapter(ScopedGSC(paths)).query_result(site, period, dimensions)
        return {"rows": [{**row, **({"page": urlunsplit((*urlsplit(row["page"])[:3], "", ""))} if "page" in row else {})} for row in result.rows],
                "truncated": result.truncated, "row_cap": result.row_cap}

    def inquiry_result(period):
        result = iq.query(period)
        rows = []
        attributed_database = 0
        foreign_origin_rows = 0
        for row in result.pages:
            parts = urlsplit(row["sourceUrl"])
            if parts.netloc and parts.hostname not in site.domains:
                foreign_origin_rows += 1
                continue
            if not parts.netloc or parts.scheme not in {"http", "https"}:
                continue
            attributed_database += row["storedSubmissions"]
            path = parts.path.rstrip("/") or "/"
            if path in allpaths:
                rows.append({"exact_path": path, **{key: row[key] for key in ("storedSubmissions", "quarantinedSubmissions", "nonQuarantinedSubmissions")}})
        totals = {group: {key: sum(r[key] for r in rows if r["exact_path"] in paths) for key in ("storedSubmissions", "quarantinedSubmissions", "nonQuarantinedSubmissions")} for group, paths in (("submitted", submitted), ("lowercase_variant", variants))}
        return {"rows": rows, "cohorts": totals, "page_rows_truncated": result.page_rows_truncated,
                "database_total": result.totals["storedSubmissions"],
                "database_unattributed_count": result.totals["storedSubmissions"]-attributed_database if not result.page_rows_truncated and not foreign_origin_rows else None,
                "foreign_origin_group_count": foreign_origin_rows,
                "database_scope": "configured_database_not_independently_proven_dedicated",
                "date_boundary": "legacy_server_calendar_unverified", "days_with_database_rows": [r["date"] for r in result.daily]}

    if weekly:
        def availability():
            from datetime import timedelta
            end = datetime.now(timezone.utc).date()-timedelta(days=1)
            body = {"startDate": (end-timedelta(days=8)).isoformat(), "endDate": end.isoformat(),
                "dimensions": ["date"], "type": "web", "dataState": "all", "rowLimit": 100, "startRow": 0}
            response = ScopedGSC(allpaths).query(siteUrl=site.gsc_property_url, body=body).execute()
            return {"requested_start": body["startDate"], "requested_end": body["endDate"],
                "first_incomplete_date": gsc_incomplete_date(response.get("metadata", {})),
                "metadata_fields_present": sorted(response.get("metadata", {}).keys()),
                "dates_with_rows": [row["keys"][0] for row in response.get("rows", [])],
                "zero_filled": False, "source_scope": "submitted_and_separate_variant_paths_only"}
        availability_result = safe_call(availability)
        if request.get("availability_only"):
            return {"protocol": "seo-weekly-read/1", "sample": False, "exploratory": True, "batch_confirmed": False,
                "gsc_availability": availability_result, "weeks": [], "runtime": runtime_meta(),
                "availability_only": True, "writes": 0, "cache_or_audit_persisted": False}
        first_incomplete = availability_result.get("result", {}).get("first_incomplete_date")
        weeks, skipped = [], []
        for index, _, period in periods:
            if first_incomplete and period.end >= date.fromisoformat(first_incomplete):
                skipped.append({"start": period.start.isoformat(), "end": period.end.isoformat(), "reason": "upstream_incomplete_date_overlaps_week"})
                continue
            weeks.append({"start": period.start.isoformat(), "end": period.end.isoformat(), "interpretation": scenarios[index]["interpretation"],
                "ga4_submitted": safe_call(lambda p=period: GA4Adapter(ScopedGA(submitted)).aggregate(site, p)),
                "ga4_pages": safe_call(lambda p=period: GA4Adapter(ScopedGA(allpaths))._run(site, p, ("landingPage",))),
                "gsc_submitted": safe_call(lambda p=period: gsc_result(p, submitted, ())),
                "inquiry": safe_call(lambda p=period: inquiry_result(p))})
        if not weeks:
            raise ValueError("no_complete_requested_weeks")
        broad = DateRange(date.fromisoformat(weeks[0]["start"]), date.fromisoformat(weeks[-1]["end"]))
        return {"protocol": "seo-weekly-read/1", "sample": False, "exploratory": True, "batch_confirmed": False,
            "queried_at": datetime.now(timezone.utc).isoformat(), "runtime": runtime_meta(),
            "submitted_paths": submitted, "variant_paths_not_merged": variants, "weeks": weeks,
            "gsc_availability": availability_result, "skipped_weeks": skipped,
            "gsc_page_days": safe_call(lambda: gsc_result(broad, allpaths, ("date", "page"))),
            "query_metadata": audits, "writes": 0, "cache_or_audit_persisted": False}

    out = {"protocol": "seo-preview-read/1", "sample": False, "exploratory": True, "batch_confirmed": False,
           "queried_at": datetime.now(timezone.utc).isoformat(), "runtime": runtime_meta(),
           "submitted_paths": submitted, "variant_paths_not_merged": variants, "scenarios": [],
           "writes": 0, "cache_or_audit_persisted": False}
    for offset in (-1, 0, 1):
        scenario = {"offset_days": offset, "periods": {}}
        for shift, label, period in periods:
            if shift != offset: continue
            row = {"start": period.start.isoformat(), "end": period.end.isoformat(),
                   "ga4_submitted": safe_call(lambda p=period: GA4Adapter(ScopedGA(submitted)).aggregate(site, p)),
                   "inquiry": safe_call(lambda p=period: inquiry_result(p))}
            if offset == 0:
                row["ga4_lowercase_variant"] = safe_call(lambda p=period: GA4Adapter(ScopedGA(variants)).aggregate(site, p))
                row["ga4_pages"] = safe_call(lambda p=period: GA4Adapter(ScopedGA(allpaths))._run(site, p, ("landingPage",)))
                row["gsc_submitted"] = safe_call(lambda p=period: gsc_result(p, submitted, ()))
                row["gsc_lowercase_variant"] = safe_call(lambda p=period: gsc_result(p, variants, ()))
            scenario["periods"][label] = row
        out["scenarios"].append(scenario)
    broad = DateRange(min(p.start for _,_,p in periods), max(p.end for _,_,p in periods))
    out["broad_range"] = {"start": broad.start.isoformat(), "end": broad.end.isoformat()}
    out["gsc_page_days"] = safe_call(lambda: gsc_result(broad, allpaths, ("date", "page")))
    out["ga4_page_days"] = safe_call(lambda: GA4Adapter(ScopedGA(allpaths))._run(site, broad, ("date", "landingPage")))

    def cms_result():
        rows = create_page_dimension_adapter(site.inquiry_source).query()
        dimension = build_page_dimension(load_page_classification(ROOT/"config/page_classifications/genemedi-net.yaml", SITE), rows)
        mapping = load_product_mapping(ROOT/"config/product_mappings/genemedi-net.yaml", SITE)
        found = []
        keys = {canonical_page_path(p) for p in allpaths}
        for row in rows:
            if canonical_page_path(row["route_url"]) in keys:
                found.append({key: row.get(key) for key in ("route_url", "route_page_id", "route_source", "content_page_id", "template")})
        classifications = []
        for path in allpaths:
            entry = dimension.classify(path)
            rule = match_product_rule(mapping, path, entry.page_class, entry.template)
            classifications.append({"exact_requested_path": path, "legacy_lookup_path": canonical_page_path(path),
                "page_class": entry.page_class, "classification_status": entry.classification_status,
                "page_id": entry.page_id, "product_line": rule.product_line_id if rule else None})
        return {"route_rows": found, "classifications": classifications,
                "classification_version": dimension.version, "product_mapping_version": mapping.version,
                "current_dimension_not_historical": True}
    out["cms"] = safe_call(cms_result)
    out["query_metadata"] = audits
    return out


if "SEO_PREVIEW_REQUEST" in globals():
    try:
        response = run_seo_preview(SEO_PREVIEW_REQUEST)
    except Exception as error:
        response = {"status": "error", "error_category": type(error).__name__, "writes": 0}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
