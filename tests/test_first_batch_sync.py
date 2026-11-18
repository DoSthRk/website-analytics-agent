from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from website_analytics.models import InquirySourceConfig, SiteConfig
from website_analytics.sync_plan import load_sync_profile, select_sync_periods
from website_analytics.sync_status import daily_coverage
from test_feishu_dashboard_v3 import _details, _fetch_result, _page_dimension


def runner_module():
    spec = importlib.util.spec_from_file_location('first_batch_runner', 'scripts/run_analytics_sync.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_runner(monkeypatch, tmp_path, scope='full', apply=False):
    runner = runner_module()
    source = InquirySourceConfig('legacy_contacts_mysql', 'WEBSITE_ANALYTICS_TEST_DSN',
                                 site_scope='dedicated_database', site_scope_evidence='synthetic fixture only')
    site = SiteConfig('genemedi-net', 'Synthetic', ('www.genemedi.net',),
                      'America/Los_Angeles', '123', 'sc-domain:genemedi.net', inquiry_source=source)
    monkeypatch.setattr(runner, 'load_sites', lambda _: {site.site_key: site})
    monkeypatch.setattr(runner, '_validate', lambda *a: None)
    monkeypatch.setattr(runner, 'create_page_dimension_adapter', lambda _: SimpleNamespace(query=lambda: []))
    monkeypatch.setattr(runner, 'build_page_dimension', lambda *a: _page_dimension())
    monkeypatch.setattr(runner, 'build_dashboard_records', lambda **k: pytest.fail('V3-only built V2'))
    fetched = []
    details = {}
    def fetch(site, config, period, cache, audit):
        assert period.kind == 'day' and period.start == period.end
        fetched.append(period.start.isoformat())
        rows = _details()
        for key in ('GA4 Daily', 'GSC Daily', 'Inquiry Daily'):
            for row in rows.get(key, []): row['date'] = period.start.isoformat()
        details[period.start] = rows
        return _fetch_result()
    monkeypatch.setattr(runner, '_fetch', fetch)
    monkeypatch.setattr(runner, 'load_period_details', lambda cache, site, period: details[period.start])
    monkeypatch.setattr(runner, 'sync_record_sets', lambda *a: pytest.fail('V3-only wrote V2'))
    monkeypatch.setattr(runner, 'LarkCLIRecordClient', lambda _: pytest.fail('dry-run created external client'))
    argv = ['run_analytics_sync.py', '--scope', scope, '--anchor', '2026-09-30',
            '--cache-dir', str(tmp_path/'cache'), '--audit-dir', str(tmp_path/'audit'),
            '--output-dir', str(tmp_path/'runs'), '--v3-only',
            '--v3-contract', 'config/feishu_dashboard/v3/data_contract.json',
            '--v3-target', 'config/feishu_dashboard/v3/sync_target.json']
    if apply: argv.append('--apply')
    monkeypatch.setattr(sys, 'argv', argv)
    return runner, fetched


@pytest.mark.parametrize('scope,count', [('full',11), ('intraday',2)])
def test_v3_only_fetches_exact_final_days_and_does_not_build_or_write_v2(monkeypatch,tmp_path,capsys,scope,count):
    runner, fetched = prepare_runner(monkeypatch,tmp_path,scope)
    assert runner.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert len(fetched) == len(set(fetched)) == result['fetched_periods'] == count
    assert result['overview_records'] == result['product_records'] == 0
    status = result['run_status']
    assert status['publication'] == 'not_requested'
    assert status['coverage']['missing_dates'] == []
    assert status['coverage']['complete_days'] == count
    assert status['coverage']['remote_history_coverage'] == 'not_verified'
    assert status['last_successful_check_at']
    run = Path(result['output_dir'])
    assert not (run/'overview.json').exists() and not (run/'products.json').exists()
    assert json.loads((tmp_path/'runs/latest-run-status.json').read_text())['status'] == 'ok'


def test_source_failure_reports_stage_without_secrets_or_false_zeros(monkeypatch,tmp_path,capsys):
    runner, _ = prepare_runner(monkeypatch,tmp_path)
    def fail(*args): raise RuntimeError('synthetic-secret-do-not-log')
    monkeypatch.setattr(runner,'_fetch',fail)
    runs=tmp_path/'runs';runs.mkdir()
    (runs/'latest-run-status.json').write_text(json.dumps({'last_successful_check_at':'2026-09-29T00:00:00+00:00'}))
    assert runner.main() == 1
    out=capsys.readouterr()
    assert out.out == '' and 'synthetic-secret' not in out.err
    latest=json.loads((runs/'latest-run-status.json').read_text())
    assert latest['status']=='failed' and latest['stage']=='fetch'
    assert latest['error_type']=='RuntimeError'
    assert latest['last_successful_check_at']=='2026-09-29T00:00:00+00:00'
    assert len(latest['coverage']['missing_dates'])==11
    assert latest['publication']=='not_requested'
    assert not list(runs.glob('*/v3-daily.json'))


def test_publish_failure_is_explicitly_partial_and_preserves_previous_success(monkeypatch,tmp_path,capsys):
    runner, _ = prepare_runner(monkeypatch,tmp_path,apply=True)
    monkeypatch.setattr(runner,'LarkCLIRecordClient',lambda _: object())
    def fail(*args): raise RuntimeError('hidden-error')
    monkeypatch.setattr(runner,'sync_v3_record_sets',fail)
    assert runner.main()==1
    status=json.loads((tmp_path/'runs/latest-run-status.json').read_text())
    assert status['publication']=='failed_or_partial'
    assert status['stage']=='publication' and status['status']=='failed'
    assert status['last_successful_check_at'] is None
    assert 'hidden-error' not in capsys.readouterr().err


def test_no_final_days_fails_without_a_fetch(monkeypatch,tmp_path,capsys):
    runner,fetched=prepare_runner(monkeypatch,tmp_path)
    monkeypatch.setattr(runner,'select_sync_periods',lambda *a,**k: [])
    assert runner.main()==1
    assert fetched==[]
    assert json.loads((tmp_path/'runs/latest-run-status.json').read_text())['status']=='failed'


def test_coverage_detects_holes_without_claiming_production_history():
    planned=[{'kind':'day','isFinal':True,'start':s} for s in ('2026-09-25','2026-09-27')]
    c=daily_coverage(planned,['2026-09-25'])
    assert c['expected_days']==3 and c['complete_days']==1
    assert c['missing_dates']==['2026-09-26','2026-09-27']
    assert c['finality_basis']=='configured_source_lag_estimate'
    with pytest.raises(ValueError): daily_coverage(planned,['2026-09-24'])


def test_legacy_selection_unchanged_without_v3_flag():
    p=load_sync_profile(Path('config/sync_profiles/genemedi-net.json'))
    rows=select_sync_periods(p,date(2026,9,30),'full')
    assert len(rows)==52 and any(r['kind']=='month' for r in rows)


def test_corrected_labels_keep_external_field_names_metric_keys_and_numbers():
    contract=json.loads(Path('config/feishu_dashboard/v3/data_contract.json').read_text())
    fields={f['key']:f for f in contract['tables'][0]['fields']}
    assert fields['ga4_key_events']['semantic_label']=='GA4关键事件总数'
    assert fields['ga4_key_events']['name']=='表单提交事件（GA4）'
    assert fields['ga4_key_events']['source']=='ga4.keyEvents'
    assert fields['accepted_inquiries']['semantic_label']=='非隔离表单入库数'
    assert fields['accepted_inquiries']['source']=='inquiry.nonQuarantinedSubmissions'
    manifest=json.loads(Path('config/feishu_dashboard/v3/dashboard_apply_manifest.json').read_text())
    blocks=manifest['dashboards'][0]['blocks']
    card=next(b for b in blocks if b['name']=='GA4关键事件总数')
    assert card['data_config']['series'][0]['field_name']=='表单提交事件（GA4）'
    assert card['data_config']['series'][0]['rollup']=='SUM'


def test_fetch_failure_retains_only_safe_source_health(monkeypatch,tmp_path):
    runner=runner_module()
    monkeypatch.setattr(runner,'_run_cli',lambda *a:{'_exit_code':3,'complete':False,'status':'partial',
        'sources':{'ga4':{'status':'ok','payload':'never-log-this'},
                   'inquiry':{'status':'error','error_type':'InquiryScopeError','message':'never-log-this'}}})
    monkeypatch.setattr(runner.time,'sleep',lambda _:None)
    with pytest.raises(runner.FetchIncompleteError) as err:
        runner._fetch('synthetic',tmp_path/'config',SimpleNamespace(start=date(2026,9,1),end=date(2026,9,1)),
                      tmp_path/'cache',tmp_path/'audit')
    assert err.value.source_statuses=={'ga4':{'status':'ok','error_type':None},
                                      'inquiry':{'status':'error','error_type':'InquiryScopeError'}}
    assert 'never-log-this' not in str(err.value)
