# 官网数据 Skill 第一期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build a local, read-only GA4/GSC reporting CLI with a Codex Skill, fixture-driven tests, audit manifests, and Excel exports.

**Architecture:** Place deterministic API calls, normalization, comparison, caching, audit generation, and JSON workbook-payload creation in a Python package. Build XLSX files through one Node.js Artifact Tool script, invoked by the CLI. The Codex Skill only turns a natural-language request into validated CLI arguments and interprets structured output. Inject Google clients into adapters so every test runs with fixtures and never needs a Google account.

**Tech Stack:** Python 3.11+, google-analytics-data, google-api-python-client, google-auth, PyYAML, pytest, Node.js, and the bundled @oai/artifact-tool.

---

## Target file structure

~~~text
C:\Users\dosth\Documents\Codex\2026-08-05\w\outputs\website-analytics\
├─ pyproject.toml
├─ README.md
├─ .gitignore
├─ config/sites.example.yaml
├─ src/website_analytics/{__init__,__main__,cli,config,dates,models,reporting,cache,excel}.py
├─ src/website_analytics/adapters/{__init__,ga4,gsc}.py
├─ tests/{fixtures,test_config,test_dates,test_ga4,test_gsc,test_reporting,test_cache,test_excel,test_cli,test_skill}.py
└─ skill/website-analytics/{SKILL.md,references/metrics.md}
~~~

### Task 1: Bootstrap an isolated Python project

**Files:**

- Create: C:\Users\dosth\Documents\Codex\2026-08-05\w\outputs\website-analytics\pyproject.toml
- Create: C:\Users\dosth\Documents\Codex\2026-08-05\w\outputs\website-analytics\.gitignore
- Create: C:\Users\dosth\Documents\Codex\2026-08-05\w\outputs\website-analytics\src\website_analytics\__init__.py
- Create: C:\Users\dosth\Documents\Codex\2026-08-05\w\outputs\website-analytics\tests\test_package.py

- [ ] **Step 1: initialize local version control and source folders.**

~~~powershell
Set-Location 'C:\Users\dosth\Documents\Codex\2026-08-05\w\outputs\website-analytics'
git init
New-Item -ItemType Directory -Force -Path src\website_analytics\adapters,tests\fixtures,config,skill\website-analytics\references | Out-Null
~~~

Expected: git status reports a new local repository.

- [ ] **Step 2: write a failing version test.**

~~~python
from website_analytics import __version__

def test_package_exposes_version():
    assert __version__ == "0.1.0"
~~~

- [ ] **Step 3: run the focused test before implementation.**

Run: python -m pytest tests/test_package.py -q

Expected: FAIL because website_analytics is not importable.

- [ ] **Step 4: add the minimal package and locked dependency ranges.**

~~~toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "website-analytics"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "google-analytics-data>=0.18,<1",
  "google-api-python-client>=2,<3",
  "google-auth>=2,<3",
  "PyYAML>=6,<7",
]
[project.optional-dependencies]
dev = ["pytest>=8,<9"]
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
~~~

~~~python
__version__ = "0.1.0"
~~~

~~~gitignore
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
config/sites.yaml
cache/
audits/
exports/
*.token.json
*.credentials.json
~~~

- [ ] **Step 5: install and verify.**

~~~powershell
python -m pip install -e '.[dev]'
python -m pytest tests/test_package.py -q
git add pyproject.toml .gitignore src tests
git commit -m "chore: bootstrap website analytics package"
~~~

Expected: 1 passed and one bootstrap commit.

### Task 2: Implement configuration and date contracts

**Files:**

- Create: src\website_analytics\models.py
- Create: src\website_analytics\config.py
- Create: src\website_analytics\dates.py
- Create: config\sites.example.yaml
- Create: tests\test_config.py
- Create: tests\test_dates.py

- [ ] **Step 1: write failing validation tests.**

~~~python
# tests/test_config.py
import pytest
from website_analytics.config import ConfigError, load_sites, require_site

