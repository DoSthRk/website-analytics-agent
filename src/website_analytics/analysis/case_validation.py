"""Strict aggregate-only offline import schemas, not a generic JSON archive."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from website_analytics.analysis.contracts import AnalysisError, calendar_date, metric_number, page_path, strict_keys, timestamp, timezone_name
from website_analytics.analysis.query import METRICS
from website_analytics.analysis.query import SourceEvidence
from website_analytics.analysis.contracts import Selection, fingerprint
from website_analytics.models import DateRange
from dataclasses import fields as dataclass_fields


def sequence(value, check, maximum=1000):
    if not isinstance(value, list) or len(value) > maximum:
        raise AnalysisError("invalid bounded evidence list")
    for item in value:
        check(item)


def boolean(value):
    if type(value) is not bool:
        raise AnalysisError("boolean required")


def integer(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise AnalysisError("bounded integer required")


def safe_text(value, limit=1000, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value) or any(ord(c) < 32 for c in value):
        raise AnalysisError("invalid bounded public text")
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?i:bearer\s|private[ _-]key|refresh[ _-]token|access[ _-]token|client[ _-]secret|(?:mysql|postgres)://|password\s*[:=])", value):
        raise AnalysisError("personal or credential-like text is not permitted")


def code(value):
    safe_text(value, 120)
    if not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", value):
        raise AnalysisError("invalid public metadata code")


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AnalysisError("invalid evidence digest")


def fields(value, schema, optional=None):
    strict_keys(value, set(schema), set(optional or {}))
    for key, check in {**schema, **(optional or {})}.items():
        if key in value:
            check(value[key])


def nullable(check):
    return lambda value: None if value is None else check(value)


def public_url(value):
    page_path(value)
    if urlsplit(value).scheme != "https":
        raise AnalysisError("approved public HTTPS URL required")


def metrics(value, source, *, dimensions=None):
    dimensions = dimensions or {}
    strict_keys(value, set(dimensions), set(METRICS[source]))
    for key, check in dimensions.items():
        check(value[key])
    for key in set(value)-set(dimensions):
        number = metric_number(value[key])
        if key in {"ctr", "engagementRate"} and number > 1:
            raise AnalysisError("rate must be a fraction")
    if source == "inquiry" and set(METRICS[source]) <= value.keys() and value["storedSubmissions"] != value["quarantinedSubmissions"]+value["nonQuarantinedSubmissions"]:
        raise AnalysisError("invalid inquiry count partition")


def runtime(value):
    def hashes(raw):
        allowed = {"src/website_analytics/adapters/ga4.py", "src/website_analytics/adapters/gsc.py", "src/website_analytics/adapters/inquiry.py", "src/website_analytics/product_mapping.py", "config/product_mappings/genemedi-net.yaml", "config/information_mappings/genemedi-net.yaml", "config/page_classifications/genemedi-net.yaml"}
        if not isinstance(raw, dict) or not set(raw) <= allowed:
            raise AnalysisError("unsupported public source hash metadata")
        for value in raw.values():
            digest(value)
    if isinstance(value,dict) and value.get("contract")=="runtime-provenance/v2":
        fields(value,{"contract":code,"execution_model":code,"production_release":code,"capture_mode":code,
            "release_manifest_sha256":digest,"dependency_lock_sha256":digest,"source_config_sha256":digest,
            "python_version":code,"platform":code,"public_source_sha256":hashes})
        if value["capture_mode"] not in {"local_fixture","server_shadow","production_worker"} or value["execution_model"]!="fixed-release-launcher/v1" or value["production_release"]!="rc-"+value["release_manifest_sha256"][:16] or not re.fullmatch(r"\d+\.\d+\.\d+",value["python_version"]):
            raise AnalysisError("invalid release runtime provenance")
        return
    fields(value, {"execution_model": code, "production_release": code,
                   "public_source_sha256": hashes, "local_developer_edits_deployed": boolean})
    if value["local_developer_edits_deployed"]:
        raise AnalysisError("this local import does not attest deployments")


class PacketValidator:
    def __init__(self, paths, variants):
        self.paths, self.variants = tuple(paths), tuple(variants)
        if len(set((*paths, *variants))) != len(paths)+len(variants) or len(variants) > 5:
            raise AnalysisError("variant paths must remain distinct")
        for path in (*paths, *variants):
            page_path(path)
            if not path.startswith("/") or urlsplit(path).scheme:
                raise AnalysisError("exact source path required")

    def path(self, value):
        if value not in (*self.paths, *self.variants):
            raise AnalysisError("source path outside the saved case")

    def source_url(self, value):
        public_url(value)
        self.path(urlsplit(value).path.rstrip("/") or "/")

    def envelope(self, value, result_check):
        fields(value, {"status": code, "fetched_at": timestamp}, {"result": result_check, "error_category": code})
        if value["status"] not in {"ok", "error"} or (value["status"] == "ok") != ("result" in value) or (value["status"] == "error") != ("error_category" in value):
            raise AnalysisError("invalid source outcome")

    def ga(self, value, dims=()):
        checks = {"date": calendar_date, "landingPage": self.path}
        sequence(value, lambda row: metrics(row, "ga4", dimensions={key: checks[key] for key in dims}), 2000)
        if not dims and len(value) > 1:
            raise AnalysisError("interval users cannot be multiple additive rows")

    def gsc(self, value, dims=()):
        fields(value, {"rows": lambda rows: sequence(rows, lambda row: metrics(row, "gsc", dimensions={key: {"date": calendar_date, "page": self.source_url}[key] for key in dims}), 2000),
                       "truncated": boolean, "row_cap": integer})
        if value["row_cap"] != 50000 or (not dims and len(value["rows"]) > 1):
            raise AnalysisError("unsupported GSC interval or row-cap contract")

    def inquiry(self, value):
        def row(item):
            metrics(item, "inquiry", dimensions={"exact_path": self.path})
        def cohorts(item):
            fields(item, {"submitted": lambda raw: metrics(raw, "inquiry"), "lowercase_variant": lambda raw: metrics(raw, "inquiry")})
        fields(value, {"rows": lambda raw: sequence(raw, row, 100), "cohorts": cohorts,
            "page_rows_truncated": boolean, "database_total": metric_number,
            "database_unattributed_count": nullable(metric_number), "foreign_origin_group_count": integer,
            "database_scope": code, "date_boundary": code,
            "days_with_database_rows": lambda raw: sequence(raw, calendar_date, 112)})
        if value["database_scope"] != "configured_database_not_independently_proven_dedicated" or value["date_boundary"] != "legacy_server_calendar_unverified":
            raise AnalysisError("unsupported inquiry scope claim")
        for key, paths in (("submitted", self.paths), ("lowercase_variant", self.variants)):
            for metric in METRICS["inquiry"]:
                if metric not in value["cohorts"][key] or value["cohorts"][key][metric] != sum(row[metric] for row in value["rows"] if row["exact_path"] in paths):
                    raise AnalysisError("inquiry cohort does not match exact-path records")
        if (value["page_rows_truncated"] or value["foreign_origin_group_count"]) and value["database_unattributed_count"] is not None:
            raise AnalysisError("unknown inquiry totals cannot be asserted under incomplete scope")

    def availability(self, value):
        fields(value, {"requested_start": calendar_date, "requested_end": calendar_date,
            "first_incomplete_date": nullable(calendar_date), "dates_with_rows": lambda raw: sequence(raw, calendar_date, 100),
            "zero_filled": boolean, "source_scope": code}, {"metadata_fields_present": lambda raw: sequence(raw, code, 4)})
        if value["zero_filled"] or value["source_scope"] != "submitted_and_separate_variant_paths_only":
            raise AnalysisError("unsupported freshness scope")

    def audits(self, value):
        def ga(item):
            fields(item, {"fetched_at": timestamp, "time_zone": nullable(timezone_name), "data_loss_from_other_row": boolean,
                "subject_to_thresholding": boolean, "sampling": lambda raw: sequence(raw, lambda sample: fields(sample, {"samples_read_count": integer, "sampling_space_size": integer}), 20),
                "row_count": integer, "scope_paths": lambda raw: sequence(raw, self.path, 25)})
        def gsc(item):
            fields(item, {"fetched_at": timestamp, "dataState": code, "responseAggregationType": nullable(code),
                "rows_returned": integer, "scope_paths": lambda raw: sequence(raw, self.path, 25)})
            if item["dataState"] not in {"all", "final"}:
                raise AnalysisError("unsupported GSC data state")
        fields(value, {"ga4": lambda raw: sequence(raw, ga, 60), "gsc": lambda raw: sequence(raw, gsc, 60)})

    def cms(self, value):
        def route(item):
            fields(item, {"route_url": lambda raw: self.path("/"+raw.lstrip("/")), "route_page_id": nullable(integer),
                "route_source": code, "content_page_id": nullable(integer), "template": lambda raw: safe_text(raw, 120, empty=True)})
        def classification(item):
            fields(item, {"exact_requested_path": self.path, "legacy_lookup_path": page_path,
                "page_class": code, "classification_status": code, "page_id": nullable(integer), "product_line": nullable(code)})
        fields(value, {"route_rows": lambda raw: sequence(raw, route, 50), "classifications": lambda raw: sequence(raw, classification, 25),
            "classification_version": code, "product_mapping_version": code, "current_dimension_not_historical": boolean})

    def packet(self, value, kind):
        common = {"protocol": code, "sample": boolean, "exploratory": boolean, "batch_confirmed": boolean,
            "queried_at": timestamp, "runtime": runtime, "submitted_paths": lambda raw: sequence(raw, self.path, 20),
            "variant_paths_not_merged": lambda raw: sequence(raw, self.path, 5), "writes": integer,
            "cache_or_audit_persisted": boolean, "query_metadata": self.audits,
            "gsc_page_days": lambda raw: self.envelope(raw, lambda result: self.gsc(result, ("date", "page")))}
        def period(raw, main=False):
            schema = {"start": calendar_date, "end": calendar_date,
                "ga4_submitted": lambda raw: self.envelope(raw, self.ga), "inquiry": lambda raw: self.envelope(raw, self.inquiry)}
            if main:
                schema.update({"ga4_lowercase_variant": lambda raw: self.envelope(raw, self.ga),
                    "ga4_pages": lambda raw: self.envelope(raw, lambda result: self.ga(result, ("landingPage",))),
                    "gsc_submitted": lambda raw: self.envelope(raw, self.gsc), "gsc_lowercase_variant": lambda raw: self.envelope(raw, self.gsc)})
            fields(raw, schema)
        if kind == "continuous_trend":
            def week(raw):
                fields(raw, {"start": calendar_date, "end": calendar_date, "interpretation": code,
                    "ga4_submitted": lambda raw: self.envelope(raw, self.ga),
                    "ga4_pages": lambda raw: self.envelope(raw, lambda result: self.ga(result, ("landingPage",))),
                    "gsc_submitted": lambda raw: self.envelope(raw, self.gsc), "inquiry": lambda raw: self.envelope(raw, self.inquiry)}, {"window_kind":code} if value.get("protocol")=="seo-period-read/2" else {})
            common.update({"weeks": lambda raw: sequence(raw, week, 16),
                "gsc_availability": lambda raw: self.envelope(raw, self.availability),
                "skipped_weeks": lambda raw: sequence(raw, lambda item: fields(item, {"start": calendar_date, "end": calendar_date, "reason": code}), 16)})
        elif kind == "approximate_window":
            def scenario(raw):
                fields(raw, {"offset_days": lambda raw: None if type(raw) is int and raw in {-1, 0, 1} else (_ for _ in ()).throw(AnalysisError("invalid scenario offset")),
                    "periods": lambda item: fields(item, {"before": lambda data: period(data, raw["offset_days"] == 0), "after": lambda data: period(data, raw["offset_days"] == 0)})})
            common.update({"scenarios": lambda raw: sequence(raw, scenario, 3), "broad_range": lambda raw: fields(raw, {"start": calendar_date, "end": calendar_date}),
                "ga4_page_days": lambda raw: self.envelope(raw, lambda result: self.ga(result, ("date", "landingPage"))),
                "cms": lambda raw: self.envelope(raw, self.cms)})
        else:
            raise AnalysisError("unsupported provisional evidence kind")
        optional={"worker_window_evidence":lambda raw:None,"worker_captured_at":timestamp,
            "worker_source_ids":lambda raw:None,"worker_observations":lambda raw:None,
            "worker_metadata_diagnostics":lambda raw:None,**({"selection_timezone":timezone_name} if value.get("protocol")=="seo-period-read/2" else {})} if kind=="continuous_trend" else {}
        fields(value, common,optional)
        if ("worker_window_evidence" in value)!=("worker_captured_at" in value):raise AnalysisError("worker metadata pair required")
        if "worker_source_ids" in value:
            fields(value["worker_source_ids"],{source:digest for source in ("ga4","gsc","inquiry")})
            if "worker_window_evidence" not in value:raise AnalysisError("source registration needs worker evidence")
        if "worker_observations" in value:
            from website_analytics.analysis.completeness import validate_observations
            if "worker_source_ids" not in value:raise AnalysisError("observation registration binding required")
            validate_observations(value["worker_observations"],as_of=timestamp(value["queried_at"]))
            if any(not set(row["scope"]["pages"])<=set((*self.paths,*self.variants)) for row in value["worker_observations"]):
                raise AnalysisError("observation page outside saved case")
        if "worker_metadata_diagnostics" in value:
            def diagnostic(raw):
                fields(raw,{"source":lambda x:None if x in {"ga4","gsc","inquiry"} else (_ for _ in ()).throw(AnalysisError("invalid diagnostic source")),
                    "scope_digest":digest,"notes":lambda x:sequence(x,code,8)})
            sequence(value["worker_metadata_diagnostics"],diagnostic,36)
        if value["sample"] or not value["exploratory"] or value["batch_confirmed"] or value["writes"] != 0 or value["cache_or_audit_persisted"] or value["submitted_paths"] != list(self.paths) or value["variant_paths_not_merged"] != list(self.variants):
            raise AnalysisError("saved real case must stay provisional and exact scoped")
        periods = value["weeks"] if kind == "continuous_trend" else [period for scenario in value["scenarios"] for period in scenario["periods"].values()]
        if "worker_window_evidence" in value:
            captured=timestamp(value["worker_captured_at"])
            if captured>timestamp(value["queried_at"]):raise AnalysisError("worker capture after case capture")
            records=value["worker_window_evidence"]
            if not isinstance(records,list) or len(records)!=len(periods)*(1+len(self.variants))*3:raise AnalysisError("invalid worker metadata count")
            groups={"submitted":self.paths,**{"variant_"+str(i):(p,) for i,p in enumerate(self.variants)}}
            seen=set()
            for row in records:
                strict_keys(row,{"group","scope","evidence","assessment"})
                scope=row["scope"];strict_keys(scope,{"site","start","end","pages","countries","channels"})
                if scope["site"]!="genemedi-net" or row["group"] not in groups or scope["pages"]!=list(groups[row["group"]]) or scope["countries"] or scope["channels"] or (scope["start"],scope["end"]) not in {(p["start"],p["end"]) for p in periods}:
                    raise AnalysisError("worker evidence scope mismatch")
                strict_keys(row["evidence"],{f.name for f in dataclass_fields(SourceEvidence)})
                evidence=SourceEvidence(**row["evidence"]);key=(scope["start"],row["group"],evidence.source)
                if key in seen:raise AnalysisError("duplicate worker evidence")
                seen.add(key);safe_text(evidence.date_boundary,100)
                assessed=evidence.assess(DateRange(calendar_date(scope["start"]),calendar_date(scope["end"])),fingerprint(scope),sample=False,as_of=captured)
                if fingerprint(assessed)!=fingerprint(row["assessment"]):raise AnalysisError("worker assessment mismatch")
        if not periods:
            raise AnalysisError("case has no requested source windows")
        first, last = min(p["start"] for p in periods), max(p["end"] for p in periods)
        for period in periods:
            if period["start"] > period["end"]:
                raise AnalysisError("invalid saved source window")
            if period["inquiry"]["status"] == "ok" and any(not period["start"] <= day <= period["end"] for day in period["inquiry"]["result"]["days_with_database_rows"]):
                raise AnalysisError("inquiry dates outside requested window")
        for key in ("gsc_page_days", "ga4_page_days"):
            if key not in value or value[key]["status"] != "ok":
                continue
            source_rows = value[key]["result"]["rows"] if key == "gsc_page_days" else value[key]["result"]
            if any(not first <= row["date"] <= last for row in source_rows):
                raise AnalysisError("source day outside saved query scope")

    def url_evidence(self, value, kind):
        def redirects(raw):
            sequence(raw, lambda row: fields(row, {"status": integer, "from": self.source_url, "to": self.source_url}), 10)
        def item(row):
            if "error_category" in row:
                fields(row, {"input_url" if kind == "approximate_window" else "requested_url": self.source_url, "error_category": code})
                return
            fields(row, {"input_url" if kind == "approximate_window" else "requested_url": self.source_url, "final_url": self.source_url,
                "status": integer, "redirects": redirects, "canonical": lambda raw: sequence(raw, self.source_url, 5), "html_lang": nullable(code)},
                {"title": lambda raw: safe_text(raw, 300)})
        if kind == "approximate_window":
            fields(value, {"checked_at": timestamp, "public_url_metadata": lambda raw: sequence(raw, item, 25), "raw_html_saved": boolean})
            if value["raw_html_saved"]:
                raise AnalysisError("HTML bodies must not enter the case store")
        else:
            fields(value, {"observed_at": timestamp, "actual_migration_time": code, "observations": lambda raw: sequence(raw, item, 25),
                "third_party_301_proposal_is_not_completion_evidence": boolean})
            if value["actual_migration_time"] != "unknown" or value["third_party_301_proposal_is_not_completion_evidence"] is not True:
                raise AnalysisError("migration completion cannot be invented")

    def freshness_record(self, value):
        if value is None:
            return
        fields(value, {"protocol": code, "sample": boolean, "exploratory": boolean, "batch_confirmed": boolean,
            "gsc_availability": lambda raw: self.envelope(raw, self.availability), "weeks": lambda raw: sequence(raw, lambda _: None, 0),
            "runtime": runtime, "availability_only": boolean, "writes": integer, "cache_or_audit_persisted": boolean})
        if value["protocol"] != "seo-weekly-read/1" or value["sample"] or value["batch_confirmed"] or not value["availability_only"] or value["writes"] or value["cache_or_audit_persisted"]:
            raise AnalysisError("invalid saved freshness evidence")
