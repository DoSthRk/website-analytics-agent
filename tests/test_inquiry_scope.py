from datetime import date

import pytest

from website_analytics.config import ConfigError, load_sites
from website_analytics.adapters.inquiry import InquiryScopeError, LegacyContactsAdapter, create_inquiry_adapter
from website_analytics.models import DateRange, InquirySourceConfig
from test_inquiry import _Cursor, _Connection


def adapter_for(url, domains=('www.genemedi.net',)):
    cursor=_Cursor([[{'date':date(2026,9,1),'stored_submissions':1,'quarantined_submissions':0,'non_quarantined_submissions':1}],
                    [{'source_url':url,'stored_submissions':1,'quarantined_submissions':0,'non_quarantined_submissions':1}]])
    connection=_Connection(cursor)
    adapter=LegacyContactsAdapter('mysql://synthetic:synthetic@db.example.test/analytics',
                                 connector=lambda **_:connection,domains=domains)
    return adapter,connection


@pytest.mark.parametrize("url", ["https://outside.example.test/i/gmp-example", "https://synthetic:secret@outside.example.test/i/gmp-example"])
def test_foreign_same_path_fails_entire_inquiry_source_without_printing_url(url):
    adapter,connection=adapter_for(url)
    with pytest.raises(InquiryScopeError) as err:
        adapter.query(DateRange(date(2026,9,1),date(2026,9,1)))
    assert connection.closed
    assert 'outside.example.test' not in str(err.value)
    assert 'no records were filtered' in str(err.value)


def test_registered_origins_normalize_case_and_preserve_raw_counts():
    adapter,_=adapter_for('https://WWW.GENEMEDI.NET/i/gmp-example?email=synthetic',domains=('WWW.Genemedi.Net.',))
    result=adapter.query(DateRange(date(2026,9,1),date(2026,9,1)))
    assert result.totals['storedSubmissions']==result.totals['nonQuarantinedSubmissions']==1
    assert result.pages[0]['sourceUrl'].endswith('/i/gmp-example')


@pytest.mark.parametrize('url', ['unattributed-relative', 'https://synthetic:secret@www.genemedi.net/i/gmp-example'])
def test_unattributed_submission_keeps_counts_and_never_exposes_credentials(url):
    adapter,_=adapter_for(url)
    result=adapter.query(DateRange(date(2026,9,1),date(2026,9,1)))
    assert result.totals['storedSubmissions']==1
    assert result.pages[0]['sourceUrl']=='[UNATTRIBUTED]'


def test_unverified_scope_is_rejected_before_credential_lookup(monkeypatch):
    source=InquirySourceConfig('legacy_contacts_mysql','WEBSITE_ANALYTICS_TEST_DSN')
    monkeypatch.setattr('website_analytics.adapters.inquiry.os.environ.get',lambda *_:pytest.fail('read credential before scope verification'))
    with pytest.raises(InquiryScopeError): create_inquiry_adapter(source,domains=('example.test',))


@pytest.mark.parametrize('scope,evidence', [(None,'fixture'),('shared_database','fixture'),('dedicated_database','')])
def test_missing_shared_or_unsubstantiated_scope_fails_offline_config(tmp_path,scope,evidence):
    p=tmp_path/'sites.yaml'
    block='' if scope is None else f'      site_scope: {scope}\n'
    p.write_text('sites:\n  demo:\n    display_name: Synthetic\n    domains: [example.test]\n    timezone: UTC\n    ga4_property_id: 123\n    gsc_property_url: sc-domain:example.test\n    inquiry_source:\n      kind: legacy_contacts_mysql\n      credential_env: WEBSITE_ANALYTICS_TEST_DSN\n'+block+f'      site_scope_evidence: "{evidence}"\n')
    with pytest.raises(ConfigError,match='site_scope'):load_sites(p)