def test_registered_site_is_loaded(tmp_path):
    path = tmp_path / "sites.yaml"
    path.write_text("sites:\n  demo:\n    display_name: Demo\n    domains: [example.com]\n    timezone: Asia/Shanghai\n    ga4_property_id: '123'\n    gsc_property_url: sc-domain:example.com\n", encoding="utf-8")
    assert require_site(load_sites(path), "demo").ga4_property_id == "123"

def test_unknown_site_is_rejected(tmp_path):
    path = tmp_path / "sites.yaml"
    path.write_text("sites: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not registered"):
        require_site(load_sites(path), "unknown")
~~~

~~~python
# tests/test_dates.py
import pytest
from website_analytics.dates import DateRangeError, parse_date_range, previous_period

def test_previous_period_has_equal_length():
    current = parse_date_range("2026-08-03", "2026-08-09")
    assert previous_period(current).start.isoformat() == "2026-07-27"

def test_inverted_range_is_rejected():
    with pytest.raises(DateRangeError, match="on or after"):
        parse_date_range("2026-08-09", "2026-08-03")
~~~

- [ ] **Step 2: run tests and confirm module imports fail.**

Run: python -m pytest tests/test_config.py tests/test_dates.py -q

Expected: FAIL with missing config/dates modules.

- [ ] **Step 3: implement strict models, YAML loading, and ISO ranges.**

~~~python
# models.py
from dataclasses import dataclass
from datetime import date

@dataclass(frozen=True)
class SiteConfig:
    site_key: str
    display_name: str
    domains: tuple[str, ...]
    timezone: str
    ga4_property_id: str
    gsc_property_url: str
    key_events: tuple[str, ...] = ()

@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
~~~

~~~python
# dates.py
from datetime import date, timedelta
from .models import DateRange

class DateRangeError(ValueError):
    pass

def parse_date_range(start: str, end: str) -> DateRange:
    value = DateRange(date.fromisoformat(start), date.fromisoformat(end))
    if value.end < value.start:
        raise DateRangeError("end date must be on or after start date")
    return value

def previous_period(value: DateRange) -> DateRange:
    days = (value.end - value.start).days + 1
    return DateRange(value.start - timedelta(days=days), value.start - timedelta(days=1))
~~~

Implement load_sites with yaml.safe_load, require all five site fields, reject blank/duplicate keys, and make require_site raise ConfigError containing “not registered”. Add the safe demo YAML shown in the design specification.

- [ ] **Step 4: verify and commit.**

~~~powershell
python -m pytest tests/test_config.py tests/test_dates.py -q
git add config src/website_analytics/models.py src/website_analytics/config.py src/website_analytics/dates.py tests
git commit -m "feat: add site configuration and date ranges"
~~~

Expected: 4 passed.

### Task 3: Add injected GA4 and GSC read adapters

**Files:**

- Create: src\website_analytics\adapters\ga4.py
- Create: src\website_analytics\adapters\gsc.py
- Create: tests\fixtures\ga4_report.json
- Create: tests\fixtures\gsc_daily.json
- Create: tests\fixtures\gsc_pages.json
- Create: tests\fixtures\gsc_queries.json
- Create: tests\test_ga4.py
- Create: tests\test_gsc.py

- [ ] **Step 1: write fake-client tests.**

~~~python
# tests/test_ga4.py
from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.dates import parse_date_range
from website_analytics.models import SiteConfig

class FakeGA4:
    def run_report(self, request):
        self.request = request
        return {"rows": [{"dimensionValues": [{"value": "20260803"}], "metricValues": [{"value": "10"}]}]}

def test_daily_uses_registered_property_and_normalizes_date():
    client = FakeGA4()
    site = SiteConfig("demo", "Demo", ("example.com",), "Asia/Shanghai", "123", "sc-domain:example.com")
    assert GA4Adapter(client).daily(site, parse_date_range("2026-08-03", "2026-08-03")) == [{"date": "2026-08-03", "sessions": 10.0}]
    assert client.request.property == "properties/123"
~~~

~~~python
# tests/test_gsc.py
from website_analytics.adapters.gsc import GSCAdapter
from website_analytics.dates import parse_date_range
from website_analytics.models import SiteConfig

class Query:
    def query(self, siteUrl, body):
        self.site_url, self.body = siteUrl, body
        return self
    def execute(self):
        return {"rows": [{"keys": ["2026-08-03"], "clicks": 3, "impressions": 30, "ctr": .1, "position": 5.5}]}
class Service:
    def __init__(self): self.resource = Query()
    def searchanalytics(self): return self.resource

def test_gsc_uses_final_web_request_and_normalizes_numbers():
    service = Service()
    site = SiteConfig("demo", "Demo", ("example.com",), "Asia/Shanghai", "123", "sc-domain:example.com")
    assert GSCAdapter(service).query(site, parse_date_range("2026-08-03", "2026-08-03"), ["date"])[0]["clicks"] == 3.0
    assert service.resource.body["rowLimit"] == 25000
~~~

- [ ] **Step 2: run tests before adapter code exists.**

Run: python -m pytest tests/test_ga4.py tests/test_gsc.py -q

Expected: FAIL with missing adapter imports.

- [ ] **Step 3: implement whitelisted API calls only.**

GA4Adapter.daily must call RunReportRequest with property prefix properties/, dimension date, and metrics sessions, totalUsers, activeUsers, engagedSessions, engagementRate, screenPageViews, keyEvents. GA4Adapter.pages must use landingPagePlusQueryString.

GSCAdapter.query must accept only date/page/query/country/device dimensions. It must use type web, dataState final, rowLimit 25000, and startRow pagination. Convert response key arrays to named columns and numeric fields to floats. Stop at an empty page or after 50,000 rows; record truncation in the result metadata rather than calling a broader query.

~~~python
def query(self, site, value, dimensions):
    allowed = {"date", "page", "query", "country", "device"}
    if not set(dimensions) <= allowed:
        raise ValueError("unsupported GSC dimension")
    rows = []
    for start_row in (0, 25000):
        body = {"startDate": value.start.isoformat(), "endDate": value.end.isoformat(), "dimensions": dimensions, "type": "web", "dataState": "final", "rowLimit": 25000, "startRow": start_row}
        batch = self._service.searchanalytics().query(siteUrl=site.gsc_property_url, body=body).execute().get("rows", [])
        rows.extend(self._normalize(batch, dimensions))
        if len(batch) < 25000:
            break
    return rows
~~~

- [ ] **Step 4: save representative raw JSON fixtures, verify offline tests, and commit.**

~~~powershell
python -m pytest tests/test_ga4.py tests/test_gsc.py -q
git add src/website_analytics/adapters tests/test_ga4.py tests/test_gsc.py tests/fixtures
git commit -m "feat: add GA4 and GSC read adapters"
~~~

Expected: 2 passed and no network access.

### Task 4: Add cache, audit, and comparison logic

**Files:**

- Create: src\website_analytics\cache.py
- Create: src\website_analytics\reporting.py
- Create: tests\test_cache.py
- Create: tests\test_reporting.py

- [ ] **Step 1: write failing cache and partial-source tests.**

~~~python
from website_analytics.cache import write_cached_json
from website_analytics.reporting import compare_totals

def test_cache_redacts_secret_parameters(tmp_path):
    path = write_cached_json(tmp_path, "demo", "ga4", {"start": "2026-08-03", "token": "do-not-store"}, [{"sessions": 1}])
    assert "do-not-store" not in path.read_text(encoding="utf-8")

def test_comparison_is_incomplete_when_source_missing():
    result = compare_totals({"ga4": {"sessions": 12.0}}, {"ga4": {"sessions": 10.0}, "gsc": {"clicks": 2.0}})
    assert result["complete"] is False
    assert result["metrics"]["sessions"]["delta"] == 2.0
~~~

- [ ] **Step 2: run tests to confirm missing modules.**

Run: python -m pytest tests/test_cache.py tests/test_reporting.py -q

Expected: FAIL with missing imports.

- [ ] **Step 3: implement redacted request hashing and deltas.**

Remove request keys containing token, secret, credential, or authorization before canonical JSON hashing. Store source JSON under cache/site/source/sha256.json. Write an audit manifest that contains site/date range, UTC timestamp, source status/row count, package version, and output SHA-256 but no request secrets.

~~~python
def compare_totals(current, previous):
    metrics = {}
    for source, values in current.items():
        for name, value in values.items():
            before = previous.get(source, {}).get(name)
            metrics[name] = {"current": value, "previous": before, "delta": None if before is None else value - before}
    return {"complete": set(current) == set(previous), "metrics": metrics}
~~~

- [ ] **Step 4: verify and commit.**

~~~powershell
python -m pytest tests/test_cache.py tests/test_reporting.py -q
git add src/website_analytics/cache.py src/website_analytics/reporting.py tests
git commit -m "feat: add audit cache and period comparison"
~~~

Expected: 2 passed.

### Task 5: Export and visually verify an Excel workbook through Artifact Tool

**Files:**

- Create: src\website_analytics\workbook_payload.py
- Create: scripts\build_report_workbook.mjs
- Create: tests\test_excel.py

- [ ] **Step 1: write a failing workbook test.**

~~~python
from website_analytics.workbook_payload import build_workbook_payload

def test_payload_has_fixed_sheet_order_and_typed_numbers():
    payload = build_workbook_payload({"site": "demo"}, {"GA4 Daily": [{"date": "2026-08-03", "sessions": 10.0}], "GSC Daily": [{"date": "2026-08-03", "clicks": 3.0}]}, {"sources": {"ga4": {"rows": 1}}})
    assert [sheet["name"] for sheet in payload["sheets"]] == ["README", "Executive Summary", "GA4 Daily", "GSC Daily", "Audit"]
    assert payload["sheets"][2]["rows"][1][1] == 10.0
~~~

- [ ] **Step 2: run the test before exporter implementation.**

Run: python -m pytest tests/test_excel.py -q

Expected: FAIL with missing workbook_payload module.

- [ ] **Step 3: implement fixed sheet order and formatting.**

Create README, Executive Summary, supplied detail sheets in the order GA4 Daily, GA4 Pages, GSC Daily, GSC Pages, GSC Queries, then Audit. Python creates only a JSON-safe payload. The .mjs builder must use @oai/artifact-tool block writes to create worksheets, hide gridlines, freeze detail-sheet header rows, apply bold dark header fills, format ctr and engagementRate as 0.0%, cap columns at 48 characters, and render every populated sheet to PNG before exporting XLSX. Keep missing values blank, not the string None.

~~~javascript
const workbook = Workbook.create();
for (const sheetData of payload.sheets) {
  const sheet = workbook.worksheets.add(sheetData.name);
  sheet.showGridLines = false;
  sheet.getRangeByIndexes(0, 0, sheetData.rows.length, sheetData.rows[0].length).values = sheetData.rows;
  if (sheetData.detail) sheet.freezePanes.freezeRows(1);
}
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
~~~

- [ ] **Step 4: verify workbook ZIP integrity and commit.**

~~~powershell
python -m pytest tests/test_excel.py -q
C:\Users\dosth\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe scripts\build_report_workbook.mjs --input tests\fixtures\workbook_payload.json --output exports\fixture.xlsx --render-dir exports\rendered
python -c "import zipfile; print(zipfile.is_zipfile('exports\\fixture.xlsx'))"
git add src/website_analytics/workbook_payload.py scripts/build_report_workbook.mjs tests/test_excel.py
git commit -m "feat: export analytics workbook"
~~~

Expected: test passes and command prints True.

### Task 6: Add CLI and fixture-only end-to-end mode

**Files:**

- Create: src\website_analytics\cli.py
- Create: src\website_analytics\__main__.py
- Create: tests\test_cli.py
- Create: README.md

- [ ] **Step 1: write failing parser tests.**

~~~python
from website_analytics.cli import build_parser

def test_validate_config_is_offline():
    args = build_parser().parse_args(["validate-config", "--site", "demo"])
    assert args.command == "validate-config"

def test_report_has_safe_default_comparison():
    args = build_parser().parse_args(["report", "--site", "demo", "--start", "2026-08-03", "--end", "2026-08-09"])
    assert args.compare == "previous-period"
~~~

- [ ] **Step 2: run tests before CLI exists.**

Run: python -m pytest tests/test_cli.py -q

Expected: FAIL with missing cli module.

- [ ] **Step 3: implement four commands and error contract.**

Build exactly validate-config, fetch, report, and export-excel. All commands require a registered site; network commands require ISO dates. Permit only dimensions declared by the adapters and compare values previous-period/previous-4-weeks. Print machine-readable JSON to stdout, errors to stderr, return 2 for invalid input, 3 for source failure/partial result, and 0 only for complete success. Do not accept arbitrary URLs, credentials, SQL, or raw API bodies.

~~~python
from .cli import main
if __name__ == "__main__":
    raise SystemExit(main())
~~~

Add --fixture-dir for tests and demo; it must read test JSON files instead of creating Google clients. In normal mode, use Application Default Credentials or GOOGLE_APPLICATION_CREDENTIALS. validate-config must not initialize a Google client.

- [ ] **Step 4: execute a complete fixture-only path and commit.**

~~~powershell
python -m pytest tests/test_cli.py -q
Copy-Item config\sites.example.yaml config\sites.yaml
python -m website_analytics validate-config --site demo --config config\sites.yaml
python -m website_analytics export-excel --site demo --start 2026-08-03 --end 2026-08-09 --config config\sites.yaml --fixture-dir tests\fixtures --output exports\demo.xlsx
git add README.md src/website_analytics/cli.py src/website_analytics/__main__.py tests/test_cli.py
git commit -m "feat: add safe analytics command line interface"
~~~

Expected: fixture export succeeds without Google access.

### Task 7: Package the Codex Skill and complete release verification

**Files:**

- Create: skill\website-analytics\SKILL.md
- Create: skill\website-analytics\references\metrics.md
- Create: tests\test_skill.py
- Modify: README.md

- [ ] **Step 1: write a failing Skill guardrail test.**

~~~python
from pathlib import Path

def test_skill_requires_cli_readonly_and_credential_safety():
    text = Path("skill/website-analytics/SKILL.md").read_text(encoding="utf-8")
    assert "python -m website_analytics" in text
    assert "只读" in text
    assert "凭据" in text
    assert "GSC" in text
~~~

- [ ] **Step 2: run the test before the Skill exists.**

Run: python -m pytest tests/test_skill.py -q

Expected: FAIL with FileNotFoundError.

- [ ] **Step 3: implement a concise Skill and metrics reference.**

Use frontmatter name website-analytics. Its description must trigger for GA4, GSC, 官网流量, 自然搜索, 页面表现, 周报, and Excel 导出. Its workflow must: obtain one missing input at a time; validate configuration before first live use; call only the approved CLI; always reveal source/date/freshness; label causal explanations as hypotheses; never display/store credentials; make no external writes.

Define GA4 sessions/users/key events and GSC clicks/impressions/CTR/position in references/metrics.md. State that Search Console query detail is bounded and that GA4 sessions must never be equated with GSC clicks.

- [ ] **Step 4: run all tests and validate the output workbook.**

~~~powershell
python -m pytest -q
python -c "import zipfile; assert zipfile.is_zipfile('exports\\demo.xlsx'); print('workbook verified')"
git status --short
~~~

Expected: full suite passes and output prints workbook verified.

- [ ] **Step 5: commit, tag, and gate global installation.**

~~~powershell
git add skill/website-analytics tests/test_skill.py README.md
git commit -m "feat: add Codex website analytics skill"
git tag v0.1.0
~~~

Copy only the reviewed skill directory to C:\Users\dosth\.codex\skills\website-analytics after the user approves its generated contents. Do not copy config/sites.yaml, credentials, cache, audits, or exports. Confirm behavior in a new Codex task using fixture-only input before live Google authorization.

## Plan self-review

- **Spec coverage:** Tasks 2 through 7 respectively implement configuration/date safety, both Google sources, auditable caching/comparison, the required workbook, CLI/fixture execution, and the Codex Skill. Every specified non-goal remains excluded.
- **Placeholder scan:** This plan has no deferred implementation markers. Installation is an explicit user-review gate, not omitted work.
- **Type consistency:** SiteConfig and DateRange are used by both adapters; all report commands depend on the same configuration contract; build_workbook_payload plus scripts/build_report_workbook.mjs are the only workbook entry points.
